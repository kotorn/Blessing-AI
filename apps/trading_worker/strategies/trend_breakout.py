"""Research-only trend prototype; not a production execution path."""

RESEARCH_ONLY = True

import logging
from decimal import Decimal
from typing import Optional
from domain.models import MarketState, StrategyIntent, PositionSide, MarketType, RegimeType
from apps.trading_worker.strategies.base import BaseAlphaEngine
import uuid

logger = logging.getLogger("blessing.strategies.trend")

class TrendBreakoutEngine(BaseAlphaEngine):
    """
    Trend is an independent alpha engine, not merely a hedge.
    Trend strategy should provide positive skew.
    Pyramids winners when existing position is profitable and market structure remains valid.
    """
    
    def __init__(self, strategy_id: str, symbol: str):
        super().__init__(strategy_id, symbol)
        self.max_virtual_position = Decimal("0.8")

    def evaluate(self, state: MarketState, current_position: Decimal) -> Optional[StrategyIntent]:
        if state.symbol != self.symbol:
            return None

        score = Decimal("0.0")
        target_delta = Decimal("0.0")
        direction = PositionSide.LONG

        # Trend expects expansion and velocity
        if state.primary_regime in [RegimeType.R3_STRONG_TREND, RegimeType.R4_BREAKOUT]:
            score = Decimal("0.85")
        elif state.primary_regime == RegimeType.R5_VOLATILITY_SHOCK:
            score = Decimal("0.5") # Shock is noisy, wait for trend confirmation
        else:
            score = Decimal("0.1")
            
        if score > Decimal("0.7") and abs(current_position) < self.max_virtual_position:
            # We assume directionality from the Z-score/velocity sign
            # If volatility_zscore is positive, expansion is happening.
            # We need directional momentum. Currently MarketState lacks signed momentum.
            # Assuming positive bias for the stub.
            target_delta = Decimal("0.2")
            
        if target_delta == Decimal("0"):
            return None

        intent = StrategyIntent(
            intent_id=str(uuid.uuid4()),
            strategy_id=self.strategy_id,
            symbol=self.symbol,
            market_type=MarketType.USDM_FUTURES,
            direction=direction,
            desired_delta_qty=target_delta,
            opportunity_score=score,
            confidence=Decimal("0.75")
        )
        return intent
