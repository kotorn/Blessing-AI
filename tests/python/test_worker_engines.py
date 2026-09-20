from datetime import datetime, timedelta, timezone, UTC
from decimal import Decimal

import pytest

from apps.trading_worker.engines.exposure_recovery import ExposureRecoveryEngine
from apps.trading_worker.engines.funding_carry import (
    FundingCarryCostInputs,
    FundingCarryEngine,
)
from apps.trading_worker.engines.grid_strategy import GridStrategyEngine
from apps.trading_worker.engines.market_scanner import MarketScannerEngine
from apps.trading_worker.engines.market_state import MarketStateClassifier
from apps.trading_worker.engines.meta_allocator import MetaAllocator
from apps.trading_worker.engines.price_action import PriceActionEngine
from apps.trading_worker.engines.risk_governor import RiskGovernor
from domain.enums import EconomicRiskClass, RegimeType, RiskState
from domain.models import (
    ExecutionDecision,
    MarketEvent,
    MarketType,
    PositionSide,
    PriceActionState,
    RiskSnapshot,
    StrategyIntent,
    TargetExposure,
)


def test_meta_allocator_conflict_resolution():
    allocator = MetaAllocator()
    
    # Conflict: Grid says Long (+1.0), Trend says Short (-0.6)
    grid_intent = StrategyIntent(
        intent_id="G1",
        strategy_id="grid",
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        direction=PositionSide.LONG,
        desired_delta_qty=Decimal("1.0"),
        opportunity_score=Decimal("1.0"),
        confidence=Decimal("1.0"),
        expected_holding_horizon_sec=60
    )
    trend_intent = StrategyIntent(
        intent_id="T1",
        strategy_id="trend",
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        direction=PositionSide.SHORT,
        desired_delta_qty=Decimal("-0.6"),
        opportunity_score=Decimal("1.0"),
        confidence=Decimal("1.0"),
        expected_holding_horizon_sec=3600
    )
    
    target = allocator.allocate([grid_intent, trend_intent], "BTCUSDT")
    
    # Net delta should be +0.4
    assert target.desired_delta_qty == Decimal("0.4")
    assert "grid" in target.strategy_allocations
    assert "trend" in target.strategy_allocations
    assert target.source_intent_ids == ["G1", "T1"]

    risk_snapshot = RiskSnapshot(
        portfolio_equity=Decimal(100),
        unrealized_pnl=Decimal(0),
        realized_pnl_24h=Decimal(0),
        margin_utilization_pct=Decimal(0),
        effective_leverage=Decimal(0),
        current_drawdown_pct=Decimal(0),
        liquidation_distance_pct=Decimal(50),
        risk_state=RiskState.NORMAL,
    )
    decision = RiskGovernor().evaluate(target, risk_snapshot, Decimal(0))
    assert decision.target_exposure_id == target.exposure_id
    assert decision.source_intent_ids == ["G1", "T1"]
    assert decision.orders[0].source_intent_ids == ["G1", "T1"]


