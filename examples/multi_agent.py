"""Multi-agent handoff with shared budget governance.

This example shows two agents with a handoff.  Cycles governs the
entire run — including the handoff event — through a single
``CyclesRunHooks.run()`` call.

The guardrail performs a pre-run ``/v1/decide`` check.  If the tenant's
budget is exhausted the agent never starts (zero tokens consumed).

Prerequisites:
    pip install runcycles-openai-agents
    export CYCLES_BASE_URL=http://localhost:7878
    export CYCLES_API_KEY=cyc_live_...
    export OPENAI_API_KEY=sk-...
"""

import asyncio

from agents import Agent

from runcycles_openai_agents import CyclesRunHooks, cycles_budget_guardrail


async def main() -> None:
    # Pre-run check — agent never starts if budget exhausted
    guardrail = cycles_budget_guardrail(
        tenant="acme-corp",
        estimate=5_000_000,  # expect up to ~$0.05 for this run
    )

    # Shared hooks govern both agents
    hooks = CyclesRunHooks(
        tenant="acme-corp",
        app="support-platform",
        workflow="escalation-flow",
    )

    escalation_agent = Agent(
        name="escalation-specialist",
        instructions="You handle escalated support cases with empathy.",
    )

    triage_agent = Agent(
        name="triage",
        instructions=(
            "You triage incoming support requests. If the issue is complex, hand off to the escalation specialist."
        ),
        handoffs=[escalation_agent],
        input_guardrails=[guardrail],
    )

    result = await hooks.run(
        triage_agent,
        input="My account was charged twice and I want a refund immediately!",
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
