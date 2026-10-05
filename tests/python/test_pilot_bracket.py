"""Unit tests for the Local Live Research Pilot bracket producer."""

from __future__ import annotations

from decimal import Decimal
import pytest

from domain.enums import MarketType, OrderSide, OrderType, PositionSide, TimeInForce
from domain.models import OrderIntent
from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules
from apps.trading_worker.venues.binance.pilot_bracket import (
    DEFAULT_NOTIONAL_CAP_USDC,
    DEFAULT_MAX_STOP_RISK_USDC,
    DEFAULT_MIN_NET_REWARD_USDC,
    plan_pilot_bracket,
    apply_pilot_bracket_to_intent,
)
from apps.trading_worker.venues.binance.mainnet_risk import (
    MAINNET_RISK_POLICY,
    validate_risk_increasing_order,
)


def _ethusdc_rules() -> SymbolTradingRules:
    rules = SymbolTradingRules("ETHUSDC")
    rules.parse_exchange_info({
        "symbol": "ETHUSDC",
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "baseAsset": "ETH",
        "quoteAsset": "USDC",
        "marginAsset": "USDC",
        "pricePrecision": 2,
        "quantityPrecision": 3,
        "orderTypes": ["LIMIT", "MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET"],
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "100000.00", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "8000.000", "stepSize": "0.001"},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "700.000", "stepSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "20.0"},
        ],
    })
    return rules


def test_buy_bracket_within_pilot_caps():
    rules = _ethusdc_rules()
    entry = Decimal("2713.39")
    plan = plan_pilot_bracket(rules=rules, entry_price=entry, side=OrderSide.BUY)

    assert plan.symbol == "ETHUSDC"
    assert plan.side == OrderSide.BUY
    assert plan.position_side == PositionSide.BOTH
    assert plan.management_mode == "QUICK"
    assert plan.notional_usdc <= DEFAULT_NOTIONAL_CAP_USDC
    assert plan.planned_stop_risk_usdc <= DEFAULT_MAX_STOP_RISK_USDC
    assert plan.total_risk_usdc <= DEFAULT_MAX_STOP_RISK_USDC
    assert plan.planned_net_reward_usdc >= DEFAULT_MIN_NET_REWARD_USDC
    assert plan.stop_loss_price < entry < plan.take_profit_price

    # Verify exchange normalization
    assert rules.normalize_price(plan.stop_loss_price) == plan.stop_loss_price
    assert rules.normalize_price(plan.take_profit_price) == plan.take_profit_price
    assert rules.normalize_quantity(plan.quantity, is_market=True) == plan.quantity


def test_sell_bracket_within_pilot_caps():
    rules = _ethusdc_rules()
    entry = Decimal("2713.39")
    plan = plan_pilot_bracket(rules=rules, entry_price=entry, side=OrderSide.SELL)

    assert plan.symbol == "ETHUSDC"
    assert plan.side == OrderSide.SELL
    assert plan.position_side == PositionSide.BOTH
    assert plan.management_mode == "QUICK"
    assert plan.notional_usdc <= DEFAULT_NOTIONAL_CAP_USDC
    assert plan.planned_stop_risk_usdc <= DEFAULT_MAX_STOP_RISK_USDC
    assert plan.total_risk_usdc <= DEFAULT_MAX_STOP_RISK_USDC
    assert plan.planned_net_reward_usdc >= DEFAULT_MIN_NET_REWARD_USDC
    assert plan.take_profit_price < entry < plan.stop_loss_price

    # Verify exchange normalization
    assert rules.normalize_price(plan.stop_loss_price) == plan.stop_loss_price
    assert rules.normalize_price(plan.take_profit_price) == plan.take_profit_price
    assert rules.normalize_quantity(plan.quantity, is_market=True) == plan.quantity


@pytest.mark.parametrize("price", [
    Decimal("1500.00"),
    Decimal("2250.50"),
    Decimal("2713.39"),
    Decimal("3500.00"),
    Decimal("4800.25"),
])
def test_bracket_across_eth_price_levels(price: Decimal):
    rules = _ethusdc_rules()
    for side in (OrderSide.BUY, OrderSide.SELL):
        plan = plan_pilot_bracket(rules=rules, entry_price=price, side=side)
        assert plan.notional_usdc <= Decimal("50.0")
        assert plan.total_risk_usdc <= Decimal("2.0")
        assert plan.planned_net_reward_usdc >= Decimal("0.25")
        if side == OrderSide.BUY:
            assert plan.stop_loss_price < price < plan.take_profit_price
        else:
            assert plan.take_profit_price < price < plan.stop_loss_price


