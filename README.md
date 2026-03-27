# runcycles-openai-agents

Cycles budget governance for the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python).

## Why

The OpenAI Agents SDK gives you hooks and guardrails for content safety, but **nothing for cost control or action authority**. Without budget governance:

- A retry loop burns through $47 of API calls before anyone notices.
- An agent with a `send_email` tool sends 200 emails in a single run because nothing limits it.
- You can't give Tenant A a $10/day budget and Tenant B a $100/day budget — every tenant gets unlimited access.
- There's no audit trail showing which agent called which tool, how many tokens it used, or what it cost.

**This plugin fixes all of that with one line:**

```python
result = await Runner.run(agent, input="...", hooks=CyclesRunHooks(tenant="acme"))
```

Every LLM call and every tool call in the entire agent run — including handoffs to sub-agents — automatically reserves budget before execution and commits actual usage after. If the budget is exhausted, the agent stops. No per-function decoration. No code changes to your tools.

## What It Does

| Problem | How This Solves It |
|---------|-------------------|
| Runaway LLM costs | Every LLM call reserves budget before running. DENY = agent stops. |
| Uncontrolled tool actions | Tool risk map assigns point costs (`send_email: 50`, `search: 0`). High-risk tools exhaust budget faster. |
| No per-tenant limits | Pass `tenant="acme"` — Cycles enforces per-tenant budgets server-side. |
| No pre-run check | `cycles_budget_guardrail` calls `/v1/decide` before the agent starts. Zero tokens consumed on DENY. |
| No audit trail | Every reservation, commit, and handoff is recorded in the Cycles ledger. |
| Agent runs forever | TTL heartbeat auto-extends reservations. If the agent dies, reservations expire and budget is released. |

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
