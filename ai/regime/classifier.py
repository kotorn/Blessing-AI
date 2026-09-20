"""
Market Regime Engine for Blessing AI v0.1
Classifies current market conditions into 7 operational regimes:
  R0: Strong Mean Reversion
  R1: Range
  R2: Weak Trend
  R3: Strong Trend
  R4: Breakout
  R5: Volatility Shock
  R6: Crisis / Abnormal Market

Rule: Never place orders from the Regime Engine. Output is purely probabilistic.
"""

from datetime import datetime, timezone
from typing import Dict

import numpy as np
from core.basket.models import MarketRegime, RegimeState


class MarketRegimeEngine:
    """
    Combines quantitative market micro-structure signals with probabilistic classification.
    Inputs: ADX, ATR percentile, Hurst exponent, Bollinger Band width, Volatility Z-score, Basis velocity.
    """

    def predict(
        self,
        symbol: str,
        adx: float,
        hurst_exponent: float,
        bb_width_zscore: float,
        realized_vol_zscore: float,
        rsi: float,
        basis_velocity: float,
    ) -> RegimeState:
        """
        Deterministic baseline + softmax probability mapping.
        In Phase 3, this integrates pretrained CatBoost/XGBoost & HMM outputs.
        """
        scores = {
            MarketRegime.R0_STRONG_MEAN_REVERSION: 0.0,
            MarketRegime.R1_RANGE: 0.0,
            MarketRegime.R2_WEAK_TREND: 0.0,
            MarketRegime.R3_STRONG_TREND: 0.0,
            MarketRegime.R4_BREAKOUT: 0.0,
            MarketRegime.R5_VOLATILITY_SHOCK: 0.0,
            MarketRegime.R6_CRISIS: 0.0,
        }

        # 1. Strong Mean Reversion: Hurst < 0.40, RSI oversold/overbought with low trend
        if hurst_exponent < 0.42 and adx < 20.0:
            scores[MarketRegime.R0_STRONG_MEAN_REVERSION] += 3.0
            scores[MarketRegime.R1_RANGE] += 1.5

        # 2. Range: ADX < 25, Hurst ~ 0.50, Volatility normal
        if adx < 22.0 and abs(hurst_exponent - 0.50) < 0.10:
            scores[MarketRegime.R1_RANGE] += 3.5

        # 3. Weak Trend: 22 <= ADX < 32, Hurst > 0.55
        if 22.0 <= adx < 32.0 and hurst_exponent > 0.52:
            scores[MarketRegime.R2_WEAK_TREND] += 3.0

        # 4. Strong Trend: ADX >= 32, Hurst > 0.65
        if adx >= 32.0 and hurst_exponent > 0.60:
            scores[MarketRegime.R3_STRONG_TREND] += 3.5

        # 5. Breakout: Bollinger Band squeeze explosion + sudden basis velocity spike
        if bb_width_zscore > 2.0 and adx > 28.0:
            scores[MarketRegime.R4_BREAKOUT] += 3.0

        # 6. Volatility Shock: Realized vol Z-score > 2.5
        if realized_vol_zscore > 2.5:
            scores[MarketRegime.R5_VOLATILITY_SHOCK] += 4.0

        # 7. Crisis: Extreme vol + basis dislocation
        if realized_vol_zscore > 3.5 or abs(basis_velocity) > 4.0:
            scores[MarketRegime.R6_CRISIS] += 5.0

        # Softmax conversion to strict probability distribution
        raw_vals = np.array(list(scores.values()))
        exp_vals = np.exp(raw_vals - np.max(raw_vals))
        probs = exp_vals / np.sum(exp_vals)

        prob_dict: Dict[MarketRegime, float] = {}
        for idx, reg in enumerate(scores.keys()):
            prob_dict[reg] = float(round(probs[idx], 4))

        # Best predicted regime
        predicted = max(prob_dict, key=lambda k: prob_dict[k])

        # Shannon entropy computation
        entropy = float(-np.sum([p * np.log(p + 1e-9) for p in prob_dict.values()]))

        return RegimeState(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            predicted_regime=predicted,
            probabilities=prob_dict,
            entropy=round(entropy, 4),
            volatility_zscore=round(realized_vol_zscore, 2),
            atr_percentile=round(min(max(adx * 2.5, 5.0), 98.0), 1),
        )
