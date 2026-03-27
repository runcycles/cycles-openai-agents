"""Shared test fixtures for Cycles OpenAI Agents integration tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from runcycles import AsyncCyclesClient, CyclesResponse


@pytest.fixture()
def mock_client() -> AsyncMock:
    """Return an ``AsyncMock`` of :class:`AsyncCyclesClient`."""
    client = AsyncMock(spec=AsyncCyclesClient)
    return client


@pytest.fixture()
def mock_context() -> MagicMock:
    """Return a mock ``RunContextWrapper``."""
    ctx = MagicMock()
    ctx.usage = MagicMock()
    return ctx


@pytest.fixture()
def mock_agent() -> MagicMock:
    """Return a mock ``Agent`` with a name."""
    agent = MagicMock()
    agent.name = "test-agent"
    return agent


@pytest.fixture()
def mock_tool() -> MagicMock:
    """Return a mock ``Tool`` with a name."""
    tool = MagicMock()
    tool.name = "test-tool"
    return tool


def make_success_response(
    decision: str = "ALLOW",
    reservation_id: str = "res_test123",
    **extra: Any,
) -> CyclesResponse:
    """Build a successful :class:`CyclesResponse` with the given body attributes."""
    body: dict[str, Any] = {"decision": decision, "reservation_id": reservation_id, "affected_scopes": [], **extra}
    return CyclesResponse.success(201, body)


def make_deny_response(reason_code: str = "budget_exceeded") -> CyclesResponse:
    """Build a DENY :class:`CyclesResponse`."""
    body: dict[str, Any] = {"decision": "DENY", "reason_code": reason_code, "affected_scopes": []}
    return CyclesResponse.success(200, body)


def make_commit_response() -> CyclesResponse:
    """Build a successful commit :class:`CyclesResponse`."""
    return CyclesResponse.success(200, {"status": "COMMITTED", "charged": {"unit": "RISK_POINTS", "amount": 1}})


def make_event_response() -> CyclesResponse:
    """Build a successful event :class:`CyclesResponse`."""
    return CyclesResponse.success(201, {"status": "APPLIED", "event_id": "evt_test123"})


def make_transport_error() -> CyclesResponse:
    """Build a transport-error :class:`CyclesResponse`."""
    return CyclesResponse.transport_error(ConnectionError("connection refused"))


def make_http_error(status: int = 500, message: str = "internal error") -> CyclesResponse:
    """Build an HTTP-error :class:`CyclesResponse`."""
    return CyclesResponse.http_error(status, message)
