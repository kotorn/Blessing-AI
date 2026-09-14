import logging
from decimal import Decimal
from typing import Optional
from domain.models import MarketState, StrategyIntent, PositionSide, MarketType, RegimeType
from apps.trading_worker.strategies.base import BaseAlphaEngine
import uuid

logger = logging.getLogger("blessing.strategies.shock")

class ShockMomentumEngine(BaseAlphaEngine):
    """
    Shock Momentum uses small initial risk.
    Reacts to acceleration (2nd derivative) and extreme structural displacement.
    Must explicitly transition from SHOCK -> TREND to remain in position.
    """
    
    def __init__(self, strategy_id: str, symbol: str):
        super().__init__(strategy_id, symbol)
        self.max_virtual_position = Decimal("0.3") # Small initial risk

    def evaluate(self, state: MarketState, current_position: Decimal) -> Optional[StrategyIntent]:
        if state.symbol != self.symbol:
            return None

        score = Decimal("0.0")
        target_delta = Decimal("0.0")
        direction = PositionSide.LONG

        if state.primary_regime == RegimeType.R5_VOLATILITY_SHOCK and state.shock_active:
            score = Decimal("0.9")
        else:
            # Not a shock, immediately decay score
            score = Decimal("0.05")
            
        if score > Decimal("0.8") and abs(current_position) < self.max_virtual_position:
            # Capture the shock momentum
            target_delta = Decimal("0.1")
            
        # If we have a position but the shock has decayed, we should emit an intent to exit.
        if score < Decimal("0.2") and abs(current_position) > Decimal("0"):
            target_delta = -current_position # Flatten shock exposure

        if target_delta == Decimal("0"):
            return None

        intent = StrategyIntent(
            intent_id=str(uuid.uuid4()),
            strategy_id=self.strategy_id,
            symbol=self.symbol,
            market_type=MarketType.USDM_FUTURES,
            direction=PositionSide.SHORT if target_delta < 0 else PositionSide.LONG,
            desired_delta_qty=target_delta,
            opportunity_score=score,
            confidence=Decimal("0.5") # Shocks have high variance
        )
        return intent
