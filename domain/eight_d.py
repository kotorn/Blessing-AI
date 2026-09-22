"""
8D Problem Solving and 5-Why Root Cause Incident Domain for Blessing AI.
Provides disciplined, enterprise-grade closed-loop investigation for trade drift,
unexpected drawdowns, and execution anomalies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from domain.models import utc_now


class IncidentSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class IncidentStatus(str, Enum):
    D1_ESTABLISHED = "D1_ESTABLISHED"
    D2_DESCRIBED = "D2_DESCRIBED"
    D3_CONTAINED = "D3_CONTAINED"
    D4_ROOT_CAUSE_FOUND = "D4_ROOT_CAUSE_FOUND"
    D5_PCA_CHOSEN = "D5_PCA_CHOSEN"
    D6_PCA_VERIFIED = "D6_PCA_VERIFIED"
    D7_PREVENTION_APPLIED = "D7_PREVENTION_APPLIED"
    D8_CLOSED = "D8_CLOSED"


class IncidentTriggerType(str, Enum):
    DRAWDOWN_BREACH = "DRAWDOWN_BREACH"
    SLIPPAGE_SPIKE = "SLIPPAGE_SPIKE"
    UNEXPECTED_LOSS = "UNEXPECTED_LOSS"
    REGIME_MISMATCH = "REGIME_MISMATCH"
    EDGE_DECAY = "EDGE_DECAY"
    DATA_QUALITY = "DATA_QUALITY"
    RULE_ZERO_BREACH = "RULE_ZERO_BREACH"


@dataclass
class WhyWhyTree:
    problem_statement: str
    levels: List[Dict[str, str]] = field(default_factory=list)
    root_cause_summary: str = ""
    preventive_insight: str = ""

    def add_level(self, level_num: int, question: str, answer: str) -> None:
        self.levels.append({
            "level": f"Why #{level_num}",
            "question": question,
            "answer": answer,
        })

    def to_dict(self) -> Dict[str, Any]:
        return {
            "problem_statement": self.problem_statement,
            "levels": self.levels,
            "root_cause_summary": self.root_cause_summary,
            "preventive_insight": self.preventive_insight,
        }


@dataclass
class EightDIncident:
    incident_id: str
    title: str
    severity: IncidentSeverity
    status: IncidentStatus
    trigger_type: IncidentTriggerType
    symbol: Optional[str] = None
    strategy_id: Optional[str] = None
    lineage_id: Optional[str] = None

    # D1: Establish the Cross-Functional AI/Agent Team
    d1_team: List[str] = field(default_factory=list)

    # D2: Describe the Problem (Quantified Gap: Expected vs Actual)
    d2_problem: Dict[str, Any] = field(default_factory=dict)

    # D3: Interim Containment Actions (Immediate mitigation to protect capital)
    d3_containment: Dict[str, Any] = field(default_factory=dict)

    # D4: Root Cause Analysis (Integrated 5-Why analysis)
    d4_root_cause: Optional[WhyWhyTree] = None

    # D5: Formulate & Select Permanent Corrective Actions (PCA)
    d5_pca: List[Dict[str, Any]] = field(default_factory=list)

    # D6: Implement & Validate PCA (Evidence proving the flaw is removed)
    d6_verification: Dict[str, Any] = field(default_factory=dict)

    # D7: Prevent Recurrence (Systemic updates across all strategies/venues)
    d7_prevention: List[Dict[str, Any]] = field(default_factory=list)

    # D8: Recognize Team & Close Incident (Knowledge cataloging)
    d8_closure: Dict[str, Any] = field(default_factory=dict)

    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    closed_at: Optional[datetime] = None

    @classmethod
    def create(
        cls,
        title: str,
        severity: IncidentSeverity,
        trigger_type: IncidentTriggerType,
        symbol: Optional[str] = None,
        strategy_id: Optional[str] = None,
        lineage_id: Optional[str] = None,
        team_members: Optional[List[str]] = None,
    ) -> EightDIncident:
        incident_id = f"8D-{utc_now().strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"
        default_team = team_members or ["RiskGovernor", "ExecutionGate", "LearningEngine"]
        return cls(
            incident_id=incident_id,
            title=title,
            severity=severity,
            status=IncidentStatus.D1_ESTABLISHED,
            trigger_type=trigger_type,
            symbol=symbol,
            strategy_id=strategy_id,
            lineage_id=lineage_id,
            d1_team=default_team,
        )

    def set_d2_problem(self, expected: Dict[str, Any], actual: Dict[str, Any], impact_usdt: float, context: str) -> None:
        self.d2_problem = {
            "expected_behavior": expected,
            "actual_outcome": actual,
            "financial_impact_usdt": impact_usdt,
            "context_summary": context,
            "recorded_at": utc_now().isoformat(),
        }
        self.status = IncidentStatus.D2_DESCRIBED
        self.updated_at = utc_now()

    def set_d3_containment(self, action_name: str, parameters: Dict[str, Any], applied_by: str) -> None:
        self.d3_containment = {
            "action_name": action_name,
            "parameters": parameters,
            "applied_by": applied_by,
            "applied_at": utc_now().isoformat(),
            "status": "CONTAINED",
        }
        self.status = IncidentStatus.D3_CONTAINED
        self.updated_at = utc_now()

    def set_d4_root_cause(self, why_tree: WhyWhyTree) -> None:
        self.d4_root_cause = why_tree
        self.status = IncidentStatus.D4_ROOT_CAUSE_FOUND
        self.updated_at = utc_now()

    def add_d5_pca(self, title: str, description: str, target_component: str) -> None:
        self.d5_pca.append({
            "pca_id": f"PCA-{len(self.d5_pca) + 1}",
            "title": title,
            "description": description,
            "target_component": target_component,
            "staged_at": utc_now().isoformat(),
        })
        self.status = IncidentStatus.D5_PCA_CHOSEN
        self.updated_at = utc_now()

    def set_d6_verification(self, test_evidence: str, verification_passed: bool) -> None:
        self.d6_verification = {
            "evidence": test_evidence,
            "passed": verification_passed,
            "verified_at": utc_now().isoformat(),
        }
        if verification_passed:
            self.status = IncidentStatus.D6_PCA_VERIFIED
        self.updated_at = utc_now()

    def add_d7_prevention(self, policy_update: str, scope: str = "GLOBAL") -> None:
        self.d7_prevention.append({
            "policy_update": policy_update,
            "scope": scope,
            "applied_at": utc_now().isoformat(),
        })
        self.status = IncidentStatus.D7_PREVENTION_APPLIED
        self.updated_at = utc_now()

    def close_incident(self, summary: str, signoff_agent: str = "ChiefRiskOfficerAgent") -> None:
        self.d8_closure = {
            "lessons_learned_summary": summary,
            "signoff_agent": signoff_agent,
            "closed_at": utc_now().isoformat(),
        }
        self.status = IncidentStatus.D8_CLOSED
        self.closed_at = utc_now()
        self.updated_at = utc_now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "title": self.title,
            "severity": self.severity.value,
            "status": self.status.value,
            "trigger_type": self.trigger_type.value,
            "symbol": self.symbol,
            "strategy_id": self.strategy_id,
            "lineage_id": self.lineage_id,
            "d1_team": self.d1_team,
            "d2_problem": self.d2_problem,
            "d3_containment": self.d3_containment,
            "d4_root_cause": self.d4_root_cause.to_dict() if self.d4_root_cause else None,
            "d5_pca": self.d5_pca,
            "d6_verification": self.d6_verification,
            "d7_prevention": self.d7_prevention,
            "d8_closure": self.d8_closure,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
        }
