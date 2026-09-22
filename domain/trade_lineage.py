"""
Trade Lineage Domain for Blessing AI.
Captures complete causal provenance from Market Observation to Post-Trade Learning.
Trace: Market State -> Intent -> Opportunity Score -> Allocation -> Risk Decision -> Order -> Fill -> Position -> P&L -> Learning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from domain.models import utc_now


class OutcomeGrade(str, Enum):
    ALPHA = "ALPHA"
    ACCEPTABLE_PROFIT = "ACCEPTABLE_PROFIT"
    ACCEPTABLE_LOSS = "ACCEPTABLE_LOSS"
    EXCESS_SLIPPAGE = "EXCESS_SLIPPAGE"
    REGIME_MISMATCH = "REGIME_MISMATCH"
    UNEXPECTED_LOSS = "UNEXPECTED_LOSS"


@dataclass
class TradeLineage:
    lineage_id: str
    symbol: str
    strategy_id: str
    
    # 1. Market State at decision tick
    market_state_snapshot: Dict[str, Any]
    
    # 2. Strategy Intent & Alpha hypothesis
    intent_snapshot: Dict[str, Any]
    
    # 3. Calibrated Opportunity Score
    opportunity_score_snapshot: Dict[str, Any]
    
    # 4. Meta Allocation & Capital Multiplier
    allocation_multiplier: Decimal
    target_exposure_snapshot: Dict[str, Any]
    
    # 5. Risk Governor Decision (Authority Gate)
    risk_decision_snapshot: Dict[str, Any]
    
    # 6. Execution orders and fills
    orders: List[Dict[str, Any]] = field(default_factory=list)
    fills: List[Dict[str, Any]] = field(default_factory=list)
    
    # 7. Financial & Execution Metrics
    entry_price: Decimal = Decimal("0.0")
    exit_price: Optional[Decimal] = None
    total_quantity: Decimal = Decimal("0.0")
    notional: Decimal = Decimal("0.0")
    gross_pnl: Decimal = Decimal("0.0")
    net_pnl: Decimal = Decimal("0.0")
    total_commission: Decimal = Decimal("0.0")
    total_funding: Decimal = Decimal("0.0")
    slippage_bps: Decimal = Decimal("0.0")
    holding_seconds: float = 0.0
    
    # 8. Learning & Causality Feedback
    expected_edge_bps: Decimal = Decimal("0.0")
    actual_edge_bps: Decimal = Decimal("0.0")
    edge_decay_bps: Decimal = Decimal("0.0")
    outcome_grade: OutcomeGrade = OutcomeGrade.ACCEPTABLE_PROFIT
    requires_8d: bool = False
    why_why_analysis_id: Optional[str] = None
    lessons_learned: List[str] = field(default_factory=list)
    
    created_at: datetime = field(default_factory=utc_now)
    closed_at: Optional[datetime] = None

    @classmethod
    def create(
        cls,
        symbol: str,
        strategy_id: str,
        market_state_snapshot: Dict[str, Any],
        intent_snapshot: Dict[str, Any],
        opportunity_score_snapshot: Dict[str, Any],
        target_exposure_snapshot: Dict[str, Any],
        risk_decision_snapshot: Dict[str, Any],
        allocation_multiplier: Decimal = Decimal("1.0"),
    ) -> TradeLineage:
        lineage_id = f"LIN-{uuid4().hex[:12].upper()}"
        expected_edge = Decimal(str(opportunity_score_snapshot.get("expected_edge", "0.0")))
        return cls(
            lineage_id=lineage_id,
            symbol=symbol,
            strategy_id=strategy_id,
            market_state_snapshot=market_state_snapshot,
            intent_snapshot=intent_snapshot,
            opportunity_score_snapshot=opportunity_score_snapshot,
            allocation_multiplier=allocation_multiplier,
            target_exposure_snapshot=target_exposure_snapshot,
            risk_decision_snapshot=risk_decision_snapshot,
            expected_edge_bps=(expected_edge * Decimal("10000")).quantize(Decimal("0.01")),
        )

    def record_order(self, order: Dict[str, Any]) -> None:
        self.orders.append(order)

    def record_fill(self, fill: Dict[str, Any]) -> None:
        self.fills.append(fill)

    def close_and_evaluate(
        self,
        exit_price: Decimal,
        realized_pnl: Decimal,
        commission: Decimal,
        funding: Decimal,
        slippage_bps: Decimal,
        holding_seconds: float,
        closed_at: Optional[datetime] = None,
    ) -> None:
        """
        Closes the trade lineage, computes edge decay, grades the outcome,
        and determines if an 8D incident investigation is required.
        """
        self.exit_price = exit_price
        self.gross_pnl = realized_pnl
        self.total_commission = commission
        self.total_funding = funding
        self.net_pnl = realized_pnl - commission + funding
        self.slippage_bps = slippage_bps
        self.holding_seconds = holding_seconds
        self.closed_at = closed_at or utc_now()

        # Compute actual edge in basis points
        if self.notional > Decimal("0.0"):
            self.actual_edge_bps = ((self.net_pnl / self.notional) * Decimal("10000")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        else:
            self.actual_edge_bps = Decimal("0.0")

        self.edge_decay_bps = self.expected_edge_bps - self.actual_edge_bps

        # Outcome Grading & 8D Trigger Evaluation
        if self.net_pnl > Decimal("0.0"):
            if self.actual_edge_bps >= self.expected_edge_bps:
                self.outcome_grade = OutcomeGrade.ALPHA
            else:
                self.outcome_grade = OutcomeGrade.ACCEPTABLE_PROFIT
            self.requires_8d = False
        else:
            # Net loss
            expected_atr = Decimal(str(self.market_state_snapshot.get("atr_1h", "10.0")))
            loss_magnitude = abs(self.net_pnl)
            
            # Check for excessive slippage
            if self.slippage_bps >= Decimal("25.0"):
                self.outcome_grade = OutcomeGrade.EXCESS_SLIPPAGE
                self.requires_8d = True
                self.lessons_learned.append(f"Excessive slippage observed: {self.slippage_bps} bps")
            # Check for regime mismatch (e.g. entered in Trend, but regime transitioned to Volatility Shock)
            elif self.market_state_snapshot.get("primary_regime") == "R5_VOLATILITY_SHOCK":
                self.outcome_grade = OutcomeGrade.REGIME_MISMATCH
                self.requires_8d = True
                self.lessons_learned.append("Trade failed during high-volatility shock regime transition")
            # Check for unexpected outsized loss (> 2x ATR * quantity or > $50)
            elif (self.total_quantity > Decimal("0.0") and loss_magnitude > (Decimal("2.0") * expected_atr * self.total_quantity)) or loss_magnitude > Decimal("100.0"):
                self.outcome_grade = OutcomeGrade.UNEXPECTED_LOSS
                self.requires_8d = True
                self.lessons_learned.append(f"Outsized loss breach: {loss_magnitude} USDT exceeded 2x expected ATR envelope")
            else:
                self.outcome_grade = OutcomeGrade.ACCEPTABLE_LOSS
                self.requires_8d = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lineage_id": self.lineage_id,
            "symbol": self.symbol,
            "strategy_id": self.strategy_id,
            "market_state": self.market_state_snapshot,
            "intent": self.intent_snapshot,
            "opportunity_score": self.opportunity_score_snapshot,
            "allocation_multiplier": str(self.allocation_multiplier),
            "target_exposure": self.target_exposure_snapshot,
            "risk_decision": self.risk_decision_snapshot,
            "orders": self.orders,
            "fills": self.fills,
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price) if self.exit_price is not None else None,
            "total_quantity": str(self.total_quantity),
            "notional": str(self.notional),
            "gross_pnl": str(self.gross_pnl),
            "net_pnl": str(self.net_pnl),
            "total_commission": str(self.total_commission),
            "total_funding": str(self.total_funding),
            "slippage_bps": str(self.slippage_bps),
            "holding_seconds": self.holding_seconds,
            "expected_edge_bps": str(self.expected_edge_bps),
            "actual_edge_bps": str(self.actual_edge_bps),
            "edge_decay_bps": str(self.edge_decay_bps),
            "outcome_grade": self.outcome_grade.value,
            "requires_8d": self.requires_8d,
            "why_why_analysis_id": self.why_why_analysis_id,
            "lessons_learned": self.lessons_learned,
            "created_at": self.created_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }
