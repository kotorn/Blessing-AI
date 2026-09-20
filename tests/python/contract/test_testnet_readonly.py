"""Credentialed, read-only Binance USDⓈ-M Testnet contract checks."""

import os

import pytest

from apps.trading_worker.main import ArmRequest, TradingWorkerApp

pytestmark = [pytest.mark.asyncio, pytest.mark.contract_readonly]


@pytest.fixture
def api_credentials():
    if os.getenv("BINANCE_TESTNET", "").strip().lower() not in {"1", "true", "yes", "on"}:
        pytest.skip("BINANCE_TESTNET is not explicitly enabled")
    api_key = os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "").strip()
    if not api_key or not api_secret:
        pytest.skip("Testnet credentials not found. Skipping contract test.")
    # Never return credentials from a fixture. Pytest includes fixture values
    # in failure reports, so returning the tuple would disclose secrets when
    # a read-only contract assertion fails.
    return True


async def test_worker_testnet_readonly_lifecycle(api_credentials):
    """Exercise time, capabilities, account, stream, bootstrap, and reconcile together."""

    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    adapter = None
    try:
        armed, reason = await worker.arm(
            ArmRequest(
                executionMode="TESTNET",
                instruments=["BTCUSDT"],
                strategies={"grid": True},
                riskProfile="CONSERVATIVE",
            )
        )
        assert armed, reason
        adapter = worker.execution_adapter
        assert adapter is not None

        # Server time is synchronized before any signed request by the client.
        server_time = await adapter.rest_client.request("GET", "/fapi/v1/time")
        assert isinstance(server_time.get("serverTime"), int)

        exchange_info = await adapter.rest_client.request("GET", "/fapi/v1/exchangeInfo")
        btc = next(item for item in exchange_info["symbols"] if item["symbol"] == "BTCUSDT")
        assert btc["status"] == "TRADING"
        assert adapter.symbol_rules["BTCUSDT"].is_ready_for("LIMIT")
        assert adapter.symbol_rules["BTCUSDT"].is_ready_for("MARKET")

        account = await adapter.rest_client.request("GET", "/fapi/v2/account", signed=True)
        assert "totalWalletBalance" in account
        assert account.get("canTrade") is True
        assert adapter.authenticated is True
        assert adapter.capabilities.trade_authorized is True
        assert adapter.capabilities.hedge_mode in {True, False}

        position_mode = await adapter.rest_client.request(
            "GET", "/fapi/v1/positionSide/dual", signed=True
        )
        assert "dualSidePosition" in position_mode
        positions = await adapter.rest_client.request(
            "GET", "/fapi/v2/positionRisk", signed=True
        )
        open_orders = await adapter.rest_client.request(
            "GET", "/fapi/v1/openOrders", signed=True
        )
        assert isinstance(positions, list)
        assert isinstance(open_orders, list)

        assert adapter.user_stream.is_connected is True
        assert "stream.binancefuture.com" in adapter.user_stream.base_ws_url
        assert await adapter.user_stream.keepalive() is True
        assert adapter.user_stream.last_keepalive_at is not None

        assert adapter.reconciliation.last_status == "IN_SYNC"
        assert worker.is_account_snapshot_ready() is True
        assert await adapter.refresh_market_data(["BTCUSDT"]) is True
        worker.last_market_event_at.update(adapter.last_market_event_at)
        assert worker.is_market_data_fresh(["BTCUSDT"]) is True
        assert worker.get_capabilities()["testnetExecutionReady"] is True
    finally:
        await worker.disarm()
        if adapter is not None:
            assert adapter.user_stream.is_connected is False
