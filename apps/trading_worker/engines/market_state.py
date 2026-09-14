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
        
        # Robust metrics extracted from EWMA PriceAction state
        vel = pa_state.displacement_velocity_pct
        accel = pa_state.displacement_acceleration
        expansion = pa_state.range_expansion_ratio
        
        abs_vel = abs(vel)
        abs_accel = abs(accel)
        
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
        
        # 1. Shock Detection (2nd derivative spike OR extreme expansion Z-score)
        if expansion > Decimal("3.0") or abs_vel > Decimal("0.8") or abs_accel > Decimal("0.3"):
            regime = RegimeType.R5_VOLATILITY_SHOCK
            probs[RegimeType.R5_VOLATILITY_SHOCK.name] = Decimal("0.8")
            probs[RegimeType.R4_BREAKOUT.name] = Decimal("0.2")
            is_shock = True
            
        # 2. Breakout Detection (Significant expansion with directional velocity)
        elif expansion > Decimal("1.8") and abs_vel > Decimal("0.2"):
            regime = RegimeType.R4_BREAKOUT
            probs[RegimeType.R4_BREAKOUT.name] = Decimal("0.7")
            probs[RegimeType.R3_STRONG_TREND.name] = Decimal("0.3")
            
        # 3. Strong Trend (Moderate expansion, persistent smooth velocity)
        elif expansion > Decimal("1.2") and abs_vel > Decimal("0.1"):
            regime = RegimeType.R3_STRONG_TREND
            probs[RegimeType.R3_STRONG_TREND.name] = Decimal("0.6")
            probs[RegimeType.R2_WEAK_TREND.name] = Decimal("0.4")
            
        # 4. Liquidity Sweep / Mean Reversion (Price Action structural event)
        elif pa_state.liquidity_swept:
            regime = RegimeType.R0_STRONG_MEAN_REVERSION
            probs[RegimeType.R0_STRONG_MEAN_REVERSION.name] = Decimal("0.8")
            probs[RegimeType.R1_RANGE.name] = Decimal("0.2")
            
        # 5. Weak Trend
        elif expansion > Decimal("0.8") and abs_vel > Decimal("0.05"):
            regime = RegimeType.R2_WEAK_TREND
            probs[RegimeType.R2_WEAK_TREND.name] = Decimal("0.5")
            probs[RegimeType.R1_RANGE.name] = Decimal("0.5")
            
        # 6. Default: Range Bound (Low expansion, low velocity)
        else:
            regime = RegimeType.R1_RANGE
            probs[RegimeType.R1_RANGE.name] = Decimal("0.8")
            probs[RegimeType.R2_WEAK_TREND.name] = Decimal("0.2")

        # Compute proxy for ATR (1 hour high/low diff smoothed by an assumption)
        atr_1h_proxy = (pa_state.prior_24h_high - pa_state.prior_24h_low) * Decimal("0.05")
        
        # Volatility Z-Score proxy is the expansion ratio normalized around 1.0
        vol_zscore = expansion - Decimal("1.0")

        state = MarketState(
            symbol=sym,
            timestamp=pa_state.timestamp,
            primary_regime=regime,
            regime_probabilities=probs,
            atr_1h=atr_1h_proxy,
            volatility_zscore=vol_zscore,
            funding_zscore=Decimal("0.0"),
            shock_active=is_shock
        )
        
        self.last_state[sym] = state
        return state
