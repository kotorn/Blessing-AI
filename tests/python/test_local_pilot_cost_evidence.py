from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from domain.enums import MarketType, OrderSide, OrderType, PositionSide, TimeInForce
from domain.models import OrderIntent


def pilot_intent():
    return OrderIntent(
        client_order_id="pilot-cost-1",
        symbol="ETHUSDC",
        basket_id="basket-pilot-1",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.15"),
        stop_loss_price=Decimal("90"),
        take_profit_price=Decimal("110"),
        management_mode="QUICK",
    )


@pytest.mark.asyncio
async def test_pilot_cost_evidence_uses_exchange_commission_depth_and_funding_caps(monkeypatch):
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.MAINNET
    adapter.preflight_only = False
    calls = []
    responses = {
        "/fapi/v1/commissionRate": {"symbol": "ETHUSDC", "takerCommissionRate": "0.0004"},
        "/fapi/v1/depth": {
            "bids": [["99.9", "1"]],
            "asks": [["100.1", "1"]],
        },
        "/fapi/v1/fundingRate": [{"symbol": "ETHUSDC", "fundingRate": "0.0001"}],
        "/fapi/v1/fundingInfo": [],
        "/fapi/v1/leverageBracket": [{"symbol": "ETHUSDC", "brackets": [
            {"notionalFloor": "0", "notionalCap": "50000", "maintMarginRatio": "0.005"},
        ]}],
    }

    async def request(method, route, signed=False, **kwargs):
        calls.append((route, signed, kwargs.get("params", {})))
        return responses[route]

    adapter.rest_client = SimpleNamespace(request=request)
    result = await adapter.get_local_mainnet_cost_evidence(
        pilot_intent(),
        {"runtime_target": "LOCAL", "validated_quantity": Decimal("0.15"),
         "validated_entry_price": Decimal("100")},
    )

    assert result is not None
    assert result["source"] == "BINANCE_FAPI_COMMISSION_FUNDING_DEPTH"
    assert result["fees_upper_bound_usdc"] == Decimal("0.012000")
    assert result["funding_upper_bound_usdc"] == Decimal("1.40625")
    assert result["cost_horizon_seconds"] == 86400
    assert result["funding_events_assumed"] == 25
    assert result["cost_horizon_source"] == "LOCAL_LIVE_PILOT_POLICY"
    assert result["runtime_target"] == "LOCAL"
    assert result["venue"] == "BINANCE_MAINNET"
    assert result["slippage_upper_bound_usdc"] == Decimal("0.03")
    assert result["depth_levels_requested"] == 1000
    assert result["depth_bid_levels_received"] == 1
    assert result["depth_ask_levels_received"] == 1
    assert set(result["request_observations"]) == {
        "commission", "depth", "funding", "funding_info", "leverage_brackets"
    }
    assert all(
        observation["source_timestamp"] is None
        and observation["duration_ms"] >= 0
        for observation in result["request_observations"].values()
    )
    assert ("/fapi/v1/commissionRate", True, {"symbol": "ETHUSDC"}) in calls
    assert ("/fapi/v1/depth", False, {"symbol": "ETHUSDC", "limit": 1000}) in calls
    assert ("/fapi/v1/fundingInfo", False, {}) in calls
    assert all(route.startswith("/fapi/") for route, _, _ in calls)


@pytest.mark.asyncio
async def test_pilot_cost_evidence_fails_closed_when_book_or_bracket_is_missing(monkeypatch):
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.MAINNET
    adapter.preflight_only = False
    responses = {
        "/fapi/v1/commissionRate": {"symbol": "ETHUSDC", "takerCommissionRate": "0.0004"},
        "/fapi/v1/depth": {"bids": [["99.9", "0.01"]], "asks": [["100.1", "0.01"]]},
        "/fapi/v1/fundingRate": [{"symbol": "ETHUSDC", "fundingRate": "0.0001"}],
        "/fapi/v1/fundingInfo": [],
        "/fapi/v1/leverageBracket": [],
    }

    async def request(_method, route, **_kwargs):
        return responses[route]

    adapter.rest_client = SimpleNamespace(request=request)
    result = await adapter.get_local_mainnet_cost_evidence(
        pilot_intent(),
        {"runtime_target": "LOCAL", "validated_quantity": Decimal("0.15"),
         "validated_entry_price": Decimal("100")},
    )
    assert result is None
