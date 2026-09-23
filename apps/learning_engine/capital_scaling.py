"""
Dynamic Capital Scaling Engine for Blessing AI.
Adjusts capital allocation multipliers according to empirical performance, drawdown depth,
statistical consistency, and operational reliability.

Fundamental Governance Invariant:
"AI optimizes inside the Risk Envelope; AI does not redefine the Risk Envelope by itself."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from domain.wealth_metrics import WealthPerformanceMetrics

logger = logging.getLogger("blessing.learning.capital_scaling")


@dataclass(frozen=True, slots=True)
class CapitalAllocationVerdict:
    strategy_id: str
    base_allocation_multiplier: Decimal
    scaled_allocation_multiplier: Decimal
    performance_factor: Decimal
    drawdown_factor: Decimal
    consistency_factor: Decimal
    reliability_factor: Decimal
    rule_zero_compliant: bool
    rationale: str


class DynamicCapitalAllocator:
    def __init__(
        self,
        min_multiplier: Decimal = Decimal("0.0"),
        max_multiplier: Decimal = Decimal("1.5"),
        caution_drawdown_pct: Decimal = Decimal("5.0"),
        no_new_grid_drawdown_pct: Decimal = Decimal("10.0"),
        recovery_only_drawdown_pct: Decimal = Decimal("15.0"),
        emergency_drawdown_pct: Decimal = Decimal("20.0"),
    ):
        tiers = (
            caution_drawdown_pct,
            no_new_grid_drawdown_pct,
            recovery_only_drawdown_pct,
            emergency_drawdown_pct,
        )
        if not (tiers[0] < tiers[1] < tiers[2] < tiers[3]):
            raise ValueError("drawdown scaling tiers must be strictly increasing")
        self.min_multiplier = min_multiplier
        self.max_multiplier = max_multiplier
        self.caution_drawdown_pct = caution_drawdown_pct
        self.no_new_grid_drawdown_pct = no_new_grid_drawdown_pct
        self.recovery_only_drawdown_pct = recovery_only_drawdown_pct
        self.emergency_drawdown_pct = emergency_drawdown_pct

    def evaluate_allocation(
        self,
        strategy_id: str,
        metrics: WealthPerformanceMetrics,
        current_drawdown_pct: Decimal,
        active_critical_incidents: int = 0,
        active_high_incidents: int = 0,
        base_multiplier: Decimal = Decimal("1.0"),
    ) -> CapitalAllocationVerdict:
        """
        Calculates the risk-budget multiplier for a strategy.
        Strictly enforces Rule #0: Unknown Risk = No New Risk.
        """
        # 1. Rule #0 Check
        if metrics.unknown_risk_violations > 0:
            return CapitalAllocationVerdict(
                strategy_id=strategy_id,
                base_allocation_multiplier=base_multiplier,
                scaled_allocation_multiplier=Decimal("0.0"),
                performance_factor=Decimal("0.0"),
                drawdown_factor=Decimal("0.0"),
                consistency_factor=Decimal("0.0"),
                reliability_factor=Decimal("0.0"),
                rule_zero_compliant=False,
                rationale="RULE #0 ENFORCED: Unknown risk observed. All new capital allocation is blocked (0.0x).",
            )

        if active_critical_incidents > 0:
            return CapitalAllocationVerdict(
                strategy_id=strategy_id,
                base_allocation_multiplier=base_multiplier,
                scaled_allocation_multiplier=Decimal("0.0"),
                performance_factor=Decimal("0.0"),
                drawdown_factor=Decimal("0.0"),
                consistency_factor=Decimal("0.0"),
                reliability_factor=Decimal("0.0"),
                rule_zero_compliant=True,
                rationale=f"Active critical 8D incident ({active_critical_incidents}) requires complete capital hold (0.0x).",
            )

        # 2. Performance Factor (Sharpe & Sortino)
        if metrics.sharpe_ratio >= Decimal("1.8"):
            perf_factor = Decimal("1.25")
        elif metrics.sharpe_ratio >= Decimal("1.2"):
            perf_factor = Decimal("1.10")
        elif metrics.sharpe_ratio >= Decimal("0.8"):
            perf_factor = Decimal("1.00")
        elif metrics.sharpe_ratio > Decimal("0.0"):
            perf_factor = Decimal("0.85")
        else:
            perf_factor = Decimal("0.70")

        # 3. Drawdown Factor. At 5% new-risk sizing is halved. At 10% and
        # above the governance contract blocks new risk entirely; the 15% and
        # 20% tiers further restrict execution to recovery/emergency behavior.
        if current_drawdown_pct < self.caution_drawdown_pct:
            dd_factor = Decimal("1.0")
        elif current_drawdown_pct < self.no_new_grid_drawdown_pct:
            dd_factor = Decimal("0.50")
        else:
            dd_factor = Decimal("0.0")

        # 4. Consistency Factor (Profit factor and expectancy)
        if metrics.profit_factor >= Decimal("1.5") and metrics.expectancy_usdt > Decimal("0.0"):
            const_factor = Decimal("1.15")
        elif metrics.profit_factor >= Decimal("1.2"):
            const_factor = Decimal("1.00")
        elif metrics.profit_factor >= Decimal("1.0"):
            const_factor = Decimal("0.85")
        else:
            const_factor = Decimal("0.60")

        # 5. Reliability Factor (Active High Incidents penalty)
        if active_high_incidents > 0:
            rel_factor = Decimal("0.50")
        else:
            rel_factor = Decimal("1.00")

        # Composite Multiplier
        raw_multiplier = base_multiplier * perf_factor * dd_factor * const_factor * rel_factor
        scaled_multiplier = max(self.min_multiplier, min(self.max_multiplier, raw_multiplier)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        rationale = (
            f"Capital Multiplier = {scaled_multiplier}x (Base={base_multiplier}, "
            f"Perf={perf_factor}, DD_Penalty={dd_factor}, Consistency={const_factor}, Reliability={rel_factor})"
        )

        return CapitalAllocationVerdict(
            strategy_id=strategy_id,
            base_allocation_multiplier=base_multiplier,
            scaled_allocation_multiplier=scaled_multiplier,
            performance_factor=perf_factor,
            drawdown_factor=dd_factor,
            consistency_factor=const_factor,
            reliability_factor=rel_factor,
            rule_zero_compliant=True,
            rationale=rationale,
        )
