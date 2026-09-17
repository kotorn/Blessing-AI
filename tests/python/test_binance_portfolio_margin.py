import os
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from apps.trading_worker.venues.binance.config import (
    BinanceEnvironment,
    PAPI_REST_URL,
    PAPI_WS_URL,
    is_portfolio_margin_enabled,
)
from apps.trading_worker.venues.binance.rest_client import BinanceRestClient
from apps.trading_worker.venues.binance.capabilities import BinanceCapabilities
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.reconciliation import (
    BinanceReconciliation,
    build_account_snapshot,
)
from apps.trading_worker.venues.binance.user_stream import BinanceUserStream


def test_is_portfolio_margin_enabled_env(monkeypatch):
    monkeypatch.delenv("BINANCE_PORTFOLIO_MARGIN", raising=False)
    assert is_portfolio_margin_enabled() is False

    monkeypatch.setenv("BINANCE_PORTFOLIO_MARGIN", "true")
    assert is_portfolio_margin_enabled() is True

    monkeypatch.setenv("BINANCE_PORTFOLIO_MARGIN", "1")
    assert is_portfolio_margin_enabled() is True

    monkeypatch.setenv("BINANCE_PORTFOLIO_MARGIN", "false")
    assert is_portfolio_margin_enabled() is False


from apps.trading_worker.venues.binance.rest_client import (
    BinanceRestClient,
    _ALLOWED_REQUEST_METHODS,
)


def test_rest_client_routes_papi_urls():
    client = BinanceRestClient("key", "secret", BinanceEnvironment.MAINNET, portfolio_margin=True)
    assert client.portfolio_margin is True
    assert "GET" in _ALLOWED_REQUEST_METHODS.get("/papi/v1/account", set())
    assert "GET" in _ALLOWED_REQUEST_METHODS.get("/papi/v1/balance", set())
    assert "GET" in _ALLOWED_REQUEST_METHODS.get("/papi/v1/um/account", set())
    assert "POST" in _ALLOWED_REQUEST_METHODS.get("/papi/v1/um/order", set())
    assert "GET" in _ALLOWED_REQUEST_METHODS.get("/papi/v1/um/openOrders", set())
    assert "GET" in _ALLOWED_REQUEST_METHODS.get("/papi/v1/um/positionRisk", set())
    assert "POST" in _ALLOWED_REQUEST_METHODS.get("/papi/v1/um/leverage", set())
    assert "POST" in _ALLOWED_REQUEST_METHODS.get("/papi/v1/listenKey", set())


@pytest.mark.asyncio
async def test_rest_client_read_only_blocks_papi_order():
    client = BinanceRestClient(
        "key", "secret", BinanceEnvironment.MAINNET, read_only=True, portfolio_margin=True
    )
    with pytest.raises(PermissionError, match="Read-only Binance client cannot call the order endpoint"):
        await client.request("POST", "/papi/v1/um/order", signed=True)
    assert client.order_endpoint_attempts == 1


