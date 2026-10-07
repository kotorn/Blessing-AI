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


@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
@pytest.mark.asyncio
async def test_end_to_end_local_mainnet_risk_gate_accepts_pilot_bracket(monkeypatch, side):
    from apps.trading_worker.venues.binance.gates import _local_mainnet_risk_gate
    from tests.python.test_local_mainnet_gate import make_adapter, make_risk_context, make_cost_evidence

    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")

    rules = _ethusdc_rules()
    entry = Decimal("2713.39")
    # Independent, realistic exchange cost bounds (NOT derived or echoed from plan)
    exchange_fees = Decimal("0.05")
    exchange_funding = Decimal("0.60")
    exchange_slippage = Decimal("0.05")

    plan = plan_pilot_bracket(
        rules=rules,
        entry_price=entry,
        side=side,
        estimated_fees_usdc=exchange_fees,
        estimated_funding_usdc=exchange_funding,
        estimated_slippage_usdc=exchange_slippage,
    )

    intent = OrderIntent(
        client_order_id=f"local-pilot-e2e-{side.value.lower()}",
        symbol="ETHUSDC",
        basket_id="basket-1",
        market_type=MarketType.USDM_FUTURES,
        side=side,
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
            side=side.value,
            fees_upper_bound_usdc=exchange_fees,
            funding_upper_bound_usdc=exchange_funding,
            slippage_upper_bound_usdc=exchange_slippage,
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


@pytest.mark.asyncio
async def test_worker_clamp_applies_pilot_bracket_to_unprotected_pilot_order(monkeypatch):
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

    clamped = await worker._clamp_order_notional_if_needed(decision, reference_price=Decimal("2713.39"))
    assert len(clamped.orders) == 1
    bracketed = clamped.orders[0]
    assert bracketed.management_mode == "QUICK"
    assert bracketed.stop_loss_price is not None
    assert bracketed.take_profit_price is not None
    assert bracketed.quantity <= Decimal("0.02")
    assert bracketed.quantity * Decimal("2713.39") <= Decimal("50.0")


@pytest.mark.asyncio
async def test_worker_clamp_sizes_at_side_price_for_buy_and_sell(monkeypatch):
    from apps.trading_worker.main import TradingWorkerApp, WorkerExecutionMode
    from domain.models import ExecutionDecision
    from domain.enums import EconomicRiskClass

    monkeypatch.setenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "test-campaign-123")

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE

    rules = _ethusdc_rules()
    ask_price = Decimal("2750.00")
    bid_price = Decimal("2700.00")

    async def mock_fresh_price(_self, symbol, side=None):
        if str(side).upper() == "BUY":
            return ask_price
        return bid_price

    worker.execution_adapter = type("MockAdapter", (), {
        "safety_limits": type("Limits", (), {"max_single_order_notional": Decimal("50.0")})(),
        "symbol_rules": {"ETHUSDC": rules},
        "get_fresh_market_price": mock_fresh_price,
        "_is_local_live_pilot_bound": lambda _self: True,
    })()

    for side, expected_price in [(OrderSide.BUY, ask_price), (OrderSide.SELL, bid_price)]:
        raw_order = OrderIntent(
            client_order_id=f"raw-{side.value}",
            symbol="ETHUSDC",
            market_type=MarketType.USDM_FUTURES,
            side=side,
            position_side=PositionSide.BOTH,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.GTC,
            quantity=Decimal("1.0"),
        )
        decision = ExecutionDecision(
            decision_id=f"dec-{side.value}",
            symbol="ETHUSDC",
            action="SUBMIT_ORDER",
            risk_class=EconomicRiskClass.NEW_RISK,
            orders=[raw_order],
        )

        clamped = await worker._clamp_order_notional_if_needed(decision, reference_price=Decimal("2500.00"))
        bracketed = clamped.orders[0]
        # Sizing must satisfy 50 USDC cap at the side price (ask for BUY, bid for SELL)
        assert bracketed.quantity * expected_price <= Decimal("50.0")
        expected_qty = rules.normalize_quantity(Decimal("50.0") / expected_price, is_market=True)
        while expected_qty * expected_price > Decimal("50.0"):
            expected_qty -= rules.step_size
            expected_qty = rules.normalize_quantity(expected_qty, is_market=True)
        assert bracketed.quantity == expected_qty


