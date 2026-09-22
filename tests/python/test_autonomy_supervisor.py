from domain.enums import RiskState

from apps.trading_worker.autonomy import (
    AdvisoryAction,
    AutonomyAction,
    AutonomyObservation,
    AutonomySupervisor,
)


def _observation(**overrides: object) -> AutonomyObservation:
    values: dict[str, object] = {
        "execution_mode": "TESTNET",
        "engine_state": "ARMED",
        "reconciliation_status": "IN_SYNC",
        "account_synchronized": True,
        "market_data_healthy": True,
        "private_stream_healthy": True,
        "trading_connection_healthy": True,
        "kill_switch_active": False,
        "risk_state": RiskState.NORMAL,
        "release_approved": False,
        "execution_lease_held": False,
        "pending_ambiguous_execution": False,
    }
    values.update(overrides)
    return AutonomyObservation(**values)


def test_healthy_testnet_allows_existing_pipeline() -> None:
    decision = AutonomySupervisor().step(_observation())

    assert decision.action == AutonomyAction.ALLOW_PIPELINE
    assert decision.new_risk_allowed is True


def test_kill_switch_is_emergency() -> None:
    decision = AutonomySupervisor().step(_observation(kill_switch_active=True))

    assert decision.action == AutonomyAction.EMERGENCY
    assert decision.new_risk_allowed is False


def test_reconciliation_mismatch_forces_recovery_only() -> None:
    decision = AutonomySupervisor().step(
        _observation(reconciliation_status="MISMATCH", account_synchronized=False)
    )

    assert decision.action == AutonomyAction.RECOVERY_ONLY


def test_unhealthy_market_data_pauses_new_risk() -> None:
    decision = AutonomySupervisor().step(_observation(market_data_healthy=False))

    assert decision.action == AutonomyAction.PAUSE_NEW_RISK


def test_mainnet_needs_release_and_execution_lease() -> None:
    supervisor = AutonomySupervisor()

    no_release = supervisor.step(_observation(execution_mode="LIVE"))
    assert no_release.action == AutonomyAction.OBSERVE_ONLY

    no_lease = supervisor.step(
        _observation(execution_mode="LIVE", release_approved=True)
    )
    assert no_lease.action == AutonomyAction.OBSERVE_ONLY

    ready = supervisor.step(
        _observation(
            execution_mode="LIVE",
            release_approved=True,
            execution_lease_held=True,
        )
    )
    assert ready.action == AutonomyAction.ALLOW_PIPELINE


def test_advisory_can_downgrade_allow_to_pause() -> None:
    decision = AutonomySupervisor().step(
        _observation(),
        advisory_action=AdvisoryAction.PAUSE_NEW_RISK,
        advisory_source="laya-shadow",
    )

    assert decision.action == AutonomyAction.PAUSE_NEW_RISK
    assert "ADVISORY_DOWNGRADE" in decision.reason_codes


def test_advisory_can_downgrade_allow_to_observe_only() -> None:
    decision = AutonomySupervisor().step(
        _observation(),
        advisory_action=AdvisoryAction.OBSERVE_ONLY,
        advisory_source="laya-shadow",
    )

    assert decision.action == AutonomyAction.OBSERVE_ONLY
    assert "ADVISORY_DOWNGRADE" in decision.reason_codes


def test_advisory_continue_cannot_upgrade_recovery_to_allow() -> None:
    decision = AutonomySupervisor().step(
        _observation(reconciliation_status="MISMATCH"),
        advisory_action=AdvisoryAction.CONTINUE,
        advisory_source="laya-shadow",
    )

    assert decision.action == AutonomyAction.RECOVERY_ONLY
    assert "ADVISORY_NO_EFFECT_ON_HARD_STATE" in decision.reason_codes


def test_advisory_pause_cannot_upgrade_observe_only() -> None:
    decision = AutonomySupervisor().step(
        _observation(engine_state="DISARMED"),
        advisory_action=AdvisoryAction.PAUSE_NEW_RISK,
        advisory_source="laya-shadow",
    )

    assert decision.action == AutonomyAction.OBSERVE_ONLY
    assert "ADVISORY_NO_EFFECT" in decision.reason_codes


def test_advisory_cannot_override_emergency() -> None:
    decision = AutonomySupervisor().step(
        _observation(kill_switch_active=True),
        advisory_action=AdvisoryAction.CONTINUE,
        advisory_source="laya-shadow",
    )

    assert decision.action == AutonomyAction.EMERGENCY
    assert "ADVISORY_NO_EFFECT_ON_HARD_STATE" in decision.reason_codes
