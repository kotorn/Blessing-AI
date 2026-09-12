import logging
from decimal import Decimal
from typing import Optional
from domain.models import MarketState, PriceActionState, StrategyIntent, PositionSide, MarketType, utc_now
from domain.enums import RegimeType

logger = logging.getLogger("blessing.engines.grid_strategy")

class GridStrategyEngine:
    def __init__(self, strategy_id: str = "Structural Grid"):
        self.strategy_id = strategy_id
        
    def evaluate(self, pa_state: PriceActionState, market_state: MarketState) -> Optional[StrategyIntent]:
        regime = market_state.primary_regime
        
        # Disable Grid expanding risk in Breakout, Crisis, or Vol Shock modes
        if regime in [RegimeType.R4_BREAKOUT, RegimeType.R5_VOLATILITY_SHOCK, RegimeType.R6_CRISIS]:
            # Emit zero-delta intent to signal safety brake
            return StrategyIntent(
                intent_id=f"GRID-BRAKE-{utc_now().timestamp()}",
                strategy_id=self.strategy_id,
                symbol=pa_state.symbol,
                market_type=MarketType.USDM_FUTURES,
                direction=PositionSide.BOTH,
                desired_delta_qty=Decimal("0.0"),
                opportunity_score=Decimal("0.0"),
                confidence=Decimal("1.0"),
                expected_holding_horizon_sec=0,
                evidence={"brake_reason": f"Dangerous regime: {regime.name}"}
            )
            
        # Standard Grid Opportunity
        base_confidence = Decimal("0.8")
        opportunity_score = Decimal("0.5")
        
        if regime == RegimeType.R0_STRONG_MEAN_REVERSION:
            opportunity_score = Decimal("0.9")
            base_confidence = Decimal("0.9")
            
        direction = PositionSide.LONG if pa_state.is_reclaiming else PositionSide.BOTH
        delta = Decimal("0.1") if direction == PositionSide.LONG else Decimal("0.0")
        
        return StrategyIntent(
            intent_id=f"GRID-INTENT-{utc_now().timestamp()}",
            strategy_id=self.strategy_id,
            symbol=pa_state.symbol,
            market_type=MarketType.USDM_FUTURES,
            direction=direction,
            desired_delta_qty=delta,
            opportunity_score=opportunity_score,
            confidence=base_confidence,
            expected_holding_horizon_sec=3600,
            evidence={"regime": regime.name, "atr": str(market_state.atr_1h)}
        )
