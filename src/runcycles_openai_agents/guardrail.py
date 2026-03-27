"""CyclesBudgetGuardrail — pre-run budget check using the Cycles /v1/decide endpoint."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from agents import Agent, InputGuardrail, RunContextWrapper
from agents.guardrail import GuardrailFunctionOutput
from runcycles import (
    Action,
    Amount,
    AsyncCyclesClient,
    CyclesConfig,
    DecisionRequest,
    Subject,
    Unit,
)

from runcycles_openai_agents._defaults import DEFAULT_ACTION_KIND_RUN

logger = logging.getLogger(__name__)


def cycles_budget_guardrail(
    *,
    client: AsyncCyclesClient | None = None,
    config: CyclesConfig | None = None,
    tenant: str | None = None,
    workspace: str | None = None,
    app: str | None = None,
    estimate: int = 0,
    unit: Unit = Unit.USD_MICROCENTS,
    fail_open: bool = True,
) -> InputGuardrail[Any]:
    """Return an :class:`InputGuardrail` that calls ``/v1/decide`` before the agent runs.

    If the decision is ``DENY`` the guardrail trips and the agent never starts
    (zero tokens consumed).

    Example::

        guardrail = cycles_budget_guardrail(tenant="acme", estimate=1_000_000)
        agent = Agent(name="support", input_guardrails=[guardrail], ...)
    """
    if client is not None:
        _client = client
    elif config is not None:
        _client = AsyncCyclesClient(config)
    else:
        _client = AsyncCyclesClient(CyclesConfig.from_env())

    async def _check(
        ctx: RunContextWrapper[Any],
        agent: Agent[Any],
        input: str | list[Any],  # noqa: A002
    ) -> GuardrailFunctionOutput:
        request = DecisionRequest(
            idempotency_key=uuid.uuid4().hex,
            subject=Subject(tenant=tenant, workspace=workspace, app=app),
            action=Action(kind=DEFAULT_ACTION_KIND_RUN, name=agent.name),
            estimate=Amount(unit=unit, amount=estimate),
        )

        response = await _client.decide(request)

        if response.is_transport_error:
            if fail_open:
                logger.warning("cycles: guardrail transport error, fail-open")
                return GuardrailFunctionOutput(
                    output_info={"decision": "ALLOW", "reason": "fail_open"},
                    tripwire_triggered=False,
                )
            return GuardrailFunctionOutput(
                output_info={"reason": "Cycles budget service unavailable"},
                tripwire_triggered=True,
            )

        if response.is_success:
            decision = response.get_body_attribute("decision")
            if decision == "DENY":
                reason = response.get_body_attribute("reason_code") or "budget_exhausted"
                return GuardrailFunctionOutput(
                    output_info={"decision": "DENY", "reason": reason},
                    tripwire_triggered=True,
                )
            return GuardrailFunctionOutput(
                output_info={"decision": decision or "ALLOW"},
                tripwire_triggered=False,
            )

        if fail_open:
            logger.warning("cycles: guardrail unexpected error, fail-open err=%s", response.error_message)
            return GuardrailFunctionOutput(
                output_info={"decision": "ALLOW", "reason": "fail_open"},
                tripwire_triggered=False,
            )
        return GuardrailFunctionOutput(
            output_info={"reason": f"Cycles error: {response.error_message}"},
            tripwire_triggered=True,
        )

    return InputGuardrail(guardrail_function=_check, name="cycles_budget")
