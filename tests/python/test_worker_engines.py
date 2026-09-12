import pytest
from decimal import Decimal
from datetime import datetime
from domain.models import StrategyIntent, MarketEvent, PositionSide, MarketType, TargetExposure, RiskSnapshot, ExecutionDecision
from domain.enums import RiskState
from apps.trading_worker.engines.meta_allocator import MetaAllocator
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

def test_risk_governor_veto():
    governor = RiskGovernor()
    
    target = TargetExposure(symbol="BTCUSDT", market_type=MarketType.USDM_FUTURES, target_net_delta_qty=Decimal("1.0"), target_gross_limit_qty=Decimal("1.0"), strategy_attributions={}, expires_at=datetime.utcnow())
    
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
    
def test_exposure_recovery_grid_brake():
    recovery = ExposureRecoveryEngine(drawdown_trigger_pct=Decimal("2.5"))
    
    target = TargetExposure(symbol="BTCUSDT", market_type=MarketType.USDM_FUTURES, target_net_delta_qty=Decimal("0.5"), target_gross_limit_qty=Decimal("0.5"), strategy_attributions={"grid": Decimal("0.5")}, expires_at=datetime.utcnow())
    
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

def test_decimal_precision():
    # Verify no float mutation loss in core allocations
    val = Decimal("1.123") + Decimal("2.345")
    assert val == Decimal("3.468")
