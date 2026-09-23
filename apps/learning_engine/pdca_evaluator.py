"""
PDCA (Plan-Do-Check-Act) Evaluator for Blessing AI.
Continually compares realized trade performance against planned expectations,
detects statistical edge decay and execution drift, and triggers automated corrective actions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, List, Optional, Sequence

from domain.trade_lineage import TradeLineage

logger = logging.getLogger("blessing.learning.pdca")


@dataclass(frozen=True, slots=True)
class StrategyPlan:
    strategy_id: str
    target_win_rate_pct: Decimal = Decimal("55.0")
    target_edge_bps: Decimal = Decimal("20.0")
    max_slippage_bps: Decimal = Decimal("5.0")
    max_drawdown_pct: Decimal = Decimal("3.0")
    min_profit_factor: Decimal = Decimal("1.3")


@dataclass(frozen=True, slots=True)
class PDCACheckResult:
    strategy_id: str
    sample_size: int
    evidence_status: str
    authoritative: bool
    plan_win_rate_pct: Decimal
    actual_win_rate_pct: Decimal
    win_rate_gap_pct: Decimal
    plan_edge_bps: Decimal
    actual_edge_bps: Decimal
    edge_decay_bps: Decimal
    plan_slippage_bps: Decimal
    actual_slippage_bps: Decimal
    slippage_excess_bps: Decimal
    drift_detected: Optional[bool]
    drift_severity: str  # "NONE", "MODERATE", "CRITICAL"
    recommended_actions: List[str]
    triggers_8d: Optional[bool]


class PDCAEvaluator:
    def __init__(self, plans: Optional[Dict[str, StrategyPlan]] = None):
        self.plans = plans or {
            "trend_breakout": StrategyPlan(strategy_id="trend_breakout", target_win_rate_pct=Decimal("50.0"), target_edge_bps=Decimal("35.0")),
            "range_fade": StrategyPlan(strategy_id="range_fade", target_win_rate_pct=Decimal("60.0"), target_edge_bps=Decimal("15.0")),
            "shock_momentum": StrategyPlan(strategy_id="shock_momentum", target_win_rate_pct=Decimal("48.0"), target_edge_bps=Decimal("45.0")),
            "structural_grid": StrategyPlan(strategy_id="structural_grid", target_win_rate_pct=Decimal("70.0"), target_edge_bps=Decimal("12.0")),
            "default": StrategyPlan(strategy_id="default", target_win_rate_pct=Decimal("55.0"), target_edge_bps=Decimal("20.0")),
        }

    def evaluate_strategy(
        self,
        strategy_id: str,
        lineages: Sequence[TradeLineage],
        window_size: int = 20,
    ) -> PDCACheckResult:
        plan = self.plans.get(strategy_id, self.plans["default"])
        relevant = [
            l for l in lineages
            if l.strategy_id == strategy_id and l.closed_at is not None
        ][-window_size:]

        if not relevant:
            return PDCACheckResult(
                strategy_id=strategy_id,
                sample_size=0,
                evidence_status="INSUFFICIENT_SAMPLE",
                authoritative=False,
                plan_win_rate_pct=plan.target_win_rate_pct,
                actual_win_rate_pct=Decimal("0.0"),
                win_rate_gap_pct=Decimal("0.0"),
                plan_edge_bps=plan.target_edge_bps,
                actual_edge_bps=Decimal("0.0"),
                edge_decay_bps=Decimal("0.0"),
                plan_slippage_bps=plan.max_slippage_bps,
                actual_slippage_bps=Decimal("0.0"),
                slippage_excess_bps=Decimal("0.0"),
                drift_detected=None,
                drift_severity="UNKNOWN",
                recommended_actions=[
                    "Insufficient evidence: collect verified closed-trade lineages before drawing a PDCA health conclusion."
                ],
                triggers_8d=None,
            )

        n = len(relevant)
        wins = sum(1 for l in relevant if l.net_pnl > Decimal("0.0"))
        actual_win_rate = ((Decimal(wins) / Decimal(n)) * Decimal("100.0")).quantize(Decimal("0.01"))
        win_rate_gap = (plan.target_win_rate_pct - actual_win_rate).quantize(Decimal("0.01"))

        total_actual_edge = sum((l.actual_edge_bps for l in relevant), Decimal("0.0"))
        actual_edge = (total_actual_edge / Decimal(n)).quantize(Decimal("0.01"))
        edge_decay = (plan.target_edge_bps - actual_edge).quantize(Decimal("0.01"))

        total_slippage = sum((l.slippage_bps for l in relevant), Decimal("0.0"))
        actual_slippage = (total_slippage / Decimal(n)).quantize(Decimal("0.01"))
        slippage_excess = max(Decimal("0.0"), (actual_slippage - plan.max_slippage_bps).quantize(Decimal("0.01")))

        # Check conditions
        drift_detected = False
        drift_severity = "NONE"
        triggers_8d = False
        actions: List[str] = []

        # 1. Edge Decay Check
        if edge_decay > Decimal("20.0"):
            drift_detected = True
            drift_severity = "CRITICAL"
            triggers_8d = True
            actions.append(
                f"Severe edge decay ({edge_decay} bps below plan). Clamp strategy allocation multiplier by 50%."
            )
        elif edge_decay > Decimal("10.0"):
            drift_detected = True
            drift_severity = "MODERATE"
            actions.append(
                f"Moderate edge decay ({edge_decay} bps below plan). Tighten entry opportunity threshold."
            )

        # 2. Win Rate Degradation Check
        if win_rate_gap > Decimal("15.0"):
            drift_detected = True
            drift_severity = "CRITICAL"
            triggers_8d = True
            actions.append(
                f"Win rate underperforming plan by {win_rate_gap}%. Trigger 8D investigation into market regime shifts."
            )
        elif win_rate_gap > Decimal("8.0"):
            drift_detected = True
            if drift_severity != "CRITICAL":
                drift_severity = "MODERATE"
            actions.append(
                f"Win rate lag of {win_rate_gap}%. Restrict trading to high-confidence regime matches only."
            )

        # 3. Slippage Excess Check
        if slippage_excess > Decimal("10.0"):
            actions.append(
                f"Excessive execution slippage ({actual_slippage} bps vs {plan.max_slippage_bps} bps plan). "
                f"Enforce Maker Post-Only order routing."
            )

        if not actions:
            actions.append("Strategy performing within or above expected PDCA plan parameters.")

        return PDCACheckResult(
            strategy_id=strategy_id,
            sample_size=n,
            evidence_status="PROCESS_LOCAL_UNVERIFIED",
            authoritative=False,
            plan_win_rate_pct=plan.target_win_rate_pct,
            actual_win_rate_pct=actual_win_rate,
            win_rate_gap_pct=win_rate_gap,
            plan_edge_bps=plan.target_edge_bps,
            actual_edge_bps=actual_edge,
            edge_decay_bps=edge_decay,
            plan_slippage_bps=plan.max_slippage_bps,
            actual_slippage_bps=actual_slippage,
            slippage_excess_bps=slippage_excess,
            drift_detected=drift_detected,
            drift_severity=drift_severity,
            recommended_actions=actions,
            triggers_8d=triggers_8d,
        )
