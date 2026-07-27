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
    ErrorCode,
    EventCreateRequest,
    ReleaseRequest,
    ReservationCreateRequest,
    ReservationExtendRequest,
    Subject,
    Unit,
)
from runcycles.retry import AsyncCommitRetryEngine

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
        retry_engine: AsyncCommitRetryEngine | None = None,
    ) -> None:
        engine_config: Any = config
        if client is not None:
            self._client = client
            if engine_config is None:
                engine_config = getattr(client, "_config", None)
        elif config is not None:
            self._client = AsyncCyclesClient(config)
        else:
            engine_config = CyclesConfig.from_env()
            self._client = AsyncCyclesClient(engine_config)

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

        # Background settlement for commits that exhaust the bounded inline
        # attempts: the SDK's durable retry engine (runcycles>=0.5.0) journals
        # them on disk, retries with backoff, and falls back to POST /v1/events
        # when the reservation expires before the commit lands.
        if retry_engine is not None:
            self._retry_engine: AsyncCommitRetryEngine | None = retry_engine
        elif isinstance(engine_config, CyclesConfig):
            self._retry_engine = AsyncCommitRetryEngine(engine_config)
        else:
            self._retry_engine = None
        if self._retry_engine is not None:
            self._retry_engine.set_client(self._client)

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
        request: CommitRequest,
    ) -> CyclesResponse | None:
        """Commit with one stable request and idempotency key across retries.

        Returns ``None`` when every bounded inline attempt raised; the caller
        then routes the commit to the background retry engine instead of
        letting the exception abort the run (and release spent budget).
        """
        for attempt in range(1, self._commit_max_attempts + 1):
            try:
                response = await self._client.commit_reservation(pending.reservation_id, request)
            except Exception:
                logger.warning(
                    "cycles: commit raised reservation=%s attempt=%s",
                    pending.reservation_id,
                    attempt,
                    exc_info=True,
                )
                if attempt >= self._commit_max_attempts:
                    return None
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

    @staticmethod
    def _commit_error_code(response: CyclesResponse) -> str | None:
        error_response = response.get_error_response()
        if error_response is not None and error_response.error_code is not None:
            return str(error_response.error_code.value)
        # Mirror runcycles.retry._extract_error_code: fall back to the raw
        # body field so partial error bodies still classify correctly.
        raw = response.get_body_attribute("error")
        return raw if isinstance(raw, str) else None

    @staticmethod
    def _recognized_rejection(error_code: str | None) -> bool:
        """True when the error code is a known protocol code (not forward-compat).

        Mirrors ``runcycles.retry``: only a recognized rejection justifies
        releasing a reservation whose guarded work already ran; a codeless or
        unknown-future-code 4xx is journaled instead.
        """
        return error_code is not None and ErrorCode.from_string(error_code) is not ErrorCode.UNKNOWN

    def _build_event_fallback_body(
        self,
        pending: PendingReservation,
        request: CommitRequest,
    ) -> dict[str, Any]:
        """Build a POST /v1/events body recording spend whose reservation expired.

        Mirrors ``runcycles.lifecycle._build_event_fallback_body``: reuses the
        commit's idempotency key (the event idempotency namespace is separate,
        so replays across process restarts stay exactly-once) and omits
        ``overage_policy`` — the spec default ALLOW_IF_AVAILABLE never rejects,
        the right bias when the spend has already happened.
        """
        commit_body = request.model_dump(mode="json", exclude_none=True)
        subject = pending.subject if pending.subject is not None else self._subject(agent_name=pending.agent_name)
        action = pending.action if pending.action is not None else Action(kind="unknown", name=pending.tool_name)
        body: dict[str, Any] = {
            "idempotency_key": commit_body["idempotency_key"],
            "subject": subject.model_dump(mode="json", exclude_none=True),
            "action": action.model_dump(mode="json", exclude_none=True),
            "actual": commit_body["actual"],
            "metadata": {
                "recovered_reservation_id": pending.reservation_id,
                "recovery_reason": "commit_after_reservation_expired",
            },
        }
        if "metrics" in commit_body:
            body["metrics"] = commit_body["metrics"]
        return body

    def _schedule_commit_recovery(
        self,
        pending: PendingReservation,
        request: CommitRequest,
        *,
        retry_after_ms: int | None = None,
        expired: bool = False,
    ) -> None:
        """Hand an exhausted commit to the SDK's durable retry engine."""
        if self._retry_engine is None:
            logger.error(
                "cycles: no retry engine available; failed commit dropped reservation=%s",
                pending.reservation_id,
            )
            return
        event_body = self._build_event_fallback_body(pending, request)
        if expired:
            self._retry_engine.schedule_event(pending.reservation_id, event_body)
            return
        self._retry_engine.schedule(
            pending.reservation_id,
            request.model_dump(mode="json", exclude_none=True),
            event_body,
            retry_after_ms=retry_after_ms,
        )

    async def _settle_failed_commit(
        self,
        pending: PendingReservation,
        request: CommitRequest,
        response: CyclesResponse | None,
        *,
        operation: Literal["tool", "llm"],
    ) -> None:
        """Route an exhausted commit failure; never release spent budget.

        Classification mirrors ``runcycles.lifecycle._handle_commit`` (SDK
        0.5.0): ambiguous/transient outcomes and auth failures are journaled
        for background retry, an expired reservation falls back to
        POST /v1/events, and only recognized protocol rejections keep the
        pre-0.5.0 release behavior. The operation is always retired from the
        tracker so run cleanup cannot release a journaled commit's budget.
        """
        try:
            if response is None or response.is_transport_error or response.is_server_error:
                # Raised on every inline attempt, transport failure, or 5xx:
                # the spend happened — journal and retry in the background.
                self._schedule_commit_recovery(pending, request)
                return
            error_code = self._commit_error_code(response)
            if response.status == 429 or error_code == "LIMIT_EXCEEDED":
                # Rate-limited, not rejected: retry honoring Retry-After.
                self._schedule_commit_recovery(pending, request, retry_after_ms=response.retry_after_ms_header)
            elif response.status in (401, 403):
                # Credentials failed after the spend happened: journal for
                # replay once fixed. Never release — that would return budget
                # for real spend.
                self._schedule_commit_recovery(pending, request)
            elif response.status == 410 or error_code == "RESERVATION_EXPIRED":
                logger.warning(
                    "cycles: reservation expired before commit; recovering spend via POST /v1/events reservation=%s",
                    pending.reservation_id,
                )
                self._schedule_commit_recovery(pending, request, expired=True)
            elif error_code == "RESERVATION_FINALIZED":
                logger.warning(
                    "cycles: reservation already finalized reservation=%s",
                    pending.reservation_id,
                )
            elif error_code == "IDEMPOTENCY_MISMATCH":
                logger.warning(
                    "cycles: commit idempotency mismatch; reservation not released reservation=%s",
                    pending.reservation_id,
                )
            elif response.is_client_error and self._recognized_rejection(error_code):
                await self._release_reservation(pending, f"commit_rejected_{error_code}")
            elif response.is_client_error:
                # Codeless or forward-compat-unknown 4xx: neither release nor
                # drop — retain the spend record for background settlement.
                self._schedule_commit_recovery(pending, request)
            else:
                logger.warning(
                    "cycles: unrecognized commit response reservation=%s status=%s",
                    pending.reservation_id,
                    response.status,
                )
        finally:
            if operation == "tool":
                self._tracker.complete_tool(pending)
            else:
                self._tracker.complete_llm(pending)

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
        subject = self._subject(agent_name=agent.name, toolset_name=tool.name)
        action = Action(kind=est_cfg.action_kind, name=tool.name)
        request = ReservationCreateRequest(
            idempotency_key=self._idempotency_key(run_id, "reserve", operation_id),
            subject=subject,
            action=action,
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
            subject=subject,
            action=action,
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

        if not self._tracker.begin_tool_commit(pending):
            return
        pending.cancel_heartbeat()
        request = CommitRequest(
            idempotency_key=pending.commit_idempotency_key,
            actual=Amount(unit=pending.unit, amount=pending.estimate),
        )
        response = await self._commit_reservation(pending, request)
        if response is not None and response.is_success:
            self._tracker.complete_tool(pending)
        else:
            logger.error(
                "cycles: commit failed reservation=%s tool=%s err=%s",
                pending.reservation_id,
                tool.name,
                response.error_message if response is not None else "commit raised on every attempt",
            )
            await self._settle_failed_commit(pending, request, response, operation="tool")

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
        subject = self._subject(agent_name=agent.name)
        action = Action(kind=DEFAULT_ACTION_KIND_LLM, name=agent.name)
        request = ReservationCreateRequest(
            idempotency_key=self._idempotency_key(run_id, "reserve", operation_id),
            subject=subject,
            action=action,
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
            subject=subject,
            action=action,
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

        if not self._tracker.begin_llm_commit(pending):
            return
        pending.cancel_heartbeat()
        tokens_in = getattr(response.usage, "input_tokens", 0) or 0
        tokens_out = getattr(response.usage, "output_tokens", 0) or 0
        actual_amount = pending.estimate

        if pending.unit == Unit.TOKENS:
            actual_amount = tokens_in + tokens_out

        request = CommitRequest(
            idempotency_key=pending.commit_idempotency_key,
            actual=Amount(unit=pending.unit, amount=actual_amount),
            metrics=CyclesMetrics(tokens_input=tokens_in, tokens_output=tokens_out),
        )
        resp = await self._commit_reservation(pending, request)
        if resp is not None and resp.is_success:
            self._tracker.complete_llm(pending)
        else:
            logger.error(
                "cycles: LLM commit failed reservation=%s err=%s",
                pending.reservation_id,
                resp.error_message if resp is not None else "commit raised on every attempt",
            )
            await self._settle_failed_commit(pending, request, resp, operation="llm")

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