def test_apply_bracket_to_intent_and_validate_risk_gate():
    rules = _ethusdc_rules()
    entry = Decimal("2713.39")
    plan = plan_pilot_bracket(rules=rules, entry_price=entry, side=OrderSide.BUY)

    raw_intent = OrderIntent(
        client_order_id="TEST-INTENT-1",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.01"),
    )

    bracketed = apply_pilot_bracket_to_intent(raw_intent, plan, basket_id="pilot-basket-123")
    assert bracketed.management_mode == "QUICK"
    assert bracketed.basket_id == "pilot-basket-123"
    assert bracketed.stop_loss_price == plan.stop_loss_price
    assert bracketed.take_profit_price == plan.take_profit_price
    assert bracketed.quantity == plan.quantity

    # Now pass directly to the pure Mainnet risk math validation function
    res = validate_risk_increasing_order(
        bracketed,
        entry_price=entry,
        basket_headroom_usdc=Decimal("125.0"),
        daily_loss_headroom_usdc=Decimal("5.0"),
        current_gross_exposure_usdc=Decimal("0.0"),
        current_basket_exposure_usdc=Decimal("0.0"),
        collateral_usdc=Decimal("250.0"),
        available_balance_usdc=Decimal("200.0"),
        configured_leverage=Decimal("10.0"),
        effective_leverage=Decimal("0.0"),
        active_exposure_chains=0,
        same_active_basket=False,
        is_first_risk_increasing_order=True,
        policy=MAINNET_RISK_POLICY,
        min_net_reward_usdc=Decimal("0.25"),
        min_reward_to_risk=Decimal("0.125"),
    )
    assert res.allowed is True, f"Validation rejected: {res.reason}"
    assert res.notional_usdc <= Decimal("50.0")
    assert res.stop_loss_risk_usdc <= Decimal("2.0")
    assert res.net_reward_usdc >= Decimal("0.25")


def test_fails_closed_on_invalid_rules():
    rules = SymbolTradingRules("BTCUSDT")
    with pytest.raises(ValueError, match="ETHUSDC"):
        plan_pilot_bracket(rules=rules, entry_price=Decimal("2700"), side=OrderSide.BUY)


def test_fails_closed_when_costs_exceed_stop_risk():
    rules = _ethusdc_rules()
    with pytest.raises(ValueError, match="exceed or equal"):
        plan_pilot_bracket(
            rules=rules,
            entry_price=Decimal("2700"),
            side=OrderSide.BUY,
            estimated_fees_usdc=Decimal("1.50"),
            estimated_funding_usdc=Decimal("1.00"),
            max_stop_risk_usdc=Decimal("2.00"),
        )


@pytest.mark.asyncio
async def test_end_to_end_local_mainnet_risk_gate_accepts_pilot_bracket(monkeypatch):
    from apps.trading_worker.venues.binance.gates import _local_mainnet_risk_gate
    from tests.python.test_local_mainnet_gate import make_adapter, make_risk_context, make_cost_evidence

    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")

    rules = _ethusdc_rules()
    entry = Decimal("2713.39")
    plan = plan_pilot_bracket(rules=rules, entry_price=entry, side=OrderSide.BUY)

    intent = OrderIntent(
        client_order_id="local-pilot-e2e-1",
        symbol="ETHUSDC",
        basket_id="basket-1",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=plan.quantity,
        stop_loss_price=plan.stop_loss_price,
        take_profit_price=plan.take_profit_price,
        management_mode=plan.management_mode,
    )

    async def risk_context(_intent, _execution):
        return make_risk_context()

    async def cost_evidence(_intent, _context):
        return make_cost_evidence(
            intent,
            fees_upper_bound_usdc=plan.estimated_fees_usdc,
            funding_upper_bound_usdc=plan.estimated_funding_usdc,
            slippage_upper_bound_usdc=plan.estimated_slippage_usdc,
            funding_interval_hours=Decimal("8"),
            funding_events_assumed=4,
        )

    adapter = make_adapter(
        symbol_rules={"ETHUSDC": rules},
        get_local_mainnet_risk_context=risk_context,
        get_local_mainnet_cost_evidence=cost_evidence,
    )

    gate_result = await _local_mainnet_risk_gate(
        adapter,
        intent,
        entry_price=entry,
        quantity=plan.quantity,
    )
    assert gate_result is None, f"Local Mainnet gate blocked pilot order: {gate_result.reason if gate_result else ''}"


def test_worker_clamp_applies_pilot_bracket_to_unprotected_pilot_order(monkeypatch):
    from apps.trading_worker.main import TradingWorkerApp, WorkerExecutionMode
    from domain.models import ExecutionDecision
    from domain.enums import EconomicRiskClass

    monkeypatch.setenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "test-campaign-123")

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE

    rules = _ethusdc_rules()
    worker.execution_adapter = type("MockAdapter", (), {
        "safety_limits": type("Limits", (), {"max_single_order_notional": Decimal("50.0")})(),
        "symbol_rules": {"ETHUSDC": rules},
        "_is_local_live_pilot_bound": lambda: True,
    })()

    raw_order = OrderIntent(
        client_order_id="raw-1",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("1.0"),
    )
    decision = ExecutionDecision(
        decision_id="dec-1",
        symbol="ETHUSDC",
        action="SUBMIT_ORDER",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[raw_order],
    )

    clamped = worker._clamp_order_notional_if_needed(decision, reference_price=Decimal("2713.39"))
    assert len(clamped.orders) == 1
    bracketed = clamped.orders[0]
    assert bracketed.management_mode == "QUICK"
    assert bracketed.stop_loss_price is not None
    assert bracketed.take_profit_price is not None
    assert bracketed.quantity <= Decimal("0.02")
    assert bracketed.quantity * Decimal("2713.39") <= Decimal("50.0")
