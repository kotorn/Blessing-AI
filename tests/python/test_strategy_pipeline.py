import pytest
from decimal import Decimal
from datetime import datetime, timezone
UTC = timezone.utc
from domain.models import PriceActionState, MarketState, RegimeType
from apps.trading_worker.strategies.structural_grid import StructuralGridEngine
from apps.trading_worker.strategies.trend_breakout import TrendBreakoutEngine
from apps.trading_worker.strategies.shock_momentum import ShockMomentumEngine
from apps.trading_worker.engines.meta_allocator import MetaAllocator
from apps.trading_worker.engines.portfolio_risk import PortfolioRiskGovernor

def test_conflict_resolution():
    sym = "BTCUSDT"
    
    # Simulate a market state
    state = MarketState(
        symbol=sym,
        timestamp=datetime.now(UTC),
        primary_regime=RegimeType.R4_BREAKOUT,
        regime_probabilities={RegimeType.R4_BREAKOUT.name: Decimal("0.8")},
        atr_1h=Decimal("100"),
        volatility_zscore=Decimal("2.5"),
        shock_active=False
    )
    
    # Initialize strategies
    grid = StructuralGridEngine("grid_1", sym)
    trend = TrendBreakoutEngine("trend_1", sym)
    shock = ShockMomentumEngine("shock_1", sym)
    
    # All strategies evaluate the same state
    grid_intent = grid.evaluate(state, current_position=Decimal("0.5"))
    trend_intent = trend.evaluate(state, current_position=Decimal("0.0"))
    shock_intent = shock.evaluate(state, current_position=Decimal("0.0"))
    
    intents = [i for i in [grid_intent, trend_intent, shock_intent] if i is not None]
    
    # Only Trend should have fired positively due to BREAKOUT regime
    assert len(intents) == 1
    assert intents[0].strategy_id == "trend_1"
    
    # Meta Allocator aggregates intents
    allocator = MetaAllocator()
    target_deltas = allocator.aggregate_intents(intents, current_virtual_positions={})
    
    assert sym in target_deltas
    assert target_deltas[sym] > 0 # Trend is trying to go long
    
    # Risk Governor applies hard constraints
    governor = PortfolioRiskGovernor(max_gross_exposure=Decimal("5.0"), max_net_exposure=Decimal("3.0"))
    
    # Simulate already having 2.9 Net
    approved_deltas = governor.check_and_cap_exposure(
        current_physical_net=Decimal("2.9"), 
        current_physical_gross=Decimal("2.9"), 
        target_deltas=target_deltas
    )
    
    # The trend delta is 0.2 * (0.85 * 0.75) = ~0.1275
    # 2.9 + 0.1275 = 3.0275 > 3.0, so the Risk Governor MUST reject it
    assert sym not in approved_deltas or approved_deltas[sym] == Decimal("0")
