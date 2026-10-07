from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.trading_worker.venues.binance.capabilities import BinanceCapabilities
from apps.trading_worker.venues.binance.config import (
    PAPI_WS_URL,
    BinanceEnvironment,
    is_portfolio_margin_enabled,
)
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.reconciliation import (
    BinanceReconciliation,
    build_account_snapshot,
)
from apps.trading_worker.venues.binance.rest_client import BinanceRestClient
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
    _ALLOWED_REQUEST_METHODS,
    BinanceRestClient,
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
    with pytest.raises(PermissionError, match="Read-only Binance client cannot mutate an order endpoint"):
        await client.request("POST", "/papi/v1/um/order", signed=True)
    assert client.order_endpoint_attempts == 1


@pytest.mark.asyncio
async def test_rest_client_read_only_allows_get_order_query():
    client = BinanceRestClient(
        "key", "secret", BinanceEnvironment.MAINNET, read_only=True, portfolio_margin=True
    )
    class MockContextManager:
        def __init__(self, resp):
            self.resp = resp
        async def __aenter__(self):
            return self.resp
        async def __aexit__(self, exc_type, exc, tb):
            pass

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"symbol": "ETHUSDC", "orderId": 12345, "status": "FILLED"})
    mock_resp.headers = {}
    mock_session = MagicMock()
    mock_session.request = MagicMock(return_value=MockContextManager(mock_resp))
    client.session = mock_session
    client._server_time_offset_ms = 0

    res = await client.request("GET", "/papi/v1/um/order", signed=True, params={"symbol": "ETHUSDC"})
    assert res["status"] == "FILLED"
    assert client.order_endpoint_attempts == 0


def test_rest_client_rejects_portfolio_margin_on_testnet():
    with pytest.raises(ValueError, match="Portfolio Margin is not supported in Binance TESTNET environment"):
        BinanceRestClient("key", "secret", BinanceEnvironment.TESTNET, portfolio_margin=True)

    client_futures = BinanceRestClient("key", "secret", BinanceEnvironment.TESTNET, portfolio_margin=False)
    assert client_futures.portfolio_margin is False

    client_pm = BinanceRestClient("key", "secret", BinanceEnvironment.MAINNET, portfolio_margin=True)
    assert client_pm.portfolio_margin is True


@pytest.mark.asyncio
async def test_portfolio_margin_capability_discovery():
    class FakePapiRestClient:
        env = BinanceEnvironment.MAINNET
        portfolio_margin = True

        async def request(self, method, path, **kwargs):
            if path == "/papi/v1/account":
                return {"accountStatus": "NORMAL"}
            if path == "/papi/v1/um/account":
                return {"canTrade": True}
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


@pytest.mark.asyncio
async def test_portfolio_margin_capability_discovery_can_trade_false():
    class FakePapiRestClient:
        env = BinanceEnvironment.MAINNET
        portfolio_margin = True

        async def request(self, method, path, **kwargs):
            if path == "/papi/v1/account":
                return {"accountStatus": "NORMAL"}
            if path == "/papi/v1/um/account":
                return {"canTrade": False}
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
    assert success is False
    assert capabilities.authenticated is True
    assert capabilities.account_request_succeeded is True
    assert capabilities.trade_authorized is False
    assert capabilities.hedge_mode is False
    assert capabilities.position_mode_known is True


