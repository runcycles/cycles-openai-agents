"""Basic LLM budget enforcement with Cycles.

This example shows how to add budget governance to an OpenAI Agents SDK
agent with a single line change.  Every LLM call is automatically
reserved and committed through Cycles.

Prerequisites:
    pip install runcycles-openai-agents
    export CYCLES_BASE_URL=http://localhost:7878
    export CYCLES_API_KEY=cyc_live_...
    export OPENAI_API_KEY=sk-...
"""

import asyncio

from agents import Agent

from runcycles_openai_agents import CyclesRunHooks


async def main() -> None:
    hooks = CyclesRunHooks(
        tenant="acme-corp",
        app="chatbot",
        # Each LLM call reserves 500_000 USD_MICROCENTS (~$0.005) by default.
        # Override with llm_estimate= and llm_unit= if needed.
    )

    agent = Agent(
        name="assistant",
        instructions="You are a helpful assistant.",
    )

    result = await hooks.run(agent, input="What is the capital of France?")
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
