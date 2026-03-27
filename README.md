# runcycles-openai-agents

Cycles budget governance for the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python).

## Overview

`runcycles-openai-agents` plugs [Cycles](https://runcycles.com) budget governance into the OpenAI Agents SDK's native hook and guardrail system. Every tool call, LLM call, and agent handoff is automatically governed — no per-function decoration required.

## Installation

```bash
pip install runcycles-openai-agents
```

## Quick Start

```python
from agents import Agent, Runner
from runcycles_openai_agents import CyclesRunHooks, cycles_budget_guardrail

# Pre-run budget check — agent never starts if budget exhausted
guardrail = cycles_budget_guardrail(tenant="acme-corp", estimate=5_000_000)

# Runtime governance — every tool/LLM call goes through Cycles
hooks = CyclesRunHooks(
    tenant="acme-corp",
    app="support-platform",
    tool_risk={
        "send_email": 50,      # 50 risk points
        "update_crm": 10,      # 10 risk points
        "search_knowledge": 0, # free — no reservation
    },
)

agent = Agent(
    name="case-resolver",
    instructions="You resolve support cases.",
    input_guardrails=[guardrail],
)

result = await Runner.run(agent, input="...", hooks=hooks)
```

## Features

### CyclesRunHooks

A `RunHooks` implementation that automatically governs the entire agent run:

| Hook | Cycles API Call | Blocking |
|------|----------------|----------|
| `on_tool_start` | `create_reservation` (risk points) | Raises on DENY |
| `on_tool_end` | `commit_reservation` | No |
| `on_llm_start` | `create_reservation` (token/USD budget) | Raises on DENY |
| `on_llm_end` | `commit_reservation` (actual tokens) | No |
| `on_handoff` | `create_event` (audit trail) | No |

### cycles_budget_guardrail

An `InputGuardrail` that calls `/v1/decide` before the agent starts. If the tenant is suspended or budget is exhausted, the guardrail trips and the agent never runs — zero tokens consumed.

### ToolRiskMap

Define a risk policy once. New tools get a default risk level automatically:

```python
from runcycles_openai_agents import ToolRiskMap, ToolRiskConfig

risk_map = ToolRiskMap(
    mapping={
        "send_email": 50,
        "update_crm": ToolRiskConfig(risk_points=10, action_kind="tool.crm.update"),
        "search_knowledge": 0,
    },
    default_risk=1,
)
```

## Configuration

### Environment Variables

Set `CYCLES_BASE_URL` and `CYCLES_API_KEY` for zero-config setup:

```bash
export CYCLES_BASE_URL=http://localhost:7878
export CYCLES_API_KEY=cyc_live_...
```

### Explicit Config

```python
from runcycles import CyclesConfig, AsyncCyclesClient

config = CyclesConfig(base_url="http://localhost:7878", api_key="cyc_live_...")
client = AsyncCyclesClient(config)

hooks = CyclesRunHooks(client=client, tenant="acme-corp")
```

### Fail-Open / Fail-Closed

By default, if the Cycles server is unreachable the agent continues (`fail_open=True`). Set `fail_open=False` to enforce strict budget governance:

```python
hooks = CyclesRunHooks(tenant="acme", fail_open=False)
```

## Development

```bash
pip install -e ".[dev]"
pytest --cov
ruff check .
mypy src/runcycles_openai_agents
```

## License

Apache 2.0
