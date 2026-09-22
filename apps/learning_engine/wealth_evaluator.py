"""
Wealth Evaluator for Blessing AI.
Aggregates trade records into portfolio-wide and per-strategy risk-adjusted metrics,
tracks wealth growth milestones, and enforces progressive mainnet promotion gates.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from typing import Dict, List, Optional, Sequence

from domain.wealth_metrics import (
    DeploymentStage,
    PromotionGateVerdict,
    TradeRecord,
    WealthPerformanceMetrics,
    calculate_wealth_metrics,
    evaluate_promotion_gate,
)
from domain.trade_lineage import TradeLineage

logger = logging.getLogger("blessing.learning.wealth_evaluator")


class WealthEvaluator:
    def __init__(
        self,
        initial_capital: Decimal = Decimal("1000.0"),
        current_stage: DeploymentStage = DeploymentStage.OBSERVE_ONLY,
    ):
        self.initial_capital = initial_capital
        self.current_stage = current_stage
        self.trades: List[TradeRecord] = []
        self.lineages: Dict[str, TradeLineage] = {}

    def record_trade(self, trade: TradeRecord) -> None:
        self.trades.append(trade)
        if trade.is_unknown_risk:
            logger.critical(
                "RULE #0 BREACH RECORDED: Trade %s exhibited unknown risk conditions!",
                trade.trade_id,
            )

    def record_lineage(self, lineage: TradeLineage) -> None:
        self.lineages[lineage.lineage_id] = lineage
        # Also convert closed lineage into TradeRecord if not already tracked
        if lineage.closed_at is not None:
            tr = TradeRecord(
                trade_id=lineage.lineage_id,
                symbol=lineage.symbol,
                strategy_id=lineage.strategy_id,
                realized_pnl=lineage.gross_pnl,
                commission=lineage.total_commission,
                funding=lineage.total_funding,
                slippage_bps=lineage.slippage_bps,
                holding_seconds=lineage.holding_seconds,
                entry_price=lineage.entry_price,
                exit_price=lineage.exit_price or Decimal("0.0"),
                closed_at=lineage.closed_at,
                is_unknown_risk=lineage.requires_8d and "RULE_ZERO" in str(lineage.lessons_learned),
                notional=lineage.notional,
            )
            # Avoid duplicate if trade_id already registered
            if not any(t.trade_id == tr.trade_id for t in self.trades):
                self.record_trade(tr)

    def get_portfolio_metrics(self, period_days: int = 365) -> WealthPerformanceMetrics:
        return calculate_wealth_metrics(
            self.trades,
            initial_capital=self.initial_capital,
            period_days=period_days,
        )

    def get_strategy_metrics(self) -> Dict[str, WealthPerformanceMetrics]:
        by_strategy: Dict[str, List[TradeRecord]] = defaultdict(list)
        for t in self.trades:
            by_strategy[t.strategy_id].append(t)

        results: Dict[str, WealthPerformanceMetrics] = {}
        for strat, strat_trades in by_strategy.items():
            results[strat] = calculate_wealth_metrics(
                strat_trades,
                initial_capital=self.initial_capital,
            )
        return results

    def get_symbol_metrics(self) -> Dict[str, WealthPerformanceMetrics]:
        by_symbol: Dict[str, List[TradeRecord]] = defaultdict(list)
        for t in self.trades:
            by_symbol[t.symbol].append(t)

        results: Dict[str, WealthPerformanceMetrics] = {}
        for sym, sym_trades in by_symbol.items():
            results[sym] = calculate_wealth_metrics(
                sym_trades,
                initial_capital=self.initial_capital,
            )
        return results

    def check_promotion_readiness(self) -> PromotionGateVerdict:
        metrics = self.get_portfolio_metrics()
        return evaluate_promotion_gate(metrics, self.current_stage)

    def promote_if_eligible(self) -> tuple[bool, str]:
        verdict = self.check_promotion_readiness()
        if verdict.eligible:
            old_stage = self.current_stage
            self.current_stage = verdict.target_stage
            msg = f"System successfully PROMOTED from {old_stage.value} to {self.current_stage.value}"
            logger.info(msg)
            return True, msg
        else:
            reasons = "; ".join(verdict.blocking_reasons)
            msg = f"Promotion from {self.current_stage.value} to {verdict.target_stage.value} BLOCKED: {reasons}"
            logger.warning(msg)
            return False, msg
