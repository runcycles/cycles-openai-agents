"""Internal reservation state tracking between hook start/end pairs."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from runcycles.models import Unit


@dataclass
class PendingReservation:
    """Tracks an in-flight reservation between a start and end hook."""

    reservation_id: str
    tool_name: str
    agent_name: str
    estimate: int
    unit: Unit
    started_at: float = field(default_factory=time.monotonic)


class ReservationTracker:
    """Correlates start/end hooks for tools and LLM calls.

    Tool reservations are keyed by tool name.  If the same tool name appears
    concurrently, a numeric suffix is appended (``tool_name#2``, etc.).
    """

    def __init__(self) -> None:
        self._pending_tools: dict[str, PendingReservation] = {}
        self._pending_llm: PendingReservation | None = None
        self._tool_counts: dict[str, int] = {}

    def _make_key(self, tool_name: str) -> str:
        count = self._tool_counts.get(tool_name, 0) + 1
        self._tool_counts[tool_name] = count
        if count == 1:
            return tool_name
        return f"{tool_name}#{count}"

    def register_tool(self, tool_name: str, reservation: PendingReservation) -> str:
        """Store a tool reservation and return the key used."""
        key = self._make_key(tool_name)
        self._pending_tools[key] = reservation
        return key

    def pop_tool(self, tool_name: str) -> PendingReservation | None:
        """Remove and return the reservation for *tool_name*.

        Tries the plain name first, then any suffixed variants.
        """
        if tool_name in self._pending_tools:
            res = self._pending_tools.pop(tool_name)
            self._decrement_count(tool_name)
            return res
        # Try suffixed keys (oldest first)
        for key in sorted(self._pending_tools):
            if key == tool_name or key.startswith(f"{tool_name}#"):
                res = self._pending_tools.pop(key)
                self._decrement_count(tool_name)
                return res
        return None

    def _decrement_count(self, tool_name: str) -> None:
        count = self._tool_counts.get(tool_name, 0)
        if count <= 1:
            self._tool_counts.pop(tool_name, None)
        else:
            self._tool_counts[tool_name] = count - 1

    def register_llm(self, reservation: PendingReservation) -> None:
        """Store a pending LLM reservation."""
        self._pending_llm = reservation

    def pop_llm(self) -> PendingReservation | None:
        """Remove and return the pending LLM reservation."""
        res = self._pending_llm
        self._pending_llm = None
        return res

    @property
    def pending_tool_count(self) -> int:
        return len(self._pending_tools)

    @property
    def has_pending_llm(self) -> bool:
        return self._pending_llm is not None
