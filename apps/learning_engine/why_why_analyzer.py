"""
5-Why Root Cause Analyzer for Blessing AI.
Automates systematic recursive why-why diagnosis for unexpected trade outcomes,
abnormal slippage, and adverse regime transitions.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, Optional

from domain.eight_d import WhyWhyTree
from domain.trade_lineage import TradeLineage, OutcomeGrade

logger = logging.getLogger("blessing.learning.why_why")


class WhyWhyAnalyzer:
    @staticmethod
    def analyze_lineage(lineage: TradeLineage) -> WhyWhyTree:
        """
        Derives an authoritative 5-Why diagnostic tree from a TradeLineage record.
        """
        problem_stmt = (
            f"Trade {lineage.lineage_id} ({lineage.symbol} - {lineage.strategy_id}) "
            f"resulted in {lineage.outcome_grade.value} with Net PnL {lineage.net_pnl} USDT "
            f"and slippage {lineage.slippage_bps} bps."
        )

        tree = WhyWhyTree(problem_statement=problem_stmt)

        mkt = lineage.market_state_snapshot
        primary_regime = mkt.get("primary_regime", "UNKNOWN")
        vol_zscore = Decimal(str(mkt.get("volatility_zscore", "0.0")))
        atr = Decimal(str(mkt.get("atr_1h", "10.0")))
        slippage = lineage.slippage_bps
        pnl = lineage.net_pnl

        # Scenario 1: Excessive Slippage
        if lineage.outcome_grade == OutcomeGrade.EXCESS_SLIPPAGE or slippage >= Decimal("25.0"):
            tree.add_level(
                1,
                "Why was the realized execution slippage excessive?",
                f"Fill price deviated by {slippage} bps from target benchmark price upon order execution.",
            )
            tree.add_level(
                2,
                "Why did the fill price deviate significantly from the benchmark?",
                "The order was routed as an aggressive taker order into a thin top-of-book depth.",
            )
            tree.add_level(
                3,
                "Why was the order book depth insufficient for the trade quantity?",
                f"Market liquidity for {lineage.symbol} temporarily evaporated during high-frequency volatility (vol_zscore={vol_zscore}).",
            )
            tree.add_level(
                4,
                "Why did the execution gate permit an aggressive market order when liquidity was thin?",
                "The execution policy did not enforce a pre-trade L2 book depth verification threshold for this symbol.",
            )
            tree.add_level(
                5,
                "What is the systemic root cause?",
                "Absence of dynamic top-of-book depth liquidity filter prior to taker order dispatch.",
            )
            tree.root_cause_summary = "Missing L2 order book depth gate during sudden liquidity evaporation."
            tree.preventive_insight = (
                "Require DecisionExecutionGate to verify cumulative bid/ask depth within 5 bps >= 3x order quantity "
                "before authorizing taker orders, otherwise force POST_ONLY."
            )

        # Scenario 2: Regime Mismatch
        elif lineage.outcome_grade == OutcomeGrade.REGIME_MISMATCH or primary_regime == "R5_VOLATILITY_SHOCK":
            tree.add_level(
                1,
                "Why did the trade experience an unexpected adverse excursion?",
                f"The position moved immediately against the directional intent upon entry under regime {primary_regime}.",
            )
            tree.add_level(
                2,
                "Why did market dynamics oppose the strategy premise?",
                f"The strategy ({lineage.strategy_id}) expected mean reversion or steady trend, but market was undergoing volatility expansion (vol_zscore={vol_zscore}).",
            )
            tree.add_level(
                3,
                "Why was the strategy enabled or active during this adverse regime?",
                "Regime transition latency caused the state classifier to lag the real-time order flow shock by 1-2 bars.",
            )
            tree.add_level(
                4,
                "Why did the regime classifier lag behind the order flow shock?",
                "The rolling lookback window for ATR and volatility z-score was calibrated to 1-hour candles rather than sub-minute displacement.",
            )
            tree.add_level(
                5,
                "What is the systemic root cause?",
                "Displacement velocity accelerator was not tightly coupled to instant strategy invalidation.",
            )
            tree.root_cause_summary = "Slow regime transition detection allowed strategy to enter during regime changeover."
            tree.preventive_insight = (
                "Integrate 1-minute displacement acceleration trigger to immediately invalidate strategy intents "
                "prior to RiskGovernor execution decision."
            )

        # Scenario 3: Outsized Unexpected Loss
        elif lineage.outcome_grade == OutcomeGrade.UNEXPECTED_LOSS or pnl < Decimal("-50.0"):
            tree.add_level(
                1,
                "Why did the trade suffer an outsized financial loss beyond normal stop bounds?",
                f"Net loss of {pnl} USDT exceeded the planned 1.5x ATR risk budget envelope.",
            )
            tree.add_level(
                2,
                "Why was the position not closed at the predetermined stop invalidation level?",
                f"Holding duration ({lineage.holding_seconds:.1f}s) coincided with rapid gap-through where limit stop was missed or delayed.",
            )
            tree.add_level(
                3,
                "Why was the stop delayed or gapped through?",
                "High adverse momentum triggered cascading liquidations across the exchange perpetual book.",
            )
            tree.add_level(
                4,
                "Why was position sizing not adjusted down for elevated tail risk?",
                f"The opportunity score calculation under-weighted the tail_risk_factor ({lineage.opportunity_score_snapshot.get('tail_risk_factor', '1.0')}).",
            )
            tree.add_level(
                5,
                "What is the systemic root cause?",
                "Position sizing formula did not penalize tail risk non-linearly during cross-venue momentum surges.",
            )
            tree.root_cause_summary = "Linear position sizing failed to curtail exposure under severe tail risk conditions."
            tree.preventive_insight = (
                "Implement non-linear quadratic dampening on target delta whenever tail_risk_factor > 1.5, "
                "and ensure stop loss orders are pre-placed on venue natively."
            )

        # Scenario 4: Standard / Normal Deviation
        else:
            tree.add_level(
                1,
                "Why did the trade experience variance from expectation?",
                f"Outcome grade {lineage.outcome_grade.value} with actual edge {lineage.actual_edge_bps} bps vs expected {lineage.expected_edge_bps} bps.",
            )
            tree.add_level(
                2,
                "Why did edge decay occur during holding?",
                "Alpha signal decayed as short-term microstructure rebalanced faster than holding duration.",
            )
            tree.add_level(
                3,
                "Why did the alpha signal decay faster than anticipated?",
                f"Holding horizon ({lineage.holding_seconds:.1f}s) was slightly longer than optimal alpha decay half-life.",
            )
            tree.add_level(
                4,
                "Why was the position held past the half-life?",
                "Exit condition relied on static threshold rather than dynamic signal decay monitoring.",
            )
            tree.add_level(
                5,
                "What is the systemic root cause?",
                "Static time-in-trade exit thresholds without continuous decay-based alpha tracking.",
            )
            tree.root_cause_summary = "Time-based decay mismatch with signal alpha half-life."
            tree.preventive_insight = "Introduce continuous decay-based exit signals in strategy state machines."

        return tree