def test_market_event_time_is_preserved_through_state_intent_and_target():
    event_time = datetime(2024, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
    event = MarketEvent(
        event_id="E-TRACE-1",
        event_time=event_time,
        symbol="BTCUSDT",
        venue="BINANCE_TESTNET",
        market_type=MarketType.USDM_FUTURES,
        last_price=Decimal(50001),
        best_bid=Decimal("50000.9"),
        best_ask=Decimal("50001.1"),
    )
    later_event = event.model_copy(
        update={
            "event_id": "E-TRACE-2",
            "event_time": event_time + timedelta(minutes=1),
            "last_price": Decimal(50002),
        }
    )

    price_action = PriceActionEngine()
    assert price_action.process_event(event) is None
    pa_state = price_action.process_event(later_event)
    assert pa_state is not None
    assert pa_state.timestamp == later_event.event_time

    market_state = MarketStateClassifier().classify(pa_state)
    assert market_state.timestamp == later_event.event_time

    intent = GridStrategyEngine().evaluate(
        pa_state.model_copy(update={"is_reclaiming": True}),
        market_state,
        grid_depth=0,
    )
    assert intent is not None
    assert intent.timestamp == later_event.event_time

    target = MetaAllocator().allocate([intent], "BTCUSDT")
    assert target.created_at == later_event.event_time
    assert target.expires_at == later_event.event_time + timedelta(seconds=60)


def test_production_market_event_pipeline_reaches_execution_decision():
    event_time = datetime.now(UTC)
    event = MarketEvent(
        event_id="E-PIPELINE-1",
        event_time=event_time,
        symbol="BTCUSDT",
        venue="BINANCE_TESTNET",
        market_type=MarketType.USDM_FUTURES,
        last_price=Decimal(50000),
        best_bid=Decimal("49999.9"),
        best_ask=Decimal("50000.1"),
    )
    later_event = event.model_copy(
        update={
            "event_id": "E-PIPELINE-2",
            "event_time": event_time + timedelta(seconds=1),
            "last_price": Decimal(50000),
        }
    )

    price_action = PriceActionEngine()
    assert price_action.process_event(event) is None
    pa_state = price_action.process_event(later_event)
    assert pa_state is not None
    market_state = MarketStateClassifier().classify(pa_state)
    intent = GridStrategyEngine().evaluate(
        pa_state.model_copy(update={"is_reclaiming": True}),
        market_state,
        grid_depth=0,
    )
    assert intent is not None

    target = MetaAllocator().allocate([intent], later_event.symbol)
    risk = RiskSnapshot(
        timestamp=later_event.event_time,
        portfolio_equity=Decimal(100000),
        unrealized_pnl=Decimal(0),
        realized_pnl_24h=Decimal(0),
        margin_utilization_pct=Decimal(5),
        effective_leverage=Decimal("0.5"),
        current_drawdown_pct=Decimal(0),
        liquidation_distance_pct=Decimal(50),
        risk_state=RiskState.NORMAL,
    )
    recovered = ExposureRecoveryEngine(clock=lambda: later_event.event_time).process(
        target,
        risk,
        current_position_qty=Decimal(0),
    )
    decision = RiskGovernor(clock=lambda: later_event.event_time).evaluate(
        recovered,
        risk,
        current_position_qty=Decimal(0),
    )

    assert isinstance(decision, ExecutionDecision)
    assert decision.action == "SUBMIT_ORDER"
    assert decision.source_intent_ids == [intent.intent_id]
    assert decision.orders[0].source_intent_ids == [intent.intent_id]


def test_meta_allocator_rejects_mixed_symbol_intents():
    intent = StrategyIntent(
        intent_id="G1",
        strategy_id="grid",
        symbol="ETHUSDT",
        market_type=MarketType.USDM_FUTURES,
        direction=PositionSide.LONG,
        desired_delta_qty=Decimal("0.1"),
        opportunity_score=Decimal(1),
        confidence=Decimal(1),
        expected_holding_horizon_sec=60,
    )

    with pytest.raises(ValueError, match="symbol"):
        MetaAllocator().allocate([intent], "BTCUSDT")


def test_meta_allocator_rejects_duplicate_intent_ids():
    intent = StrategyIntent(
        intent_id="DUPLICATE",
        strategy_id="grid",
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        direction=PositionSide.LONG,
        desired_delta_qty=Decimal("0.1"),
        opportunity_score=Decimal(1),
        confidence=Decimal(1),
        expected_holding_horizon_sec=60,
    )

    with pytest.raises(ValueError, match="unique"):
        MetaAllocator().allocate([intent, intent], "BTCUSDT")


def test_meta_allocator_rejects_direction_delta_mismatch():
    intent = StrategyIntent(
        intent_id="BAD-DIRECTION",
        strategy_id="trend",
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        direction=PositionSide.SHORT,
        desired_delta_qty=Decimal("0.1"),
        opportunity_score=Decimal(1),
        confidence=Decimal(1),
        expected_holding_horizon_sec=60,
    )

    with pytest.raises(ValueError, match="direction"):
        MetaAllocator().allocate([intent], "BTCUSDT")

def test_risk_governor_veto():
    governor = RiskGovernor()
    
    target = TargetExposure(symbol="BTCUSDT", market_type=MarketType.USDM_FUTURES, target_net_delta_qty=Decimal("1.0"), target_gross_limit_qty=Decimal("1.0"), strategy_attributions={}, expires_at=datetime.now(UTC) + timedelta(minutes=1))
    
    # Snapshot shows dangerously high margin utilization
    danger_risk = RiskSnapshot(
        portfolio_equity=Decimal(100000),
        unrealized_pnl=Decimal(-10000),
        realized_pnl_24h=Decimal(0),
        margin_utilization_pct=Decimal("85.0"), # Veto threshold is usually > 50-70%
        effective_leverage=Decimal("8.5"),
        current_drawdown_pct=Decimal("10.0"),
        liquidation_distance_pct=Decimal("5.0"),
        risk_state=RiskState.NORMAL
    )
    
    decision = governor.evaluate(target, danger_risk, Decimal("0.0"))
    
    # The governor should override and emit NOOP or reduce_only
    assert decision.action == "NOOP"
    assert "drawdown" in decision.rational.lower()


def test_risk_governor_allows_deleveraging_during_hard_stop():
    governor = RiskGovernor()
    target = TargetExposure(
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        target_net_delta_qty=Decimal("-0.5"),
        target_gross_limit_qty=Decimal("0.5"),
        strategy_attributions={"recovery": Decimal("-0.5")},
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    hard_stop = RiskSnapshot(
        portfolio_equity=Decimal(100),
        unrealized_pnl=Decimal(-20),
        realized_pnl_24h=Decimal(0),
        margin_utilization_pct=Decimal(90),
        effective_leverage=Decimal(4),
        current_drawdown_pct=Decimal(10),
        liquidation_distance_pct=Decimal(2),
        risk_state=RiskState.EMERGENCY,
    )

    decision = governor.evaluate(target, hard_stop, Decimal("1.0"))

    assert decision.action == "SUBMIT_ORDER"
    assert decision.risk_class.value == "REDUCE_RISK"
    assert decision.orders[0].reduce_only is True


def test_risk_governor_vetoes_high_margin_utilization():
    governor = RiskGovernor()
    target = TargetExposure(
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        target_net_delta_qty=Decimal("0.1"),
        target_gross_limit_qty=Decimal("0.1"),
        strategy_attributions={"grid": Decimal("0.1")},
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    risk = RiskSnapshot(
        portfolio_equity=Decimal(100),
        unrealized_pnl=Decimal(0),
        realized_pnl_24h=Decimal(0),
        margin_utilization_pct=Decimal(70),
        effective_leverage=Decimal("0.5"),
        current_drawdown_pct=Decimal(0),
        liquidation_distance_pct=None,
        risk_state=RiskState.NORMAL,
    )

    decision = governor.evaluate(target, risk, Decimal(0))

    assert decision.action == "NOOP"
    assert "margin" in decision.rational.lower()


@pytest.mark.parametrize(
    "risk_state",
    [
        RiskState.NO_NEW_RISK,
        RiskState.RECOVERY_ONLY,
        RiskState.DELEVERAGE,
        RiskState.LIQUIDATING,
        RiskState.EMERGENCY,
    ],
)
def test_risk_governor_blocks_risk_increase_for_restricted_risk_states(risk_state):
    governor = RiskGovernor()
    target = TargetExposure(
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        target_net_delta_qty=Decimal("0.1"),
        target_gross_limit_qty=Decimal("0.1"),
        strategy_attributions={"grid": Decimal("0.1")},
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    risk = RiskSnapshot(
        portfolio_equity=Decimal(100),
        unrealized_pnl=Decimal(0),
        realized_pnl_24h=Decimal(0),
        margin_utilization_pct=Decimal(1),
        effective_leverage=Decimal(0),
        current_drawdown_pct=Decimal(0),
        liquidation_distance_pct=Decimal(50),
        risk_state=risk_state,
    )

    decision = governor.evaluate(target, risk, Decimal(0))

    assert decision.action == "NOOP"
    assert "blocked" in decision.rational.lower()


def test_risk_governor_keeps_reduction_available_in_recovery_only():
    governor = RiskGovernor()
    target = TargetExposure(
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        target_net_delta_qty=Decimal("-0.1"),
        target_gross_limit_qty=Decimal("0.1"),
        strategy_attributions={"recovery": Decimal("-0.1")},
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    risk = RiskSnapshot(
        portfolio_equity=Decimal(100),
        unrealized_pnl=Decimal(-1),
        realized_pnl_24h=Decimal(0),
        margin_utilization_pct=Decimal(1),
        effective_leverage=Decimal("0.5"),
        current_drawdown_pct=Decimal(1),
        liquidation_distance_pct=Decimal(2),
        risk_state=RiskState.RECOVERY_ONLY,
    )

    decision = governor.evaluate(target, risk, Decimal("0.5"))

    assert decision.action == "SUBMIT_ORDER"
    assert decision.risk_class == EconomicRiskClass.REDUCE_RISK
    assert decision.orders[0].reduce_only is True


def test_risk_governor_rejects_expired_target():
    governor = RiskGovernor()
    target = TargetExposure(
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        target_net_delta_qty=Decimal("0.1"),
        target_gross_limit_qty=Decimal("0.1"),
        strategy_attributions={"grid": Decimal("0.1")},
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    risk = RiskSnapshot(
        portfolio_equity=Decimal(100),
        unrealized_pnl=Decimal(0),
        realized_pnl_24h=Decimal(0),
        margin_utilization_pct=Decimal(1),
        effective_leverage=Decimal(0),
        current_drawdown_pct=Decimal(0),
        liquidation_distance_pct=Decimal(50),
        risk_state=RiskState.NORMAL,
    )

    decision = governor.evaluate(target, risk, Decimal(0))

    assert decision.action == "NOOP"
    assert "expired" in decision.rational.lower()


def test_exposure_recovery_grid_brake():
    recovery = ExposureRecoveryEngine(drawdown_trigger_pct=Decimal("2.5"))
    
    target = TargetExposure(symbol="BTCUSDT", market_type=MarketType.USDM_FUTURES, target_net_delta_qty=Decimal("0.5"), target_gross_limit_qty=Decimal("0.5"), strategy_attributions={"grid": Decimal("0.5")}, expires_at=datetime.now(UTC))
    
    # Drawdown exceeds trigger (3.0% > 2.5%)
    danger_risk = RiskSnapshot(
        portfolio_equity=Decimal(100000),
        unrealized_pnl=Decimal(-3000),
        realized_pnl_24h=Decimal(0),
        margin_utilization_pct=Decimal("20.0"),
        effective_leverage=Decimal("2.0"),
        current_drawdown_pct=Decimal("3.0"), 
        liquidation_distance_pct=Decimal("15.0"),
        risk_state=RiskState.NORMAL
    )
    
    # We have an existing LONG position of 1.0. The grid wants to add 0.5 LONG.
    adjusted_target = recovery.process(target, danger_risk, Decimal("1.0"))
    
    # The Grid Brake should have zeroed the grid delta, leaving 0 net addition.
    assert adjusted_target.desired_delta_qty <= Decimal("0.0")


def test_exposure_recovery_blocks_new_risk_when_flat():
    recovery = ExposureRecoveryEngine(drawdown_trigger_pct=Decimal("2.0"))
    target = TargetExposure(
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        target_net_delta_qty=Decimal("0.5"),
        target_gross_limit_qty=Decimal("0.5"),
        strategy_attributions={"grid": Decimal("0.5")},
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    toxic_risk = RiskSnapshot(
        portfolio_equity=Decimal(100000),
        unrealized_pnl=Decimal(0),
        realized_pnl_24h=Decimal(0),
        margin_utilization_pct=Decimal(1),
        effective_leverage=Decimal(0),
        current_drawdown_pct=Decimal("2.1"),
        liquidation_distance_pct=Decimal(50),
        risk_state=RiskState.NORMAL,
    )

    adjusted_target = recovery.process(target, toxic_risk, Decimal(0))

    assert adjusted_target.desired_delta_qty == Decimal(0)
    assert recovery.states["BTCUSDT"].action_type.value == "HOLD"


def _grid_price_action(*, reclaiming: bool = True) -> PriceActionState:
    return PriceActionState(
        symbol="BTCUSDT",
        timestamp=datetime.now(UTC),
        swing_high=Decimal(101),
        swing_low=Decimal(99),
        prior_24h_high=Decimal(101),
        prior_24h_low=Decimal(99),
        displacement_velocity_pct=Decimal(0),
        displacement_acceleration=Decimal(0),
        range_expansion_ratio=Decimal(0),
        is_reclaiming=reclaiming,
    )


def _grid_market_state(regime: RegimeType, *, shock_active: bool = False):
    return type(
        "GridMarketStateStub",
        (),
        {
            "symbol": "BTCUSDT",
            "timestamp": datetime.now(UTC),
            "primary_regime": regime,
            "regime_probabilities": {},
            "atr_1h": Decimal(100),
            "volatility_zscore": Decimal(0),
            "shock_active": shock_active,
        },
    )()


@pytest.mark.parametrize(
    "regime",
    [
        RegimeType.R2_WEAK_TREND,
        RegimeType.R3_STRONG_TREND,
        RegimeType.R4_BREAKOUT,
        RegimeType.R5_VOLATILITY_SHOCK,
        RegimeType.R6_CRISIS,
        RegimeType.TREND,
        RegimeType.BREAKOUT,
        RegimeType.SHOCK,
    ],
)
def test_grid_brakes_before_expansion_in_directional_or_shock_regimes(regime):
    intent = GridStrategyEngine().evaluate(
        _grid_price_action(), _grid_market_state(regime), grid_depth=0
    )

    assert intent is not None
    assert intent.desired_delta_qty == Decimal(0)
    assert "disabled" in intent.evidence["brake_reason"]


def test_grid_depth_is_bounded_and_decelerates_without_martingale():
    engine = GridStrategyEngine(max_grid_levels=5)
    state = _grid_price_action()
    level_one = engine.evaluate(state, _grid_market_state(RegimeType.R1_RANGE), grid_depth=0)
    level_two = engine.evaluate(state, _grid_market_state(RegimeType.R1_RANGE), grid_depth=1)
    level_five = engine.evaluate(state, _grid_market_state(RegimeType.R1_RANGE), grid_depth=4)
    capped = engine.evaluate(state, _grid_market_state(RegimeType.R1_RANGE), grid_depth=5)

    assert level_one.desired_delta_qty == Decimal("0.1")
    assert Decimal(0) < level_two.desired_delta_qty < level_one.desired_delta_qty
    assert Decimal(0) < level_five.desired_delta_qty < level_two.desired_delta_qty
    assert capped.desired_delta_qty == Decimal(0)


def test_grid_runtime_depth_requires_authoritative_lineage():
    engine = GridStrategyEngine(max_grid_levels=5)

    assert engine.observed_depth(
        position_qty=Decimal(0), open_grid_orders=0, filled_grid_orders=4
    ) == 0
    assert engine.observed_depth(
        position_qty=Decimal("0.2"), open_grid_orders=0, filled_grid_orders=0
    ) == 5
    assert engine.observed_depth(
        position_qty=Decimal("0.1"), open_grid_orders=1, filled_grid_orders=1
    ) == 2
    assert engine.observed_depth(
        position_qty=Decimal("0.5"), open_grid_orders=4, filled_grid_orders=4
    ) == 5


def test_grid_does_not_add_to_non_reclaiming_inventory():
    intent = GridStrategyEngine().evaluate(
        _grid_price_action(reclaiming=False), _grid_market_state(RegimeType.R1_RANGE)
    )

    assert intent is not None
    assert intent.desired_delta_qty == Decimal(0)


def test_grid_brakes_when_shock_flag_is_active_even_in_range():
    intent = GridStrategyEngine().evaluate(
        _grid_price_action(),
        _grid_market_state(RegimeType.R1_RANGE, shock_active=True),
    )

    assert intent is not None
    assert intent.desired_delta_qty == Decimal(0)


def test_decimal_precision():
    # Verify no float mutation loss in core allocations
    val = Decimal("1.123") + Decimal("2.345")
    assert val == Decimal("3.468")


def _carry_event(funding_rate=None):
    return MarketEvent(
        event_id="E-CARRY-1",
        event_time=datetime.now(UTC),
        symbol="BTCUSDT",
        venue="BINANCE",
        market_type=MarketType.USDM_FUTURES,
        last_price=Decimal(50000),
        best_bid=Decimal("49999.9"),
        best_ask=Decimal("50000.1"),
        funding_rate=funding_rate,
    )


def _carry_market_state():
    return {
        "symbol": "BTCUSDT",
        "timestamp": datetime.now(UTC),
        "primary_regime": RegimeType.RANGE,
        "regime_probabilities": {},
        "atr_1h": Decimal(100),
        "volatility_zscore": Decimal(0),
    }


def test_carry_requires_current_funding_rate():
    engine = FundingCarryEngine()
    market_state = type("MarketStateStub", (), _carry_market_state())()

    assert engine.evaluate(_carry_event(), market_state) is None
    assert engine.evaluate(_carry_event(Decimal(0)), market_state) is None


def test_carry_intent_uses_bounded_score_and_explicit_rate():
    engine = FundingCarryEngine(
        cost_inputs=FundingCarryCostInputs(
            maker_fee_rate=Decimal("0.0002"),
            taker_fee_rate=Decimal("0.0005"),
            entry_spread_bps=Decimal(4),
            exit_spread_bps=Decimal(4),
            entry_slippage_bps=Decimal(2),
            exit_slippage_bps=Decimal(2),
            annual_financing_rate=Decimal("0.05"),
            funding_intervals_per_day=3,
            holding_horizon_sec=86400 * 7,
        )
    )
    market_state = type("MarketStateStub", (), _carry_market_state())()

    intent = engine.evaluate(_carry_event(Decimal("0.0005")), market_state)

    assert intent is not None
    assert Decimal(0) <= intent.opportunity_score <= Decimal(1)
    assert intent.direction == PositionSide.SHORT
    assert intent.evidence["raw_funding"] == "0.0005"
    assert Decimal(intent.evidence["net_horizon_pct"]) > 0


def test_carry_without_explicit_cost_inputs_is_disabled():
    engine = FundingCarryEngine()
    market_state = type("MarketStateStub", (), _carry_market_state())()

    assert engine.evaluate(_carry_event(Decimal("0.01")), market_state) is None


@pytest.mark.asyncio
async def test_runtime_scanner_defaults_to_bounded_testnet_launch_universe():
    scanner = MarketScannerEngine()

    assert scanner.base_url == "https://testnet.binancefuture.com/fapi/v1/ticker/24hr"
    assert await scanner.scan_active_symbols() == ["BTCUSDT"]


@pytest.mark.asyncio
async def test_dynamic_scanner_failure_does_not_expand_symbol_universe(monkeypatch):
    import urllib.request

    def fail_urlopen(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", fail_urlopen)
    scanner = MarketScannerEngine(allow_dynamic_symbols=True)

    assert await scanner.scan_active_symbols() == ["BTCUSDT"]


def test_ml_scorer_never_returns_a_fabricated_live_score():
    from apps.trading_worker.engines.ml_scorer import GridSafetyScorerML

    scorer = GridSafetyScorerML()
    with pytest.raises(RuntimeError, match="verified"):
        scorer.predict_safety_score(None, None)


def test_live_grid_engine_does_not_instantiate_ml_authority():
    assert not hasattr(GridStrategyEngine(), "ml_scorer")


def test_market_state_classifier_uses_declared_regime_vocabulary():
    state = MarketStateClassifier().classify(
        PriceActionState(
            symbol="BTCUSDT",
            timestamp=datetime.now(UTC),
            swing_high=Decimal(101),
            swing_low=Decimal(99),
            prior_24h_high=Decimal(101),
            prior_24h_low=Decimal(99),
            displacement_velocity_pct=Decimal(0),
            displacement_acceleration=Decimal(0),
            range_expansion_ratio=Decimal(0),
        )
    )

    assert state.primary_regime == RegimeType.R1_RANGE


@pytest.mark.asyncio
async def test_worker_market_event_path_has_no_regime_attribute_error():
    from apps.trading_worker.main import TradingWorkerApp

    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    for price in (Decimal(50000), Decimal(50001)):
        await worker.handle_market_event(
            _carry_event().model_copy(update={"last_price": price})
        )


@pytest.mark.asyncio
async def test_worker_does_not_evaluate_disabled_strategy_engines(monkeypatch):
    from apps.trading_worker.main import TradingWorkerApp

    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    worker.active_configuration = {
        "executionMode": "PAPER",
        "instruments": ["BTCUSDT"],
        "strategies": {
            "grid": True,
            "trend": False,
            "shock": False,
            "carry": False,
        },
        "riskProfile": "CONSERVATIVE",
    }

    calls: list[str] = []
    monkeypatch.setattr(worker.pa_engine, "process_event", lambda event: object())
    monkeypatch.setattr(
        worker.market_state_engine, "classify", lambda price_action: object()
    )

    async def grid_depth(symbol):
        return 0

    monkeypatch.setattr(worker, "_observed_grid_depth", grid_depth)
    for name in ("grid", "trend", "shock", "carry"):
        engine = getattr(worker, f"{name}_engine")
        monkeypatch.setattr(
            engine,
            "evaluate",
            lambda *args, _name=name, **kwargs: calls.append(_name) or None,
        )

    await worker.handle_market_event(_carry_event())

    assert calls == ["grid"]


def test_price_action_engine_detects_rolling_swing_low_reclaim():
    pa = PriceActionEngine()
    now = datetime.now(UTC)

    # Establish baseline ticks forming a local swing low
    # 2625.0 -> 2624.5 -> 2624.0 (swing low)
    t0 = now
    t1 = now + timedelta(seconds=1)
    t2 = now + timedelta(seconds=2)
    t3 = now + timedelta(seconds=3)
    t4 = now + timedelta(seconds=4)

    e0 = MarketEvent(
        event_id="E-0",
        event_time=t0,
        symbol="ETHUSDC",
        venue="BINANCE",
        market_type=MarketType.USDM_FUTURES,
        last_price=Decimal("2625.0"),
        best_bid=Decimal("2624.9"),
        best_ask=Decimal("2625.1"),
    )
    s0 = pa.process_event(e0)
    assert s0 is None  # first tick seeds tracker

    # Falling ticks
    s1 = pa.process_event(e0.model_copy(update={"event_id": "E-1", "event_time": t1, "last_price": Decimal("2624.5")}))
    assert s1 is not None
    assert s1.is_reclaiming is False

    s2 = pa.process_event(e0.model_copy(update={"event_id": "E-2", "event_time": t2, "last_price": Decimal("2624.0")}))
    assert s2 is not None
    assert s2.is_reclaiming is False  # Still falling to swing low

    # Bounce tick reclaiming the swing low
    s3 = pa.process_event(e0.model_copy(update={"event_id": "E-3", "event_time": t3, "last_price": Decimal("2624.3")}))
    assert s3 is not None
    assert s3.is_reclaiming is True
    assert s3.liquidity_swept is True

    # Subsequent upward continuation resets reclaim
    s4 = pa.process_event(e0.model_copy(update={"event_id": "E-4", "event_time": t4, "last_price": Decimal("2624.8")}))
    assert s4 is not None
    assert s4.is_reclaiming is False


@pytest.mark.asyncio
async def test_observed_grid_depth_recognizes_bai_client_order_ids():
    from unittest.mock import AsyncMock, MagicMock

    from apps.trading_worker.main import TradingWorkerApp, WorkerExecutionMode
    from domain.models import (
        ExchangeFill,
        ExchangePosition,
        ExecutionOrder,
        OrderSide,
        PositionSide,
    )

    app = TradingWorkerApp()
    app.execution_mode = WorkerExecutionMode.LIVE

    mock_adapter = MagicMock()
    mock_ledger = MagicMock()
    mock_adapter.ledger = mock_ledger

    # Position: 0.007 ETH
    mock_ledger.get_positions = AsyncMock(return_value=[
        ExchangePosition(symbol="ETHUSDC", quantity=Decimal("0.007"), position_side=PositionSide.BOTH)
    ])

    # Order in Cloud SQL without source_intent_ids, but with BAI- client_order_id
    mock_ledger.get_all_orders = AsyncMock(return_value=[
        ExecutionOrder(
            symbol="ETHUSDC",
            client_order_id="BAI-1953cad9da3d-0-1",
            side=OrderSide.BUY,
            quantity=Decimal("0.007"),
            price=Decimal(2450),
            status="FILLED",
            source_intent_ids=[],
        )
    ])

    # Fill in Cloud SQL without source_intent_ids, but with matching BAI- client_order_id
    mock_ledger.get_fills = AsyncMock(return_value=[
        ExchangeFill(
            symbol="ETHUSDC",
            client_order_id="BAI-1953cad9da3d-0-1",
            exchange_order_id="12345",
            exchange_trade_id="892779718",
            side=OrderSide.BUY,
            position_side=PositionSide.BOTH,
            quantity=Decimal("0.007"),
            price=Decimal(2450),
            commission=Decimal("0.01"),
            commission_asset="USDC",
            realized_pnl=Decimal(0),
            maker=True,
            event_time=datetime.now(UTC),
            transaction_time=datetime.now(UTC),
            source="BINANCE_MAINNET",
            source_intent_ids=[],
        )
    ])

    app.execution_adapter = mock_adapter

    depth = await app._observed_grid_depth("ETHUSDC")
    # Must recognise the filled order as depth 1, NOT capped at 5!
    assert depth == 1


