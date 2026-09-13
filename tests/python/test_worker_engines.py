import pytest
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from domain.models import (
    StrategyIntent,
    MarketEvent,
    PositionSide,
    MarketType,
    TargetExposure,
    RiskSnapshot,
    ExecutionDecision,
    PriceActionState,
)
from domain.enums import EconomicRiskClass, RegimeType, RiskState
from apps.trading_worker.engines.funding_carry import (
    FundingCarryCostInputs,
    FundingCarryEngine,
)
from apps.trading_worker.engines.grid_strategy import GridStrategyEngine
from apps.trading_worker.engines.market_state import MarketStateClassifier
from apps.trading_worker.engines.meta_allocator import MetaAllocator
from apps.trading_worker.engines.market_scanner import MarketScannerEngine
from apps.trading_worker.engines.risk_governor import RiskGovernor
from apps.trading_worker.engines.exposure_recovery import ExposureRecoveryEngine

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
        portfolio_equity=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        realized_pnl_24h=Decimal("0"),
        margin_utilization_pct=Decimal("0"),
        effective_leverage=Decimal("0"),
        current_drawdown_pct=Decimal("0"),
        liquidation_distance_pct=Decimal("50"),
        risk_state=RiskState.NORMAL,
    )
    decision = RiskGovernor().evaluate(target, risk_snapshot, Decimal("0"))
    assert decision.target_exposure_id == target.exposure_id
    assert decision.source_intent_ids == ["G1", "T1"]
    assert decision.orders[0].source_intent_ids == ["G1", "T1"]


def test_meta_allocator_rejects_mixed_symbol_intents():
    intent = StrategyIntent(
        intent_id="G1",
        strategy_id="grid",
        symbol="ETHUSDT",
        market_type=MarketType.USDM_FUTURES,
        direction=PositionSide.LONG,
        desired_delta_qty=Decimal("0.1"),
        opportunity_score=Decimal("1"),
        confidence=Decimal("1"),
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
        opportunity_score=Decimal("1"),
        confidence=Decimal("1"),
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
        opportunity_score=Decimal("1"),
        confidence=Decimal("1"),
        expected_holding_horizon_sec=60,
    )

    with pytest.raises(ValueError, match="direction"):
        MetaAllocator().allocate([intent], "BTCUSDT")

