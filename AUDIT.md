# Cycles OpenAI Agents SDK Integration — Audit

**Date:** 2026-03-27
**Package:** `runcycles-openai-agents` v0.1.0
**OpenAI Agents SDK:** v0.13.2
**Cycles Client:** `runcycles` v0.2.0
**Protocol Spec:** `cycles-protocol-v0.yaml` (v0.1.24)

---

## Summary

| Category | Pass | Issues |
|----------|------|--------|
| Hook method signatures vs SDK | 7/7 | 0 |
| Guardrail integration vs SDK | 3/3 | 0 |
| Cycles API calls (reserve/commit/release/decide/event) | 5/5 | 0 |
| Model constructors (field names, required fields) | 7/7 | 0 |
| Error handling (fail-open/fail-closed, DENY, transport) | 6/6 | 0 |
| Reservation lifecycle (heartbeat, release on failure) | 3/3 | 0 |
| Test coverage | — | 0 (97.4%, threshold 95%) |
| Type safety (mypy strict) | — | 0 |

**Overall: Integration is SDK-conformant and protocol-conformant.** All hook signatures match the OpenAI Agents SDK `RunHooksBase` class. All Cycles API calls use correct model constructors with valid field names.

---

## Audit Scope

Verified the following across OpenAI Agents SDK source, Cycles protocol spec, and `runcycles` client source:

- All 7 hook method signatures against `RunHooksBase` in `agents/lifecycle.py`
- `InputGuardrail` construction and function signature against `agents/guardrail.py`
- All 5 Cycles API call patterns (reserve, commit, release, extend, decide, event)
- All 7 model constructors (field names, required fields, types)
- Error handling paths (transport error, HTTP error, DENY decision)
- Reservation lifecycle (heartbeat extension, release on failure)

---

## PASS — Hook Signatures

All 7 hook methods match the `RunHooksBase[TContext, Agent]` signatures in `agents/lifecycle.py`:

| Hook | Base Signature | Match |
|------|---------------|-------|
| `on_agent_start(context, agent)` | `AgentHookContext[T], TAgent` | PASS |
| `on_agent_end(context, agent, output)` | `AgentHookContext[T], TAgent, Any` | PASS |
| `on_tool_start(context, agent, tool)` | `RunContextWrapper[T], TAgent, Tool` | PASS |
| `on_tool_end(context, agent, tool, result)` | `RunContextWrapper[T], TAgent, Tool, str` | PASS |
| `on_llm_start(context, agent, system_prompt, input_items)` | `RunContextWrapper[T], Agent[T], Optional[str], list[TResponseInputItem]` | PASS |
| `on_llm_end(context, agent, response)` | `RunContextWrapper[T], Agent[T], ModelResponse` | PASS |
| `on_handoff(context, from_agent, to_agent)` | `RunContextWrapper[T], TAgent, TAgent` | PASS |

**Note:** `TAgent` is bound to `Agent` via `RunHooks = RunHooksBase[TContext, Agent]` (lifecycle.py:171).

**Blocking mechanism:** Hooks are observational (`return None`). Budget enforcement raises `BudgetExceededError` which propagates up and aborts the run. This is the only available mechanism — `context.reject_tool()` is the approval system API, not for hook-based blocking.

---

## PASS — Guardrail Integration

| Check | Status |
|-------|--------|
| `InputGuardrail(guardrail_function=..., name=...)` construction | PASS |
| Function signature `(context, agent, input) -> GuardrailFunctionOutput` | PASS |
| `GuardrailFunctionOutput(output_info, tripwire_triggered)` fields | PASS |

Verified against `InputGuardrail.run()` in `agents/guardrail.py:120` which calls `self.guardrail_function(context, agent, input)`.

---

## PASS — Cycles API Calls

| API Call | Hook | Model | Required Fields | Match |
|----------|------|-------|----------------|-------|
| `create_reservation` | `on_tool_start`, `on_llm_start` | `ReservationCreateRequest` | `idempotency_key, subject, action, estimate` | PASS |
| `commit_reservation` | `on_tool_end`, `on_llm_end` | `CommitRequest` | `idempotency_key, actual` | PASS |
| `release_reservation` | `release_pending` | `ReleaseRequest` | `idempotency_key` | PASS |
| `extend_reservation` | heartbeat task | `ReservationExtendRequest` | `idempotency_key, extend_by_ms` | PASS |
| `decide` | `cycles_budget_guardrail` | `DecisionRequest` | `idempotency_key, subject, action, estimate` | PASS |
| `create_event` | `on_handoff` | `EventCreateRequest` | `idempotency_key, subject, action, actual` | PASS |

---

## PASS — Model Constructor Fields

| Model | Fields Used | Match |
|-------|------------|-------|
| `Subject` | `tenant, workspace, app, workflow, agent, toolset` | PASS |
| `Action` | `kind, name` | PASS |
| `Amount` | `unit, amount` | PASS |
| `CyclesMetrics` | `tokens_input, tokens_output` | PASS |
| `CommitOveragePolicy` | enum `ALLOW_IF_AVAILABLE` | PASS |
| `Unit` | `USD_MICROCENTS, TOKENS, RISK_POINTS` | PASS |
| `CyclesResponse` | `is_success, is_transport_error, get_body_attribute, error_message, status` | PASS |

---

## PASS — Error Handling

| Scenario | Behaviour | Match |
|----------|-----------|-------|
| Transport error + `fail_open=True` | Log warning, allow execution | PASS |
| Transport error + `fail_open=False` | Raise `BudgetExceededError` | PASS |
| HTTP error + `fail_open=True` | Log warning, allow execution | PASS |
| HTTP error + `fail_open=False` | Raise `BudgetExceededError` | PASS |
| DENY decision (tool/LLM) | Raise `BudgetExceededError` | PASS |
| DENY decision (guardrail) | Return `tripwire_triggered=True` | PASS |

---

## PASS — Reservation Lifecycle

| Feature | Implementation | Match |
|---------|---------------|-------|
| Heartbeat (TTL extension) | `asyncio.Task` at `max(ttl_ms/2, 1000)ms` intervals using `extend_reservation` | PASS |
| Heartbeat cancellation | `cancel_heartbeat()` on `on_tool_end` / `on_llm_end` | PASS |
| Release on failure | `release_pending()` releases all pending reservations and cancels heartbeats | PASS |
| Zero-cost tool skip | Tools with `risk_points=0` bypass reservation entirely | PASS |

---

## Test Coverage

```
92 tests, 97.44% coverage (threshold: 95%)

src/runcycles_openai_agents/__init__.py        100%
src/runcycles_openai_agents/_defaults.py       100%
src/runcycles_openai_agents/_state.py          100%
src/runcycles_openai_agents/guardrail.py       100%
src/runcycles_openai_agents/hooks.py            95%
src/runcycles_openai_agents/risk_map.py        100%
```

Uncovered lines in hooks.py (139-150): heartbeat async loop body — internal timing-dependent code that requires real async scheduling to exercise.

---

## Verdict

The integration is **fully conformant** with both the OpenAI Agents SDK v0.13.2 hook/guardrail API and the Cycles Protocol v0.1.24 reservation lifecycle. All model constructors, field names, and API call patterns match the respective source code. No open issues.
