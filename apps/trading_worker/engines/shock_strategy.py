import logging
from decimal import Decimal
from typing import Optional
from domain.models import MarketState, PriceActionState, StrategyIntent, PositionSide, MarketType, utc_now
from domain.enums import RegimeType

logger = logging.getLogger("blessing.engines.shock_strategy")

class ShockStrategyEngine:
    def __init__(self, strategy_id: str = "Shock Momentum"):
        self.strategy_id = strategy_id
        
    def evaluate(self, pa_state: PriceActionState, market_state: MarketState) -> Optional[StrategyIntent]:
        # Shock strategy looks for Volatility Shocks or Liquidity Sweeps
        is_shock = market_state.shock_active
        is_sweep = pa_state.liquidity_swept
        
        if not (is_shock or is_sweep):
            return None
            
        direction = PositionSide.LONG if is_sweep else (
            PositionSide.LONG if pa_state.displacement_velocity_pct > 0 else PositionSide.SHORT
        )
        delta = Decimal("0.3") if direction == PositionSide.LONG else Decimal("-0.3")
        
        return StrategyIntent(
            intent_id=f"SHOCK-INTENT-{utc_now().timestamp()}",
            strategy_id=self.strategy_id,
            symbol=pa_state.symbol,
            market_type=MarketType.USDM_FUTURES,
            direction=direction,
            desired_delta_qty=delta,
            opportunity_score=Decimal("0.9"),
            confidence=Decimal("0.6"),
            expected_holding_horizon_sec=300, # 5 mins
            evidence={"is_sweep": is_sweep, "is_shock": is_shock}
        )
