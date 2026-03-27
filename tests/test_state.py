"""Tests for ReservationTracker and PendingReservation."""

from runcycles.models import Unit

from runcycles_openai_agents._state import PendingReservation, ReservationTracker


def _make_pending(tool: str = "tool_a", reservation_id: str = "res_1") -> PendingReservation:
    return PendingReservation(
        reservation_id=reservation_id,
        tool_name=tool,
        agent_name="agent",
        estimate=10,
        unit=Unit.RISK_POINTS,
    )


class TestReservationTracker:
    def test_register_and_pop_tool(self) -> None:
        t = ReservationTracker()
        p = _make_pending("search")
        t.register_tool("search", p)
        assert t.pending_tool_count == 1
        result = t.pop_tool("search")
        assert result is p
        assert t.pending_tool_count == 0

    def test_pop_missing_tool_returns_none(self) -> None:
        t = ReservationTracker()
        assert t.pop_tool("nonexistent") is None

    def test_concurrent_tools_different_names(self) -> None:
        t = ReservationTracker()
        p1 = _make_pending("search", "res_1")
        p2 = _make_pending("email", "res_2")
        t.register_tool("search", p1)
        t.register_tool("email", p2)
        assert t.pending_tool_count == 2
        assert t.pop_tool("email") is p2
        assert t.pop_tool("search") is p1
        assert t.pending_tool_count == 0

    def test_duplicate_tool_names_use_suffix(self) -> None:
        t = ReservationTracker()
        p1 = _make_pending("search", "res_1")
        p2 = _make_pending("search", "res_2")
        k1 = t.register_tool("search", p1)
        k2 = t.register_tool("search", p2)
        assert k1 == "search"
        assert k2 == "search#2"
        assert t.pending_tool_count == 2

    def test_pop_duplicate_tool_returns_oldest(self) -> None:
        t = ReservationTracker()
        p1 = _make_pending("search", "res_1")
        p2 = _make_pending("search", "res_2")
        t.register_tool("search", p1)
        t.register_tool("search", p2)
        first = t.pop_tool("search")
        assert first is p1
        second = t.pop_tool("search")
        assert second is p2
        assert t.pop_tool("search") is None

    def test_register_and_pop_llm(self) -> None:
        t = ReservationTracker()
        p = _make_pending("__llm__", "res_llm")
        assert t.has_pending_llm is False
        t.register_llm(p)
        assert t.has_pending_llm is True
        result = t.pop_llm()
        assert result is p
        assert t.has_pending_llm is False

    def test_pop_llm_when_empty(self) -> None:
        t = ReservationTracker()
        assert t.pop_llm() is None

    def test_llm_overwrite(self) -> None:
        t = ReservationTracker()
        p1 = _make_pending("__llm__", "res_1")
        p2 = _make_pending("__llm__", "res_2")
        t.register_llm(p1)
        t.register_llm(p2)
        result = t.pop_llm()
        assert result is p2

    def test_pending_reservation_has_started_at(self) -> None:
        p = _make_pending()
        assert p.started_at > 0
