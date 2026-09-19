"""Durable staged and autonomous launch-session tests."""

from datetime import datetime, timezone

import pytest

from apps.trading_worker.persistence import PersistenceConfig, PersistenceManager, PersistenceMode
from apps.trading_worker.persistence.postgres.repositories import PersistenceRepository
from apps.trading_worker.main import (
    MAINNET_LAUNCH_AUTONOMOUS,
    MAINNET_LAUNCH_STAGED,
    TradingWorkerApp,
    WorkerEngineState,
    WorkerExecutionMode,
)


IMAGE = "asia-southeast1-docker.pkg.dev/demo/trading-worker@sha256:" + "a" * 64


class LaunchDatabase:
    def __init__(self) -> None:
        self.row = None
        self.reserved = False
        self.submitted = False
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *args: object) -> str:
        self.calls.append((query, args))
        if "INSERT INTO mainnet_launch_sessions" in query:
            if self.row is None:
                self.row = {
                    "launch_id": args[0],
                    "approval_id": args[1],
                    "image_digest": args[2],
                    "symbol": args[3],
                    "policy": args[4],
                    "max_risk_increasing_orders": 1,
                    "reserved_orders": 0,
                    "submitted_orders": 0,
                    "state": "ACTIVE",
                }
            return "INSERT 0 1"
        if "reserved_orders = reserved_orders - 1" in query:
            if self.reserved and not self.submitted:
                self.reserved = False
                return "UPDATE 1"
            return "UPDATE 0"
        if "SET state = 'RECONCILIATION_REQUIRED'" in query:
            if self.row:
                self.row["state"] = "RECONCILIATION_REQUIRED"
            return "UPDATE 1"
        if "state = CASE" in query:
            if self.row and self.row.get("state") == "RECONCILIATION_REQUIRED":
                self.row["state"] = "PAUSED_NEW_RISK"
                return "UPDATE 1"
            return "UPDATE 0"
        if "UPDATE mainnet_launch_sessions" in query and "submitted_orders = 0" in query:
            if (
                self.row
                and self.row.get("symbol") == args[0]
                and self.row.get("approval_id") != args[1]
                and int(self.row.get("submitted_orders", 0) or 0) == 0
            ):
                self.row["state"] = "CLOSED"
                return "UPDATE 1"
            return "UPDATE 0"
        return "INSERT 0 1"

    async def fetchrow(self, query: str, *args: object):
        self.calls.append((query, args))
        if "INSERT INTO mainnet_launch_sessions" in query:
            if self.row is None:
                self.row = {
                    "launch_id": args[0],
                    "approval_id": args[1],
                    "image_digest": args[2],
                    "symbol": args[3],
                    "policy": args[4],
                    "max_risk_increasing_orders": 1,
                    "reserved_orders": 0,
                    "submitted_orders": 0,
                    "state": "ACTIVE",
                }
            return self.row
        if "reserved_orders = reserved_orders + 1" in query:
            if self.row and self.row["state"] == "ACTIVE" and not self.reserved and not self.submitted:
                self.reserved = True
                self.row["reserved_orders"] = 1
                return {"launch_id": args[0]}
            return None
        if "submitted_orders = submitted_orders + 1" in query:
            if self.row and self.reserved and not self.submitted:
                self.submitted = True
                self.row["submitted_orders"] = 1
                self.row["state"] = "PAUSED_NEW_RISK"
                return {"launch_id": args[0], "submitted_orders": 1, "state": "PAUSED_NEW_RISK"}
            return None
        if "WHERE approval_id = $1" in query:
            return self.row
        if "WHERE symbol = $1" in query:
            if "submitted_orders > 0" in query:
                if (
                    self.row
                    and self.row.get("symbol") == args[0]
                    and self.row.get("approval_id") != args[1]
                    and int(self.row.get("submitted_orders", 0) or 0) > 0
                ):
                    return self.row
                return None
            return self.row
        return None


