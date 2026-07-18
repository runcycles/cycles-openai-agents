"""Internal reservation state tracking between hook start/end pairs."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from runcycles.models import Unit


@dataclass
class PendingReservation:
    """Tracks an in-flight reservation between a start and end hook."""

    run_id: str
    operation_id: str
    reservation_id: str
    tool_name: str
    agent_name: str
    estimate: int
    unit: Unit
    commit_idempotency_key: str
    started_at: float = field(default_factory=time.monotonic)
    heartbeat_task: asyncio.Task[None] | None = field(default=None, repr=False)

    def cancel_heartbeat(self) -> None:
        """Cancel the heartbeat task if running."""
        if self.heartbeat_task is not None and not self.heartbeat_task.done():
            self.heartbeat_task.cancel()
            self.heartbeat_task = None


class ReservationTracker:
    """Correlates reservations by run and SDK operation identifiers."""

    def __init__(self) -> None:
        self._pending_tools: dict[tuple[str, str], PendingReservation] = {}
        self._pending_llms: dict[str, PendingReservation] = {}
        self._operation_counts: dict[tuple[str, str], int] = {}

    def next_operation_id(self, run_id: str, kind: str) -> str:
        """Return the next process-local operation ID for a run and operation kind."""
        key = (run_id, kind)
        count = self._operation_counts.get(key, 0) + 1
        self._operation_counts[key] = count
        return f"{kind}:{count}"

    def register_tool(self, reservation: PendingReservation) -> PendingReservation | None:
        """Store a tool reservation, returning a duplicate operation if present."""
        key = (reservation.run_id, reservation.operation_id)
        previous = self._pending_tools.get(key)
        self._pending_tools[key] = reservation
        return previous

    def get_tool(
        self,
        run_id: str,
        *,
        operation_id: str | None = None,
        tool_name: str | None = None,
    ) -> PendingReservation | None:
        """Return an exact tool operation or the oldest matching legacy operation."""
        if operation_id is not None:
            return self._pending_tools.get((run_id, operation_id))
        return next(
            (
                pending
                for (pending_run_id, _), pending in self._pending_tools.items()
                if pending_run_id == run_id and (tool_name is None or pending.tool_name == tool_name)
            ),
            None,
        )

    def complete_tool(self, reservation: PendingReservation) -> bool:
        """Remove a tool reservation after a confirmed commit."""
        key = (reservation.run_id, reservation.operation_id)
        if self._pending_tools.get(key) is not reservation:
            return False
        del self._pending_tools[key]
        return True

    def register_llm(self, reservation: PendingReservation) -> PendingReservation | None:
        """Store the current LLM operation for one run and return any duplicate."""
        previous = self._pending_llms.get(reservation.run_id)
        self._pending_llms[reservation.run_id] = reservation
        return previous

    def get_llm(self, run_id: str) -> PendingReservation | None:
        """Return the pending LLM reservation for one run."""
        return self._pending_llms.get(run_id)

    def complete_llm(self, reservation: PendingReservation) -> bool:
        """Remove an LLM reservation after a confirmed commit."""
        if self._pending_llms.get(reservation.run_id) is not reservation:
            return False
        del self._pending_llms[reservation.run_id]
        return True

    def pop_run(self, run_id: str) -> list[PendingReservation]:
        """Atomically detach every pending reservation belonging to one run."""
        tool_keys = [key for key in self._pending_tools if key[0] == run_id]
        result = [self._pending_tools.pop(key) for key in tool_keys]
        pending_llm = self._pending_llms.pop(run_id, None)
        if pending_llm is not None:
            result.append(pending_llm)
        self.forget_run(run_id)
        return result

    def pop_all(self) -> list[PendingReservation]:
        """Atomically detach all reservations across all runs."""
        result = [*self._pending_tools.values(), *self._pending_llms.values()]
        self._pending_tools.clear()
        self._pending_llms.clear()
        self._operation_counts.clear()
        return result

    def forget_run(self, run_id: str) -> None:
        """Discard sequence counters once a run has finalized."""
        count_keys = [key for key in self._operation_counts if key[0] == run_id]
        for key in count_keys:
            del self._operation_counts[key]

    def pending_count(self, run_id: str) -> int:
        """Return the number of pending reservations for one run."""
        tool_count = sum(key[0] == run_id for key in self._pending_tools)
        return tool_count + int(run_id in self._pending_llms)

    @property
    def pending_tool_count(self) -> int:
        return len(self._pending_tools)

    @property
    def has_pending_llm(self) -> bool:
        return bool(self._pending_llms)
