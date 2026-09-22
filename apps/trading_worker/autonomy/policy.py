"""Deterministic safety policy for Laya-style orchestration.

The policy deliberately decides *pipeline permission*, not trade direction,
quantity, or order submission.  All mutable execution remains behind the
existing RiskGovernor, decision gate, order gate, reconciliation, and venue
adapter.
"""

from __future__ import annotations

from domain.enums import RiskState

from .models import AutonomyAction, AutonomyDecision, AutonomyObservation


class DeterministicAutonomyPolicy:
    """Map authoritative worker observations to a fail-closed control action."""

    _VALID_MODES = frozenset({"PAPER", "TESTNET", "LIVE"})

    def evaluate(self, observation: AutonomyObservation) -> AutonomyDecision:
        reasons: list[str] = []
        mode = observation.execution_mode.strip().upper()
        engine = observation.engine_state.strip().upper()
        reconciliation = observation.reconciliation_status.strip().upper()

        if observation.kill_switch_active or engine == "EMERGENCY":
            return self._decision(observation, AutonomyAction.EMERGENCY, "KILL_OR_EMERGENCY")

        if observation.risk_state in {RiskState.EMERGENCY, RiskState.LIQUIDATING}:
            return self._decision(observation, AutonomyAction.EMERGENCY, "RISK_STATE_EMERGENCY")

        if observation.pending_ambiguous_execution:
            return self._decision(
                observation,
                AutonomyAction.RECOVERY_ONLY,
                "AMBIGUOUS_EXECUTION_PENDING",
            )

        if observation.risk_state in {RiskState.RECOVERY_ONLY, RiskState.DELEVERAGE}:
            return self._decision(observation, AutonomyAction.RECOVERY_ONLY, "RISK_STATE_RECOVERY")

        if engine == "RECOVERY_ONLY":
            return self._decision(observation, AutonomyAction.RECOVERY_ONLY, "ENGINE_RECOVERY_ONLY")

        if observation.risk_state == RiskState.NO_NEW_RISK or engine == "PAUSED_NEW_RISK":
            return self._decision(observation, AutonomyAction.PAUSE_NEW_RISK, "NEW_RISK_PAUSED")

        if mode not in self._VALID_MODES:
            return self._decision(observation, AutonomyAction.OBSERVE_ONLY, "UNKNOWN_EXECUTION_MODE")

        if engine in {"DISARMED", "ARMING"}:
            return self._decision(observation, AutonomyAction.OBSERVE_ONLY, "ENGINE_NOT_ARMED")

        if engine != "ARMED":
            return self._decision(observation, AutonomyAction.OBSERVE_ONLY, "UNKNOWN_ENGINE_STATE")

        # PAPER can use local simulated state; connected execution requires
        # authoritative exchange health and reconciliation.
        if mode in {"TESTNET", "LIVE"}:
            if reconciliation != "IN_SYNC" or not observation.account_synchronized:
                return self._decision(
                    observation,
                    AutonomyAction.RECOVERY_ONLY,
                    "EXCHANGE_STATE_NOT_IN_SYNC",
                )

            if not observation.private_stream_healthy:
                reasons.append("PRIVATE_STREAM_UNHEALTHY")
            if not observation.trading_connection_healthy:
                reasons.append("TRADING_CONNECTION_UNHEALTHY")
            if not observation.market_data_healthy:
                reasons.append("MARKET_DATA_UNHEALTHY")
            if reasons:
                return AutonomyDecision(
                    action=AutonomyAction.PAUSE_NEW_RISK,
                    reason_codes=tuple(reasons),
                    observed_at=observation.observed_at,
                )

        # Mainnet cannot be promoted by the supervisor.  Release approval and
        # an account/environment-scoped execution lease must already exist.
        if mode == "LIVE":
            if not observation.release_approved:
                return self._decision(
                    observation,
                    AutonomyAction.OBSERVE_ONLY,
                    "MAINNET_RELEASE_NOT_APPROVED",
                )
            if not observation.execution_lease_held:
                return self._decision(
                    observation,
                    AutonomyAction.OBSERVE_ONLY,
                    "MAINNET_EXECUTION_LEASE_MISSING",
                )

        return self._decision(observation, AutonomyAction.ALLOW_PIPELINE, "SAFETY_ENVELOPE_OK")

    @staticmethod
    def _decision(
        observation: AutonomyObservation,
        action: AutonomyAction,
        reason: str,
    ) -> AutonomyDecision:
        return AutonomyDecision(
            action=action,
            reason_codes=(reason,),
            observed_at=observation.observed_at,
        )
