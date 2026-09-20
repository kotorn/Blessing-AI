from decimal import Decimal

import pytest

from venues.binance.capabilities import BinanceCapabilityDiscovery
from venues.binance_global.usdm import BinanceGlobalUSDMAdapter


class FakeResponse:
    def __init__(self, payload, status: int = 200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = responses

    def get(self, url):
        path = url.split("?", 1)[0]
        return FakeResponse(*self.responses[path])


def exchange_info_payload():
    return {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "status": "TRADING",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "pricePrecision": 1,
                "quantityPrecision": 4,
                "filters": [
                    {
                        "filterType": "PRICE_FILTER",
                        "minPrice": "261.10",
                        "maxPrice": "809484",
                        "tickSize": "0.10",
                    },
                    {
                        "filterType": "LOT_SIZE",
                        "minQty": "0.0001",
                        "maxQty": "1000",
                        "stepSize": "0.0001",
                    },
                    {"filterType": "MIN_NOTIONAL", "notional": "50"},
                ],
            }
        ]
    }


def make_adapter(responses):
    adapter = BinanceGlobalUSDMAdapter("test-key", "test-secret", testnet=True)
    adapter._session = FakeSession(responses)
    return adapter


def position_payload(*, amount: str, liquidation_price: str):
    return [
        {
            "symbol": "BTCUSDT",
            "positionAmt": amount,
            "entryPrice": "100",
            "markPrice": "100",
            "liquidationPrice": liquidation_price,
            "unRealizedProfit": "0",
            "leverage": "2",
            "marginType": "cross",
            "isolatedMargin": "0",
        }
    ]


def test_capability_parser_rejects_missing_exchange_filters():
    discovery = BinanceCapabilityDiscovery()
    raw = exchange_info_payload()["symbols"][0]
    raw["filters"] = [raw["filters"][0], raw["filters"][1]]

    with pytest.raises(ValueError, match="MIN_NOTIONAL"):
        discovery.parse_mock_exchange_info("BTCUSDT", raw)


def test_capability_parser_keeps_leverage_unknown_and_uses_exchange_rules():
    discovery = BinanceCapabilityDiscovery()
    raw = exchange_info_payload()["symbols"][0]

    instrument = discovery.parse_mock_exchange_info("BTCUSDT", raw)

    assert instrument.tick_size == Decimal("0.10")
    assert instrument.step_size == Decimal("0.0001")
    assert instrument.min_notional == Decimal(50)
    assert instrument.max_leverage is None


@pytest.mark.asyncio
async def test_legacy_symbol_info_has_no_hardcoded_rule_or_leverage_fallback():
    adapter = make_adapter({"https://testnet.binancefuture.com/fapi/v1/exchangeInfo": (exchange_info_payload(), 200)})

    info = await adapter.get_symbol_info("BTCUSDT")

    assert info.tick_size == Decimal("0.10")
    assert info.step_size == Decimal("0.0001")
    assert info.min_notional == Decimal(50)
    assert info.max_leverage is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("amount", "liquidation_price", "expected_distance"),
    [
        ("1", "90", Decimal(10)),
        ("-1", "110", Decimal(10)),
        ("1", "110", None),
        ("-1", "90", None),
    ],
)
async def test_legacy_liquidation_distance_is_side_aware(
    amount, liquidation_price, expected_distance
):
    adapter = make_adapter(
        {
            "https://testnet.binancefuture.com/fapi/v2/positionRisk": (
                position_payload(
                    amount=amount,
                    liquidation_price=liquidation_price,
                ),
                200,
            )
        }
    )

    result = await adapter.get_liquidation_information("BTCUSDT")

    assert result["distance_pct"] == expected_distance
    assert result["liquidation_safety"] == (
        "KNOWN" if expected_distance is not None else "UNKNOWN"
    )


@pytest.mark.asyncio
async def test_legacy_balance_does_not_turn_missing_asset_into_zero():
    adapter = make_adapter(
        {
            "https://testnet.binancefuture.com/fapi/v2/account": (
                {"assets": []},
                200,
            )
        }
    )

    with pytest.raises(ValueError, match="does not contain asset USDT"):
        await adapter.get_balance("USDT")


@pytest.mark.asyncio
async def test_legacy_stream_methods_fail_explicitly_instead_of_claiming_health():
    adapter = BinanceGlobalUSDMAdapter("test-key", "test-secret", testnet=True)

    with pytest.raises(RuntimeError, match="does not implement streams"):
        await adapter.subscribe_trades("BTCUSDT", lambda _: None)