@pytest.mark.asyncio
async def test_portfolio_margin_capability_discovery_missing_can_trade_fails():
    class FakePapiRestClient:
        env = BinanceEnvironment.MAINNET
        portfolio_margin = True

        async def request(self, method, path, **kwargs):
            if path == "/papi/v1/account":
                return {"accountStatus": "NORMAL"}
            if path == "/papi/v1/um/account":
                return {"assets": []}
            if path == "/papi/v1/um/positionSide/dual":
                return {"dualSidePosition": False}
            if path == "/fapi/v1/exchangeInfo":
                return {"symbols": []}
            raise AssertionError(f"unexpected capability request: {method} {path}")

    capabilities = BinanceCapabilities()
    success = await capabilities.discover(FakePapiRestClient())
    assert success is False
    assert capabilities.account_request_succeeded is False
    assert capabilities.trade_authorized is False


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

    observed_at = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    snapshot = build_account_snapshot(
        account,
        positions,
        environment="BINANCE_MAINNET",
        daily_realized_pnl=Decimal(0),
        daily_loss_known=True,
        daily_loss_asset="USDC",
        daily_pnl_includes_fees=True,
        daily_pnl_includes_funding=True,
        observed_at=observed_at,
    )
    assert snapshot.timestamp == observed_at
    assert snapshot.margin_mode == "CROSS"
    assert snapshot.margin_mode_known is True
    assert snapshot.collateral_asset == "USDC"
    assert snapshot.margin_balance == Decimal("50.88337369")
    assert snapshot.available_balance == Decimal("50.88337369")
    assert snapshot.configured_leverage == Decimal(2)
    assert snapshot.configured_leverage_known is True


def test_build_account_snapshot_portfolio_margin_liquidation_distance():
    account = {
        "portfolioMargin": True,
        "assets": [
            {
                "asset": "USDC",
                "walletBalance": "50.0",
                "marginBalance": "50.0",
                "availableBalance": "50.0",
                "crossMarginAsset": "50.0",
                "crossMarginFree": "50.0",
                "unrealizedProfit": "0.0",
                "initialMargin": "8.5",
                "maintMargin": "1.0",
                "positionInitialMargin": "8.5",
            }
        ],
        "positions": [
            {
                "symbol": "ETHUSDC",
                "positionSide": "BOTH",
                "leverage": "2",
                "positionAmt": "0.007",
                "entryPrice": "2450.0",
                "markPrice": "2450.0",
                "liquidationPrice": "0",  # Binance PM returns 0
                "initialMargin": "8.575",
                "maintMargin": "0.17",
                "unrealizedProfit": "0.0",
            }
        ],
    }
    positions = account["positions"]
    snapshot = build_account_snapshot(
        account,
        positions,
        environment="BINANCE_MAINNET",
        daily_realized_pnl=Decimal(0),
        daily_loss_known=True,
        daily_loss_asset="USDC",
        daily_pnl_includes_fees=True,
        daily_pnl_includes_funding=True,
    )
    assert snapshot.liquidation_safety == "KNOWN"
    assert snapshot.min_liquidation_distance_pct is not None
    assert snapshot.min_liquidation_distance_pct > 0
    # Headroom = 50 - 1 = 49. Notional = 0.007 * 2450 = 17.15. Distance = min(100, (49 / 17.15) * 100) = 100
    assert snapshot.min_liquidation_distance_pct == Decimal(100)


def test_build_account_snapshot_portfolio_margin_zero_headroom_floors_at_zero():
    """When maintenance margin exhausts margin balance, liquidation distance must floor at 0, not 0.01."""
    account = {
        "portfolioMargin": True,
        "assets": [
            {
                "asset": "USDC",
                "walletBalance": "10.0",
                "marginBalance": "10.0",
                "availableBalance": "0.0",
                "crossMarginAsset": "10.0",
                "crossMarginFree": "0.0",
                "unrealizedProfit": "0.0",
                "initialMargin": "10.0",
                "maintMargin": "10.0",
                "positionInitialMargin": "10.0",
            }
        ],
        "positions": [
            {
                "symbol": "ETHUSDC",
                "positionSide": "BOTH",
                "leverage": "2",
                "positionAmt": "0.010",
                "entryPrice": "2000.0",
                "markPrice": "2000.0",
                "liquidationPrice": "0",
                "initialMargin": "10.0",
                "maintMargin": "10.0",
                "unrealizedProfit": "0.0",
            }
        ],
    }
    positions = account["positions"]
    snapshot = build_account_snapshot(
        account,
        positions,
        environment="BINANCE_MAINNET",
        daily_realized_pnl=Decimal(0),
        daily_loss_known=True,
        daily_loss_asset="USDC",
        daily_pnl_includes_fees=True,
        daily_pnl_includes_funding=True,
    )
    # Headroom is max(0, 10.0 - 10.0) = 0. Distance must be exactly 0 (floored at 0, not 0.01).
    assert snapshot.min_liquidation_distance_pct == Decimal(0)


