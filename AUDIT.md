# Cycles OpenAI Agents SDK Integration — Audit

**Date:** 2026-07-18
**Package:** `runcycles-openai-agents` v0.2.1 + unreleased lifecycle fixes
**OpenAI Agents SDK:** v0.13.2
**Cycles Client:** `runcycles` v0.4.1
**Protocol Spec:** `cycles-protocol-v0.yaml` (v0.1.24)

---

## Summary

| Category | Pass | Issues |
|----------|------|--------|
| Hook method signatures vs SDK | 7/7 | 0 |
| Guardrail integration vs SDK | 3/3 | 0 |
| Cycles API calls (reserve/commit/release/decide/event) | 6/6 | 0 |
| Model constructors (field names, required fields) | 7/7 | 0 |
| Amount constructions (unit, amount fields) | 6/6 | 0 |
| Error handling (fail-open/fail-closed, DENY, transport) | 7/7 | 0 |
| Reservation lifecycle (bounded heartbeat, exception/cancellation cleanup) | 12/12 | 0 |
| Concurrent-run state isolation | 2/2 | 0 |
| Commit idempotency replay | 3/3 | 0 |
| Protocol terminology alignment | — | 0 |
| Test coverage | — | 0 (95.28%, threshold 95%) |
| Type safety (mypy strict) | — | 0 |

**Overall: Integration is SDK-conformant, protocol-conformant, lifecycle-safe through the documented non-streaming and streaming wrappers, and terminology-aligned.** The SDK exposes no general `RunHooks.on_error` callback; `CyclesRunHooks.run()` and `CyclesRunHooks.run_streamed()` therefore wrap the SDK Runner at its finalization boundaries. Bare Runner calls still require caller-managed error cleanup, but the bounded heartbeat ensures a missed path degrades to TTL expiry instead of an immortal reservation.

---

## Audit Scope

Verified the following across OpenAI Agents SDK source, Cycles protocol spec, and `runcycles` client source:

- All 7 hook method signatures against `RunHooksBase` in `agents/lifecycle.py`
- `InputGuardrail` construction and function signature against `agents/guardrail.py`
- All 6 Cycles API call patterns (reserve, commit, release, extend, decide, event)
- All 7 model constructors (field names, required fields, types)
- All 6 `Amount()` constructions (correct `unit` and `amount` fields)
- Error handling paths (transport error, HTTP error, DENY decision)
- Reservation lifecycle (non-streaming and streaming exception/cancellation release, bounded heartbeat, TTL fallback)
- Deterministic commit idempotency across ambiguous retry attempts
- Production commit retries reuse one request for exceptions, transport errors,
  HTTP 429, and HTTP 5xx responses
- One hooks instance serving concurrent runs without cross-run overwrite or cleanup
- Terminology consistency with protocol spec (`estimate`, `actual`, `Amount`)

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
| `release_reservation` | run wrappers, `on_agent_end`, scoped cleanup methods | `ReleaseRequest` | `idempotency_key` | PASS |
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

## PASS — Amount Construction Verification

All 6 `Amount()` constructions verified against protocol spec:

| Location | Construction | Request Field | Unit Source | Amount Source | Match |
|----------|-------------|---------------|-------------|---------------|-------|
| `hooks.py` tool reserve | `Amount(unit=est_cfg.unit, amount=est_cfg.estimate)` | `ReservationCreateRequest.estimate` | `ToolEstimateConfig.unit` | `ToolEstimateConfig.estimate` | PASS |
| `hooks.py` tool commit | `Amount(unit=pending.unit, amount=pending.estimate)` | `CommitRequest.actual` | `PendingReservation.unit` | `PendingReservation.estimate` | PASS |
| `hooks.py` LLM reserve | `Amount(unit=self._llm_unit, amount=self._llm_estimate)` | `ReservationCreateRequest.estimate` | `CyclesRunHooks.llm_unit` | `CyclesRunHooks.llm_estimate` | PASS |
| `hooks.py` LLM commit | `Amount(unit=pending.unit, amount=actual_amount)` | `CommitRequest.actual` | `PendingReservation.unit` | computed or estimate | PASS |
| `hooks.py` handoff event | `Amount(unit=Unit.RISK_POINTS, amount=0)` | `EventCreateRequest.actual` | literal `RISK_POINTS` | literal `0` | PASS |
| `guardrail.py` decide | `Amount(unit=unit, amount=estimate)` | `DecisionRequest.estimate` | function param | function param | PASS |

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
| Hooks default | `fail_open=False`; `True` remains explicit opt-in | PASS |

---

## PASS — Reservation Lifecycle

