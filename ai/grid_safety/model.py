"""
AI Grid Safety Score Model for Blessing AI v0.1
Core risk intelligence model answering:
'If a new basket is initiated now, what is the exact quantitative risk distribution?'
Replaces simplistic BUY/SELL models with strategy-embedded risk predictions.
"""

from typing import Dict, Any
from datetime import datetime, timezone
from core.basket.models import GridSafetyPrediction, MarketRegime


class GridSafetyModel:
    """
    Computes expected basket survivability, maximum adverse excursion (MAE),
    and expected grid depth to produce the composite GridSafetyScore (0 - 100).
    """

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}

    def predict(
        self,
        symbol: str,
        regime_probs: Dict[MarketRegime, float],
        funding_rate: float,
        basis_zscore: float,
        atr_1h_pct: float,
        rsi_14: float,
        orderbook_imbalance: float,
    ) -> GridSafetyPrediction:
        """
        Calculates multi-target risk parameters and synthesizes the GridSafetyScore.
        """
        # Baseline mean reversion and range probabilities
        p_mean_rev = regime_probs.get(MarketRegime.R0_STRONG_MEAN_REVERSION, 0.0)
        p_range = regime_probs.get(MarketRegime.R1_RANGE, 0.0)
        p_strong_trend = regime_probs.get(MarketRegime.R3_STRONG_TREND, 0.0)
        p_crisis = regime_probs.get(MarketRegime.R6_CRISIS, 0.0)

        # 1. Probability Basket Profit
        prob_basket_profit = 0.65 + (p_mean_rev * 0.25) + (p_range * 0.20) - (p_strong_trend * 0.35) - (p_crisis * 0.50)
        prob_basket_profit = max(min(prob_basket_profit, 0.98), 0.10)

        # 2. Probability reaching deeper grid levels
        prob_reach_l2 = min(max(0.35 + (p_strong_trend * 0.40) + (abs(basis_zscore) * 0.05), 0.05), 0.95)
        prob_reach_l3 = min(max(prob_reach_l2 * 0.55 + (p_strong_trend * 0.25), 0.02), 0.85)
        prob_reach_l5 = min(max(prob_reach_l3 * 0.40 + (p_crisis * 0.50), 0.01), 0.70)

        # 3. Expected Maximum Adverse Excursion (% MAE)
        expected_mae_pct = max(0.8 + (atr_1h_pct * 1.5) + (prob_reach_l5 * 4.5) + (abs(basis_zscore) * 0.4), 0.5)

        # 4. Expected Max Equity Drawdown (%)
        expected_max_equity_dd_pct = expected_mae_pct * 1.35 * (1.0 + prob_reach_l5)

        # 5. Expected Grid Depth (1.0 to 5.0)
        expected_grid_depth = 1.0 + (prob_reach_l2 * 0.8) + (prob_reach_l3 * 1.0) + (prob_reach_l5 * 1.8)

        # 6. Expected Recovery Time (hours)
        expected_recovery_time_hrs = max(1.5 + (prob_reach_l3 * 8.0) + (prob_reach_l5 * 24.0), 0.5)

        # 7. Expected Net Basket Return (in basis points, factoring fees and funding drag)
        funding_drag_bps = abs(funding_rate) * 3.0 * 10000  # annualized drag
        expected_net_basket_return = max((prob_basket_profit * 65.0) - (expected_mae_pct * 12.0) - funding_drag_bps, -500.0)

        # Composite GridSafetyScore Formulation:
        # Score = 100 * P(Profit) - (P(L5) * 50) - (MAE% * 8) - (CrisisProb * 80)
        raw_score = (
            (prob_basket_profit * 60.0)
            + ((1.0 - prob_reach_l5) * 25.0)
            - (expected_mae_pct * 3.5)
            - (p_crisis * 40.0)
            - (abs(basis_zscore) * 4.0)
        )
        grid_safety_score = float(round(max(min(raw_score, 100.0), 0.0), 1))

        # Tiers determination:
        # 80-100: Grid Allowed
        # 65-79: Grid Allowed Reduced Risk
        # 50-64: Conservative / Reduced Levels
        # 35-49: Recovery Only
        # <35: No New Grid
        grid_allowed = grid_safety_score >= 65.0
        reduced_risk = 65.0 <= grid_safety_score < 80.0

        return GridSafetyPrediction(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            grid_safety_score=grid_safety_score,
            prob_basket_profit=round(prob_basket_profit, 4),
            prob_reach_level_2=round(prob_reach_l2, 4),
            prob_reach_level_3=round(prob_reach_l3, 4),
            prob_reach_level_5=round(prob_reach_l5, 4),
            expected_mae_pct=round(expected_mae_pct, 2),
            expected_max_equity_dd_pct=round(expected_max_equity_dd_pct, 2),
            expected_grid_depth=round(expected_grid_depth, 2),
            expected_recovery_time_hrs=round(expected_recovery_time_hrs, 1),
            expected_net_basket_return=round(expected_net_basket_return, 2),
            grid_allowed=grid_allowed,
            reduced_risk=reduced_risk,
        )
