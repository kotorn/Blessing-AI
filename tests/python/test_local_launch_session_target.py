from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.trading_worker.persistence.postgres.repositories import PersistenceRepository


IMAGE = "asia-southeast1-docker.pkg.dev/demo/trading-worker@sha256:" + "a" * 64
LOCAL_FINGERPRINT = "b" * 64
OTHER_FINGERPRINT = "c" * 64


class LaunchIdentityDatabase:
    """Asyncpg-shaped double for launch identity persistence transitions."""

    def __init__(self) -> None:
        self.row: dict[str, object] | None = None

    async def execute(self, query: str, *args: object) -> str:
        if "INSERT INTO mainnet_launch_sessions" in query:
            if self.row is None:
                self.row = {
                    "launch_id": args[0],
                    "approval_id": args[1],
                    "image_digest": args[2],
                    "symbol": args[3],
                    "policy": args[4],
                    "max_risk_increasing_orders": args[5],
                    "reserved_orders": 0,
                    "submitted_orders": 0,
                    "state": "ACTIVE",
                    "runtime_target": args[6],
                    "runtime_fingerprint": args[7],
                }
            return "INSERT 0 1"
        return "UPDATE 0"

    async def fetchrow(self, query: str, *args: object):
        if "INSERT INTO mainnet_launch_sessions" in query:
            return self.row
        if "continuation_approval_id = $2" in query:
            if self.row is None:
                return None
            identity_matches = (
                self.row["runtime_target"] == args[4]
                and (
                    (
                        args[4] == "LOCAL"
                        and self.row["image_digest"] is None
                        and self.row["runtime_fingerprint"] == args[5]
                    )
                    or (
                        args[4] == "CLOUD_RUN"
                        and self.row["image_digest"] == args[3]
                    )
                )
            )
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
            if identity_matches and (staged or reauth):
                self.row.update(
                    {
                        "policy": "AUTONOMOUS_AFTER_REVIEW",
                        "max_risk_increasing_orders": None,
                        "continuation_approval_id": args[1],
                        "first_order_verified_at": args[2],
                        "state": "AUTONOMOUS_ACTIVE",
                    }
                )
                return self.row
            return None
        if "WHERE approval_id = $1" in query:
            return self.row
        if "WHERE launch_id = $1" in query:
            return self.row if self.row and self.row["launch_id"] == args[0] else None
        if "WHERE symbol = $1" in query:
            if "submitted_orders > 0" in query:
                return None
            return self.row
        return None


@pytest.mark.asyncio
async def test_local_launch_identity_is_created_and_replayed_without_digest():
    db = LaunchIdentityDatabase()
    repository = PersistenceRepository(db)  # type: ignore[arg-type]

    created = await repository.create_mainnet_launch_session(
        launch_id="launch-local-1",
        approval_id="approval-local-1",
        runtime_target="LOCAL",
        runtime_fingerprint=LOCAL_FINGERPRINT,
    )
    replayed = await repository.create_mainnet_launch_session(
        launch_id="launch-local-1",
        approval_id="approval-local-1",
        runtime_target="LOCAL",
        runtime_fingerprint=LOCAL_FINGERPRINT,
    )

    assert created["runtime_target"] == "LOCAL"
    assert created["image_digest"] is None
    assert created["runtime_fingerprint"] == LOCAL_FINGERPRINT
    assert replayed["launch_id"] == "launch-local-1"


@pytest.mark.asyncio
async def test_local_launch_identity_mismatch_is_rejected_after_restart_read():
    db = LaunchIdentityDatabase()
    repository = PersistenceRepository(db)  # type: ignore[arg-type]
    await repository.create_mainnet_launch_session(
        launch_id="launch-local-2",
        approval_id="approval-local-2",
        runtime_target="LOCAL",
        runtime_fingerprint=LOCAL_FINGERPRINT,
    )

    restarted = PersistenceRepository(db)  # type: ignore[arg-type]
    persisted = await restarted.get_mainnet_launch("launch-local-2")
    assert persisted is not None
    assert persisted["runtime_target"] == "LOCAL"
    assert persisted["runtime_fingerprint"] == LOCAL_FINGERPRINT
    assert persisted["image_digest"] is None

    with pytest.raises(RuntimeError, match="does not match release approval"):
        await restarted.create_mainnet_launch_session(
            launch_id="launch-local-2",
            approval_id="approval-local-2",
            runtime_target="LOCAL",
            runtime_fingerprint=OTHER_FINGERPRINT,
        )


@pytest.mark.asyncio
async def test_local_activation_requires_matching_identity_and_replay_is_blocked():
    db = LaunchIdentityDatabase()
    repository = PersistenceRepository(db)  # type: ignore[arg-type]
    await repository.create_mainnet_launch_session(
        launch_id="launch-local-3",
        approval_id="approval-local-3",
        runtime_target="LOCAL",
        runtime_fingerprint=LOCAL_FINGERPRINT,
    )
    assert db.row is not None
    db.row.update(
        {
            "state": "PAUSED_NEW_RISK",
            "reserved_orders": 1,
            "submitted_orders": 1,
            "continuation_approval_id": None,
        }
    )

    assert (
        await repository.activate_mainnet_autonomous(
            launch_id="launch-local-3",
            continuation_approval_id="continuation-local-3",
            first_order_verified_at=datetime.now(UTC),
            runtime_target="LOCAL",
            runtime_fingerprint=OTHER_FINGERPRINT,
        )
        is None
    )
    activated = await repository.activate_mainnet_autonomous(
        launch_id="launch-local-3",
        continuation_approval_id="continuation-local-3",
        first_order_verified_at=datetime.now(UTC),
        runtime_target="LOCAL",
        runtime_fingerprint=LOCAL_FINGERPRINT,
    )
    assert activated is not None
    assert activated["runtime_target"] == "LOCAL"
    assert activated["image_digest"] is None

    assert (
        await repository.activate_mainnet_autonomous(
            launch_id="launch-local-3",
            continuation_approval_id="continuation-local-replay",
            first_order_verified_at=datetime.now(UTC),
            runtime_target="LOCAL",
            runtime_fingerprint=LOCAL_FINGERPRINT,
        )
        is None
    )


@pytest.mark.asyncio
async def test_cloud_run_default_remains_digest_bound():
    db = LaunchIdentityDatabase()
    repository = PersistenceRepository(db)  # type: ignore[arg-type]

    session = await repository.create_mainnet_launch_session(
        launch_id="launch-cloud-1",
        approval_id="approval-cloud-1",
        image_digest=IMAGE,
    )

    assert session["runtime_target"] == "CLOUD_RUN"
    assert session["image_digest"] == IMAGE
    assert session["runtime_fingerprint"] is None

    with pytest.raises(ValueError, match="Local launch sessions must not have an image digest"):
        await repository.create_mainnet_launch_session(
            launch_id="launch-invalid-local",
            approval_id="approval-invalid-local",
            image_digest=IMAGE,
            runtime_target="LOCAL",
            runtime_fingerprint=LOCAL_FINGERPRINT,
        )