@pytest.mark.asyncio
async def test_worker_clamp_cost_aware_preplan_with_adapter_evidence(monkeypatch):
    from apps.trading_worker.main import TradingWorkerApp, WorkerExecutionMode
    from domain.models import ExecutionDecision
    from domain.enums import EconomicRiskClass
    from tests.python.test_local_mainnet_gate import make_cost_evidence

    monkeypatch.setenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "test-campaign-123")

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE

    rules = _ethusdc_rules()
    side_price = Decimal("2700.00")
    cost_evidence_called = False

    async def mock_fresh_price(_self, symbol, side=None):
        return side_price

    async def mock_cost_evidence(_self, intent, context):
        nonlocal cost_evidence_called
        cost_evidence_called = True
        return make_cost_evidence(
            intent,
            fees_upper_bound_usdc=Decimal("0.05"),
            funding_upper_bound_usdc=Decimal("0.60"),
            slippage_upper_bound_usdc=Decimal("0.05"),
            funding_interval_hours=Decimal("8"),
            funding_events_assumed=4,
        )

    worker.execution_adapter = type("MockAdapter", (), {
        "safety_limits": type("Limits", (), {"max_single_order_notional": Decimal("50.0")})(),
        "symbol_rules": {"ETHUSDC": rules},
        "get_fresh_market_price": mock_fresh_price,
        "get_local_mainnet_cost_evidence": mock_cost_evidence,
        "_is_local_live_pilot_bound": lambda _self: True,
    })()

    raw_order = OrderIntent(
        client_order_id="raw-preplan-1",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("1.0"),
    )
    decision = ExecutionDecision(
        decision_id="dec-preplan-1",
        symbol="ETHUSDC",
        action="SUBMIT_ORDER",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[raw_order],
    )

    clamped = await worker._clamp_order_notional_if_needed(decision, reference_price=side_price)
    assert cost_evidence_called is True
    bracketed = clamped.orders[0]
    assert bracketed.estimated_fees_usdc == Decimal("0.05")
    assert bracketed.estimated_funding_usdc == Decimal("0.60")
    assert bracketed.estimated_slippage_usdc == Decimal("0.05")
    # Total risk <= 2.0 and net reward >= 0.25
    stop_risk = (side_price - bracketed.stop_loss_price) * bracketed.quantity
    total_risk = stop_risk + Decimal("0.70")
    assert total_risk <= Decimal("2.0")
    net_reward = (bracketed.take_profit_price - side_price) * bracketed.quantity - Decimal("0.70")
    assert net_reward >= Decimal("0.25")


@pytest.mark.asyncio
async def test_worker_clamp_cost_aware_preplan_fails_closed_when_costs_exceed_budget(monkeypatch):
    from apps.trading_worker.main import TradingWorkerApp, WorkerExecutionMode
    from domain.models import ExecutionDecision
    from domain.enums import EconomicRiskClass
    from tests.python.test_local_mainnet_gate import make_cost_evidence

    monkeypatch.setenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "test-campaign-123")

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE

    rules = _ethusdc_rules()
    side_price = Decimal("2700.00")

    async def mock_fresh_price(_self, symbol, side=None):
        return side_price

    async def excessive_cost_evidence(_self, intent, context):
        return make_cost_evidence(
            intent,
            fees_upper_bound_usdc=Decimal("1.50"),
            funding_upper_bound_usdc=Decimal("1.00"),
            slippage_upper_bound_usdc=Decimal("0.50"),
            funding_interval_hours=Decimal("8"),
            funding_events_assumed=4,
        )

    worker.execution_adapter = type("MockAdapter", (), {
        "safety_limits": type("Limits", (), {"max_single_order_notional": Decimal("50.0")})(),
        "symbol_rules": {"ETHUSDC": rules},
        "get_fresh_market_price": mock_fresh_price,
        "get_local_mainnet_cost_evidence": excessive_cost_evidence,
        "_is_local_live_pilot_bound": lambda _self: True,
    })()

    raw_order = OrderIntent(
        client_order_id="raw-excess-1",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("1.0"),
    )
    decision = ExecutionDecision(
        decision_id="dec-excess-1",
        symbol="ETHUSDC",
        action="SUBMIT_ORDER",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[raw_order],
    )

    with pytest.raises(RuntimeError, match="exceed or equal"):
        await worker._clamp_order_notional_if_needed(decision, reference_price=side_price)