class AutonomousLaunchDatabase:
    """Small asyncpg-shaped double for the durable continuation transitions."""

    def __init__(self) -> None:
        self.row: dict[str, object] | None = None
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *args: object) -> str:
        self.calls.append((query, args))
        if "INSERT INTO mainnet_launch_sessions" in query:
            if self.row is None:
                self.row = {
                    "launch_id": args[0],
                    "approval_id": args[1],
                    "image_digest": args[2],
                    "symbol": args[3],
                    "policy": args[4],
                    "max_risk_increasing_orders": 1,
                    "reserved_orders": 0,
                    "submitted_orders": 0,
                    "state": "ACTIVE",
                    "continuation_approval_id": None,
                }
            return "INSERT 0 1"
        if "state = 'REAUTH_REQUIRED'" in query:
            if self.row and self.row["policy"] == "AUTONOMOUS_AFTER_REVIEW" and self.row["state"] == "AUTONOMOUS_ACTIVE":
                self.row["state"] = "REAUTH_REQUIRED"
                return "UPDATE 1"
            return "UPDATE 0"
        return "UPDATE 0"

    async def fetchrow(self, query: str, *args: object):
        self.calls.append((query, args))
        if "INSERT INTO mainnet_launch_sessions" in query:
            if self.row is None:
                self.row = {
                    "launch_id": args[0],
                    "approval_id": args[1],
                    "image_digest": args[2],
                    "symbol": args[3],
                    "policy": args[4],
                    "max_risk_increasing_orders": 1,
                    "reserved_orders": 0,
                    "submitted_orders": 0,
                    "state": "ACTIVE",
                    "continuation_approval_id": None,
                }
            return self.row
        if "continuation_approval_id = $2" in query:
            if self.row and self.row["image_digest"] == args[3]:
                staged = (
                    self.row["policy"] == "STAGED_FIRST_ORDER"
                    and self.row["state"] == "PAUSED_NEW_RISK"
                    and self.row["submitted_orders"] == 1
                    and self.row["continuation_approval_id"] is None
                )
                reauth = (
                    self.row["policy"] == "AUTONOMOUS_AFTER_REVIEW"
                    and self.row["state"] == "REAUTH_REQUIRED"
                    and int(self.row["submitted_orders"] or 0) >= 1
                )
                if staged or reauth:
                    self.row.update(
                        {
                            "policy": "AUTONOMOUS_AFTER_REVIEW",
                            "max_risk_increasing_orders": None,
                            "continuation_approval_id": args[1],
                            "state": "AUTONOMOUS_ACTIVE",
                        }
                    )
                    return self.row
            return None
        if "reserved_orders = reserved_orders + 1" in query:
            if not self.row:
                return None
            staged = (
                self.row["policy"] == "STAGED_FIRST_ORDER"
                and self.row["state"] == "ACTIVE"
                and self.row["submitted_orders"] == 0
                and self.row["reserved_orders"] < 1
            )
            autonomous = (
                self.row["policy"] == "AUTONOMOUS_AFTER_REVIEW"
                and self.row["state"] == "AUTONOMOUS_ACTIVE"
            )
            if staged or autonomous:
                self.row["reserved_orders"] = int(self.row["reserved_orders"] or 0) + 1
                return {"launch_id": args[0]}
            return None
        if "state = CASE" in query:
            if not self.row or int(self.row["reserved_orders"] or 0) <= int(self.row["submitted_orders"] or 0):
                return None
            if self.row["policy"] == "STAGED_FIRST_ORDER" and self.row["submitted_orders"] >= 1:
                return None
            self.row["submitted_orders"] = int(self.row["submitted_orders"] or 0) + 1
            self.row["state"] = "PAUSED_NEW_RISK" if self.row["policy"] == "STAGED_FIRST_ORDER" else "AUTONOMOUS_ACTIVE"
            return {"launch_id": args[0], "submitted_orders": self.row["submitted_orders"], "state": self.row["state"]}
        if "WHERE approval_id = $1" in query:
            return self.row
        if "WHERE launch_id = $1" in query:
            return self.row if self.row and self.row["launch_id"] == args[0] else None
        if "WHERE symbol = $1" in query:
            if "submitted_orders > 0" in query:
                if (
                    self.row
                    and self.row.get("symbol") == args[0]
                    and self.row.get("approval_id") != args[1]
                    and int(self.row.get("submitted_orders", 0) or 0) > 0
                ):
                    return self.row
                return None
            return self.row
        return None


@pytest.mark.asyncio
async def test_launch_session_reservation_is_atomic_and_restart_safe():
    db = LaunchDatabase()
    repository = PersistenceRepository(db)  # type: ignore[arg-type]

    session = await repository.create_mainnet_launch_session(
        launch_id="launch-approval-1",
        approval_id="approval-1",
        image_digest=IMAGE,
    )
    assert session["state"] == "ACTIVE"
    assert await repository.reserve_mainnet_risk_order("launch-approval-1") is True
    assert await repository.reserve_mainnet_risk_order("launch-approval-1") is False
    assert await repository.mark_mainnet_risk_order_submitted("launch-approval-1") is True
    assert await repository.mark_mainnet_risk_order_submitted("launch-approval-1") is False
    assert db.row["state"] == "PAUSED_NEW_RISK"

    # A new repository/worker sees the durable paused state rather than
    # resetting the one-order budget after a process restart.
    restarted = PersistenceRepository(db)  # type: ignore[arg-type]
    persisted = await restarted.get_active_mainnet_launch()
    assert persisted is not None
    assert persisted["submitted_orders"] == 1
    assert persisted["state"] == "PAUSED_NEW_RISK"


