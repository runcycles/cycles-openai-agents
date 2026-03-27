"""Cycles budget governance for OpenAI Agents SDK."""

from importlib.metadata import version as _metadata_version

from runcycles_openai_agents.guardrail import cycles_budget_guardrail
from runcycles_openai_agents.hooks import CyclesRunHooks
from runcycles_openai_agents.risk_map import ToolRiskConfig, ToolRiskMap

__all__ = [
    "CyclesRunHooks",
    "cycles_budget_guardrail",
    "ToolRiskConfig",
    "ToolRiskMap",
]

__version__ = _metadata_version("runcycles-openai-agents")
