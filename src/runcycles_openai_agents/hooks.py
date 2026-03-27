"""CyclesRunHooks — Cycles budget governance for OpenAI Agents SDK runs."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, TypeVar

from agents import Agent, AgentHookContext, ModelResponse, RunContextWrapper, RunHooks, Tool
from agents.items import TResponseInputItem
from runcycles import (
    Action,
    Amount,
    AsyncCyclesClient,
    BudgetExceededError,
    CommitOveragePolicy,
    CommitRequest,
    CyclesConfig,
    CyclesMetrics,
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
    DEFAULT_LLM_ESTIMATE,
    DEFAULT_TTL_MS,
)
from runcycles_openai_agents._state import PendingReservation, ReservationTracker
from runcycles_openai_agents.risk_map import ToolRiskConfig, ToolRiskMap

logger = logging.getLogger(__name__)

TContext = TypeVar("TContext")


class CyclesRunHooks(RunHooks[TContext]):
    """Plugs Cycles budget governance into every tool call, LLM call, and
    handoff in an OpenAI Agents SDK run.

    Usage::

        hooks = CyclesRunHooks(
            tenant="acme-corp",
            app="support-platform",
            tool_risk={"send_email": 50, "search": 0},
        )
        result = await Runner.run(agent, input="...", hooks=hooks)
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
        tool_risk: dict[str, int | ToolRiskConfig] | ToolRiskMap | None = None,
        default_tool_risk: int = 1,
        # LLM governance
        llm_estimate: int = DEFAULT_LLM_ESTIMATE,
        llm_unit: Unit = Unit.USD_MICROCENTS,
        # Behaviour
        fail_open: bool = True,
        ttl_ms: int = DEFAULT_TTL_MS,
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

        if isinstance(tool_risk, ToolRiskMap):
            self._risk_map = tool_risk
        else:
            self._risk_map = ToolRiskMap(mapping=tool_risk, default_risk=default_tool_risk)

        self._llm_estimate = llm_estimate
        self._llm_unit = llm_unit
        self._fail_open = fail_open
        self._ttl_ms = ttl_ms
        self._overage_policy = (
            overage_policy if isinstance(overage_policy, CommitOveragePolicy) else CommitOveragePolicy(overage_policy)
        )
        self._dry_run = dry_run

        self._tracker = ReservationTracker()
        self._current_agent_name: str | None = agent

    # --- helpers ---

    def _subject(self, *, agent_name: str | None = None, toolset_name: str | None = None) -> Subject:
        return Subject(
            tenant=self._tenant,
            workspace=self._workspace,
            app=self._app,
            workflow=self._workflow,
            agent=agent_name or self._current_agent_name,
            toolset=toolset_name or self._toolset,
        )

    @staticmethod
    def _idem() -> str:
        return uuid.uuid4().hex

    def _start_heartbeat(self, reservation_id: str) -> asyncio.Task[None] | None:
        """Spawn a background task that extends the reservation at half-TTL intervals.

        Heartbeat is disabled when ``ttl_ms`` is too small (< 2000ms) because the
        minimum interval of 1s would exceed the TTL window.
        """
        if self._ttl_ms < 2000:
            return None
        interval_s = max(self._ttl_ms / 2, 1000) / 1000.0

        async def _loop() -> None:
            try:
                while True:
                    await asyncio.sleep(interval_s)
                    try:
                        request = ReservationExtendRequest(
                            idempotency_key=self._idem(),
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
            request = ReleaseRequest(idempotency_key=self._idem(), reason=reason)
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
        self._current_agent_name = agent.name
        logger.debug("cycles: agent_start agent=%s", agent.name)

    async def on_agent_end(
        self,
        context: AgentHookContext[TContext],
        agent: Agent[TContext],
        output: Any,
    ) -> None:
        logger.debug("cycles: agent_end agent=%s", agent.name)

    async def on_tool_start(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        tool: Tool,
    ) -> None:
        if self._risk_map.is_zero_cost(tool.name):
            logger.debug("cycles: tool_start skip zero-cost tool=%s", tool.name)
            return

        risk_cfg = self._risk_map.get(tool.name)
        request = ReservationCreateRequest(
            idempotency_key=self._idem(),
            subject=self._subject(agent_name=agent.name, toolset_name=tool.name),
            action=Action(kind=risk_cfg.action_kind, name=tool.name),
            estimate=Amount(unit=risk_cfg.unit, amount=risk_cfg.risk_points),
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
                f"Cycles budget service unavailable for tool={tool.name}",
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
                f"Tool budget denied for {tool.name}: {reason}",
                status=response.status,
                error_code="BUDGET_EXCEEDED",
            )

        reservation_id = response.get_body_attribute("reservation_id")
        if reservation_id is None:
            logger.warning("cycles: no reservation_id in response for tool=%s", tool.name)
            return

        pending = PendingReservation(
            reservation_id=reservation_id,
            tool_name=tool.name,
            agent_name=agent.name,
            estimate=risk_cfg.risk_points,
            unit=risk_cfg.unit,
            heartbeat_task=self._start_heartbeat(reservation_id),
        )
        self._tracker.register_tool(tool.name, pending)
        logger.debug("cycles: tool_start reserved=%s tool=%s", reservation_id, tool.name)

    async def on_tool_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        tool: Tool,
        result: str,
    ) -> None:
        pending = self._tracker.pop_tool(tool.name)
        if pending is None:
            return

        pending.cancel_heartbeat()
        commit = CommitRequest(
            idempotency_key=self._idem(),
            actual=Amount(unit=pending.unit, amount=pending.estimate),
        )
        response = await self._client.commit_reservation(pending.reservation_id, commit)
        if not response.is_success:
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
        request = ReservationCreateRequest(
            idempotency_key=self._idem(),
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
                "Cycles budget service unavailable",
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
                f"LLM budget denied: {reason}",
                status=response.status,
                error_code="BUDGET_EXCEEDED",
            )

        reservation_id = response.get_body_attribute("reservation_id")
        if reservation_id is None:
            logger.warning("cycles: no reservation_id in LLM response for agent=%s", agent.name)
            return

        pending = PendingReservation(
            reservation_id=reservation_id,
            tool_name="__llm__",
            agent_name=agent.name,
            estimate=self._llm_estimate,
            unit=self._llm_unit,
            heartbeat_task=self._start_heartbeat(reservation_id),
        )
        previous = self._tracker.register_llm(pending)
        if previous is not None:
            logger.warning("cycles: overwriting pending LLM reservation=%s", previous.reservation_id)
            await self._release_reservation(previous, "overwritten_by_new_llm_call")
        logger.debug("cycles: llm_start reserved=%s agent=%s", reservation_id, agent.name)

    async def on_llm_end(
        self,
        context: RunContextWrapper[TContext],
        agent: Agent[TContext],
        response: ModelResponse,
    ) -> None:
        pending = self._tracker.pop_llm()
        if pending is None:
            return

        pending.cancel_heartbeat()
        tokens_in = getattr(response.usage, "input_tokens", 0) or 0
        tokens_out = getattr(response.usage, "output_tokens", 0) or 0
        actual_amount = pending.estimate

        if pending.unit == Unit.TOKENS:
            actual_amount = tokens_in + tokens_out

        commit = CommitRequest(
            idempotency_key=self._idem(),
            actual=Amount(unit=pending.unit, amount=actual_amount),
            metrics=CyclesMetrics(tokens_input=tokens_in, tokens_output=tokens_out),
        )
        resp = await self._client.commit_reservation(pending.reservation_id, commit)
        if not resp.is_success:
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
        self._current_agent_name = to_agent.name
        logger.debug("cycles: handoff from=%s to=%s", from_agent.name, to_agent.name)

        request = EventCreateRequest(
            idempotency_key=self._idem(),
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

    async def release_pending(self, reason: str = "agent_run_failed") -> None:
        """Release all pending reservations.  Call this when the agent run
        fails and ``on_tool_end`` / ``on_llm_end`` won't fire.
        """
        pending_tools = self._tracker.pop_all_tools()
        pending_llm = self._tracker.pop_llm()
        all_pending = pending_tools + ([pending_llm] if pending_llm else [])
        for pending in all_pending:
            await self._release_reservation(pending, reason)