@pytest.mark.asyncio
async def test_cannot_arm_new_session_when_prior_session_has_unreviewed_order():
    db = LaunchDatabase()
    repository = PersistenceRepository(db)  # type: ignore[arg-type]

    # First session is created, reserves order, submits order -> paused
    session = await repository.create_mainnet_launch_session(
        launch_id="launch-approval-orig",
        approval_id="approval-orig",
        image_digest=IMAGE,
    )
    assert session["state"] == "ACTIVE"
    assert await repository.reserve_mainnet_risk_order("launch-approval-orig") is True
    assert await repository.mark_mainnet_risk_order_submitted("launch-approval-orig") is True
    assert db.row["state"] == "PAUSED_NEW_RISK"
    assert db.row["submitted_orders"] == 1

    # Attempting to arm/create a new session for the same symbol with a different approval
    # MUST raise a clear domain error and MUST NOT close the prior session.
    with pytest.raises(
        RuntimeError,
        match="existing session launch-approval-orig has a submitted order pending review",
    ):
        await repository.create_mainnet_launch_session(
            launch_id="launch-approval-new",
            approval_id="approval-new",
            image_digest=IMAGE,
        )

    # Prior session remains unchanged and NOT closed
    assert db.row["state"] == "PAUSED_NEW_RISK"
    assert db.row["launch_id"] == "launch-approval-orig"
    assert db.row["submitted_orders"] == 1


@pytest.mark.asyncio
async def test_definitive_rejection_can_release_but_ambiguous_outcome_cannot():
    db = LaunchDatabase()
    repository = PersistenceRepository(db)  # type: ignore[arg-type]
    await repository.create_mainnet_launch_session(
        launch_id="launch-approval-2",
        approval_id="approval-2",
        image_digest=IMAGE,
    )
    assert await repository.reserve_mainnet_risk_order("launch-approval-2") is True
    assert await repository.release_mainnet_risk_order_reservation("launch-approval-2") is True
    assert await repository.reserve_mainnet_risk_order("launch-approval-2") is True
    assert await repository.mark_mainnet_risk_order_submitted("launch-approval-2") is True
    assert await repository.mark_mainnet_launch_reconciliation_required("launch-approval-2") is True
    assert db.row["state"] == "RECONCILIATION_REQUIRED"
    assert await repository.mark_mainnet_launch_reconciled("launch-approval-2") is True
    assert db.row["state"] == "PAUSED_NEW_RISK"
    # The reconciliation transition clears only the durable ambiguity fence;
    # it does not release the staged slot or resume risk.
    assert db.reserved is True


@pytest.mark.asyncio
async def test_manager_requires_required_durable_persistence_for_launch():
    manager = PersistenceManager(
        db=object(),  # type: ignore[arg-type]
        config=PersistenceConfig(mode=PersistenceMode.OPTIONAL),
    )
    with pytest.raises(RuntimeError, match="durable REQUIRED persistence"):
        await manager.create_mainnet_launch_session(
            approval_id="approval-3",
            image_digest=IMAGE,
        )


@pytest.mark.asyncio
async def test_autonomous_transition_is_atomic_and_restart_requires_reauthorization():
    db = AutonomousLaunchDatabase()
    repository = PersistenceRepository(db)  # type: ignore[arg-type]
    await repository.create_mainnet_launch_session(
        launch_id="launch-approval-autonomous",
        approval_id="approval-autonomous",
        image_digest=IMAGE,
    )
    assert await repository.reserve_mainnet_risk_order("launch-approval-autonomous") is True
    assert await repository.mark_mainnet_risk_order_submitted("launch-approval-autonomous") is True

    activated = await repository.activate_mainnet_autonomous(
        launch_id="launch-approval-autonomous",
        continuation_approval_id="continuation-first",
        first_order_verified_at=datetime.now(timezone.utc),
        image_digest=IMAGE,
    )
    assert activated is not None
    assert activated["policy"] == "AUTONOMOUS_AFTER_REVIEW"
    assert activated["state"] == "AUTONOMOUS_ACTIVE"
    assert activated["max_risk_increasing_orders"] is None

    # Autonomous mode has no session-wide order count; each reservation is
    # still guarded by the Worker risk governor before the exchange call.
    assert await repository.reserve_mainnet_risk_order("launch-approval-autonomous") is True
    assert await repository.mark_mainnet_risk_order_submitted("launch-approval-autonomous") is True
    assert await repository.reserve_mainnet_risk_order("launch-approval-autonomous") is True
    assert await repository.mark_mainnet_risk_order_submitted("launch-approval-autonomous") is True
    assert db.row["submitted_orders"] == 3

    assert await repository.mark_mainnet_launches_reauth_required() == 1
    assert db.row["state"] == "REAUTH_REQUIRED"
    assert db.row["submitted_orders"] == 3
    assert await repository.reserve_mainnet_risk_order("launch-approval-autonomous") is False

    reauthorized = await repository.activate_mainnet_autonomous(
        launch_id="launch-approval-autonomous",
        continuation_approval_id="continuation-after-restart",
        first_order_verified_at=datetime.now(timezone.utc),
        image_digest=IMAGE,
    )
    assert reauthorized is not None
    assert reauthorized["state"] == "AUTONOMOUS_ACTIVE"
    assert reauthorized["continuation_approval_id"] == "continuation-after-restart"
    assert reauthorized["submitted_orders"] == 3