@pytest.mark.asyncio
async def test_worker_clamp_missing_side_price_fails_closed(monkeypatch):
    from apps.trading_worker.main import TradingWorkerApp, WorkerExecutionMode
    from domain.models import ExecutionDecision
    from domain.enums import EconomicRiskClass

    monkeypatch.setenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "test-campaign-123")

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE

    rules = _ethusdc_rules()

    async def mock_none_price(_self, symbol, side=None):
        return None

    worker.execution_adapter = type("MockAdapter", (), {
        "safety_limits": type("Limits", (), {"max_single_order_notional": Decimal("50.0")})(),
        "symbol_rules": {"ETHUSDC": rules},
        "get_fresh_market_price": mock_none_price,
        "_is_local_live_pilot_bound": lambda _self: True,
    })()

    raw_order = OrderIntent(
        client_order_id="raw-missing-price-1",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("1.0"),
    )
    decision = ExecutionDecision(
        decision_id="dec-missing-price-1",
        symbol="ETHUSDC",
        action="SUBMIT_ORDER",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[raw_order],
    )

    with pytest.raises(RuntimeError, match="unavailable"):
        await worker._clamp_order_notional_if_needed(decision, reference_price=None)


@pytest.mark.asyncio
async def test_worker_clamp_cost_aware_preplan_quantity_parity_failure_fails_closed(monkeypatch):
    from apps.trading_worker.main import TradingWorkerApp, WorkerExecutionMode
    from domain.models import ExecutionDecision
    from domain.enums import EconomicRiskClass
    from tests.python.test_local_mainnet_gate import make_cost_evidence
    import apps.trading_worker.main as main_module
    from dataclasses import replace

    monkeypatch.setenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "test-campaign-123")

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE

    rules = _ethusdc_rules()
    side_price = Decimal("2700.00")

    async def mock_fresh_price(_self, symbol, side=None):
        return side_price

    async def mock_cost_evidence(_self, intent, context):
        return make_cost_evidence(
            intent,
            fees_upper_bound_usdc=Decimal("0.05"),
            funding_upper_bound_usdc=Decimal("0.60"),
            slippage_upper_bound_usdc=Decimal("0.05"),
            funding_interval_hours=Decimal("8"),
            funding_events_assumed=4,
        )

    worker.execution_adapter = type("MockAdapter", (), {
        "safety_limits": type("Limits", (), {"max_single_order_notional": Decimal("50.0")})(),
        "symbol_rules": {"ETHUSDC": rules},
        "get_fresh_market_price": mock_fresh_price,
        "get_local_mainnet_cost_evidence": mock_cost_evidence,
        "_is_local_live_pilot_bound": lambda _self: True,
    })()

    raw_order = OrderIntent(
        client_order_id="raw-qty-parity-1",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("1.0"),
    )
    decision = ExecutionDecision(
        decision_id="dec-qty-parity-1",
        symbol="ETHUSDC",
        action="SUBMIT_ORDER",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[raw_order],
    )

    real_plan_bracket = main_module.plan_pilot_bracket
    calls = 0

    def mock_plan_bracket(*args, **kwargs):
        nonlocal calls
        calls += 1
        plan = real_plan_bracket(*args, **kwargs)
        if calls == 2:
            return replace(plan, quantity=Decimal("0.010"))
        return plan

    monkeypatch.setattr(main_module, "plan_pilot_bracket", mock_plan_bracket)

    with pytest.raises(RuntimeError, match="quantity parity mismatch"):
        await worker._clamp_order_notional_if_needed(decision, reference_price=side_price)

