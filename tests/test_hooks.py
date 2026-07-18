"""Tests for CyclesRunHooks."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agents import Agent, Model, ModelResponse, Usage, function_tool
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
from runcycles import BudgetExceededError, Unit

from runcycles_openai_agents.hooks import CyclesRunHooks
from runcycles_openai_agents.tool_estimate_map import ToolEstimateMap

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


def _final_model_response(text: str = "ok") -> ModelResponse:
    output_text = ResponseOutputText(
        type="output_text",
        text=text,
        annotations=[],
        logprobs=[],
    )
    message = ResponseOutputMessage(
        type="message",
        id="msg_test",
        role="assistant",
        status="completed",
        content=[output_text],
    )
    return ModelResponse(
        output=[message],
        usage=Usage(requests=1, input_tokens=2, output_tokens=1, total_tokens=3),
        response_id="resp_test",
    )


class _FailingModel(Model):
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        raise self.error

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class _BlockingModel(Model):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finish = asyncio.Event()

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.started.set()
        await self.finish.wait()
        return _final_model_response()

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class _ToolCallModel(Model):
    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        tool_call = ResponseFunctionToolCall(
            type="function_call",
            id="fc_test",
            call_id="call_explode",
            name="explode",
            arguments="{}",
        )
        return ModelResponse(
            output=[tool_call],
            usage=Usage(requests=1, input_tokens=2, output_tokens=1, total_tokens=3),
            response_id="resp_tool",
        )

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


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

    def test_estimate_map_from_dict(self, mock_client: AsyncMock) -> None:
        h = _hooks(mock_client, tool_estimates={"email": 50})
        assert not h._tool_estimate_map.is_zero_estimate("email")

    def test_estimate_map_from_instance(self, mock_client: AsyncMock) -> None:
        rm = ToolEstimateMap({"email": 50})
        h = _hooks(mock_client, tool_estimates=rm)
        assert h._tool_estimate_map is rm

    def test_fail_closed_is_default(self, mock_client: AsyncMock) -> None:
        h = _hooks(mock_client)
        assert h._fail_open is False

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"heartbeat_max_age_ms": 0}, "heartbeat_max_age_ms"),
            ({"heartbeat_max_extensions": 0}, "heartbeat_max_extensions"),
        ],
    )
    def test_invalid_heartbeat_caps_raise(
        self,
        mock_client: AsyncMock,
        kwargs: dict[str, int],
        message: str,
    ) -> None:
        with pytest.raises(ValueError, match=message):
            _hooks(mock_client, **kwargs)


# --- on_tool_start ---


class TestOnToolStart:
    async def test_allow_stores_reservation(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response()
        h = _hooks(mock_client, tool_estimates={"test-tool": 10})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

        mock_client.create_reservation.assert_awaited_once()
        assert h._tracker.pending_tool_count == 1

    async def test_deny_raises_budget_exceeded(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_deny_response()
        h = _hooks(mock_client, tool_estimates={"test-tool": 10})

        with pytest.raises(BudgetExceededError):
            await h.on_tool_start(mock_context, mock_agent, mock_tool)

    async def test_transport_error_fail_open(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_transport_error()
        h = _hooks(mock_client, tool_estimates={"test-tool": 10}, fail_open=True)

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

    async def test_transport_error_fail_closed(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_transport_error()
        h = _hooks(mock_client, tool_estimates={"test-tool": 10}, fail_open=False)

        with pytest.raises(BudgetExceededError):
            await h.on_tool_start(mock_context, mock_agent, mock_tool)

    async def test_zero_estimate_tool_skips_reservation(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_tool.name = "free-search"
        h = _hooks(mock_client, tool_estimates={"free-search": 0})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

        mock_client.create_reservation.assert_not_awaited()

    async def test_http_error_fail_open(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_http_error(500)
        h = _hooks(mock_client, tool_estimates={"test-tool": 10}, fail_open=True)

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

    async def test_http_error_fail_closed(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_http_error(500)
        h = _hooks(mock_client, tool_estimates={"test-tool": 10}, fail_open=False)

        with pytest.raises(BudgetExceededError):
            await h.on_tool_start(mock_context, mock_agent, mock_tool)

    async def test_dry_run_passes_flag(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response()
        h = _hooks(mock_client, tool_estimates={"test-tool": 10}, dry_run=True)

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

        call_args = mock_client.create_reservation.call_args[0][0]
        assert call_args.dry_run is True

    async def test_no_reservation_id_in_response(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        resp = make_success_response(reservation_id=None)  # type: ignore[arg-type]
        resp.body["reservation_id"] = None
        mock_client.create_reservation.return_value = resp
        h = _hooks(mock_client, tool_estimates={"test-tool": 10})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)

        assert h._tracker.pending_tool_count == 0


# --- on_tool_end ---


class TestOnToolEnd:
    async def test_commit_called_with_correct_reservation(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response(reservation_id="res_abc")
        mock_client.commit_reservation.return_value = make_commit_response()
        h = _hooks(mock_client, tool_estimates={"test-tool": 10})

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
        h = _hooks(mock_client, tool_estimates={"test-tool": 10})

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

    async def test_handoff_does_not_mutate_shared_agent_state(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_event.return_value = make_event_response()
        to_agent = MagicMock()
        to_agent.name = "agent-b"
        h = _hooks(mock_client)

        await h.on_handoff(mock_context, mock_agent, to_agent)

        assert h._subject().agent is None

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
    async def test_agent_start_does_not_mutate_shared_agent_state(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        h = _hooks(mock_client)
        mock_agent.name = "new-agent"

        await h.on_agent_start(mock_context, mock_agent)

        assert h._subject().agent is None

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


# --- Heartbeat ---


class TestHeartbeat:
    async def test_tool_reservation_gets_heartbeat(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response()
        mock_client.commit_reservation.return_value = make_commit_response()
        h = _hooks(mock_client, tool_estimates={"test-tool": 10})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)
        pending = h._tracker.get_tool(h._run_id(mock_context), tool_name="test-tool")
        assert pending is not None
        assert pending.heartbeat_task is not None
        pending.cancel_heartbeat()

    async def test_llm_reservation_gets_heartbeat(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response(reservation_id="res_llm")
        h = _hooks(mock_client)

        await h.on_llm_start(mock_context, mock_agent, None, [])
        pending = h._tracker.get_llm(h._run_id(mock_context))
        assert pending is not None
        assert pending.heartbeat_task is not None
        pending.cancel_heartbeat()

    async def test_tool_end_cancels_heartbeat(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response()
        mock_client.commit_reservation.return_value = make_commit_response()
        h = _hooks(mock_client, tool_estimates={"test-tool": 10})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)
        # Verify heartbeat is running
        pending_key = list(h._tracker._pending_tools.keys())[0]
        task = h._tracker._pending_tools[pending_key].heartbeat_task
        assert task is not None

        await h.on_tool_end(mock_context, mock_agent, mock_tool, "result")
        await asyncio.sleep(0)  # let event loop process cancellation
        assert task.done()

    async def test_llm_end_cancels_heartbeat(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response(reservation_id="res_llm")
        mock_client.commit_reservation.return_value = make_commit_response()
        h = _hooks(mock_client)

        await h.on_llm_start(mock_context, mock_agent, None, [])
        pending = h._tracker.get_llm(h._run_id(mock_context))
        assert pending is not None
        task = pending.heartbeat_task
        assert task is not None

        mock_response = MagicMock()
        mock_response.usage.input_tokens = 10
        mock_response.usage.output_tokens = 5
        await h.on_llm_end(mock_context, mock_agent, mock_response)
        await asyncio.sleep(0)  # let event loop process cancellation
        assert task.done()

    def test_no_heartbeat_when_ttl_zero(self, mock_client: AsyncMock) -> None:
        h = _hooks(mock_client, ttl_ms=0)
        task = h._start_heartbeat("res_test", run_id="run", operation_id="op")
        assert task is None

    def test_no_heartbeat_when_ttl_too_small(self, mock_client: AsyncMock) -> None:
        h = _hooks(mock_client, ttl_ms=1999)
        task = h._start_heartbeat("res_test", run_id="run", operation_id="op")
        assert task is None

    async def test_heartbeat_fires_and_extends(self, mock_client: AsyncMock) -> None:
        mock_client.extend_reservation.return_value = make_success_response()
        h = _hooks(mock_client, ttl_ms=2000)

        # Patch sleep to return immediately so the heartbeat fires
        call_count = 0
        original_sleep = asyncio.sleep

        async def fast_sleep(seconds: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError  # stop after one real iteration
            await original_sleep(0)

        with patch("runcycles_openai_agents.hooks.asyncio.sleep", side_effect=fast_sleep):
            task = h._start_heartbeat("res_hb", run_id="run", operation_id="op")
            assert task is not None
            try:
                await task
            except asyncio.CancelledError:
                pass

        mock_client.extend_reservation.assert_awaited_once()
        req = mock_client.extend_reservation.call_args[0][1]
        assert req.extend_by_ms == 2000

    async def test_heartbeat_handles_extend_failure(self, mock_client: AsyncMock) -> None:
        mock_client.extend_reservation.return_value = make_http_error(500)
        h = _hooks(mock_client, ttl_ms=2000)

        call_count = 0
        original_sleep = asyncio.sleep

        async def fast_sleep(seconds: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError
            await original_sleep(0)

        with patch("runcycles_openai_agents.hooks.asyncio.sleep", side_effect=fast_sleep):
            task = h._start_heartbeat("res_hb", run_id="run", operation_id="op")
            assert task is not None
            try:
                await task
            except asyncio.CancelledError:
                pass

        mock_client.extend_reservation.assert_awaited_once()

    async def test_heartbeat_handles_extend_exception(self, mock_client: AsyncMock) -> None:
        mock_client.extend_reservation.side_effect = ConnectionError("down")
        h = _hooks(mock_client, ttl_ms=2000)

        call_count = 0
        original_sleep = asyncio.sleep

        async def fast_sleep(seconds: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError
            await original_sleep(0)

        with patch("runcycles_openai_agents.hooks.asyncio.sleep", side_effect=fast_sleep):
            task = h._start_heartbeat("res_hb", run_id="run", operation_id="op")
            assert task is not None
            try:
                await task
            except asyncio.CancelledError:
                pass

        mock_client.extend_reservation.assert_awaited_once()

    async def test_heartbeat_stops_after_extension_cap(self, mock_client: AsyncMock) -> None:
        mock_client.extend_reservation.return_value = make_success_response()
        h = _hooks(mock_client, ttl_ms=2000, heartbeat_max_extensions=1)
        original_sleep = asyncio.sleep

        async def fast_sleep(seconds: float) -> None:
            await original_sleep(0)

        with patch("runcycles_openai_agents.hooks.asyncio.sleep", side_effect=fast_sleep):
            task = h._start_heartbeat("res_hb", run_id="run", operation_id="op")
            assert task is not None
            await task

        mock_client.extend_reservation.assert_awaited_once()

    async def test_heartbeat_stops_at_max_age(self, mock_client: AsyncMock) -> None:
        h = _hooks(mock_client, ttl_ms=2000, heartbeat_max_age_ms=1000)

        with (
            patch("runcycles_openai_agents.hooks.monotonic", side_effect=[0.0, 0.0, 1.0]),
            patch("runcycles_openai_agents.hooks.asyncio.sleep", new_callable=AsyncMock),
        ):
            task = h._start_heartbeat("res_hb", run_id="run", operation_id="op")
            assert task is not None
            await task

        mock_client.extend_reservation.assert_not_awaited()


# --- real Runner failure and concurrency paths ---


class TestRunnerLifecycleSafety:
    async def test_llm_exception_releases_in_flight_reservation(self, mock_client: AsyncMock) -> None:
        mock_client.create_reservation.return_value = make_success_response(reservation_id="res_llm_error")
        mock_client.release_reservation.return_value = make_commit_response()
        h = _hooks(mock_client, ttl_ms=2000)
        agent = Agent(name="llm-error", model=_FailingModel(RuntimeError("model exploded")))

        with pytest.raises(RuntimeError, match="model exploded"):
            await h.run(agent, "hello", run_id="run-llm-error")

        mock_client.release_reservation.assert_awaited_once()
        reservation_id, request = mock_client.release_reservation.call_args.args
        assert reservation_id == "res_llm_error"
        assert request.reason == "agent_run_failed:RuntimeError"

    async def test_tool_exception_releases_in_flight_reservation(self, mock_client: AsyncMock) -> None:
        mock_client.create_reservation.side_effect = [
            make_success_response(reservation_id="res_llm_before_tool"),
            make_success_response(reservation_id="res_tool_error"),
        ]
        mock_client.commit_reservation.return_value = make_commit_response()
        mock_client.release_reservation.return_value = make_commit_response()
        h = _hooks(mock_client, ttl_ms=2000, tool_estimates={"explode": 5})

        async def explode() -> str:
            """Raise a real tool execution failure."""
            raise RuntimeError("tool exploded")

        tool = function_tool(explode, failure_error_function=None)
        agent = Agent(name="tool-error", model=_ToolCallModel(), tools=[tool])

        with pytest.raises(Exception, match="tool exploded"):
            await h.run(agent, "call the tool", run_id="run-tool-error")

        mock_client.release_reservation.assert_awaited_once()
        reservation_id, request = mock_client.release_reservation.call_args.args
        assert reservation_id == "res_tool_error"
        assert request.reason.startswith("agent_run_failed:")

    async def test_cancelled_run_releases_before_propagating(self, mock_client: AsyncMock) -> None:
        mock_client.create_reservation.return_value = make_success_response(reservation_id="res_cancelled")
        mock_client.release_reservation.return_value = make_commit_response()
        h = _hooks(mock_client, ttl_ms=2000)
        model = _BlockingModel()
        agent = Agent(name="cancelled", model=model)

        task = asyncio.create_task(h.run(agent, "wait", run_id="run-cancelled"))
        await model.started.wait()
        pending = h._tracker.get_llm("run-cancelled")
        assert pending is not None
        heartbeat_task = pending.heartbeat_task
        assert heartbeat_task is not None
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        mock_client.release_reservation.assert_awaited_once()
        reservation_id, request = mock_client.release_reservation.call_args.args
        assert reservation_id == "res_cancelled"
        assert request.reason == "agent_run_cancelled"
        await asyncio.sleep(0)
        assert heartbeat_task.done()

    async def test_concurrent_runs_on_one_hooks_instance_are_isolated(self, mock_client: AsyncMock) -> None:
        async def reserve(request: Any) -> Any:
            return make_success_response(reservation_id=f"res_{request.action.name}")

        mock_client.create_reservation.side_effect = reserve
        mock_client.commit_reservation.return_value = make_commit_response()
        h = _hooks(mock_client, ttl_ms=0)
        model_a = _BlockingModel()
        model_b = _BlockingModel()
        agent_a = Agent(name="agent-a", model=model_a)
        agent_b = Agent(name="agent-b", model=model_b)

        task_a = asyncio.create_task(h.run(agent_a, "a", run_id="run-a"))
        task_b = asyncio.create_task(h.run(agent_b, "b", run_id="run-b"))
        await asyncio.gather(model_a.started.wait(), model_b.started.wait())

        assert h._tracker.pending_count("run-a") == 1
        assert h._tracker.pending_count("run-b") == 1
        mock_client.release_reservation.assert_not_awaited()

        model_a.finish.set()
        model_b.finish.set()
        results = await asyncio.gather(task_a, task_b)

        assert [result.final_output for result in results] == ["ok", "ok"]
        assert mock_client.commit_reservation.await_count == 2
        assert h._tracker.pending_count("run-a") == 0
        assert h._tracker.pending_count("run-b") == 0
        mock_client.release_reservation.assert_not_awaited()


class TestCommitIdempotency:
    async def test_tool_commit_retry_reuses_same_key(
        self,
        mock_client: AsyncMock,
        mock_context: MagicMock,
        mock_agent: MagicMock,
        mock_tool: MagicMock,
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response(reservation_id="res_retry")
        mock_client.commit_reservation.side_effect = [make_http_error(503), make_commit_response()]
        h = _hooks(mock_client, ttl_ms=0, tool_estimates={"test-tool": 10})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)
        await h.on_tool_end(mock_context, mock_agent, mock_tool, "result")
        first_key = mock_client.commit_reservation.await_args_list[0].args[1].idempotency_key
        assert h._tracker.pending_tool_count == 1

        await h.on_tool_end(mock_context, mock_agent, mock_tool, "result")
        second_key = mock_client.commit_reservation.await_args_list[1].args[1].idempotency_key

        assert first_key == second_key
        assert h._tracker.pending_tool_count == 0

    async def test_llm_commit_retry_reuses_same_key(
        self,
        mock_client: AsyncMock,
        mock_context: MagicMock,
        mock_agent: MagicMock,
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response(reservation_id="res_llm_retry")
        mock_client.commit_reservation.side_effect = [make_http_error(503), make_commit_response()]
        h = _hooks(mock_client, ttl_ms=0)
        response = MagicMock()
        response.usage.input_tokens = 10
        response.usage.output_tokens = 5

        await h.on_llm_start(mock_context, mock_agent, None, [])
        await h.on_llm_end(mock_context, mock_agent, response)
        first_key = mock_client.commit_reservation.await_args_list[0].args[1].idempotency_key
        assert h._tracker.has_pending_llm is True

        await h.on_llm_end(mock_context, mock_agent, response)
        second_key = mock_client.commit_reservation.await_args_list[1].args[1].idempotency_key

        assert first_key == second_key
        assert h._tracker.has_pending_llm is False


# --- release_pending ---


class TestReleasePending:
    async def test_releases_pending_tools_and_llm(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response()
        mock_client.release_reservation.return_value = make_commit_response()
        h = _hooks(mock_client, tool_estimates={"test-tool": 10})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)
        await h.on_llm_start(mock_context, mock_agent, None, [])

        await h.release_pending("test_failure")

        assert mock_client.release_reservation.await_count == 2
        assert h._tracker.pending_tool_count == 0
        assert h._tracker.has_pending_llm is False

    async def test_release_pending_when_empty(self, mock_client: AsyncMock) -> None:
        h = _hooks(mock_client)
        await h.release_pending()
        mock_client.release_reservation.assert_not_awaited()

    async def test_release_failure_does_not_raise(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response()
        mock_client.release_reservation.return_value = make_http_error(500)
        h = _hooks(mock_client, tool_estimates={"test-tool": 10})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)
        await h.release_pending()  # should not raise

    async def test_release_exception_does_not_raise(
        self, mock_client: AsyncMock, mock_context: MagicMock, mock_agent: MagicMock, mock_tool: MagicMock
    ) -> None:
        mock_client.create_reservation.return_value = make_success_response()
        mock_client.release_reservation.side_effect = ConnectionError("down")
        h = _hooks(mock_client, tool_estimates={"test-tool": 10})

        await h.on_tool_start(mock_context, mock_agent, mock_tool)
        await h.release_pending()  # should not raise


# --- overage_policy type ---


class TestOveragePolicy:
    def test_string_overage_policy(self, mock_client: AsyncMock) -> None:
        from runcycles import CommitOveragePolicy

        h = _hooks(mock_client, overage_policy="REJECT")
        assert h._overage_policy == CommitOveragePolicy.REJECT

    def test_enum_overage_policy(self, mock_client: AsyncMock) -> None:
        from runcycles import CommitOveragePolicy

        h = _hooks(mock_client, overage_policy=CommitOveragePolicy.ALLOW_WITH_OVERDRAFT)
        assert h._overage_policy == CommitOveragePolicy.ALLOW_WITH_OVERDRAFT
