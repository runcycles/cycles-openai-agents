"""Tool risk mapping with action authority.

This example shows how to define a risk policy for tools.  High-risk
tools like ``send_email`` consume more budget, while read-only tools
like ``search_knowledge`` are free (zero risk points, no reservation).

If a tool call would exceed the budget, ``context.reject_tool()`` is
called and the agent receives an error message instead of executing
the tool.

Prerequisites:
    pip install runcycles-openai-agents
    export CYCLES_BASE_URL=http://localhost:7878
    export CYCLES_API_KEY=cyc_live_...
    export OPENAI_API_KEY=sk-...
"""

import asyncio

from agents import Agent, Runner, function_tool

from runcycles_openai_agents import CyclesRunHooks, ToolRiskConfig


@function_tool
def send_email(to: str, body: str) -> str:
    """Send an email to a recipient."""
    return f"Email sent to {to}"


@function_tool
def search_knowledge(query: str) -> str:
    """Search the internal knowledge base."""
    return f"Results for '{query}': [article-1, article-2]"


@function_tool
def update_crm(customer_id: str, notes: str) -> str:
    """Update CRM notes for a customer."""
    return f"CRM updated for {customer_id}"


async def main() -> None:
    hooks = CyclesRunHooks(
        tenant="acme-corp",
        app="support-platform",
        tool_risk={
            "send_email": 50,  # high risk — 50 points
            "update_crm": ToolRiskConfig(risk_points=10, action_kind="tool.crm.update"),
            "search_knowledge": 0,  # free — no reservation needed
        },
        default_tool_risk=5,  # any new tool defaults to 5 points
    )

    agent = Agent(
        name="case-resolver",
        instructions="You resolve customer support cases. Use the available tools.",
        tools=[send_email, search_knowledge, update_crm],
    )

    result = await Runner.run(
        agent,
        input="Customer #123 can't log in. Search for a fix and email them the solution.",
        hooks=hooks,
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
