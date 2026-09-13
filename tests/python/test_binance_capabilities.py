import pytest

from apps.trading_worker.venues.binance.capabilities import BinanceCapabilities
from apps.trading_worker.venues.binance.config import BinanceEnvironment


class FakeCapabilityRestClient:
    env = BinanceEnvironment.TESTNET

    def __init__(self, *, can_trade: bool):
        self.can_trade = can_trade

    async def request(self, method, path, **kwargs):
        if path == "/fapi/v2/account":
            return {"totalWalletBalance": "100", "canTrade": self.can_trade}
        if path == "/fapi/v1/positionSide/dual":
            return {"dualSidePosition": False}
        if path == "/fapi/v1/exchangeInfo":
            return {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "orderTypes": ["LIMIT", "MARKET"],
                        "filters": [
                            {
                                "filterType": "PRICE_FILTER",
                                "minPrice": "0.1",
                                "maxPrice": "1000000",
                                "tickSize": "0.1",
                            },
                            {
                                "filterType": "LOT_SIZE",
                                "minQty": "0.001",
                                "maxQty": "100",
                                "stepSize": "0.001",
                            },
                            {
                                "filterType": "MIN_NOTIONAL",
                                "minNotional": "5",
                            },
                        ],
                    }
                ]
            }
        raise AssertionError(f"unexpected capability request: {method} {path}")


@pytest.mark.asyncio
async def test_capability_discovery_requires_account_trade_permission():
    capabilities = BinanceCapabilities()

    assert await capabilities.discover(FakeCapabilityRestClient(can_trade=True)) is True
    assert capabilities.authenticated is True
    assert capabilities.account_request_succeeded is True
    assert capabilities.trade_authorized is True

    assert await capabilities.discover(FakeCapabilityRestClient(can_trade=False)) is False
    assert capabilities.authenticated is True
    assert capabilities.account_request_succeeded is True
    assert capabilities.trade_authorized is False


@pytest.mark.asyncio
async def test_capability_discovery_rejects_missing_trade_permission_field():
    class MissingTradePermissionClient(FakeCapabilityRestClient):
        async def request(self, method, path, **kwargs):
            response = await super().request(method, path, **kwargs)
            if path == "/fapi/v2/account":
                response.pop("canTrade")
            return response

    capabilities = BinanceCapabilities()

    assert await capabilities.discover(MissingTradePermissionClient(can_trade=True)) is False
    assert capabilities.authenticated is False
    assert capabilities.account_request_succeeded is False
    assert capabilities.trade_authorized is False