@pytest.mark.asyncio
async def test_portfolio_margin_synthesizes_margin_balance_with_unrealized_pnl():
    """Synthesized PM marginBalance must equal crossMarginAsset + umUnrealizedPNL (drawdown aware)."""
    class FakeDrawdownReconciliationClient:
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
                        "crossMarginAsset": "100.0",
                        "crossMarginFree": "80.0",
                        "umUnrealizedPNL": "-15.5",
                    }
                ]
            if path == "/papi/v1/um/account":
                return {
                    "assets": [
                        {
                            "asset": "USDC",
                            "initialMargin": "10.0",
                            "maintMargin": "5.0",
                            "positionInitialMargin": "10.0",
                        }
                    ],
                    "positions": [],
                }
            if path == "/papi/v1/um/income":
                return []
            raise AssertionError(f"unexpected request: {path}")

    from apps.trading_worker.venues.binance.ledger import InMemoryLedger
    reconciliation = BinanceReconciliation(FakeDrawdownReconciliationClient(), InMemoryLedger())
    account, positions, open_orders = await reconciliation._fetch_reconciliation_snapshot_inputs()

    usdc_asset = account["assets"][0]
    assert usdc_asset["asset"] == "USDC"
    assert usdc_asset["walletBalance"] == "100.0"
    # marginBalance = 100.0 + (-15.5) = 84.5 (not 100.0)
    assert usdc_asset["marginBalance"] == "84.5"
    assert usdc_asset["unrealizedProfit"] == "-15.5"


@pytest.mark.parametrize("portfolio_margin", [False, True])
@pytest.mark.asyncio
async def test_cost_evidence_routes_for_portfolio_margin(portfolio_margin, monkeypatch):
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    adapter = BinanceExecutionAdapter(
        api_key="key",
        api_secret="secret",
        env=BinanceEnvironment.MAINNET,
        portfolio_margin=portfolio_margin,
    )
    adapter.preflight_only = False

    requested_routes = []

    async def mock_request(method, path, *, signed=False, params=None):
        requested_routes.append((method, path, signed, params or {}))
        if path in ("/papi/v1/um/commissionRate", "/fapi/v1/commissionRate"):
            return {"symbol": "ETHUSDC", "takerCommissionRate": "0.0004"}
        if path == "/fapi/v1/depth":
            return {"bids": [["2400.0", "10.0"]], "asks": [["2401.0", "10.0"]]}
        if path == "/fapi/v1/fundingRate":
            return [{"symbol": "ETHUSDC", "fundingRate": "0.0001"}]
        if path == "/fapi/v1/fundingInfo":
            return []
        if path in ("/papi/v1/um/leverageBracket", "/fapi/v1/leverageBracket"):
            return [
                {
                    "symbol": "ETHUSDC",
                    "brackets": [
                        {
                            "bracket": 1,
                            "initialLeverage": 10,
                            "notionalCap": "50000",
                            "notionalFloor": "0",
                            "maintMarginRatio": "0.005",
                            "cum": "0.0",
                        }
                    ],
                }
            ]
        raise AssertionError(f"unexpected request: {method} {path}")

    adapter.rest_client.request = AsyncMock(side_effect=mock_request)

    intent = MagicMock()
    intent.symbol = "ETHUSDC"
    intent.management_mode = "QUICK"
    intent.side = "BUY"
    intent.client_order_id = "cid-pm-cost-test"

    context = {
        "runtime_target": "LOCAL",
        "validated_quantity": Decimal("0.01"),
        "validated_entry_price": Decimal("2400.0"),
    }

    cost_evidence = await adapter.get_local_mainnet_cost_evidence(intent, context)
    assert cost_evidence is not None

    expected_source = (
        "BINANCE_PAPI_COMMISSION_FUNDING_DEPTH"
        if portfolio_margin
        else "BINANCE_FAPI_COMMISSION_FUNDING_DEPTH"
    )
    expected_commission_route = (
        "/papi/v1/um/commissionRate" if portfolio_margin else "/fapi/v1/commissionRate"
    )
    expected_bracket_route = (
        "/papi/v1/um/leverageBracket" if portfolio_margin else "/fapi/v1/leverageBracket"
    )

    assert cost_evidence["source"] == expected_source
    obs = cost_evidence["request_observations"]
    assert obs["commission"]["route"] == expected_commission_route
    assert obs["leverage_brackets"]["route"] == expected_bracket_route
    assert obs["depth"]["route"] == "/fapi/v1/depth"
    assert obs["funding"]["route"] == "/fapi/v1/fundingRate"
    assert obs["funding_info"]["route"] == "/fapi/v1/fundingInfo"

    routes_called = [r[1] for r in requested_routes]
    assert expected_commission_route in routes_called
    assert expected_bracket_route in routes_called
    assert "/fapi/v1/depth" in routes_called
    assert "/fapi/v1/fundingRate" in routes_called
    assert "/fapi/v1/fundingInfo" in routes_called


