"""Tool estimate mapping for Cycles governance."""

from __future__ import annotations

from dataclasses import dataclass

from runcycles.models import Unit

from runcycles_openai_agents._defaults import DEFAULT_ACTION_KIND_TOOL, DEFAULT_TOOL_ESTIMATE


@dataclass(frozen=True)
class ToolEstimateConfig:
    """Estimate configuration for a single tool's reservation."""

    estimate: int = 0
    action_kind: str = DEFAULT_ACTION_KIND_TOOL
    unit: Unit = Unit.RISK_POINTS


class ToolEstimateMap:
    """Maps tool names to estimate configurations with a configurable default.

    Accepts either ``int`` shorthand (interpreted as the estimate value) or full
    :class:`ToolEstimateConfig` instances::

        ToolEstimateMap({
            "send_email": 50,
            "update_crm": ToolEstimateConfig(estimate=10, action_kind="tool.crm.update"),
            "search_knowledge": 0,
        })
    """

    def __init__(
        self,
        mapping: dict[str, int | ToolEstimateConfig] | None = None,
        default_estimate: int = DEFAULT_TOOL_ESTIMATE,
        default_unit: Unit = Unit.RISK_POINTS,
    ) -> None:
        self._default = ToolEstimateConfig(
            estimate=default_estimate,
            action_kind=DEFAULT_ACTION_KIND_TOOL,
            unit=default_unit,
        )
        self._map: dict[str, ToolEstimateConfig] = {}
        for name, value in (mapping or {}).items():
            if isinstance(value, int):
                self._map[name] = ToolEstimateConfig(
                    estimate=value,
                    action_kind=DEFAULT_ACTION_KIND_TOOL,
                    unit=default_unit,
                )
            else:
                self._map[name] = value

    def get(self, tool_name: str) -> ToolEstimateConfig:
        """Look up estimate config for *tool_name*, falling back to the default."""
        return self._map.get(tool_name, self._default)

    def is_zero_estimate(self, tool_name: str) -> bool:
        """Return ``True`` if *tool_name* has a zero estimate (skip reservation)."""
        return self.get(tool_name).estimate == 0

    @property
    def mapping(self) -> dict[str, ToolEstimateConfig]:
        """Return a copy of the internal mapping."""
        return dict(self._map)

    @property
    def default(self) -> ToolEstimateConfig:
        """Return the default config applied to unmapped tools."""
        return self._default
