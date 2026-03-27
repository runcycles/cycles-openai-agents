"""Tool risk mapping for budget governance."""

from __future__ import annotations

from dataclasses import dataclass

from runcycles.models import Unit

from runcycles_openai_agents._defaults import DEFAULT_ACTION_KIND_TOOL, DEFAULT_TOOL_RISK


@dataclass(frozen=True)
class ToolRiskConfig:
    """Risk configuration for a single tool."""

    risk_points: int = 0
    action_kind: str = DEFAULT_ACTION_KIND_TOOL
    unit: Unit = Unit.RISK_POINTS


class ToolRiskMap:
    """Maps tool names to risk configurations with a configurable default.

    Accepts either ``int`` shorthand (interpreted as risk points) or full
    :class:`ToolRiskConfig` instances::

        ToolRiskMap({
            "send_email": 50,
            "update_crm": ToolRiskConfig(risk_points=10, action_kind="tool.crm.update"),
            "search_knowledge": 0,
        })
    """

    def __init__(
        self,
        mapping: dict[str, int | ToolRiskConfig] | None = None,
        default_risk: int = DEFAULT_TOOL_RISK,
        default_unit: Unit = Unit.RISK_POINTS,
    ) -> None:
        self._default = ToolRiskConfig(
            risk_points=default_risk,
            action_kind=DEFAULT_ACTION_KIND_TOOL,
            unit=default_unit,
        )
        self._map: dict[str, ToolRiskConfig] = {}
        for name, value in (mapping or {}).items():
            if isinstance(value, int):
                self._map[name] = ToolRiskConfig(
                    risk_points=value,
                    action_kind=DEFAULT_ACTION_KIND_TOOL,
                    unit=default_unit,
                )
            else:
                self._map[name] = value

    def get(self, tool_name: str) -> ToolRiskConfig:
        """Look up risk config for *tool_name*, falling back to the default."""
        return self._map.get(tool_name, self._default)

    def is_zero_cost(self, tool_name: str) -> bool:
        """Return ``True`` if *tool_name* has zero risk points (skip reservation)."""
        return self.get(tool_name).risk_points == 0

    @property
    def mapping(self) -> dict[str, ToolRiskConfig]:
        """Return a copy of the internal mapping."""
        return dict(self._map)

    @property
    def default(self) -> ToolRiskConfig:
        """Return the default config applied to unmapped tools."""
        return self._default