@pytest.mark.parametrize("portfolio_margin", [False, True])
@pytest.mark.asyncio
async def test_kill_switch_release_checks_open_orders_and_algos(portfolio_margin):
    from apps.trading_worker.main import TradingWorkerApp, WorkerExecutionMode

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE
    worker.kill_switch_active = True

    adapter = MagicMock()
    adapter.portfolio_margin = portfolio_margin
    adapter._open_orders_path = (
        "/papi/v1/um/openOrders" if portfolio_margin else "/fapi/v1/openOrders"
    )
    adapter._open_algo_orders_path = (
        "/papi/v1/um/algo/openAlgoOrders" if portfolio_margin else "/fapi/v1/openAlgoOrders"
    )
    adapter.private_stream_healthy = True
    adapter.authenticated = True

    rest_client = MagicMock()
    adapter.rest_client = rest_client
    worker.execution_adapter = adapter
    worker.trigger_reconciliation = AsyncMock(return_value="IN_SYNC")

    # Case 1: normal open orders exist -> blocked
    async def mock_request_orders_exist(method, path, signed=False, params=None):
        if path == adapter._open_orders_path:
            return [{"orderId": 123}]
        if path == adapter._open_algo_orders_path:
            return []
        raise AssertionError(f"unexpected request: {path}")

    rest_client.request = AsyncMock(side_effect=mock_request_orders_exist)
    res = await worker.set_kill_switch(False)
    assert res["status"] == "PARTIAL"
    assert res["remaining_orders"] == 1
    assert worker.kill_switch_active is True

    # Case 2: normal open orders are empty, but algo orders exist -> blocked
    async def mock_request_algo(method, path, signed=False, params=None):
        if path == adapter._open_orders_path:
            return []
        if path == adapter._open_algo_orders_path:
            return [{"algoId": 456}]
        raise AssertionError(f"unexpected request: {path}")

    rest_client.request = AsyncMock(side_effect=mock_request_algo)
    res = await worker.set_kill_switch(False)
    assert res["status"] == "PARTIAL"
    assert res["remaining_orders"] == 1
    assert worker.kill_switch_active is True

    # Case 3: both normal and algo orders are empty -> released
    async def mock_request_clean(method, path, signed=False, params=None):
        if path == adapter._open_orders_path:
            return []
        if path == adapter._open_algo_orders_path:
            return []
        raise AssertionError(f"unexpected request: {path}")

    rest_client.request = AsyncMock(side_effect=mock_request_clean)
    res = await worker.set_kill_switch(False)
    assert res["status"] == "CONFIRMED"
    assert res["remaining_orders"] == 0
    assert worker.kill_switch_active is False
    worker.trigger_reconciliation.assert_called_once()

