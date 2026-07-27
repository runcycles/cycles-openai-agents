"""Internal reservation state tracking between hook start/end pairs."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from runcycles.models import Action, Subject, Unit


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
    # Reservation-time subject/action, retained so an expired commit can be
    # recovered through the SDK retry engine's POST /v1/events fallback.
    subject: Subject | None = field(default=None, repr=False)
    action: Action | None = field(default=None, repr=False)

    def cancel_heartbeat(self) -> None:
        """Cancel the heartbeat task if running."""
        if self.heartbeat_task is not None and not self.heartbeat_task.done():
            self.heartbeat_task.cancel()
            self.heartbeat_task = None


class ReservationTracker:
    """Correlates reservations by run and SDK operation identifiers."""

    def __init__(self) -> None:
        self._pending_tools: dict[tuple[str, str], PendingReservation] = {}
        self._pending_llms: dict[tuple[str, str], PendingReservation] = {}
        self._settling_tools: dict[tuple[str, str], PendingReservation] = {}
        self._settling_llms: dict[tuple[str, str], PendingReservation] = {}
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
        if previous is None:
            previous = self._settling_tools.pop(key, None)
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
        """Remove a tool reservation after commit settlement."""
        key = (reservation.run_id, reservation.operation_id)
        for reservations in (self._pending_tools, self._settling_tools):
            if reservations.get(key) is reservation:
                del reservations[key]
                return True
        return False

    def begin_tool_commit(self, reservation: PendingReservation) -> bool:
        """Move a tool operation out of active lookup while its commit settles."""
        key = (reservation.run_id, reservation.operation_id)
        if self._pending_tools.get(key) is not reservation:
            return False
        del self._pending_tools[key]
        self._settling_tools[key] = reservation
        return True

    def register_llm(self, reservation: PendingReservation) -> PendingReservation | None:
        """Store an LLM reservation, returning an exact duplicate if present."""
        key = (reservation.run_id, reservation.operation_id)
        previous = self._pending_llms.get(key)
        if previous is None:
            previous = self._settling_llms.pop(key, None)
        self._pending_llms[key] = reservation
        return previous

    def get_llm(self, run_id: str, *, operation_id: str | None = None) -> PendingReservation | None:
        """Return an exact LLM operation or the oldest operation for one run."""
        if operation_id is not None:
            return self._pending_llms.get((run_id, operation_id))
        return next(
            (pending for (pending_run_id, _), pending in self._pending_llms.items() if pending_run_id == run_id),
            None,
        )

    def complete_llm(self, reservation: PendingReservation) -> bool:
        """Remove an LLM reservation after commit settlement."""
        key = (reservation.run_id, reservation.operation_id)
        for reservations in (self._pending_llms, self._settling_llms):
            if reservations.get(key) is reservation:
                del reservations[key]
                return True
        return False

    def begin_llm_commit(self, reservation: PendingReservation) -> bool:
        """Move an LLM operation out of active lookup while its commit settles."""
        key = (reservation.run_id, reservation.operation_id)
        if self._pending_llms.get(key) is not reservation:
            return False
        del self._pending_llms[key]
        self._settling_llms[key] = reservation
        return True

    def pop_run(self, run_id: str) -> list[PendingReservation]:
        """Atomically detach every pending reservation belonging to one run."""
        tool_keys = [key for key in self._pending_tools if key[0] == run_id]
        result = [self._pending_tools.pop(key) for key in tool_keys]
        llm_keys = [key for key in self._pending_llms if key[0] == run_id]
        result.extend(self._pending_llms.pop(key) for key in llm_keys)
        settling_tool_keys = [key for key in self._settling_tools if key[0] == run_id]
        result.extend(self._settling_tools.pop(key) for key in settling_tool_keys)
        settling_llm_keys = [key for key in self._settling_llms if key[0] == run_id]
        result.extend(self._settling_llms.pop(key) for key in settling_llm_keys)
        self.forget_run(run_id)
        return result

    def pop_all(self) -> list[PendingReservation]:
        """Atomically detach all reservations across all runs."""
        result = [
            *self._pending_tools.values(),
            *self._pending_llms.values(),
            *self._settling_tools.values(),
            *self._settling_llms.values(),
        ]
        self._pending_tools.clear()
        self._pending_llms.clear()
        self._settling_tools.clear()
        self._settling_llms.clear()
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
        llm_count = sum(key[0] == run_id for key in self._pending_llms)
        settling_tool_count = sum(key[0] == run_id for key in self._settling_tools)
        settling_llm_count = sum(key[0] == run_id for key in self._settling_llms)
        return tool_count + llm_count + settling_tool_count + settling_llm_count

    @property
    def pending_run_ids(self) -> tuple[str, ...]:
        """Return the run IDs that currently own pending reservations."""
        run_ids = {run_id for run_id, _ in self._pending_tools}
        run_ids.update(run_id for run_id, _ in self._pending_llms)
        run_ids.update(run_id for run_id, _ in self._settling_tools)
        run_ids.update(run_id for run_id, _ in self._settling_llms)
        return tuple(sorted(run_ids))

    @property
    def pending_tool_count(self) -> int:
        return len(self._pending_tools) + len(self._settling_tools)

    @property
    def has_pending_llm(self) -> bool:
        return bool(self._pending_llms or self._settling_llms)
