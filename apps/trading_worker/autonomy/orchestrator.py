"""Laya-style observe -> decide supervisor for Blessing AI."""

from __future__ import annotations

from .models import AutonomyAction, AutonomyDecision, AutonomyObservation
from .policy import DeterministicAutonomyPolicy


# Lower is safer/more restrictive.  An advisory model may only move the
# supervisor toward a safer action; it can never grant more execution authority.
_ACTION_SAFETY_RANK: dict[AutonomyAction, int] = {
    AutonomyAction.EMERGENCY: 0,
    AutonomyAction.RECOVERY_ONLY: 1,
    AutonomyAction.PAUSE_NEW_RISK: 2,
    AutonomyAction.OBSERVE_ONLY: 3,
    AutonomyAction.ALLOW_PIPELINE: 4,
}


class AutonomySupervisor:
    """Evaluate one event-loop tick without creating or submitting orders."""

    def __init__(self, policy: DeterministicAutonomyPolicy | None = None) -> None:
        self.policy = policy or DeterministicAutonomyPolicy()

    def step(
        self,
        observation: AutonomyObservation,
        *,
        advisory_action: AutonomyAction | None = None,
        advisory_source: str | None = None,
    ) -> AutonomyDecision:
        """Return the effective pipeline permission for one observation.

        Laya/JEV/LLM output is intentionally advisory.  It can downgrade a
        deterministic permission (for example ALLOW -> PAUSE), but it cannot
        upgrade PAUSE/RECOVERY/EMERGENCY into ALLOW.
        """

        deterministic = self.policy.evaluate(observation)
        if advisory_action is None:
            return deterministic

        effective = deterministic.action
        reasons = list(deterministic.reason_codes)
        if _ACTION_SAFETY_RANK[advisory_action] < _ACTION_SAFETY_RANK[effective]:
            effective = advisory_action
            reasons.append("ADVISORY_DOWNGRADE")
        elif advisory_action != effective:
            reasons.append("ADVISORY_UPGRADE_IGNORED")
        else:
            reasons.append("ADVISORY_AGREED")

        return AutonomyDecision(
            action=effective,
            reason_codes=tuple(reasons),
            observed_at=observation.observed_at,
            advisory_action=advisory_action,
            advisory_source=advisory_source,
        )
