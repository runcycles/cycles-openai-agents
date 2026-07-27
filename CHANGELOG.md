# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking Changes

- **`CyclesRunHooks` is now fail-closed by default.** `fail_open` changed from
  `True` to `False`, so Cycles transport and HTTP failures now block governed
  tool and LLM calls. Set `fail_open=True` explicitly to retain the previous
  availability-first behavior. The `cycles_budget_guardrail` default is
  unchanged.
- Calling `release_pending()` without a `run_id` no longer releases unrelated
  concurrent runs. It infers the target only when one run is pending and raises
  on ambiguity; use `release_all_pending()` for an intentional global cleanup.

### Added

- **Durable commit-failure settlement via the runcycles 0.5.0 retry engine.**
  When a reservation commit exhausts its bounded inline attempts
  (`commit_max_attempts`), `CyclesRunHooks` now hands it to the SDK's
  `AsyncCommitRetryEngine` — on-disk journal with replay across restarts,
  exponential backoff, and a `POST /v1/events` fallback when the reservation
  expires before the commit lands (the event reuses the commit's idempotency
  key and reservation-time subject/action, and records
  `recovered_reservation_id` / `recovery_reason="commit_after_reservation_expired"`).
  Classification mirrors `runcycles.lifecycle`: 429/`LIMIT_EXCEEDED` retries
  with Retry-After passthrough, 401/403 journals and never releases spent
  budget, codeless or unknown 4xx journals, and recognized protocol
  rejections keep the existing immediate-release behavior. New optional
  `retry_engine` constructor parameter (defaults to an engine built from the
  client's `CyclesConfig`).

- Repository `CODEOWNERS` for required-review routing.
- Least-privilege `permissions:` blocks on CI workflows.
- `CyclesRunHooks.run(...)`, an awaited SDK run-finalization wrapper that
  releases only that run's in-flight reservations before propagating an
  exception or `asyncio.CancelledError`.
- `CyclesRunHooks.run_streamed(...)` and `CyclesRunResultStreaming`, which cover
  streamed model errors, consumer cancellation, early stream closure, and
  explicit `cancel()` even when events are never consumed.
- Configurable heartbeat safety caps: `heartbeat_max_age_ms` defaults to 10
  minutes, and `heartbeat_max_extensions` can impose an additional count cap.
- A production commit retry seam (`commit_max_attempts`, default 2) for
  exceptions, transport failures, HTTP 429, and HTTP 5xx responses. Every
  attempt reuses the same request and deterministic idempotency key.

### Changed

- **`runcycles` dependency floor raised from `>=0.2.0` to `>=0.5.0`** for the
  durable commit retry engine.
- **Exhausted or ambiguous commit failures are no longer released.**
  Previously an exhausted transient commit failure was left for run-end
  cleanup (which released the reservation — returning budget for spend that
  already happened) and a codeless 4xx rejection was released immediately.
  Both are now journaled for background settlement, and the operation is
  retired from the tracker so run cleanup cannot release it.

### Fixed

- Prevent orphaned reservations from being extended forever after tool/LLM
  failures or cancellation. Automatic cleanup now covers real runner failure
  paths when using `hooks.run(...)` or `hooks.run_streamed(...)`; bounded
  heartbeat expiry remains the fallback if callers use bare SDK Runner methods
  without manual cleanup.
- Derive commit idempotency keys from the stable run ID and SDK operation ID
  (tool call ID or LLM span/sequence), and reuse the same request for every
  retry attempt.
- Scope pending tools, LLM calls, operation counters, and cleanup by run and
  operation ID so one hooks instance can safely serve concurrent runs.
- Prevent an unscoped manual cleanup from releasing reservations belonging to
  other live runs.
- Move operations out of active hook correlation before committing. Terminal
  client errors are settled or released immediately; exhausted ambiguous
  failures remain cleanup-eligible but cannot capture a later LLM/tool end or
  its usage metrics.

## [0.2.1] — 2026-05-07

PyPI metadata refresh for category-search discovery. **No code changes** — wire format and public API are identical to 0.2.0.

### Changed

- `pyproject.toml`: rewrote `description` to lead with the cost / action / audit pillars (*"Runtime budget, action, and audit authority for the OpenAI Agents SDK — enforce LLM cost limits, tool call caps, and audit trails before execution."*) and expanded `keywords` from 12 to 25. Added `Topic :: Scientific/Engineering :: Artificial Intelligence` classifier for PyPI browse-by-category surfacing. New keyword groups: cost pillar (`ai-agent`, `agent-budget`, `budget-control`, `cost-enforcement`, `spending-limit`, `llm-cost`), action/risk pillar (`runtime-authority`, `action-control`, `action-authority` — kept existing `tool-risk`), audit pillar (`audit-trail`, `compliance` — kept existing `audit`), and framework targeting (`openai-agents-sdk`, `mcp`, `langchain`).

## [0.2.0] — 2026-04-02

Align API terminology with Cycles protocol spec v0.1.24 and clarify unit handling in examples.

### Changed

- Align API terminology with the Cycles protocol spec (v0.1.24): unit-agnostic
  `Amount(unit, amount)` model and `estimate` / `actual` naming used consistently
  across hooks, guardrails, and reservation calls.
- Bump `runcycles` client dependency to `>=0.2.0`.
- Make unit types explicit in README/code examples.
- Fix `pyproject.toml` description: "tool risk estimates" → "tool estimates".

### Audit

- `AUDIT.md` (2026-04-02): integration is SDK-conformant and protocol-conformant;
  100% test coverage (threshold 95%).

## [0.1.1] — 2026-03-28

Documentation polish and PyPI publish reliability; no functional changes.

### Added

- README: prerequisites, setup section, `OPENAI_API_KEY` configuration, and links
  to tenant / budget / API key setup guides.
- PyPI download-count badge.

### Fixed

- Use `skip-existing` in the PyPI publish step so re-runs do not fail when an
  identical version already exists.
- Bust shields.io cache for the PyPI badge.

## [0.1.0] — 2026-03-27

Initial public release of the Cycles integration for the OpenAI Agents SDK.

### Added

- Cycles budget and tool governance for the OpenAI Agents SDK.
- All 7 `RunHooksBase` lifecycle hooks (`on_agent_start`, `on_agent_end`,
  `on_tool_start`, `on_tool_end`, `on_llm_start`, `on_llm_end`, `on_handoff`).
- `InputGuardrail` integration with `GuardrailFunctionOutput` tripwire.
- Cycles API calls: `reserve`, `commit`, `release`, `extend`, `decide`, `event`.
- Reservation lifecycle: heartbeat extension and release on failure.
- Fail-open / fail-closed error handling on transport errors, HTTP errors, and
  `DENY` decisions.

[Unreleased]: https://github.com/runcycles/cycles-openai-agents/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/runcycles/cycles-openai-agents/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/runcycles/cycles-openai-agents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/runcycles/cycles-openai-agents/releases/tag/v0.1.0