@pytest.mark.asyncio
async def test_portfolio_margin_capability_discovery():
    class FakePapiRestClient:
        env = BinanceEnvironment.MAINNET
        portfolio_margin = True

        async def request(self, method, path, **kwargs):
            if path == "/papi/v1/account":
                return {"accountStatus": "NORMAL"}
            if path == "/papi/v1/um/positionSide/dual":
                return {"dualSidePosition": False}
            if path == "/fapi/v1/exchangeInfo":
                return {
                    "symbols": [
                        {
                            "symbol": "ETHUSDC",
                            "status": "TRADING",
                            "orderTypes": ["LIMIT", "MARKET"],
                            "filters": [
                                {"filterType": "PRICE_FILTER", "minPrice": "0.1", "maxPrice": "100000", "tickSize": "0.1"},
                                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
                                {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                            ],
                        }
                    ]
                }
            raise AssertionError(f"unexpected capability request: {method} {path}")

    capabilities = BinanceCapabilities()
    success = await capabilities.discover(FakePapiRestClient())
    assert success is True
    assert capabilities.authenticated is True
    assert capabilities.account_request_succeeded is True
    assert capabilities.trade_authorized is True
    assert capabilities.hedge_mode is False
    assert capabilities.position_mode_known is True
    assert "ETHUSDC" in capabilities.symbol_rules


def test_user_stream_uses_papi_urls():
    client = BinanceRestClient("key", "secret", BinanceEnvironment.MAINNET, portfolio_margin=True)
    stream = BinanceUserStream(client, BinanceEnvironment.MAINNET)
    assert stream.base_ws_url == PAPI_WS_URL
    assert stream._listen_key_path == "/papi/v1/listenKey"


def test_execution_adapter_path_resolution(monkeypatch):
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    adapter = BinanceExecutionAdapter(
        api_key="key",
        api_secret="secret",
        env=BinanceEnvironment.MAINNET,
        portfolio_margin=True,
    )
    assert adapter.portfolio_margin is True
    assert adapter._order_path == "/papi/v1/um/order"
    assert adapter._open_orders_path == "/papi/v1/um/openOrders"
    assert adapter._position_risk_path == "/papi/v1/um/positionRisk"


@pytest.mark.asyncio
async def test_reconciliation_snapshot_portfolio_margin():
    class FakeReconciliationClient:
        env = BinanceEnvironment.MAINNET
        portfolio_margin = True

        async def request(self, method, path, **kwargs):
            if path == "/papi/v1/um/positionRisk":
                return []
            if path == "/papi/v1/um/openOrders":
                return []
            if path == "/papi/v1/balance":
                return [
                    {
                        "asset": "USDC",
                        "totalWalletBalance": "50.88337369",
                        "crossMarginAsset": "50.88337369",
                        "crossMarginFree": "50.88337369",
                        "umUnrealizedPNL": "0.0",
                    }
                ]
            if path == "/papi/v1/um/account":
                return {
                    "assets": [
                        {
                            "asset": "USDC",
                            "initialMargin": "0",
                            "maintMargin": "0",
                            "positionInitialMargin": "0",
                        }
                    ],
                    "positions": [
                        {
                            "symbol": "ETHUSDC",
                            "leverage": "2",
                            "positionAmt": "0.000",
                            "initialMargin": "0",
                            "maintMargin": "0",
                            "unrealizedProfit": "0.00000000",
                        }
                    ],
                }
            if path == "/papi/v1/um/income":
                return []
            raise AssertionError(f"unexpected reconciliation request: {method} {path}")

    from apps.trading_worker.venues.binance.ledger import InMemoryLedger
    ledger = InMemoryLedger()
    client = FakeReconciliationClient()
    reconciliation = BinanceReconciliation(client, ledger)
    assert reconciliation.portfolio_margin is True
    assert reconciliation._income_path == "/papi/v1/um/income"

    account, positions, open_orders = await reconciliation._fetch_reconciliation_snapshot_inputs()
    assert account.get("portfolioMargin") is True
    assert len(positions) == 1
    assert positions[0]["symbol"] == "ETHUSDC"
    assert positions[0]["leverage"] == "2"

    snapshot = build_account_snapshot(
        account,
        positions,
        environment="BINANCE_MAINNET",
        daily_realized_pnl=Decimal("0"),
        daily_loss_known=True,
        daily_loss_asset="USDC",
        daily_pnl_includes_fees=True,
        daily_pnl_includes_funding=True,
    )
    assert snapshot.margin_mode == "CROSS"
    assert snapshot.margin_mode_known is True
    assert snapshot.collateral_asset == "USDC"
    assert snapshot.margin_balance == Decimal("50.88337369")
    assert snapshot.available_balance == Decimal("50.88337369")
    assert snapshot.configured_leverage == Decimal("2")
    assert snapshot.configured_leverage_known is True
