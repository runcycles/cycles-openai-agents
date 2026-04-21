# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Repository `CODEOWNERS` for required-review routing.
- Least-privilege `permissions:` blocks on CI workflows.

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
