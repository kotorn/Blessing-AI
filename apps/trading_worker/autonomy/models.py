"""Typed contracts for the Laya-style Blessing AI supervisor.

The supervisor can allow or restrict the existing strategy pipeline, but it
cannot create an exchange OrderIntent or bypass the Trading Worker's execution
gates.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from domain.enums import RiskState
from domain.models import utc_now


class AutonomyAction(StrEnum):
    """Deterministic Worker control action emitted by the supervisor."""

    EMERGENCY = "EMERGENCY"
    RECOVERY_ONLY = "RECOVERY_ONLY"
    PAUSE_NEW_RISK = "PAUSE_NEW_RISK"
    OBSERVE_ONLY = "OBSERVE_ONLY"
    ALLOW_PIPELINE = "ALLOW_PIPELINE"


class AdvisoryAction(StrEnum):
    """Restricted vocabulary accepted from Laya/JEV/LLM shadow advisers.

    Advisers cannot request RECOVERY or EMERGENCY because those paths may cause
    mutable risk-reduction actions.  Only authoritative deterministic Worker
    facts may select those states.
    """

    CONTINUE = "CONTINUE"
    PAUSE_NEW_RISK = "PAUSE_NEW_RISK"
    OBSERVE_ONLY = "OBSERVE_ONLY"


class AutonomyObservation(BaseModel):
    """Authoritative facts consumed by one supervisor evaluation tick."""

    model_config = ConfigDict(frozen=True)

    observed_at: datetime = Field(default_factory=utc_now)
    execution_mode: str
    engine_state: str
    reconciliation_status: str
    account_synchronized: bool
    market_data_healthy: bool
    private_stream_healthy: bool
    trading_connection_healthy: bool
    kill_switch_active: bool
    risk_state: RiskState
    release_approved: bool = False
    execution_lease_held: bool = False
    pending_ambiguous_execution: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class AutonomyDecision(BaseModel):
    """A control decision for the strategy pipeline, never an order decision."""

    model_config = ConfigDict(frozen=True)

    action: AutonomyAction
    reason_codes: tuple[str, ...]
    observed_at: datetime
    advisory_action: AdvisoryAction | None = None
    advisory_source: str | None = None

    @property
    def new_risk_allowed(self) -> bool:
        return self.action == AutonomyAction.ALLOW_PIPELINE
