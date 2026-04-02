"""Default constants for the Cycles OpenAI Agents integration."""

from __future__ import annotations

DEFAULT_LLM_ESTIMATE: int = 500_000
"""Default LLM call estimate in USD_MICROCENTS (~$0.005)."""

DEFAULT_TOOL_ESTIMATE: int = 1
"""Default estimate for unmapped tool reservations."""

DEFAULT_TTL_MS: int = 60_000
"""Default reservation time-to-live in milliseconds."""

DEFAULT_ACTION_KIND_TOOL: str = "tool.invoke"
"""Default action kind for tool calls."""

DEFAULT_ACTION_KIND_LLM: str = "llm.completion"
"""Default action kind for LLM calls."""

DEFAULT_ACTION_KIND_HANDOFF: str = "agent.handoff"
"""Default action kind for agent handoffs."""

DEFAULT_ACTION_KIND_RUN: str = "agent.run"
"""Default action kind for guardrail pre-run checks."""
