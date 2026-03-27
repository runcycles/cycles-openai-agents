"""Tests for CyclesRunHooks."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from runcycles import BudgetExceededError, Unit

from runcycles_openai_agents.hooks import CyclesRunHooks
from runcycles_openai_agents.risk_map import ToolRiskMap

from .conftest import (
    make_commit_response,
    make_deny_response,
    make_event_response,
    make_http_error,
    make_success_response,
    make_transport_error,
)


def _hooks(client: AsyncMock, **kwargs: Any) -> CyclesRunHooks[Any]:
    defaults: dict[str, Any] = {"tenant": "test-tenant"}
    defaults.update(kwargs)
    return CyclesRunHooks(client=client, **defaults)


# --- Constructor ---


class TestConstructor:
    def test_explicit_client(self, mock_client: AsyncMock) -> None:
        h = CyclesRunHooks(client=mock_client, tenant="t")
        assert h._client is mock_client

    def test_config_creates_client(self) -> None:
        from runcycles import CyclesConfig

        cfg = CyclesConfig(base_url="http://localhost:7878", api_key="cyc_test_key")
        h = CyclesRunHooks(config=cfg, tenant="t")
        assert h._client is not None

    @patch("runcycles_openai_agents.hooks.CyclesConfig.from_env")
    @patch("runcycles_openai_agents.hooks.AsyncCyclesClient")
    def test_env_fallback(self, mock_cls: MagicMock, mock_from_env: MagicMock) -> None:
        mock_from_env.return_value = MagicMock()
        CyclesRunHooks(tenant="t")
        mock_from_env.assert_called_once()

    def test_risk_map_from_dict(self, mock_client: AsyncMock) -> None:
        h = _hooks(mock_client, tool_risk={"email": 50})
        assert not h._risk_map.is_zero_cost("email")

    def test_risk_map_from_instance(self, mock_client: AsyncMock) -> None:
        rm = ToolRiskMap({"email": 50})
        h = _hooks(mock_client, tool_risk=rm)
        assert h._risk_map is rm


# --- on_tool_start ---


class TestOnToolStart:
    async def test_allow_stores_reservation(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response()
        h = _hooks(mock_client, tool_risk={"test-tool": 10})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

        mock_client.create_reservation.assert_awaited_once()
        mock_context.reject_tool.assert_not_called()
        assert h._tracker.pending_tool_count == 1

    async def test_deny_calls_reject_tool(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_deny_response()
        h = _hooks(mock_client, tool_risk={"test-tool": 10})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

        mock_context.reject_tool.assert_called_once()
        msg = mock_context.reject_tool.call_args[0][0].lower()
        assert "denied" in msg or "budget" in msg

    async def test_transport_error_fail_open(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_transport_error()
        h = _hooks(mock_client, tool_risk={"test-tool": 10}, fail_open=True)

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

        mock_context.reject_tool.assert_not_called()

    async def test_transport_error_fail_closed(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_transport_error()
        h = _hooks(mock_client, tool_risk={"test-tool": 10}, fail_open=False)

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

        mock_context.reject_tool.assert_called_once()

    async def test_zero_cost_tool_skips_reservation(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_tool.name = "free-search"
        h = _hooks(mock_client, tool_risk={"free-search": 0})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

        mock_client.create_reservation.assert_not_awaited()
        mock_context.reject_tool.assert_not_called()

    async def test_http_error_fail_open(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_http_error(500)
        h = _hooks(mock_client, tool_risk={"test-tool": 10}, fail_open=True)

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

        mock_context.reject_tool.assert_not_called()

    async def test_http_error_fail_closed(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_http_error(500)
        h = _hooks(mock_client, tool_risk={"test-tool": 10}, fail_open=False)

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

        mock_context.reject_tool.assert_called_once()

    async def test_dry_run_passes_flag(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response()
        h = _hooks(mock_client, tool_risk={"test-tool": 10}, dry_run=True)

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

        call_args = mock_client.create_reservation.call_args[0][0]
        assert call_args.dry_run is True

    async def test_no_reservation_id_in_response(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        resp = make_success_response(reservation_id=None)  # type: ignore[arg-type]
        resp.body["reservation_id"] = None
        mock_client.create_reservation.return_value = resp
        h = _hooks(mock_client, tool_risk={"test-tool": 10})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

        assert h._tracker.pending_tool_count == 0


# --- on_tool_end ---


class TestOnToolEnd:
    async def test_commit_called_with_correct_reservation(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response(reservation_id="res_abc")
        mock_client.commit_reservation.return_value = make_commit_response()
        h = _hooks(mock_client, tool_risk={"test-tool": 10})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)
        await h.on_tool_end(mock_context, mock_agent, mock_tool, "result")

        mock_client.commit_reservation.assert_awaited_once()
        args = mock_client.commit_reservation.call_args
        assert args[0][0] == "res_abc"

    async def test_no_pending_reservation_skips_commit(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        h = _hooks(mock_client)

        await h.on_tool_end(mock_context, mock_agent, mock_tool, "result")

        mock_client.commit_reservation.assert_not_awaited()

    async def test_commit_failure_logs_error(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response()
        mock_client.commit_reservation.return_value = make_http_error(500)
        h = _hooks(mock_client, tool_risk={"test-tool": 10})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)
        await h.on_tool_end(mock_context, mock_agent, mock_tool, "result")

        mock_client.commit_reservation.assert_awaited_once()


# --- on_llm_start ---


class TestOnLlmStart:
    async def test_allow_stores_llm_reservation(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response(reservation_id="res_llm")
        h = _hooks(mock_client)

        await h.on_llm_start(mock_context, mock_agent, "system prompt", [])

        mock_client.create_reservation.assert_awaited_once()
        assert h._tracker.has_pending_llm is True

    async def test_deny_raises_budget_exceeded(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_deny_response()
        h = _hooks(mock_client)

        with pytest.raises(BudgetExceededError):
            await h.on_llm_start(mock_context, mock_agent, "system prompt", [])

    async def test_transport_error_fail_open(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_transport_error()
        h = _hooks(mock_client, fail_open=True)

        await h.on_llm_start(mock_context, mock_agent, None, [])

        # Should not raise

    async def test_transport_error_fail_closed(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_transport_error()
        h = _hooks(mock_client, fail_open=False)

        with pytest.raises(BudgetExceededError):
            await h.on_llm_start(mock_context, mock_agent, None, [])

    async def test_http_error_fail_open(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_http_error(500)
        h = _hooks(mock_client, fail_open=True)

        await h.on_llm_start(mock_context, mock_agent, None, [])

    async def test_http_error_fail_closed(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_http_error(500)
        h = _hooks(mock_client, fail_open=False)

        with pytest.raises(BudgetExceededError):
            await h.on_llm_start(mock_context, mock_agent, None, [])

    async def test_no_reservation_id_in_response(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        resp = make_success_response()
        resp.body["reservation_id"] = None  # type: ignore[index]
        mock_client.create_reservation.return_value = resp
        h = _hooks(mock_client)

        await h.on_llm_start(mock_context, mock_agent, None, [])

        assert h._tracker.has_pending_llm is False

    async def test_llm_unit_tokens(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response()
        h = _hooks(mock_client, llm_unit=Unit.TOKENS, llm_estimate=1000)

        await h.on_llm_start(mock_context, mock_agent, None, [])

        call_args = mock_client.create_reservation.call_args[0][0]
        assert call_args.estimate.unit == Unit.TOKENS
        assert call_args.estimate.amount == 1000


# --- on_llm_end ---


class TestOnLlmEnd:
    async def test_commit_with_actual_tokens(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response(reservation_id="res_llm")
        mock_client.commit_reservation.return_value = make_commit_response()
        h = _hooks(mock_client)

        await h.on_llm_start(mock_context, mock_agent, None, [])

        mock_response = MagicMock()
        mock_response.usage.input_tokens = 100
        mock_response.usage.output_tokens = 50

        await h.on_llm_end(mock_context, mock_agent, mock_response)

        mock_client.commit_reservation.assert_awaited_once()
        commit_req = mock_client.commit_reservation.call_args[0][1]
        assert commit_req.metrics.tokens_input == 100
        assert commit_req.metrics.tokens_output == 50

    async def test_commit_with_token_unit_uses_actual_count(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response(reservation_id="res_llm")
        mock_client.commit_reservation.return_value = make_commit_response()
        h = _hooks(mock_client, llm_unit=Unit.TOKENS, llm_estimate=1000)

        await h.on_llm_start(mock_context, mock_agent, None, [])

        mock_response = MagicMock()
        mock_response.usage.input_tokens = 200
        mock_response.usage.output_tokens = 100

        await h.on_llm_end(mock_context, mock_agent, mock_response)

        commit_req = mock_client.commit_reservation.call_args[0][1]
        assert commit_req.actual.amount == 300  # 200 + 100

    async def test_no_pending_llm_skips_commit(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        h = _hooks(mock_client)
        mock_response = MagicMock()

        await h.on_llm_end(mock_context, mock_agent, mock_response)

        mock_client.commit_reservation.assert_not_awaited()

    async def test_commit_failure_logs(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response(reservation_id="res_llm")
        mock_client.commit_reservation.return_value = make_http_error(500)
        h = _hooks(mock_client)

        await h.on_llm_start(mock_context, mock_agent, None, [])
        mock_response = MagicMock()
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        await h.on_llm_end(mock_context, mock_agent, mock_response)

        mock_client.commit_reservation.assert_awaited_once()


# --- on_handoff ---


class TestOnHandoff:
    async def test_handoff_creates_event(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_event.return_value = make_event_response()
        to_agent = MagicMock()
        to_agent.name = "agent-b"
        h = _hooks(mock_client)

        await h.on_handoff(mock_context, mock_agent, to_agent)

        mock_client.create_event.assert_awaited_once()
        event_req = mock_client.create_event.call_args[0][0]
        assert event_req.action.kind == "agent.handoff"
        assert event_req.action.name == "agent-b"

    async def test_handoff_updates_agent_name(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_event.return_value = make_event_response()
        to_agent = MagicMock()
        to_agent.name = "agent-b"
        h = _hooks(mock_client)

        await h.on_handoff(mock_context, mock_agent, to_agent)

        assert h._current_agent_name == "agent-b"

    async def test_handoff_event_failure_does_not_raise(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_event.return_value = make_http_error(500)
        to_agent = MagicMock()
        to_agent.name = "agent-b"
        h = _hooks(mock_client)

        await h.on_handoff(mock_context, mock_agent, to_agent)


# --- on_agent_start / on_agent_end ---


class TestAgentLifecycle:
    async def test_agent_start_updates_name(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        h = _hooks(mock_client)
        mock_agent.name = "new-agent"

        await h.on_agent_start(mock_context, mock_agent)

        assert h._current_agent_name == "new-agent"

    async def test_agent_end_does_not_raise(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        h = _hooks(mock_client)

        await h.on_agent_end(mock_context, mock_agent, "output")


# --- Subject construction ---


class TestSubject:
    def test_subject_fields(self, mock_client: AsyncMock) -> None:
        h = CyclesRunHooks(
            client=mock_client,
            tenant="t",
            workspace="w",
            app="a",
            workflow="wf",
            agent="ag",
            toolset="ts",
        )
        s = h._subject()
        assert s.tenant == "t"
        assert s.workspace == "w"
        assert s.app == "a"
        assert s.workflow == "wf"
        assert s.agent == "ag"
        assert s.toolset == "ts"

    def test_subject_override(self, mock_client: AsyncMock) -> None:
        h = _hooks(mock_client)
        s = h._subject(agent_name="override-agent", toolset_name="override-tool")
        assert s.agent == "override-agent"
        assert s.toolset == "override-tool"
