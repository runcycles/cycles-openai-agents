"""Tests for _defaults constants."""

from runcycles_openai_agents._defaults import (
    DEFAULT_ACTION_KIND_HANDOFF,
    DEFAULT_ACTION_KIND_LLM,
    DEFAULT_ACTION_KIND_RUN,
    DEFAULT_ACTION_KIND_TOOL,
    DEFAULT_LLM_ESTIMATE,
    DEFAULT_TOOL_RISK,
    DEFAULT_TTL_MS,
)


def test_default_llm_estimate() -> None:
    assert DEFAULT_LLM_ESTIMATE == 500_000


def test_default_tool_risk() -> None:
    assert DEFAULT_TOOL_RISK == 1


def test_default_ttl_ms() -> None:
    assert DEFAULT_TTL_MS == 60_000


def test_action_kinds_are_strings() -> None:
    assert DEFAULT_ACTION_KIND_TOOL == "tool.invoke"
    assert DEFAULT_ACTION_KIND_LLM == "llm.completion"
    assert DEFAULT_ACTION_KIND_HANDOFF == "agent.handoff"
    assert DEFAULT_ACTION_KIND_RUN == "agent.run"
