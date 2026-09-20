"""Research-only range-fade prototype; not a production execution path."""

RESEARCH_ONLY = True

import logging
import uuid
from decimal import Decimal
from typing import Optional
from domain.models import MarketState, PriceActionState, StrategyIntent, PositionSide, MarketType, RegimeType
from apps.trading_worker.strategies.base import BaseAlphaEngine

logger = logging.getLogger("blessing.strategies.range_fade")

class RangeFadeEngine(BaseAlphaEngine):
    """Fade range edges in calm mean-reverting states.

    Complements the structural grid (which only enters on reclaim events):
    emits a small contrarian intent when the regime is R0/R1 and normalized
    volatility is contained. Never adds in trend/breakout/shock states.
    """

    def __init__(self, strategy_id: str, symbol: str,
                 max_virtual_position: Decimal = Decimal("0.5")):
        super().__init__(strategy_id, symbol)
        self.max_virtual_position = Decimal(str(max_virtual_position))

    def evaluate(
        self,
        state: MarketState,
        current_position: Decimal,
        *,
        pa_state: Optional[PriceActionState] = None,
    ) -> Optional[StrategyIntent]:
        if state.symbol != self.symbol:
            return None
        score = Decimal("0.0")
        if state.primary_regime in [RegimeType.R0_STRONG_MEAN_REVERSION, RegimeType.R1_RANGE]:
            score = Decimal("0.7")
            try:
                if Decimal(str(state.volatility_zscore)) > Decimal("2.0"):
                    score = Decimal("0.2")
            except Exception:
                score = Decimal("0.2")
        elif state.primary_regime == RegimeType.R2_WEAK_TREND:
            score = Decimal("0.3")
        else:
            score = Decimal("0.05")
        target_delta = Decimal("0.0")
        # Naive calm-regime placeholder, used only when no explicit sweep
        # signal is available: no production pipeline populates
        # PriceActionState.sweep_side today, so this remains the fallback.
        direction = PositionSide.LONG
        if (
            pa_state is not None
            and pa_state.symbol == self.symbol
            and pa_state.liquidity_swept
            and pa_state.sweep_side is not None
        ):
            direction = pa_state.sweep_side
        if score >= Decimal("0.5") and abs(current_position) < self.max_virtual_position:
            target_delta = Decimal("0.05") if direction == PositionSide.LONG else Decimal("-0.05")
        if target_delta == Decimal("0"):
            return None
        return StrategyIntent(
            intent_id=str(uuid.uuid4()),
            strategy_id=self.strategy_id,
            symbol=self.symbol,
            market_type=MarketType.USDM_FUTURES,
            direction=direction,
            desired_delta_qty=target_delta,
            opportunity_score=score,
            confidence=Decimal("0.55"),
            expected_holding_horizon_sec=3600,
        )
