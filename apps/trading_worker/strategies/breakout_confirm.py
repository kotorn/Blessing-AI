"""Research-only breakout-confirmation prototype; not a production execution path."""

RESEARCH_ONLY = True

import logging
import uuid
from decimal import Decimal
from typing import Optional
from domain.models import MarketState, PriceActionState, StrategyIntent, PositionSide, MarketType, RegimeType
from apps.trading_worker.strategies.base import BaseAlphaEngine

logger = logging.getLogger("blessing.strategies.breakout_confirm")

class BreakoutConfirmEngine(BaseAlphaEngine):
    """Require a confirmed breakout streak before participating.

    The production trend engine reacts on the first R3/R4 event. This
    variant waits for ``required_confirmations`` consecutive R4 events and
    additionally requires normalized volatility to stay under a ceiling, so
    one-bar false breakouts do not produce entries. When the streak breaks
    while attributed exposure remains, it emits a flattening intent.
    """

    def __init__(self, strategy_id: str, symbol: str,
                 max_virtual_position: Decimal = Decimal("0.6"),
                 required_confirmations: int = 2,
                 max_volatility_zscore: Decimal = Decimal("2.5")):
        super().__init__(strategy_id, symbol)
        self.max_virtual_position = Decimal(str(max_virtual_position))
        self.required_confirmations = int(required_confirmations)
        self.max_volatility_zscore = Decimal(str(max_volatility_zscore))
        self._consecutive_breakout = 0

    def evaluate(
        self,
        state: MarketState,
        current_position: Decimal,
        *,
        pa_state: Optional[PriceActionState] = None,
    ) -> Optional[StrategyIntent]:
        if state.symbol != self.symbol:
            return None
        if state.primary_regime == RegimeType.R4_BREAKOUT and not state.shock_active:
            self._consecutive_breakout += 1
        else:
            self._consecutive_breakout = 0
            if abs(current_position) > Decimal("0"):
                return StrategyIntent(
                    intent_id=str(uuid.uuid4()),
                    strategy_id=self.strategy_id,
                    symbol=self.symbol,
                    market_type=MarketType.USDM_FUTURES,
                    direction=PositionSide.SHORT if current_position > 0 else PositionSide.LONG,
                    desired_delta_qty=-current_position,
                    opportunity_score=Decimal("0.3"),
                    confidence=Decimal("0.5"),
                    expected_holding_horizon_sec=0,
                )
            return None
        try:
            vol_z = Decimal(str(state.volatility_zscore))
        except Exception:
            return None
        if self._consecutive_breakout < self.required_confirmations:
            return None
        if vol_z > self.max_volatility_zscore:
            return None
        if abs(current_position) >= self.max_virtual_position:
            return None
        return StrategyIntent(
            intent_id=str(uuid.uuid4()),
            strategy_id=self.strategy_id,
            symbol=self.symbol,
            market_type=MarketType.USDM_FUTURES,
            direction=PositionSide.LONG,
            desired_delta_qty=Decimal("0.2"),
            opportunity_score=Decimal("0.8"),
            confidence=Decimal("0.65"),
            expected_holding_horizon_sec=14400,
        )
