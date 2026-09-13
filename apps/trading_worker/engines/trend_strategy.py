import logging
from decimal import Decimal
from typing import Optional
from domain.models import MarketState, PriceActionState, StrategyIntent, PositionSide, MarketType, utc_now
from domain.enums import RegimeType

logger = logging.getLogger("blessing.engines.trend_strategy")

class TrendStrategyEngine:
    def __init__(self, strategy_id: str = "Trend / Breakout"):
        self.strategy_id = strategy_id
        
    def evaluate(self, pa_state: PriceActionState, market_state: MarketState) -> Optional[StrategyIntent]:
        regime = market_state.primary_regime
        
        # Trend strategy looks for Breakout or Strong Trend regimes
        if regime not in [RegimeType.R3_STRONG_TREND, RegimeType.R4_BREAKOUT]:
            return None
            
        direction = PositionSide.LONG if pa_state.displacement_velocity_pct > 0 else PositionSide.SHORT
        delta = Decimal("0.2") if direction == PositionSide.LONG else Decimal("-0.2")
        
        return StrategyIntent(
            intent_id=f"TREND-INTENT-{utc_now().timestamp()}",
            strategy_id=self.strategy_id,
            symbol=pa_state.symbol,
            market_type=MarketType.USDM_FUTURES,
            direction=direction,
            desired_delta_qty=delta,
            opportunity_score=Decimal("0.8"),
            confidence=Decimal("0.7"),
            expected_holding_horizon_sec=14400, # 4 hours
            evidence={"regime": regime.name, "velocity": str(pa_state.displacement_velocity_pct)},
            timestamp=pa_state.timestamp,
        )
