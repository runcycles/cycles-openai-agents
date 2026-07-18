"""CyclesRunHooks — Cycles governance for OpenAI Agents SDK runs."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import suppress
from time import monotonic
from typing import Any, Literal, TypeVar

from agents import Agent, AgentHookContext, ModelResponse, RunContextWrapper, RunHooks, Runner, RunResultStreaming, Tool
from agents.items import TResponseInputItem
from agents.tracing import get_current_span, get_current_trace
from runcycles import (
    Action,
    Amount,
    AsyncCyclesClient,
    BudgetExceededError,
    CommitOveragePolicy,
    CommitRequest,
    CyclesConfig,
    CyclesMetrics,
    CyclesResponse,
    EventCreateRequest,
    ReleaseRequest,
    ReservationCreateRequest,
    ReservationExtendRequest,
    Subject,
    Unit,
)

from runcycles_openai_agents._defaults import (
    DEFAULT_ACTION_KIND_HANDOFF,
    DEFAULT_ACTION_KIND_LLM,
    DEFAULT_COMMIT_MAX_ATTEMPTS,
    DEFAULT_HEARTBEAT_MAX_AGE_MS,
    DEFAULT_LLM_ESTIMATE,
    DEFAULT_TTL_MS,
)
from runcycles_openai_agents._state import PendingReservation, ReservationTracker
from runcycles_openai_agents.tool_estimate_map import ToolEstimateConfig, ToolEstimateMap

logger = logging.getLogger(__name__)

TContext = TypeVar("TContext")

_active_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "cycles_openai_agents_run_id",
    default=None,
)


class CyclesRunResultStreaming:
    """Lifecycle-safe proxy for the SDK's streaming run result."""

    def __init__(self, result: RunResultStreaming, hooks: CyclesRunHooks[Any], run_id: str) -> None:
        self._result = result
        self._hooks = hooks
        self._run_id = run_id
        self._cancel_requested = False
        self._cleanup_task: asyncio.Task[None] | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._result, name)

    async def _cleanup(self, reason: str) -> None:
        run_loop_task = self._result.run_loop_task
        if run_loop_task is not None and not run_loop_task.done():
            with suppress(asyncio.CancelledError, Exception):
                await run_loop_task
        try:
            await self._hooks.release_pending(reason, run_id=self._run_id)
        finally:
            self._hooks._tracker.forget_run(self._run_id)

    def _ensure_cleanup(self, reason: str) -> asyncio.Task[None]:
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup(reason))
        return self._cleanup_task

    def cancel(self, mode: Literal["immediate", "after_turn"] = "immediate") -> None:
        """Cancel the SDK stream and schedule run-scoped reservation cleanup."""
        self._cancel_requested = True
        self._result.cancel(mode=mode)
        self._ensure_cleanup("agent_run_cancelled")

    async def stream_events(self) -> AsyncIterator[Any]:
        """Proxy SDK events and await cleanup before propagating termination."""
        reason: str | None = None
        try:
            async for event in self._result.stream_events():
                yield event
        except asyncio.CancelledError:
            self._cancel_requested = True
            self._result.cancel()
            reason = "agent_run_cancelled"
            raise
        except GeneratorExit:
            self._cancel_requested = True
            self._result.cancel()
            reason = "agent_run_cancelled:stream_closed"
            raise
        except BaseException as exc:
            reason = f"agent_run_failed:{type(exc).__name__}"
            raise
        else:
            reason = (
                "agent_run_cancelled" if self._cancel_requested else "agent_run_completed_with_pending_reservations"
            )
        finally:
            if reason is None:
                self._cancel_requested = True
                self._result.cancel()
                reason = "agent_run_cancelled:stream_closed"
            await asyncio.shield(self._ensure_cleanup(reason))


