"""Default constants for the Cycles OpenAI Agents integration."""

from __future__ import annotations

DEFAULT_LLM_ESTIMATE: int = 500_000
"""Default LLM call estimate in budget units (~$0.005 in USD_MICROCENTS)."""

DEFAULT_TOOL_RISK: int = 1
"""Default risk points for unmapped tools."""

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
