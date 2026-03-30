[![PyPI](https://img.shields.io/pypi/v/runcycles-openai-agents?v=1)](https://pypi.org/project/runcycles-openai-agents/)
[![PyPI Downloads](https://img.shields.io/pypi/dm/runcycles-openai-agents)](https://pypi.org/project/runcycles-openai-agents/)
[![CI](https://github.com/runcycles/cycles-openai-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/runcycles/cycles-openai-agents/actions)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](https://github.com/runcycles/cycles-openai-agents)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

# Cycles OpenAI Agents SDK Integration

Budget governance for the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python), powered by [Cycles](https://runcycles.io).

## Prerequisites

Before you begin, make sure you have:

1. **Python 3.10+**
2. **An OpenAI API key** — required by the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) to call LLMs
3. **A running Cycles server** — see the [deployment guide](https://runcycles.io/quickstart/deploying-the-full-cycles-stack) to set one up
4. **A Cycles API key** — see [API key management](https://runcycles.io/how-to/api-key-management-in-cycles)
5. **A tenant and budget** — see [tenant management](https://runcycles.io/how-to/tenant-creation-and-management-in-cycles) and [budget allocation](https://runcycles.io/how-to/budget-allocation-and-management-in-cycles)

> **New to Cycles?** The [end-to-end tutorial](https://runcycles.io/quickstart/end-to-end-tutorial) walks through the full setup — from deploying the server to making your first budget-guarded API call — in about 10 minutes.

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

## Setup

Set the following environment variables before running your agent:

```bash
# Required — OpenAI Agents SDK needs this to call LLMs
export OPENAI_API_KEY=sk-...

# Required — tells the plugin where your Cycles server is
export CYCLES_BASE_URL=http://localhost:7878
export CYCLES_API_KEY=cyc_live_...
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

### Hook lifecycle

The hooks plug into the SDK's native `RunHooks` interface and govern the **entire agent run** automatically:

| Hook | Cycles API Call | Blocking | Detail |
|------|----------------|----------|--------|
| `on_tool_start` | `create_reservation` (risk points) | Raises on DENY | Budget reserved based on tool risk map |
| `on_tool_end` | `commit_reservation` | No | Actual risk points committed |
| `on_llm_start` | `create_reservation` (token/USD budget) | Raises on DENY | Budget reserved before each LLM call |
| `on_llm_end` | `commit_reservation` (actual tokens) | No | Real token count from `response.usage` committed |
| `on_handoff` | `create_event` (audit trail) | No | Handoff recorded in Cycles ledger |

All raised exceptions from budget denial trigger `BudgetExceededError`. See [Error Handling Patterns in Python](https://runcycles.io/how-to/error-handling-patterns-in-python) for details.

## Error handling

If `Runner.run()` raises, pending reservations stay locked until TTL expires. Call `release_pending()` to free them immediately:

```python
hooks = CyclesRunHooks(tenant="acme-corp", app="support-platform")

try:
    result = await Runner.run(agent, input="...", hooks=hooks)
except Exception:
    await hooks.release_pending("agent_run_failed")
    raise
```

When budget is denied, the hooks raise `BudgetExceededError`:

```python
from runcycles import BudgetExceededError

try:
    result = await Runner.run(agent, input="...", hooks=hooks)
except BudgetExceededError as e:
    print(f"Budget denied: {e}")
    # Agent stopped — no further tokens consumed
```

## Guardrail (pre-run check)

`cycles_budget_guardrail` returns an `InputGuardrail` that calls `/v1/decide` before the agent starts. If the tenant is suspended or budget is exhausted, the guardrail trips and the agent never runs — zero tokens consumed:

```python
from runcycles_openai_agents import cycles_budget_guardrail

guardrail = cycles_budget_guardrail(
    tenant="acme-corp",
    estimate=5_000_000,      # expected total run cost
    unit=Unit.USD_MICROCENTS,
    fail_open=True,          # allow if Cycles server is down
)

agent = Agent(name="bot", input_guardrails=[guardrail])
```

## Tool risk mapping

Define a risk policy once. New tools added to the agent get a default risk level automatically:

```python
from runcycles_openai_agents import ToolRiskMap, ToolRiskConfig

hooks = CyclesRunHooks(
    tenant="acme-corp",
    tool_risk=ToolRiskMap(
        mapping={
            "send_email": 50,
            "update_crm": ToolRiskConfig(risk_points=10, action_kind="tool.crm.update"),
            "search_knowledge": 0,  # zero-cost — no reservation, no API call
        },
        default_risk=1,  # any unmapped tool costs 1 point
    ),
)
```

## Configuration

### Explicit client

```python
from runcycles import CyclesConfig, AsyncCyclesClient
from runcycles_openai_agents import CyclesRunHooks

config = CyclesConfig(base_url="http://localhost:7878", api_key="cyc_live_...")
client = AsyncCyclesClient(config)

hooks = CyclesRunHooks(client=client, tenant="acme-corp")
```

### Fail-open / fail-closed

By default, if the Cycles server is unreachable the agent continues (`fail_open=True`). Set `fail_open=False` to enforce strict budget governance:

```python
hooks = CyclesRunHooks(tenant="acme", fail_open=False)
```

### All options

```python
CyclesRunHooks(
    client=None,                # AsyncCyclesClient (or auto-created from config/env)
    config=None,                # CyclesConfig (creates client if no client given)
    tenant="acme-corp",         # Subject.tenant
    workspace="prod",           # Subject.workspace
    app="support-platform",     # Subject.app
    workflow="case-resolution", # Subject.workflow
    agent="case-resolver",      # Subject.agent (overridden by actual agent name)
    toolset=None,               # Subject.toolset (overridden by tool name)
    tool_risk={"email": 50},    # dict or ToolRiskMap
    default_tool_risk=1,        # risk points for unmapped tools
    llm_estimate=500_000,       # per-LLM-call estimate (default ~$0.005)
    llm_unit=Unit.USD_MICROCENTS,
    fail_open=True,             # allow execution if Cycles is down
    ttl_ms=60_000,              # reservation TTL (heartbeat extends at half-interval)
    overage_policy=CommitOveragePolicy.ALLOW_IF_AVAILABLE,
    dry_run=False,              # shadow mode — no budget consumed
)
```

## Features

- **Framework-native**: Plugs into the SDK's `RunHooks` interface — not function-level decoration
- **Policy-driven**: Define tool risk once in a map, not per-function
- **LLM governance**: Every LLM call reserves and commits with real token metrics
- **Pre-run guardrail**: `/v1/decide` check before agent starts — zero tokens on DENY
- **Handoff-aware**: Agent handoffs recorded as audit events in the Cycles ledger
- **Automatic heartbeat**: TTL extension keeps reservations alive during long operations
- **Fail-safe cleanup**: `release_pending()` frees locked budget when agent runs fail
- **Fail-open by default**: Agent continues if Cycles server is unreachable
- **Environment config**: `CYCLES_BASE_URL` + `CYCLES_API_KEY` for zero-config setup
- **Typed exceptions**: `BudgetExceededError` for precise error handling

## Examples

The [`examples/`](examples/) directory contains runnable integration examples:

| Example | Description |
|---------|-------------|
| [basic_budget.py](examples/basic_budget.py) | LLM token budget enforcement |
| [tool_governance.py](examples/tool_governance.py) | Tool risk mapping — high-risk tools cost more, read-only tools are free |
| [multi_agent.py](examples/multi_agent.py) | Multi-agent handoff with shared budget and pre-run guardrail |

See [examples/README.md](examples/README.md) for setup instructions.

## Development

```bash
pip install -e ".[dev]"

# Lint
ruff check .

# Type check (strict mode)
mypy src/runcycles_openai_agents

# Run tests with coverage (95% threshold enforced in CI)
pytest --cov
```

CI runs all three checks on Python 3.10 and 3.12 for every push and pull request.

## Documentation

- [Cycles Documentation](https://runcycles.io) — full docs site
- [Python Client](https://pypi.org/project/runcycles/) — the underlying `runcycles` client
- [Cycles Protocol](https://runcycles.io/protocol/how-reserve-commit-works-in-cycles) — how reserve-commit works
- [Error Handling Patterns](https://runcycles.io/how-to/error-handling-patterns-in-python) — handling budget errors

## License

Apache 2.0
