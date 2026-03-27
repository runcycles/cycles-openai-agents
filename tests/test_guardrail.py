"""Tests for cycles_budget_guardrail."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from runcycles import Unit

from runcycles_openai_agents.guardrail import cycles_budget_guardrail

from .conftest import make_deny_response, make_http_error, make_success_response, make_transport_error


def _mock_ctx() -> MagicMock:
    return MagicMock()


def _mock_agent(name: str = "test-agent") -> MagicMock:
    a = MagicMock()
    a.name = name
    return a


class TestCyclesBudgetGuardrail:
    async def test_allow_does_not_trip(self, mock_client: AsyncMock) -> None:
        mock_client.decide.return_value = make_success_response(decision="ALLOW")
        g = cycles_budget_guardrail(client=mock_client, tenant="t")

        result = await g.guardrail_function(_mock_ctx(), _mock_agent(), "hello")

        assert result.tripwire_triggered is False

    async def test_allow_with_caps_does_not_trip(self, mock_client: AsyncMock) -> None:
        mock_client.decide.return_value = make_success_response(decision="ALLOW_WITH_CAPS")
        g = cycles_budget_guardrail(client=mock_client, tenant="t")

        result = await g.guardrail_function(_mock_ctx(), _mock_agent(), "hello")

        assert result.tripwire_triggered is False

    async def test_deny_trips_guardrail(self, mock_client: AsyncMock) -> None:
        mock_client.decide.return_value = make_deny_response("budget_exhausted")
        g = cycles_budget_guardrail(client=mock_client, tenant="t")

        result = await g.guardrail_function(_mock_ctx(), _mock_agent(), "hello")

        assert result.tripwire_triggered is True
        assert result.output_info["decision"] == "DENY"

    async def test_transport_error_fail_open(self, mock_client: AsyncMock) -> None:
        mock_client.decide.return_value = make_transport_error()
        g = cycles_budget_guardrail(client=mock_client, tenant="t", fail_open=True)

        result = await g.guardrail_function(_mock_ctx(), _mock_agent(), "hello")

        assert result.tripwire_triggered is False

    async def test_transport_error_fail_closed(self, mock_client: AsyncMock) -> None:
        mock_client.decide.return_value = make_transport_error()
        g = cycles_budget_guardrail(client=mock_client, tenant="t", fail_open=False)

        result = await g.guardrail_function(_mock_ctx(), _mock_agent(), "hello")

        assert result.tripwire_triggered is True

    async def test_http_error_fail_open(self, mock_client: AsyncMock) -> None:
        mock_client.decide.return_value = make_http_error(500)
        g = cycles_budget_guardrail(client=mock_client, tenant="t", fail_open=True)

        result = await g.guardrail_function(_mock_ctx(), _mock_agent(), "hello")

        assert result.tripwire_triggered is False

    async def test_http_error_fail_closed(self, mock_client: AsyncMock) -> None:
        mock_client.decide.return_value = make_http_error(500)
        g = cycles_budget_guardrail(client=mock_client, tenant="t", fail_open=False)

        result = await g.guardrail_function(_mock_ctx(), _mock_agent(), "hello")

        assert result.tripwire_triggered is True

    async def test_guardrail_name(self, mock_client: AsyncMock) -> None:
        g = cycles_budget_guardrail(client=mock_client, tenant="t")
        assert g.name == "cycles_budget"

    async def test_custom_estimate_and_unit(self, mock_client: AsyncMock) -> None:
        mock_client.decide.return_value = make_success_response(decision="ALLOW")
        g = cycles_budget_guardrail(client=mock_client, tenant="t", estimate=999, unit=Unit.TOKENS)

        await g.guardrail_function(_mock_ctx(), _mock_agent(), "hello")

        call_args = mock_client.decide.call_args[0][0]
        assert call_args.estimate.amount == 999
        assert call_args.estimate.unit == Unit.TOKENS

    @patch("runcycles_openai_agents.guardrail.CyclesConfig.from_env")
    @patch("runcycles_openai_agents.guardrail.AsyncCyclesClient")
    async def test_env_fallback(self, mock_cls: MagicMock, mock_from_env: MagicMock) -> None:
        mock_from_env.return_value = MagicMock()
        mock_instance = AsyncMock()
        mock_instance.decide.return_value = make_success_response(decision="ALLOW")
        mock_cls.return_value = mock_instance

        g = cycles_budget_guardrail(tenant="t")
        result = await g.guardrail_function(_mock_ctx(), _mock_agent(), "hello")

        mock_from_env.assert_called_once()
        assert result.tripwire_triggered is False

    async def test_config_creates_client(self) -> None:
        from runcycles import CyclesConfig

        cfg = CyclesConfig(base_url="http://localhost:7878", api_key="cyc_test_key")
        g = cycles_budget_guardrail(config=cfg, tenant="t")
        assert g.name == "cycles_budget"

    async def test_workspace_and_app_passed(self, mock_client: AsyncMock) -> None:
        mock_client.decide.return_value = make_success_response(decision="ALLOW")
        g = cycles_budget_guardrail(client=mock_client, tenant="t", workspace="prod", app="chat")

        await g.guardrail_function(_mock_ctx(), _mock_agent(), "hello")

        call_args = mock_client.decide.call_args[0][0]
        assert call_args.subject.workspace == "prod"
        assert call_args.subject.app == "chat"