class CyclesRunHooks(RunHooks[TContext]):
    """Plugs Cycles governance into every tool call, LLM call, and
    handoff in an OpenAI Agents SDK run.

    Usage::

        hooks = CyclesRunHooks(
            tenant="acme-corp",
            app="support-platform",
            tool_estimates={"send_email": 50, "search": 0},
        )
        result = await hooks.run(agent, input="...")
    """

    def __init__(
        self,
        client: AsyncCyclesClient | None = None,
        config: CyclesConfig | None = None,
        # Subject fields
        tenant: str | None = None,
        workspace: str | None = None,
        app: str | None = None,
        workflow: str | None = None,
        agent: str | None = None,
        toolset: str | None = None,
        # Tool governance
        tool_estimates: dict[str, int | ToolEstimateConfig] | ToolEstimateMap | None = None,
        default_tool_estimate: int = 1,
        # LLM governance
        llm_estimate: int = DEFAULT_LLM_ESTIMATE,
        llm_unit: Unit = Unit.USD_MICROCENTS,
        # Behaviour
        fail_open: bool = False,
        ttl_ms: int = DEFAULT_TTL_MS,
        heartbeat_max_age_ms: int = DEFAULT_HEARTBEAT_MAX_AGE_MS,
        heartbeat_max_extensions: int | None = None,
        commit_max_attempts: int = DEFAULT_COMMIT_MAX_ATTEMPTS,
        overage_policy: CommitOveragePolicy | str = CommitOveragePolicy.ALLOW_IF_AVAILABLE,
        dry_run: bool = False,
    ) -> None:
        if client is not None:
            self._client = client
        elif config is not None:
            self._client = AsyncCyclesClient(config)
        else:
            self._client = AsyncCyclesClient(CyclesConfig.from_env())

        self._tenant = tenant
        self._workspace = workspace
        self._app = app
        self._workflow = workflow
        self._agent = agent
        self._toolset = toolset

        if isinstance(tool_estimates, ToolEstimateMap):
            self._tool_estimate_map = tool_estimates
        else:
            self._tool_estimate_map = ToolEstimateMap(mapping=tool_estimates, default_estimate=default_tool_estimate)

        self._llm_estimate = llm_estimate
        self._llm_unit = llm_unit
        self._fail_open = fail_open
        self._ttl_ms = ttl_ms
        if heartbeat_max_age_ms <= 0:
            raise ValueError("heartbeat_max_age_ms must be greater than zero")
        if heartbeat_max_extensions is not None and heartbeat_max_extensions <= 0:
            raise ValueError("heartbeat_max_extensions must be greater than zero when set")
        if commit_max_attempts <= 0:
            raise ValueError("commit_max_attempts must be greater than zero")
        self._heartbeat_max_age_ms = heartbeat_max_age_ms
        self._heartbeat_max_extensions = heartbeat_max_extensions
        self._commit_max_attempts = commit_max_attempts
        self._overage_policy = (
            overage_policy if isinstance(overage_policy, CommitOveragePolicy) else CommitOveragePolicy(overage_policy)
        )
        self._dry_run = dry_run

        self._tracker = ReservationTracker()

    # --- helpers ---

    def _subject(self, *, agent_name: str | None = None, toolset_name: str | None = None) -> Subject:
        return Subject(
            tenant=self._tenant,
            workspace=self._workspace,
            app=self._app,
            workflow=self._workflow,
            agent=agent_name or self._agent,
            toolset=toolset_name or self._toolset,
        )

    @staticmethod
    def _idempotency_key(run_id: str, operation: str, operation_id: str) -> str:
        value = f"cycles-openai-agents\0{run_id}\0{operation}\0{operation_id}"
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _run_id(context: RunContextWrapper[Any]) -> str:
        active_run_id = _active_run_id.get()
        if active_run_id is not None:
            return active_run_id
        trace = get_current_trace()
        trace_id = trace.trace_id if trace is not None else "untraced"
        return f"{trace_id}:{id(context.usage):x}"

    @staticmethod
    def _span_id() -> str | None:
        span = get_current_span()
        return span.span_id if span is not None else None

    @staticmethod
    def _tool_call_id(context: RunContextWrapper[Any]) -> str | None:
        call_id = getattr(context, "tool_call_id", None)
        return call_id if isinstance(call_id, str) and call_id else None

    async def run(
        self,
        starting_agent: Agent[TContext],
        input: Any,
        *,
        run_id: str | None = None,
        **runner_kwargs: Any,
    ) -> Any:
        """Run an agent with failure- and cancellation-safe reservation cleanup.

        The OpenAI Agents SDK does not expose a general error hook, so this method
        wraps ``Runner.run`` at its finalization boundary. Callers may supply an
        application run ID for stable operation correlation.
        """
        if "hooks" in runner_kwargs:
            raise ValueError("CyclesRunHooks.run() manages the hooks argument")

        effective_run_id = run_id or uuid.uuid4().hex
        token = _active_run_id.set(effective_run_id)
        try:
            return await Runner.run(starting_agent, input, hooks=self, **runner_kwargs)
        except asyncio.CancelledError:
            await self.release_pending("agent_run_cancelled", run_id=effective_run_id)
            raise
        except BaseException as exc:
            reason = f"agent_run_failed:{type(exc).__name__}"
            await self.release_pending(reason, run_id=effective_run_id)
            raise
        finally:
            self._tracker.forget_run(effective_run_id)
            _active_run_id.reset(token)

    def run_streamed(
        self,
        starting_agent: Agent[TContext],
        input: Any,
        *,
        run_id: str | None = None,
        **runner_kwargs: Any,
    ) -> CyclesRunResultStreaming:
        """Start a streaming run whose event iterator finalizes reservations."""
        if "hooks" in runner_kwargs:
            raise ValueError("CyclesRunHooks.run_streamed() manages the hooks argument")

        effective_run_id = run_id or uuid.uuid4().hex
        token = _active_run_id.set(effective_run_id)
        try:
            result = Runner.run_streamed(starting_agent, input, hooks=self, **runner_kwargs)
        finally:
            _active_run_id.reset(token)
        return CyclesRunResultStreaming(result, self, effective_run_id)

    @staticmethod
    def _commit_response_is_retryable(response: CyclesResponse) -> bool:
        if response.is_transport_error:
            return True
        status = int(response.status)
        return status == 429 or status >= 500

    async def _commit_reservation(
        self,
        pending: PendingReservation,
        actual: Amount,
        metrics: CyclesMetrics | None = None,
    ) -> CyclesResponse:
        """Commit with one stable request and idempotency key across retries."""
        request = CommitRequest(
            idempotency_key=pending.commit_idempotency_key,
            actual=actual,
            metrics=metrics,
        )
        for attempt in range(1, self._commit_max_attempts + 1):
            try:
                response = await self._client.commit_reservation(pending.reservation_id, request)
            except Exception:
                if attempt >= self._commit_max_attempts:
                    raise
                logger.warning(
                    "cycles: commit raised; retrying reservation=%s attempt=%s",
                    pending.reservation_id,
                    attempt,
                    exc_info=True,
                )
                continue
            if (
                response.is_success
                or attempt >= self._commit_max_attempts
                or not self._commit_response_is_retryable(response)
            ):
                return response
            logger.warning(
                "cycles: commit failed; retrying reservation=%s attempt=%s status=%s",
                pending.reservation_id,
                attempt,
                response.status,
            )
        raise AssertionError("commit retry loop exhausted unexpectedly")

    def _start_heartbeat(
        self,
        reservation_id: str,
        *,
        run_id: str,
        operation_id: str,
    ) -> asyncio.Task[None] | None:
        """Spawn a background task that extends the reservation at half-TTL intervals.

        Heartbeat is disabled when ``ttl_ms`` is too small (< 2000ms) because the
        minimum interval of 1s would exceed the TTL window.
        """
        if self._ttl_ms < 2000:
            return None
        interval_s = max(self._ttl_ms / 2, 1000) / 1000.0

        async def _loop() -> None:
            started_at = monotonic()
            extension_count = 0
            try:
                while True:
                    if self._heartbeat_max_extensions is not None and extension_count >= self._heartbeat_max_extensions:
                        logger.warning(
                            "cycles: heartbeat extension cap reached reservation=%s extensions=%s",
                            reservation_id,
                            extension_count,
                        )
                        return
                    remaining_s = self._heartbeat_max_age_ms / 1000.0 - (monotonic() - started_at)
                    if remaining_s <= 0:
                        logger.warning("cycles: heartbeat max age reached reservation=%s", reservation_id)
                        return
                    await asyncio.sleep(min(interval_s, remaining_s))
                    if monotonic() - started_at >= self._heartbeat_max_age_ms / 1000.0:
                        logger.warning("cycles: heartbeat max age reached reservation=%s", reservation_id)
                        return
                    extension_count += 1
                    try:
                        request = ReservationExtendRequest(
                            idempotency_key=self._idempotency_key(
                                run_id,
                                "heartbeat",
                                f"{operation_id}:{extension_count}",
                            ),
                            extend_by_ms=self._ttl_ms,
                        )
                        response = await self._client.extend_reservation(reservation_id, request)
                        if response.is_success:
                            logger.debug("cycles: heartbeat ok reservation=%s", reservation_id)
                        else:
                            logger.warning("cycles: heartbeat failed reservation=%s", reservation_id)
                    except Exception:
                        logger.warning("cycles: heartbeat error reservation=%s", reservation_id, exc_info=True)
            except asyncio.CancelledError:
                return

        return asyncio.create_task(_loop())

    async def _release_reservation(self, pending: PendingReservation, reason: str) -> None:
        """Release a reservation and cancel its heartbeat."""
        pending.cancel_heartbeat()
        try:
            request = ReleaseRequest(
                idempotency_key=self._idempotency_key(
                    pending.run_id,
                    "release",
                    f"{pending.operation_id}:{pending.reservation_id}",
                ),
                reason=reason,
            )
            response = await self._client.release_reservation(pending.reservation_id, request)
            if not response.is_success:
                logger.warning(
                    "cycles: release failed reservation=%s err=%s",
                    pending.reservation_id,
                    response.error_message,
                )
        except Exception:
            logger.warning("cycles: release error reservation=%s", pending.reservation_id, exc_info=True)

    # --- hooks ---

    async def on_agent_start(
        self,
        context: AgentHookContext[TContext],
        agent: Agent[TContext],
    ) -> None:
        logger.debug("cycles: agent_start run=%s agent=%s", self._run_id(context), agent.name)

    async def on_agent_end(
        self,
        context: AgentHookContext[TContext],
        agent: Agent[TContext],
        output: Any,
    ) -> None:
        run_id = self._run_id(context)
        logger.debug("cycles: agent_end run=%s agent=%s", run_id, agent.name)
        await self.release_pending("agent_run_completed_with_pending_reservations", run_id=run_id)

    async def on_tool_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        tool: Tool,
    ) -> None:
        if self._tool_estimate_map.is_zero_estimate(tool.name):
            logger.debug("cycles: tool_start skip zero-estimate tool=%s", tool.name)
            return

        run_id = self._run_id(context)
        call_id = self._tool_call_id(context)
        operation_id = (
            f"tool:{call_id}" if call_id is not None else self._tracker.next_operation_id(run_id, f"tool:{tool.name}")
        )
        est_cfg = self._tool_estimate_map.get(tool.name)
        request = ReservationCreateRequest(
            idempotency_key=self._idempotency_key(run_id, "reserve", operation_id),
            subject=self._subject(agent_name=agent.name, toolset_name=tool.name),
            action=Action(kind=est_cfg.action_kind, name=tool.name),
            estimate=Amount(unit=est_cfg.unit, amount=est_cfg.estimate),
            ttl_ms=self._ttl_ms,
            overage_policy=self._overage_policy,
            dry_run=self._dry_run or None,
        )

        response = await self._client.create_reservation(request)

        if response.is_transport_error:
            if self._fail_open:
                logger.warning("cycles: transport error on tool reserve, fail-open tool=%s", tool.name)
                return
            raise BudgetExceededError(
                f"Cycles service unavailable for tool={tool.name}",
                status=-1,
                error_code="TRANSPORT_ERROR",
            )

        if not response.is_success:
            if self._fail_open:
                logger.warning(
                    "cycles: unexpected error on tool reserve, fail-open tool=%s err=%s",
                    tool.name,
                    response.error_message,
                )
                return
            raise BudgetExceededError(
                f"Cycles error for tool={tool.name}: {response.error_message}",
                status=response.status,
                error_code=response.get_body_attribute("error") or "UNKNOWN",
            )

        decision = response.get_body_attribute("decision")
        if decision == "DENY":
            reason = response.get_body_attribute("reason_code") or "budget_exceeded"
            raise BudgetExceededError(
                f"Tool reservation denied for {tool.name}: {reason}",
                status=response.status,
                error_code="BUDGET_EXCEEDED",
            )

        reservation_id = response.get_body_attribute("reservation_id")
        if reservation_id is None:
            logger.warning("cycles: no reservation_id in response for tool=%s", tool.name)
            return

        pending = PendingReservation(
            run_id=run_id,
            operation_id=operation_id,
            reservation_id=reservation_id,
            tool_name=tool.name,
            agent_name=agent.name,
            estimate=est_cfg.estimate,
            unit=est_cfg.unit,
            commit_idempotency_key=self._idempotency_key(run_id, "commit", operation_id),
            heartbeat_task=self._start_heartbeat(
                reservation_id,
                run_id=run_id,
                operation_id=operation_id,
            ),
        )
        previous = self._tracker.register_tool(pending)
        if previous is not None:
            previous.cancel_heartbeat()
            if previous.reservation_id != pending.reservation_id:
                await self._release_reservation(previous, "duplicate_tool_operation_replaced")
        logger.debug("cycles: tool_start reserved=%s tool=%s", reservation_id, tool.name)

    async def on_tool_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        tool: Tool,
        result: object,
    ) -> None:
        # `result` is typed `object` to match RunHooksBase.on_tool_end (the
        # openai-agents base widened it from `str`); narrowing it here violates
        # the Liskov override rule (mypy >=2.1). We don't read `result` — the
        # commit amount comes from the tracked reservation estimate.
        run_id = self._run_id(context)
        call_id = self._tool_call_id(context)
        pending = self._tracker.get_tool(
            run_id,
            operation_id=f"tool:{call_id}" if call_id is not None else None,
            tool_name=tool.name,
        )
        if pending is None:
            return

        pending.cancel_heartbeat()
        response = await self._commit_reservation(
            pending,
            Amount(unit=pending.unit, amount=pending.estimate),
        )
        if response.is_success:
            self._tracker.complete_tool(pending)
        else:
            logger.error(
                "cycles: commit failed reservation=%s tool=%s err=%s",
                pending.reservation_id,
                tool.name,
                response.error_message,
            )

    async def on_llm_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        run_id = self._run_id(context)
        sequence_id = self._tracker.next_operation_id(run_id, "llm")
        span_id = self._span_id() or agent.name
        operation_id = f"llm:{span_id}:{sequence_id}"
        request = ReservationCreateRequest(
            idempotency_key=self._idempotency_key(run_id, "reserve", operation_id),
            subject=self._subject(agent_name=agent.name),
            action=Action(kind=DEFAULT_ACTION_KIND_LLM, name=agent.name),
            estimate=Amount(unit=self._llm_unit, amount=self._llm_estimate),
            ttl_ms=self._ttl_ms,
            overage_policy=self._overage_policy,
            dry_run=self._dry_run or None,
        )

        response = await self._client.create_reservation(request)

        if response.is_transport_error:
            if self._fail_open:
                logger.warning("cycles: transport error on LLM reserve, fail-open agent=%s", agent.name)
                return
            raise BudgetExceededError(
                "Cycles service unavailable",
                status=-1,
                error_code="TRANSPORT_ERROR",
            )

        if not response.is_success:
            if self._fail_open:
                logger.warning("cycles: unexpected error on LLM reserve, fail-open err=%s", response.error_message)
                return
            raise BudgetExceededError(
                f"Cycles error: {response.error_message}",
                status=response.status,
                error_code=response.get_body_attribute("error") or "UNKNOWN",
            )

        decision = response.get_body_attribute("decision")
        if decision == "DENY":
            reason = response.get_body_attribute("reason_code") or "budget_exceeded"
            raise BudgetExceededError(
                f"LLM reservation denied: {reason}",
                status=response.status,
                error_code="BUDGET_EXCEEDED",
            )

        reservation_id = response.get_body_attribute("reservation_id")
        if reservation_id is None:
            logger.warning("cycles: no reservation_id in LLM response for agent=%s", agent.name)
            return

        pending = PendingReservation(
            run_id=run_id,
            operation_id=operation_id,
            reservation_id=reservation_id,
            tool_name="__llm__",
            agent_name=agent.name,
            estimate=self._llm_estimate,
            unit=self._llm_unit,
            commit_idempotency_key=self._idempotency_key(run_id, "commit", operation_id),
            heartbeat_task=self._start_heartbeat(
                reservation_id,
                run_id=run_id,
                operation_id=operation_id,
            ),
        )
        duplicate = self._tracker.register_llm(pending)
        if duplicate is not None:
            logger.warning(
                "cycles: replacing duplicate LLM operation run=%s reservation=%s",
                run_id,
                duplicate.reservation_id,
            )
            await self._release_reservation(duplicate, "duplicate_llm_operation_replaced")
        logger.debug("cycles: llm_start reserved=%s agent=%s", reservation_id, agent.name)

    async def on_llm_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        response: ModelResponse,
    ) -> None:
        pending = self._tracker.get_llm(self._run_id(context))
        if pending is None:
            return

        pending.cancel_heartbeat()
        tokens_in = getattr(response.usage, "input_tokens", 0) or 0
        tokens_out = getattr(response.usage, "output_tokens", 0) or 0
        actual_amount = pending.estimate

        if pending.unit == Unit.TOKENS:
            actual_amount = tokens_in + tokens_out

        resp = await self._commit_reservation(
            pending,
            Amount(unit=pending.unit, amount=actual_amount),
            CyclesMetrics(tokens_input=tokens_in, tokens_output=tokens_out),
        )
        if resp.is_success:
            self._tracker.complete_llm(pending)
        else:
            logger.error(
                "cycles: LLM commit failed reservation=%s err=%s",
                pending.reservation_id,
                resp.error_message,
            )

    async def on_handoff(
        self,
        context: RunContextWrapper[TContext],
        from_agent: Agent[TContext],
        to_agent: Agent[TContext],
    ) -> None:
        logger.debug("cycles: handoff from=%s to=%s", from_agent.name, to_agent.name)

        run_id = self._run_id(context)
        operation_id = self._tracker.next_operation_id(run_id, "handoff")
        request = EventCreateRequest(
            idempotency_key=self._idempotency_key(run_id, "event", operation_id),
            subject=self._subject(agent_name=from_agent.name),
            action=Action(kind=DEFAULT_ACTION_KIND_HANDOFF, name=to_agent.name),
            actual=Amount(unit=Unit.RISK_POINTS, amount=0),
            metadata={"from_agent": from_agent.name, "to_agent": to_agent.name},
        )
        response = await self._client.create_event(request)
        if not response.is_success:
            logger.warning(
                "cycles: handoff event failed from=%s to=%s err=%s",
                from_agent.name,
                to_agent.name,
                response.error_message,
            )

    async def release_pending(self, reason: str = "agent_run_failed", *, run_id: str | None = None) -> None:
        """Release one run, inferring it only when the choice is unambiguous."""
        resolved_run_id = run_id or _active_run_id.get()
        if resolved_run_id is None:
            pending_run_ids = self._tracker.pending_run_ids
            if not pending_run_ids:
                return
            if len(pending_run_ids) > 1:
                raise RuntimeError("run_id is required when multiple runs have pending reservations")
            resolved_run_id = pending_run_ids[0]
        all_pending = self._tracker.pop_run(resolved_run_id)
        for pending in all_pending:
            await self._release_reservation(pending, reason)

    async def release_all_pending(self, reason: str = "all_agent_runs_failed") -> None:
        """Explicitly release every pending reservation across all runs."""
        for pending in self._tracker.pop_all():
            await self._release_reservation(pending, reason)