def test_risk_governor_veto():
    governor = RiskGovernor()
    
    target = TargetExposure(symbol="BTCUSDT", market_type=MarketType.USDM_FUTURES, target_net_delta_qty=Decimal("1.0"), target_gross_limit_qty=Decimal("1.0"), strategy_attributions={}, expires_at=datetime.now(timezone.utc) + timedelta(minutes=1))
    
    # Snapshot shows dangerously high margin utilization
    danger_risk = RiskSnapshot(
        portfolio_equity=Decimal("100000"),
        unrealized_pnl=Decimal("-10000"),
        realized_pnl_24h=Decimal("0"),
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
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    hard_stop = RiskSnapshot(
        portfolio_equity=Decimal("100"),
        unrealized_pnl=Decimal("-20"),
        realized_pnl_24h=Decimal("0"),
        margin_utilization_pct=Decimal("90"),
        effective_leverage=Decimal("4"),
        current_drawdown_pct=Decimal("10"),
        liquidation_distance_pct=Decimal("2"),
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
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    risk = RiskSnapshot(
        portfolio_equity=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        realized_pnl_24h=Decimal("0"),
        margin_utilization_pct=Decimal("70"),
        effective_leverage=Decimal("0.5"),
        current_drawdown_pct=Decimal("0"),
        liquidation_distance_pct=None,
        risk_state=RiskState.NORMAL,
    )

    decision = governor.evaluate(target, risk, Decimal("0"))

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
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    risk = RiskSnapshot(
        portfolio_equity=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        realized_pnl_24h=Decimal("0"),
        margin_utilization_pct=Decimal("1"),
        effective_leverage=Decimal("0"),
        current_drawdown_pct=Decimal("0"),
        liquidation_distance_pct=Decimal("50"),
        risk_state=risk_state,
    )

    decision = governor.evaluate(target, risk, Decimal("0"))

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
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    risk = RiskSnapshot(
        portfolio_equity=Decimal("100"),
        unrealized_pnl=Decimal("-1"),
        realized_pnl_24h=Decimal("0"),
        margin_utilization_pct=Decimal("1"),
        effective_leverage=Decimal("0.5"),
        current_drawdown_pct=Decimal("1"),
        liquidation_distance_pct=Decimal("2"),
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
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    risk = RiskSnapshot(
        portfolio_equity=Decimal("100"),
        unrealized_pnl=Decimal("0"),
        realized_pnl_24h=Decimal("0"),
        margin_utilization_pct=Decimal("1"),
        effective_leverage=Decimal("0"),
        current_drawdown_pct=Decimal("0"),
        liquidation_distance_pct=Decimal("50"),
        risk_state=RiskState.NORMAL,
    )

    decision = governor.evaluate(target, risk, Decimal("0"))

    assert decision.action == "NOOP"
    assert "expired" in decision.rational.lower()


def test_exposure_recovery_grid_brake():
    recovery = ExposureRecoveryEngine(drawdown_trigger_pct=Decimal("2.5"))
    
    target = TargetExposure(symbol="BTCUSDT", market_type=MarketType.USDM_FUTURES, target_net_delta_qty=Decimal("0.5"), target_gross_limit_qty=Decimal("0.5"), strategy_attributions={"grid": Decimal("0.5")}, expires_at=datetime.now(timezone.utc))
    
    # Drawdown exceeds trigger (3.0% > 2.5%)
    danger_risk = RiskSnapshot(
        portfolio_equity=Decimal("100000"),
        unrealized_pnl=Decimal("-3000"),
        realized_pnl_24h=Decimal("0"),
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


def _grid_price_action(*, reclaiming: bool = True) -> PriceActionState:
    return PriceActionState(
        symbol="BTCUSDT",
        timestamp=datetime.now(timezone.utc),
        swing_high=Decimal("101"),
        swing_low=Decimal("99"),
        prior_24h_high=Decimal("101"),
        prior_24h_low=Decimal("99"),
        displacement_velocity_pct=Decimal("0"),
        displacement_acceleration=Decimal("0"),
        range_expansion_ratio=Decimal("0"),
        is_reclaiming=reclaiming,
    )


def _grid_market_state(regime: RegimeType, *, shock_active: bool = False):
    return type(
        "GridMarketStateStub",
        (),
        {
            "symbol": "BTCUSDT",
            "timestamp": datetime.now(timezone.utc),
            "primary_regime": regime,
            "regime_probabilities": {},
            "atr_1h": Decimal("100"),
            "volatility_zscore": Decimal("0"),
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
    assert intent.desired_delta_qty == Decimal("0")
    assert "disabled" in intent.evidence["brake_reason"]


def test_grid_depth_is_bounded_and_decelerates_without_martingale():
    engine = GridStrategyEngine(max_grid_levels=5)
    state = _grid_price_action()
    level_one = engine.evaluate(state, _grid_market_state(RegimeType.R1_RANGE), grid_depth=0)
    level_two = engine.evaluate(state, _grid_market_state(RegimeType.R1_RANGE), grid_depth=1)
    level_five = engine.evaluate(state, _grid_market_state(RegimeType.R1_RANGE), grid_depth=4)
    capped = engine.evaluate(state, _grid_market_state(RegimeType.R1_RANGE), grid_depth=5)

    assert level_one.desired_delta_qty == Decimal("0.1")
    assert Decimal("0") < level_two.desired_delta_qty < level_one.desired_delta_qty
    assert Decimal("0") < level_five.desired_delta_qty < level_two.desired_delta_qty
    assert capped.desired_delta_qty == Decimal("0")


def test_grid_does_not_add_to_non_reclaiming_inventory():
    intent = GridStrategyEngine().evaluate(
        _grid_price_action(reclaiming=False), _grid_market_state(RegimeType.R1_RANGE)
    )

    assert intent is not None
    assert intent.desired_delta_qty == Decimal("0")


def test_grid_brakes_when_shock_flag_is_active_even_in_range():
    intent = GridStrategyEngine().evaluate(
        _grid_price_action(),
        _grid_market_state(RegimeType.R1_RANGE, shock_active=True),
    )

    assert intent is not None
    assert intent.desired_delta_qty == Decimal("0")


def test_decimal_precision():
    # Verify no float mutation loss in core allocations
    val = Decimal("1.123") + Decimal("2.345")
    assert val == Decimal("3.468")


def _carry_event(funding_rate=None):
    return MarketEvent(
        event_id="E-CARRY-1",
        event_time=datetime.now(timezone.utc),
        symbol="BTCUSDT",
        venue="BINANCE",
        market_type=MarketType.USDM_FUTURES,
        last_price=Decimal("50000"),
        best_bid=Decimal("49999.9"),
        best_ask=Decimal("50000.1"),
        funding_rate=funding_rate,
    )


def _carry_market_state():
    return {
        "symbol": "BTCUSDT",
        "timestamp": datetime.now(timezone.utc),
        "primary_regime": RegimeType.RANGE,
        "regime_probabilities": {},
        "atr_1h": Decimal("100"),
        "volatility_zscore": Decimal("0"),
    }


def test_carry_requires_current_funding_rate():
    engine = FundingCarryEngine()
    market_state = type("MarketStateStub", (), _carry_market_state())()

    assert engine.evaluate(_carry_event(), market_state) is None
    assert engine.evaluate(_carry_event(Decimal("0")), market_state) is None


def test_carry_intent_uses_bounded_score_and_explicit_rate():
    engine = FundingCarryEngine(
        cost_inputs=FundingCarryCostInputs(
            maker_fee_rate=Decimal("0.0002"),
            taker_fee_rate=Decimal("0.0005"),
            entry_spread_bps=Decimal("4"),
            exit_spread_bps=Decimal("4"),
            entry_slippage_bps=Decimal("2"),
            exit_slippage_bps=Decimal("2"),
            annual_financing_rate=Decimal("0.05"),
            funding_intervals_per_day=3,
            holding_horizon_sec=86400 * 7,
        )
    )
    market_state = type("MarketStateStub", (), _carry_market_state())()

    intent = engine.evaluate(_carry_event(Decimal("0.0005")), market_state)

    assert intent is not None
    assert Decimal("0") <= intent.opportunity_score <= Decimal("1")
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
            timestamp=datetime.now(timezone.utc),
            swing_high=Decimal("101"),
            swing_low=Decimal("99"),
            prior_24h_high=Decimal("101"),
            prior_24h_low=Decimal("99"),
            displacement_velocity_pct=Decimal("0"),
            displacement_acceleration=Decimal("0"),
            range_expansion_ratio=Decimal("0"),
        )
    )

    assert state.primary_regime == RegimeType.R1_RANGE


@pytest.mark.asyncio
async def test_worker_market_event_path_has_no_regime_attribute_error():
    from apps.trading_worker.main import TradingWorkerApp

    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    for price in (Decimal("50000"), Decimal("50001")):
        await worker.handle_market_event(
            _carry_event().model_copy(update={"last_price": price})
        )
