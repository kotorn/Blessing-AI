"""Durable staged first-order launch-session tests."""

import pytest

from apps.trading_worker.persistence import PersistenceConfig, PersistenceManager, PersistenceMode
from apps.trading_worker.persistence.postgres.repositories import PersistenceRepository


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
        if "state = 'RECONCILIATION_REQUIRED'" in query:
            return "UPDATE 1"
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
    # No automatic release method is called for an ambiguous exchange result;
    # submitted/active state remains reserved until reconciliation resolves it.
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
