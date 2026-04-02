"""Tests for ToolEstimateMap and ToolEstimateConfig."""

from runcycles.models import Unit

from runcycles_openai_agents.tool_estimate_map import ToolEstimateConfig, ToolEstimateMap


class TestToolEstimateConfig:
    def test_defaults(self) -> None:
        cfg = ToolEstimateConfig()
        assert cfg.estimate == 0
        assert cfg.action_kind == "tool.invoke"
        assert cfg.unit == Unit.RISK_POINTS

    def test_custom_values(self) -> None:
        cfg = ToolEstimateConfig(estimate=50, action_kind="tool.email", unit=Unit.CREDITS)
        assert cfg.estimate == 50
        assert cfg.action_kind == "tool.email"
        assert cfg.unit == Unit.CREDITS

    def test_frozen(self) -> None:
        cfg = ToolEstimateConfig()
        try:
            cfg.estimate = 99  # type: ignore[misc]
            raise AssertionError("should have raised")
        except AttributeError:
            pass


class TestToolEstimateMap:
    def test_lookup_existing_int(self) -> None:
        m = ToolEstimateMap({"send_email": 50})
        cfg = m.get("send_email")
        assert cfg.estimate == 50
        assert cfg.unit == Unit.RISK_POINTS

    def test_lookup_existing_config(self) -> None:
        custom = ToolEstimateConfig(estimate=10, action_kind="tool.crm")
        m = ToolEstimateMap({"update_crm": custom})
        assert m.get("update_crm") is custom

    def test_lookup_unknown_returns_default(self) -> None:
        m = ToolEstimateMap({"send_email": 50}, default_estimate=5)
        cfg = m.get("unknown_tool")
        assert cfg.estimate == 5

    def test_empty_map_uses_defaults(self) -> None:
        m = ToolEstimateMap()
        cfg = m.get("any_tool")
        assert cfg.estimate == 1  # DEFAULT_TOOL_ESTIMATE

    def test_zero_estimate_detection(self) -> None:
        m = ToolEstimateMap({"search": 0, "email": 50})
        assert m.is_zero_estimate("search") is True
        assert m.is_zero_estimate("email") is False

    def test_zero_estimate_unknown_with_nonzero_default(self) -> None:
        m = ToolEstimateMap(default_estimate=5)
        assert m.is_zero_estimate("anything") is False

    def test_zero_estimate_unknown_with_zero_default(self) -> None:
        m = ToolEstimateMap(default_estimate=0)
        assert m.is_zero_estimate("anything") is True

    def test_custom_default_unit(self) -> None:
        m = ToolEstimateMap({"tool_a": 10}, default_unit=Unit.CREDITS)
        assert m.get("tool_a").unit == Unit.CREDITS
        assert m.default.unit == Unit.CREDITS

    def test_mapping_property(self) -> None:
        m = ToolEstimateMap({"a": 1, "b": 2})
        mapping = m.mapping
        assert len(mapping) == 2
        assert "a" in mapping

    def test_default_property(self) -> None:
        m = ToolEstimateMap(default_estimate=7)
        assert m.default.estimate == 7

    def test_none_mapping(self) -> None:
        m = ToolEstimateMap(mapping=None)
        assert m.mapping == {}
