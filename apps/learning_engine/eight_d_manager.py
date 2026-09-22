"""
8D Incident Manager for Blessing AI.
Orchestrates the lifecycle of Eight Disciplines problem solving incidents,
integrating automated containment, Why-Why root cause diagnosis, and systemic prevention.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Dict, List, Optional

from domain.eight_d import (
    EightDIncident,
    IncidentSeverity,
    IncidentStatus,
    IncidentTriggerType,
)
from domain.trade_lineage import TradeLineage, OutcomeGrade
from .why_why_analyzer import WhyWhyAnalyzer

logger = logging.getLogger("blessing.learning.eight_d")


class EightDManager:
    def __init__(self):
        self.incidents: Dict[str, EightDIncident] = {}
        self.active_containments: Dict[str, Dict[str, Any]] = {}

    def get_incident(self, incident_id: str) -> Optional[EightDIncident]:
        return self.incidents.get(incident_id)

    def list_incidents(self, active_only: bool = False) -> List[EightDIncident]:
        incidents = list(self.incidents.values())
        if active_only:
            return [i for i in incidents if i.status != IncidentStatus.D8_CLOSED]
        return incidents

    def create_incident_from_lineage(self, lineage: TradeLineage) -> EightDIncident:
        """
        Automatically instantiate an 8D incident when a TradeLineage triggers 8D requirements.
        """
        # Determine Severity and Trigger Type
        if "RULE_ZERO" in str(lineage.lessons_learned):
            sev = IncidentSeverity.CRITICAL
            trigger = IncidentTriggerType.RULE_ZERO_BREACH
            title = f"RULE #0 BREACH on {lineage.symbol}: Unknown Risk Execution Observed"
        elif lineage.outcome_grade == OutcomeGrade.EXCESS_SLIPPAGE:
            sev = IncidentSeverity.HIGH
            trigger = IncidentTriggerType.SLIPPAGE_SPIKE
            title = f"Excessive Slippage Spike ({lineage.slippage_bps} bps) on {lineage.symbol}"
        elif lineage.outcome_grade == OutcomeGrade.REGIME_MISMATCH:
            sev = IncidentSeverity.MEDIUM
            trigger = IncidentTriggerType.REGIME_MISMATCH
            title = f"Regime Mismatch Trade Failure on {lineage.symbol} ({lineage.strategy_id})"
        elif lineage.outcome_grade == OutcomeGrade.UNEXPECTED_LOSS:
            sev = IncidentSeverity.HIGH
            trigger = IncidentTriggerType.UNEXPECTED_LOSS
            title = f"Unexpected Outsized Loss ({lineage.net_pnl} USDT) on {lineage.symbol}"
        else:
            sev = IncidentSeverity.LOW
            trigger = IncidentTriggerType.EDGE_DECAY
            title = f"Statistical Edge Decay on {lineage.symbol} ({lineage.strategy_id})"

        incident = EightDIncident.create(
            title=title,
            severity=sev,
            trigger_type=trigger,
            symbol=lineage.symbol,
            strategy_id=lineage.strategy_id,
            lineage_id=lineage.lineage_id,
            team_members=["RiskGovernor", "ExecutionGate", "WhyWhyAnalyzer", "ChiefRiskOfficerAgent"],
        )

        # D2: Describe Problem
        incident.set_d2_problem(
            expected={
                "expected_edge_bps": float(lineage.expected_edge_bps),
                "expected_holding_horizon_sec": lineage.intent_snapshot.get("expected_holding_horizon_sec", 60),
                "expected_atr": float(lineage.market_state_snapshot.get("atr_1h", 10.0)),
            },
            actual={
                "actual_edge_bps": float(lineage.actual_edge_bps),
                "edge_decay_bps": float(lineage.edge_decay_bps),
                "realized_slippage_bps": float(lineage.slippage_bps),
                "net_pnl_usdt": float(lineage.net_pnl),
                "outcome_grade": lineage.outcome_grade.value,
            },
            impact_usdt=float(abs(lineage.net_pnl)) if lineage.net_pnl < 0 else 0.0,
            context=f"Market regime: {lineage.market_state_snapshot.get('primary_regime', 'UNKNOWN')}, "
                    f"Volatility z-score: {lineage.market_state_snapshot.get('volatility_zscore', '0.0')}",
        )

        # D3: Interim Containment
        containment_action = "COOL_DOWN_SYMBOL"
        params = {"symbol": lineage.symbol, "duration_minutes": 30, "max_leverage": "1.0"}
        if sev == IncidentSeverity.CRITICAL:
            containment_action = "PAUSE_NEW_RISK_GLOBAL"
            params = {"reason": "Rule #0 unknown risk breach"}
        incident.set_d3_containment(containment_action, params, applied_by="EightDManager.auto_containment")
        self.active_containments[lineage.symbol] = params

        # D4: Why-Why Root Cause
        why_tree = WhyWhyAnalyzer.analyze_lineage(lineage)
        incident.set_d4_root_cause(why_tree)
        lineage.why_why_analysis_id = incident.incident_id

        # D5: Formulate PCA
        if trigger == IncidentTriggerType.SLIPPAGE_SPIKE:
            incident.add_d5_pca(
                "Enforce Maker Post-Only Routing",
                "Require DecisionExecutionGate to block taker MARKET orders and enforce POST_ONLY when book depth is thin.",
                target_component="BinanceExecutionAdapter",
            )
        elif trigger == IncidentTriggerType.REGIME_MISMATCH:
            incident.add_d5_pca(
                "Shorten Regime Volatility Lookback",
                "Integrate real-time sub-minute displacement velocity into MarketStateClassifier.",
                target_component="MarketStateClassifier",
            )
        else:
            incident.add_d5_pca(
                "Tighten Stop Distance and Allocation Cap",
                "Reduce max allocation for strategy and place native exchange stop order immediately on fill.",
                target_component="MetaAllocator",
            )

        self.incidents[incident.incident_id] = incident
        logger.warning(
            "Created 8D Incident %s [%s] for %s (%s)",
            incident.incident_id,
            incident.severity.value,
            incident.symbol,
            incident.title,
        )
        return incident

    def advance_and_close(
        self,
        incident_id: str,
        verification_evidence: str,
        systemic_prevention: str,
        closure_lessons: str,
        signoff_agent: str = "ChiefRiskOfficerAgent",
    ) -> bool:
        """
        Advances an incident through D6, D7, and closes it at D8.
        """
        incident = self.get_incident(incident_id)
        if not incident:
            return False

        incident.set_d6_verification(verification_evidence, verification_passed=True)
        incident.add_d7_prevention(systemic_prevention, scope="GLOBAL")
        incident.close_incident(closure_lessons, signoff_agent=signoff_agent)

        # Clear active containment for this symbol if all incidents for it are closed
        if incident.symbol in self.active_containments:
            other_active = [
                i for i in self.incidents.values()
                if i.symbol == incident.symbol and i.status != IncidentStatus.D8_CLOSED
            ]
            if not other_active:
                del self.active_containments[incident.symbol]

        logger.info("8D Incident %s successfully closed at D8", incident_id)
        return True