| Feature | Implementation | Match |
|---------|---------------|-------|
| Heartbeat (TTL extension) | `asyncio.Task` at `max(ttl_ms/2, 1000)ms` intervals using `extend_reservation` | PASS |
| Heartbeat maximum age | Stops extending after configurable `heartbeat_max_age_ms` (default 600,000 ms) | PASS |
| Heartbeat extension count | Optional `heartbeat_max_extensions` stops after N extensions | PASS |
| Heartbeat cancellation | `cancel_heartbeat()` on `on_tool_end` / `on_llm_end` | PASS |
| Exception cleanup | `CyclesRunHooks.run()` releases the failing run before re-raising | PASS |
| Cancellation cleanup | `CyclesRunHooks.run()` releases with `agent_run_cancelled` before propagating `CancelledError` | PASS |
| Streaming exception cleanup | `CyclesRunHooks.run_streamed()` releases before its event iterator propagates the error | PASS |
| Streaming cancellation cleanup | Consumer cancellation and proxy `cancel()` both release the streaming run | PASS |
| Success anomaly cleanup | `on_agent_end` releases only unexpected leftovers for its run | PASS |
| Manual cleanup isolation | `release_pending()` requires a run ID when multiple runs are pending; global cleanup is explicit | PASS |
| Direct SDK fallback | Bounded heartbeat stops; reservation then expires by TTL if caller misses manual cleanup | PASS |
| Zero-estimate tool skip | Tools with `estimate=0` bypass reservation entirely | PASS |

### Run and operation correlation

- Each `CyclesRunHooks.run()` invocation has an isolated run ID carried through
  SDK hook tasks with a `ContextVar`.
- Tool operations use the SDK `tool_call_id`; LLM operations use the current
  span plus a run-scoped sequence. Legacy direct-hook calls fall back to the
  current trace/context identity.
- Pending reservations and counters are keyed by run and operation, so two
  concurrent runs on one hooks instance cannot overwrite each other's live LLM
  or tool reservation.
- Commit idempotency keys are SHA-256 derivations of run ID, operation type,
  and operation ID. Retryable commit exceptions and responses replay the exact
  same request and key before state is removed on confirmed success.

---

## PASS — Protocol Terminology Alignment

All naming follows the protocol's unit-agnostic model:

| Concept | Protocol Term | Integration Term | Match |
|---------|--------------|-----------------|-------|
| Pre-execution amount | `ReservationCreateRequest.estimate: Amount` | `ToolEstimateConfig.estimate: int` + `unit: Unit` | PASS |
| Post-execution amount | `CommitRequest.actual: Amount` | `PendingReservation.estimate` (or computed tokens) | PASS |
| Amount container | `Amount(unit, amount)` | `Amount(unit=..., amount=...)` at all 6 sites | PASS |
| Unit types | `Unit` enum (`USD_MICROCENTS`, `TOKENS`, `CREDITS`, `RISK_POINTS`) | Same enum, configurable per operation | PASS |
| LLM pre-execution amount | `estimate: Amount` | `llm_estimate: int` + `llm_unit: Unit` | PASS |
| Guardrail pre-run amount | `estimate: Amount` | `estimate: int` + `unit: Unit` | PASS |

No references to deprecated terminology (`risk_points`, `tool_risk`, `is_zero_cost`, `zero-cost`) remain in source, tests, or documentation.

---

## Test Coverage

```
117 tests, 95.28% coverage (threshold: 95%)

src/runcycles_openai_agents/__init__.py        100%
src/runcycles_openai_agents/_defaults.py       100%
src/runcycles_openai_agents/_state.py           99%
src/runcycles_openai_agents/guardrail.py       100%
src/runcycles_openai_agents/hooks.py            93%
src/runcycles_openai_agents/tool_estimate_map.py  100%
```

---

## Verdict

The integration is **conformant** with the OpenAI Agents SDK v0.13.2 hook/guardrail API, the Cycles Protocol v0.1.24 reservation lifecycle, and the protocol's unit-agnostic terminology. Real `Runner` tests cover LLM exceptions, propagated tool exceptions, `asyncio.CancelledError`, streamed model errors, streamed consumer cancellation, explicit stream cancellation without consumption, commit retry, and concurrent runs sharing one hooks instance. Commit retries reuse stable keys and requests, heartbeat lifetime is bounded, manual cleanup is run-scoped, and fail-closed governance is now the hooks default. The documented limitation is explicit: automatic exception/cancellation release requires `CyclesRunHooks.run()` or `CyclesRunHooks.run_streamed()` because the SDK's bare hooks interface has no general error callback.

---

## 0.2.1 — PyPI Metadata Refresh (2026-05-07)

**Files:** `pyproject.toml`. **No code changes.** Wire format, public API, hook/guardrail conformance, and protocol conformance are identical to 0.2.0.

- **Description rewritten** to lead with the cost / action / audit pillars: *"Runtime budget, action, and audit authority for the OpenAI Agents SDK — enforce LLM cost limits, tool call caps, and audit trails before execution."*
- **Keywords expanded** 12 → 25, organized into category-search terms (`ai-agent`, `agent-budget`, `budget-control`, `cost-enforcement`, `spending-limit`, `llm-cost`, `runtime-authority`, `action-control`, `action-authority`, `audit-trail`, `compliance`, `multi-tenant`), framework targeting (`openai-agents`, `openai-agents-sdk`, `mcp`, `langchain`), and brand.
- **Classifier added:** `Topic :: Scientific/Engineering :: Artificial Intelligence` — standard PyPI classifier for AI/ML packages.

Driven by package-portfolio SEO diagnostic. The cost / action / audit triad now leads the description, matching the three pillars of Cycles' value proposition.
