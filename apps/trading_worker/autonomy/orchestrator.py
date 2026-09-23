"""Laya-style observe -> decide supervisor for Blessing AI."""

from __future__ import annotations

from .models import AdvisoryAction, AutonomyAction, AutonomyDecision, AutonomyObservation
from .policy import DeterministicAutonomyPolicy


class AutonomySupervisor:
    """Evaluate one event-loop tick without creating or submitting orders."""

    def __init__(self, policy: DeterministicAutonomyPolicy | None = None) -> None:
        self.policy = policy or DeterministicAutonomyPolicy()

    def step(
        self,
        observation: AutonomyObservation,
        *,
        advisory_action: AdvisoryAction | None = None,
        advisory_source: str | None = None,
    ) -> AutonomyDecision:
        """Return the effective pipeline permission for one observation.

        Laya/JEV/LLM output is intentionally non-authorizing.  It may pause new
        risk or request observation-only when the deterministic policy would
        otherwise allow more activity.  It can never select recovery/emergency,
        override those hard states, or turn a restrictive hard decision into a
        more permissive one.
        """

        deterministic = self.policy.evaluate(observation)
        if advisory_action is None:
            return deterministic

        effective = deterministic.action
        reasons = list(deterministic.reason_codes)

        # Hard recovery/emergency paths are authoritative and cannot be altered
        # by an AI adviser in either direction.
        if deterministic.action in {
            AutonomyAction.EMERGENCY,
            AutonomyAction.RECOVERY_ONLY,
        }:
            reasons.append("ADVISORY_NO_EFFECT_ON_HARD_STATE")
        elif advisory_action == AdvisoryAction.CONTINUE:
            if deterministic.action == AutonomyAction.ALLOW_PIPELINE:
                reasons.append("ADVISORY_AGREED")
            else:
                reasons.append("ADVISORY_CONTINUE_IGNORED")
        elif advisory_action == AdvisoryAction.PAUSE_NEW_RISK:
            if deterministic.action == AutonomyAction.ALLOW_PIPELINE:
                effective = AutonomyAction.PAUSE_NEW_RISK
                reasons.append("ADVISORY_DOWNGRADE")
            elif deterministic.action == AutonomyAction.PAUSE_NEW_RISK:
                reasons.append("ADVISORY_AGREED")
            else:
                # OBSERVE_ONLY is already more restrictive than PAUSE.
                reasons.append("ADVISORY_NO_EFFECT")
        elif advisory_action == AdvisoryAction.OBSERVE_ONLY:
            if deterministic.action in {
                AutonomyAction.ALLOW_PIPELINE,
                AutonomyAction.PAUSE_NEW_RISK,
            }:
                effective = AutonomyAction.OBSERVE_ONLY
                reasons.append("ADVISORY_DOWNGRADE")
            else:
                reasons.append("ADVISORY_AGREED")

        return AutonomyDecision(
            action=effective,
            reason_codes=tuple(reasons),
            observed_at=observation.observed_at,
            advisory_action=advisory_action,
            advisory_source=advisory_source,
        )
