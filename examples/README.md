# Examples

Runnable examples demonstrating Cycles budget governance with the OpenAI Agents SDK.

## Prerequisites

```bash
pip install runcycles-openai-agents
export CYCLES_BASE_URL=http://localhost:7878
export CYCLES_API_KEY=cyc_live_...
export OPENAI_API_KEY=sk-...
```

## Examples

| Example | Description |
|---------|-------------|
| [basic_budget.py](basic_budget.py) | LLM token budget enforcement — every LLM call is reserved and committed |
| [tool_governance.py](tool_governance.py) | Tool risk mapping — high-risk tools cost more budget, read-only tools are free |
| [multi_agent.py](multi_agent.py) | Multi-agent handoff with shared budget and pre-run guardrail |
