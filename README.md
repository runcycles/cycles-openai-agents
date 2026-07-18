[![PyPI](https://img.shields.io/pypi/v/runcycles-openai-agents?v=1)](https://pypi.org/project/runcycles-openai-agents/)
[![PyPI Downloads](https://img.shields.io/pypi/dm/runcycles-openai-agents)](https://pypi.org/project/runcycles-openai-agents/)
[![CI](https://github.com/runcycles/cycles-openai-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/runcycles/cycles-openai-agents/actions)
[![Coverage](https://img.shields.io/badge/coverage-95%25-brightgreen)](https://github.com/runcycles/cycles-openai-agents)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

# OpenAI Agents SDK Budget Control — Cycles integration for Python

**Runtime budget, action, and audit authority for the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) — enforce LLM cost limits, tool call caps, action permissions, and audit trails on Python AI agents before execution.** Wraps OpenAI Agents SDK hooks and guardrails with the [Cycles Protocol](https://github.com/runcycles/cycles-protocol) reservation lifecycle: per-tenant budgets, tool risk scoring, pre-run checks, and structured audit trails. Install via `pip install runcycles-openai-agents`.

## Prerequisites

Before you begin, make sure you have:

1. **Python 3.10+**
2. **An OpenAI API key** — required by the [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) to call LLMs
3. **A running Cycles server** — see the [deployment guide](https://runcycles.io/quickstart/deploying-the-full-cycles-stack) to set one up
4. **A Cycles API key** — see [API key management](https://runcycles.io/how-to/api-key-management-in-cycles)
5. **A tenant and budget** — see [tenant management](https://runcycles.io/how-to/tenant-creation-and-management-in-cycles) and [budget allocation](https://runcycles.io/how-to/budget-allocation-and-management-in-cycles)

> **New to Cycles?** The [end-to-end tutorial](https://runcycles.io/quickstart/end-to-end-tutorial) walks through the full setup — from deploying the server to making your first budget-guarded API call — in about 10 minutes.

## Why

The OpenAI Agents SDK gives you hooks and guardrails for content safety, but **nothing for governance or action authority**. Without Cycles governance:

- A retry loop burns through $47 of API calls before anyone notices.
- An agent with a `send_email` tool sends 200 emails in a single run because nothing limits it.
- You can't give Tenant A a $10/day budget and Tenant B a $100/day budget — every tenant gets unlimited access.
- There's no audit trail showing which agent called which tool, how many tokens it used, or what was consumed.

**This plugin fixes all of that with one line:**

```python
result = await CyclesRunHooks(tenant="acme").run(agent, input="...")
```

Every LLM call and every tool call in the entire agent run — including handoffs to sub-agents — automatically reserves budget before execution and commits actual usage after. If the budget is exhausted, the agent stops. No per-function decoration. No code changes to your tools.

## What It Does

| Problem | How This Solves It |
|---------|-------------------|
| Runaway LLM spending | Every LLM call reserves budget before running. DENY = agent stops. |
| Uncontrolled tool actions | Tool estimate map assigns per-call estimates (`send_email: 50`, `search: 0`). Higher-estimate tools consume budget faster. |
| No per-tenant limits | Pass `tenant="acme"` — Cycles enforces per-tenant budgets server-side. |
| No pre-run check | `cycles_budget_guardrail` calls `/v1/decide` before the agent starts. Zero tokens consumed on DENY. |
| No audit trail | Every reservation, commit, and handoff is recorded in the Cycles ledger. |
| Long-running or abandoned calls | TTL heartbeats extend active reservations for at most 10 minutes by default. Cleanup releases known reservations; a missed cleanup path still degrades to TTL expiry. |

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
from agents import Agent
from runcycles_openai_agents import CyclesRunHooks, cycles_budget_guardrail

# Pre-run budget check — agent never starts if budget exhausted
guardrail = cycles_budget_guardrail(tenant="acme-corp", estimate=5_000_000)

# Runtime governance — every tool/LLM call goes through Cycles
hooks = CyclesRunHooks(
    tenant="acme-corp",
    app="support-platform",
    tool_estimates={
        "send_email": 50,      # 50 RISK_POINTS per call
        "update_crm": 10,      # 10 RISK_POINTS per call
        "search_knowledge": 0, # zero estimate — no reservation
    },
)

agent = Agent(
    name="case-resolver",
    instructions="You resolve support cases.",
    input_guardrails=[guardrail],
)

result = await hooks.run(agent, input="...")
```

### Hook lifecycle

The hooks plug into the SDK's native `RunHooks` interface and govern the **entire agent run**. Use `hooks.run(...)` for non-streaming runs or `hooks.run_streamed(...)` for streaming runs so cleanup is attached at the SDK run-finalization boundary:

| Hook | Cycles API Call | Blocking | Detail |
|------|----------------|----------|--------|
| `on_tool_start` | `create_reservation` (tool estimate) | Raises on DENY | Budget reserved based on tool estimate map |
| `on_tool_end` | `commit_reservation` | No | Actual amount committed |
| `on_llm_start` | `create_reservation` (LLM estimate) | Raises on DENY | Budget reserved before each LLM call |
| `on_llm_end` | `commit_reservation` (actual tokens) | No | Real token count from `response.usage` committed |
| `on_handoff` | `create_event` (audit trail) | No | Handoff recorded in Cycles ledger |

All raised exceptions from budget denial trigger `BudgetExceededError`. See [Error Handling Patterns in Python](https://runcycles.io/how-to/error-handling-patterns-in-python) for details.

## Error handling

`CyclesRunHooks.run()` releases reservations scoped to that run before re-raising an exception or `asyncio.CancelledError`:

```python
hooks = CyclesRunHooks(tenant="acme-corp", app="support-platform")

result = await hooks.run(agent, input="...")
```

Streaming runs receive the same protection. Consume the returned proxy's events, or call its synchronous `cancel()` method; either path waits for or schedules run-scoped cleanup:

```python
result = hooks.run_streamed(agent, input="...")
async for event in result.stream_events():
    handle(event)
```

The OpenAI Agents SDK does not expose a general `RunHooks.on_error` callback. If you call bare `Runner.run(..., hooks=hooks)` or `Runner.run_streamed(..., hooks=hooks)`, automatic exception/cancellation cleanup cannot run. In a single-run error path, call `release_pending()`; when multiple runs are pending it raises instead of guessing and releasing another run. Prefer the wrappers for concurrent runs because they carry a stable run ID into scoped cleanup. `release_all_pending()` is the explicit application-shutdown escape hatch. Heartbeats are capped at 10 minutes by default, so even a missed cleanup path eventually falls back to reservation TTL expiry instead of extending forever.

Commit failures are isolated from later operations. A reservation leaves active start/end correlation before its commit is attempted: ordinary client rejections are released immediately, already-finalized and idempotency-mismatch responses are retired without an unsafe release, and exhausted ambiguous failures remain available only for run cleanup. A later LLM or tool completion therefore cannot accidentally reuse the failed reservation or attach new usage metrics to it.

When budget is denied, the hooks raise `BudgetExceededError`:

```python
from runcycles import BudgetExceededError

try:
    result = await hooks.run(agent, input="...")
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
    estimate=5_000_000,      # expected total run estimate
    unit=Unit.USD_MICROCENTS,
    fail_open=True,          # allow if Cycles server is down
)

agent = Agent(name="bot", input_guardrails=[guardrail])
```

## Tool estimate mapping

Define an estimate policy once. New tools added to the agent get a default estimate automatically:

```python
from runcycles_openai_agents import ToolEstimateMap, ToolEstimateConfig

hooks = CyclesRunHooks(
    tenant="acme-corp",
    tool_estimates=ToolEstimateMap(
        mapping={
            "send_email": 50,                       # 50 RISK_POINTS (default unit)
            "update_crm": ToolEstimateConfig(
                estimate=10,
                action_kind="tool.crm.update",
                unit=Unit.RISK_POINTS,              # explicit unit
            ),
            "search_knowledge": 0,                  # zero estimate — no reservation
        },
        default_estimate=1,                         # unmapped tools: 1 RISK_POINT
        default_unit=Unit.RISK_POINTS,              # unit for int shorthand values
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

Hooks are fail-closed by default (`fail_open=False`): if the Cycles server is unreachable, the governed operation is blocked. Availability-first behavior remains an explicit opt-in:

```python
hooks = CyclesRunHooks(tenant="acme", fail_open=True)
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
    tool_estimates={"email": 50}, # dict or ToolEstimateMap (default unit: RISK_POINTS)
    default_tool_estimate=1,    # estimate for unmapped tools (in default unit)
    llm_estimate=500_000,       # per-LLM-call estimate (~$0.005 in USD_MICROCENTS)
    llm_unit=Unit.USD_MICROCENTS,
    fail_open=False,            # block execution if Cycles is down (default)
    ttl_ms=60_000,              # reservation TTL (heartbeat extends at half-interval)
    heartbeat_max_age_ms=600_000, # stop extending after 10 minutes
    heartbeat_max_extensions=None, # optional additional extension-count cap
    commit_max_attempts=2,      # retry transport, 429, and 5xx commit failures
    overage_policy=CommitOveragePolicy.ALLOW_IF_AVAILABLE,
    dry_run=False,              # shadow mode — no budget consumed
)
```

## Features

- **Framework-native**: Plugs into the SDK's `RunHooks` interface — not function-level decoration
- **Policy-driven**: Define tool estimates once in a map, not per-function
- **LLM governance**: Every LLM call reserves and commits with real token metrics
- **Pre-run guardrail**: `/v1/decide` check before agent starts — zero tokens on DENY
- **Handoff-aware**: Agent handoffs recorded as audit events in the Cycles ledger
- **Bounded heartbeat**: TTL extension keeps active reservations alive but stops after a configurable maximum age
- **Run-finalization cleanup**: `hooks.run()` and `hooks.run_streamed()` release run-scoped reservations on exceptions and cancellation; TTL expiry is the fallback
- **Replay-safe, isolated commits**: Retries reuse one deterministic request, and failed settlements cannot poison later operation correlation
- **Fail-closed by default**: Cycles transport and HTTP errors block governed operations; `fail_open=True` is explicit opt-in
- **Environment config**: `CYCLES_BASE_URL` + `CYCLES_API_KEY` for zero-config setup
- **Typed exceptions**: `BudgetExceededError` for precise error handling

## Examples

The [`examples/`](examples/) directory contains runnable integration examples:

| Example | Description |
|---------|-------------|
| [basic_budget.py](examples/basic_budget.py) | LLM token budget enforcement |
| [tool_governance.py](examples/tool_governance.py) | Tool estimate mapping — higher-estimate tools consume more, read-only tools use zero estimate |
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
