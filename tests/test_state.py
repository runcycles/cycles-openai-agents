"""Tests for ReservationTracker and PendingReservation."""

from runcycles.models import Unit

from runcycles_openai_agents._state import PendingReservation, ReservationTracker


def _make_pending(
    tool: str = "tool_a",
    reservation_id: str = "res_1",
    *,
    run_id: str = "run_1",
    operation_id: str = "op_1",
) -> PendingReservation:
    return PendingReservation(
        run_id=run_id,
        operation_id=operation_id,
        reservation_id=reservation_id,
        tool_name=tool,
        agent_name="agent",
        estimate=10,
        unit=Unit.RISK_POINTS,
        commit_idempotency_key=f"commit_{run_id}_{operation_id}",
    )


class TestReservationTracker:
    def test_register_and_pop_tool(self) -> None:
        t = ReservationTracker()
        p = _make_pending("search")
        assert t.register_tool(p) is None
        assert t.pending_tool_count == 1
        assert t.get_tool("run_1", operation_id="op_1") is p
        assert t.complete_tool(p) is True
        assert t.pending_tool_count == 0

    def test_get_missing_tool_returns_none(self) -> None:
        t = ReservationTracker()
        assert t.get_tool("run_1", operation_id="nonexistent") is None

    def test_concurrent_tools_are_scoped_by_run_and_operation(self) -> None:
        t = ReservationTracker()
        p1 = _make_pending("search", "res_1", run_id="run_a", operation_id="call_1")
        p2 = _make_pending("search", "res_2", run_id="run_b", operation_id="call_1")
        t.register_tool(p1)
        t.register_tool(p2)
        assert t.pending_tool_count == 2
        assert t.get_tool("run_a", operation_id="call_1") is p1
        assert t.get_tool("run_b", operation_id="call_1") is p2
        assert t.complete_tool(p2) is True
        assert t.complete_tool(p1) is True
        assert t.pending_tool_count == 0

    def test_duplicate_tool_operation_returns_previous(self) -> None:
        t = ReservationTracker()
        p1 = _make_pending("search", "res_1", operation_id="call_1")
        p2 = _make_pending("search", "res_2", operation_id="call_1")
        assert t.register_tool(p1) is None
        assert t.register_tool(p2) is p1
        assert t.pending_tool_count == 1
        assert t.get_tool("run_1", operation_id="call_1") is p2

    def test_legacy_tool_lookup_returns_oldest_matching_name(self) -> None:
        t = ReservationTracker()
        p1 = _make_pending("search", "res_1", operation_id="legacy_1")
        p2 = _make_pending("search", "res_2", operation_id="legacy_2")
        t.register_tool(p1)
        t.register_tool(p2)
        assert t.get_tool("run_1", tool_name="search") is p1

    def test_complete_tool_rejects_stale_instance(self) -> None:
        t = ReservationTracker()
        p1 = _make_pending("search", "res_1")
        p2 = _make_pending("search", "res_2")
        t.register_tool(p1)
        t.register_tool(p2)
        assert t.complete_tool(p1) is False

    def test_register_and_pop_llm(self) -> None:
        t = ReservationTracker()
        p = _make_pending("__llm__", "res_llm")
        assert t.has_pending_llm is False
        prev = t.register_llm(p)
        assert prev is None
        assert t.has_pending_llm is True
        assert t.get_llm("run_1") is p
        assert t.complete_llm(p) is True
        assert t.has_pending_llm is False

    def test_get_llm_when_empty(self) -> None:
        t = ReservationTracker()
        assert t.get_llm("run_1") is None

    def test_distinct_llm_operations_do_not_overwrite(self) -> None:
        t = ReservationTracker()
        p1 = _make_pending("__llm__", "res_1", operation_id="llm_1")
        p2 = _make_pending("__llm__", "res_2", operation_id="llm_2")
        prev1 = t.register_llm(p1)
        assert prev1 is None
        prev2 = t.register_llm(p2)
        assert prev2 is None
        assert t.get_llm("run_1") is p1
        assert t.get_llm("run_1", operation_id="llm_2") is p2
        assert t.pending_count("run_1") == 2

    def test_exact_duplicate_llm_operation_returns_previous(self) -> None:
        t = ReservationTracker()
        p1 = _make_pending("__llm__", "res_1")
        p2 = _make_pending("__llm__", "res_2")
        assert t.register_llm(p1) is None
        assert t.register_llm(p2) is p1

    def test_llms_for_concurrent_runs_do_not_overwrite(self) -> None:
        t = ReservationTracker()
        p1 = _make_pending("__llm__", "res_1", run_id="run_a")
        p2 = _make_pending("__llm__", "res_2", run_id="run_b")
        assert t.register_llm(p1) is None
        assert t.register_llm(p2) is None
        assert t.get_llm("run_a") is p1
        assert t.get_llm("run_b") is p2
        assert t.pending_run_ids == ("run_a", "run_b")

    def test_pending_reservation_has_started_at(self) -> None:
        p = _make_pending()
        assert p.started_at > 0

    def test_pop_run_only_detaches_that_runs_reservations(self) -> None:
        t = ReservationTracker()
        p1 = _make_pending("a", "res_1", run_id="run_a")
        p2 = _make_pending("b", "res_2", run_id="run_b")
        t.register_tool(p1)
        t.register_tool(p2)
        result = t.pop_run("run_a")
        assert result == [p1]
        assert t.pending_tool_count == 1
        assert t.pending_count("run_a") == 0
        assert t.pending_count("run_b") == 1

    def test_pop_all_empty(self) -> None:
        t = ReservationTracker()
        assert t.pop_all() == []

    def test_next_operation_id_is_scoped_by_run_and_kind(self) -> None:
        t = ReservationTracker()
        assert t.next_operation_id("run_a", "llm") == "llm:1"
        assert t.next_operation_id("run_a", "llm") == "llm:2"
        assert t.next_operation_id("run_b", "llm") == "llm:1"
        assert t.next_operation_id("run_a", "tool") == "tool:1"

    def test_cancel_heartbeat_noop_when_none(self) -> None:
        p = _make_pending()
        assert p.heartbeat_task is None
        p.cancel_heartbeat()  # should not raise

    def test_cancel_heartbeat_cancels_task(self) -> None:
        import asyncio
        from unittest.mock import MagicMock

        p = _make_pending()
        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = False
        p.heartbeat_task = mock_task
        p.cancel_heartbeat()
        mock_task.cancel.assert_called_once()
        assert p.heartbeat_task is None

    def test_cancel_heartbeat_skips_done_task(self) -> None:
        import asyncio
        from unittest.mock import MagicMock

        p = _make_pending()
        mock_task = MagicMock(spec=asyncio.Task)
        mock_task.done.return_value = True
        p.heartbeat_task = mock_task
        p.cancel_heartbeat()
        mock_task.cancel.assert_not_called()
