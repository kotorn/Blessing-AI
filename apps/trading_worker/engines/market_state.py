import logging
from decimal import Decimal
from typing import Dict, Optional
from domain.models import PriceActionState, MarketState, RegimeType

logger = logging.getLogger("blessing.engines.market_state")

class MarketStateClassifier:
    def __init__(self):
        self.last_state: Dict[str, MarketState] = {}
        
    def classify(self, pa_state: PriceActionState) -> MarketState:
        sym = pa_state.symbol
        
        # Heuristics based on structural range expansion and displacement
        vel = abs(pa_state.displacement_velocity_pct)
        expansion = pa_state.range_expansion_ratio
        
        regime = RegimeType.R1_RANGE
        probs = {
            RegimeType.R0_STRONG_MEAN_REVERSION.name: Decimal("0.0"),
            RegimeType.R1_RANGE.name: Decimal("0.0"),
            RegimeType.R2_WEAK_TREND.name: Decimal("0.0"),
            RegimeType.R3_STRONG_TREND.name: Decimal("0.0"),
            RegimeType.R4_BREAKOUT.name: Decimal("0.0"),
            RegimeType.R5_VOLATILITY_SHOCK.name: Decimal("0.0"),
            RegimeType.R6_CRISIS.name: Decimal("0.0"),
        }
        
        is_shock = False
        
        if vel > Decimal("1.5") or expansion > Decimal("0.8"):
            regime = RegimeType.R5_VOLATILITY_SHOCK
            probs[RegimeType.R5_VOLATILITY_SHOCK.name] = Decimal("0.8")
            is_shock = True
        elif expansion > Decimal("0.5"):
            regime = RegimeType.R4_BREAKOUT
            probs[RegimeType.R4_BREAKOUT.name] = Decimal("0.6")
        elif expansion > Decimal("0.2"):
            regime = RegimeType.R2_WEAK_TREND
            probs[RegimeType.R2_WEAK_TREND.name] = Decimal("0.5")
        elif pa_state.liquidity_swept:
            regime = RegimeType.R0_STRONG_MEAN_REVERSION
            probs[RegimeType.R0_STRONG_MEAN_REVERSION.name] = Decimal("0.7")
        else:
            regime = RegimeType.R1_RANGE
            probs[RegimeType.R1_RANGE.name] = Decimal("0.8")

        # Mock ATR calculation derived from price bounds for now
        atr_1h = (pa_state.prior_24h_high - pa_state.prior_24h_low) * Decimal("0.05") 
        
        state = MarketState(
            symbol=sym,
            # Preserve the source market-event timestamp through the state
            # pipeline so replay cannot manufacture wall-clock time.
            timestamp=pa_state.timestamp,
            primary_regime=regime,
            regime_probabilities=probs,
            atr_1h=atr_1h,
            volatility_zscore=vel, # simplified mapping
            funding_zscore=Decimal("0.0"),
            shock_active=is_shock
        )
        
        self.last_state[sym] = state
        return state