@pytest.mark.asyncio
async def test_live_pause_cannot_be_cleared_without_autonomous_continuation():
    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE
    worker.engine_state = WorkerEngineState.PAUSED_NEW_RISK
    worker.pause_new_risk = True
    worker._mainnet_launch_session = {
        "policy": MAINNET_LAUNCH_STAGED,
        "state": "PAUSED_NEW_RISK",
    }
    assert await worker.set_pause_new_risk(False) is False
    assert worker.pause_new_risk is True

    worker._mainnet_launch_session = {
        "policy": MAINNET_LAUNCH_AUTONOMOUS,
        "state": "AUTONOMOUS_ACTIVE",
    }
    assert await worker.set_pause_new_risk(False) is True
    assert worker.pause_new_risk is False


@pytest.mark.asyncio
async def test_manual_disarm_fences_autonomous_session_before_local_shutdown():
    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE
    worker.engine_state = WorkerEngineState.ARMED
    worker._mainnet_launch_id = "launch-disarm-fence"
    worker._mainnet_launch_session = {
        "launch_id": "launch-disarm-fence",
        "policy": MAINNET_LAUNCH_AUTONOMOUS,
        "state": "AUTONOMOUS_ACTIVE",
    }
    calls: list[str] = []

    async def fence() -> int:
        calls.append("fence")
        return 1

    async def readback(launch_id: str):
        assert launch_id == "launch-disarm-fence"
        calls.append("readback")
        return {
            "launch_id": launch_id,
            "policy": MAINNET_LAUNCH_AUTONOMOUS,
            "state": "REAUTH_REQUIRED",
        }

    worker.persistence.mark_mainnet_launches_reauth_required = fence  # type: ignore[method-assign]
    worker.persistence.get_mainnet_launch_session = readback  # type: ignore[method-assign]

    await worker.disarm()

    assert calls == ["fence", "readback"]
    assert worker.engine_state == WorkerEngineState.DISARMED
    assert worker._mainnet_launch_session["state"] == "REAUTH_REQUIRED"
    assert worker.pause_new_risk is False


@pytest.mark.asyncio
async def test_manual_disarm_stays_local_fail_closed_when_fence_fails():
    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE
    worker.engine_state = WorkerEngineState.ARMED
    worker._mainnet_launch_id = "launch-disarm-uncertain"
    worker._mainnet_launch_session = {
        "launch_id": "launch-disarm-uncertain",
        "policy": MAINNET_LAUNCH_AUTONOMOUS,
        "state": "AUTONOMOUS_ACTIVE",
    }

    async def fence_failure() -> int:
        raise RuntimeError("database unavailable")

    worker.persistence.mark_mainnet_launches_reauth_required = fence_failure  # type: ignore[method-assign]

    await worker.disarm()

    assert worker.engine_state == WorkerEngineState.DISARMED
    assert worker._mainnet_launch_session is None
    assert worker._mainnet_launch_id is None


@pytest.mark.asyncio
async def test_mainnet_kill_switch_keeps_new_risk_paused_after_release_attempt():
    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE
    worker.engine_state = WorkerEngineState.ARMED
    worker.active_configuration = {"executionMode": "LIVE"}
    worker._mainnet_launch_id = "launch-kill-fence"
    worker._mainnet_launch_session = {
        "launch_id": "launch-kill-fence",
        "policy": MAINNET_LAUNCH_AUTONOMOUS,
        "state": "AUTONOMOUS_ACTIVE",
    }
    calls: list[str] = []

    async def fence() -> int:
        calls.append("fence")
        worker._mainnet_launch_session["state"] = "REAUTH_REQUIRED"
        return 1

    async def readback(launch_id: str):
        assert launch_id == "launch-kill-fence"
        calls.append("readback")
        return worker._mainnet_launch_session

    worker.persistence.mark_mainnet_launches_reauth_required = fence  # type: ignore[method-assign]
    worker.persistence.get_mainnet_launch_session = readback  # type: ignore[method-assign]

    result = await worker.set_kill_switch(True)

    assert result["status"] == "UNKNOWN"
    assert calls == ["fence", "readback"]
    assert worker.kill_switch_active is True
    assert worker.pause_new_risk is True
    assert worker.engine_state == WorkerEngineState.EMERGENCY
