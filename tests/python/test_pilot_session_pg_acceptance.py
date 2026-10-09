"""Disposable PostgreSQL integration for pilot session ARM and cap backfills.

Requires BLESSING_MIGRATION_TEST_DSN pointed at a disposable loopback PostgreSQL
instance. Each test creates a random database and removes only that database.
"""

import asyncio
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from apps.trading_worker.persistence.postgres.repositories import PersistenceRepository
from scripts.apply_local_postgres_migrations import apply_migrations

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "infra" / "postgres" / "migrations"
INIT_SCHEMA = ROOT / "infra" / "postgres" / "init_schema.sql"


def _dsn_for(database: str) -> str:
    dsn = os.environ.get("BLESSING_MIGRATION_TEST_DSN", "")
    if not dsn:
        pytest.skip("Set BLESSING_MIGRATION_TEST_DSN to isolated loopback PostgreSQL")
    parts = urlsplit(dsn)
    if parts.hostname != "127.0.0.1":
        pytest.fail("Session acceptance DSN must target 127.0.0.1", pytrace=False)
    return urlunsplit(parts._replace(path=f"/{database}"))


async def _connect(database: str):
    return await asyncpg.connect(
        dsn=_dsn_for(database), timeout=5, server_settings={"search_path": "public"}
    )


async def _insert_pilot(connection, launch_id: str, campaign_id: str) -> None:
    h = "c" * 64
    await connection.execute(
        """INSERT INTO mainnet_launch_sessions
           (launch_id, approval_id, symbol, policy, state, runtime_target,
            runtime_fingerprint, max_risk_increasing_orders, pilot_campaign_id,
            pilot_git_sha, pilot_source_hash, pilot_dependency_hash,
            pilot_migration_hash, pilot_strategy_hash, pilot_secret_project_id,
            pilot_api_key_version, pilot_api_secret_version, pilot_management_mode,
            pilot_campaign_expires_at, pilot_risk_policy_hash,
            pilot_max_position_notional_usdc, pilot_per_position_risk_usdc,
            pilot_max_drawdown_usdc, pilot_quick_target_net_usdc,
            pilot_quick_max_hold_seconds, pilot_max_leverage, pilot_status)
           VALUES ($1, $2, 'ETHUSDC', 'LIVE_RESEARCH_PILOT', 'ACTIVE', 'LOCAL',
            $3, $4, $5, $6, $7, $7, $7, $7, 'test-project', '1', '2', 'QUICK',
            CURRENT_TIMESTAMP + INTERVAL '7 days', $7, 50, 2, 5, 0.25, 86400, 10, 'ACTIVE')""",
        launch_id,
        f"approval-{launch_id}",
        "d" * 64,
        1,
        campaign_id,
        "e" * 40,
        h,
    )


def _migration_subset(tmp_path: Path, versions: range) -> Path:
    selected = tmp_path / f"migrations-{versions.start}-{versions.stop - 1}"
    selected.mkdir()
    for version in versions:
        matches = list(MIGRATIONS.glob(f"{version:03d}_*.sql"))
        assert len(matches) == 1
        shutil.copy2(matches[0], selected / matches[0].name)
    return selected


def test_fresh_020_population_021_022_and_concurrent_session_arm(tmp_path):
    async def scenario():
        database = f"pilot_session_accept_{uuid.uuid4().hex[:16]}"
        admin = await _connect("postgres")
        created_database = False
        connection = None
        try:
            await admin.execute(f'CREATE DATABASE "{database}"')
            created_database = True
            connection = await _connect(database)
            await connection.execute(INIT_SCHEMA.read_text(encoding="utf-8"))
            through_020 = _migration_subset(tmp_path, range(1, 21))
            await apply_migrations(connection, directory=through_020)

            # Seed rows that resemble sessions present before the non-null cap
            # migrations. Startup fencing preserves their counters and REAUTH state.
            await _insert_pilot(connection, "preexisting-populated", "pilot-preexisting01")
            await connection.execute(
                "UPDATE mainnet_launch_sessions SET state='REAUTH_REQUIRED', "
                "max_risk_increasing_orders=NULL, reserved_orders=1, submitted_orders=1 "
                "WHERE launch_id='preexisting-populated'"
            )
            await _insert_pilot(connection, "preexisting-nullcap", "pilot-preexisting02")
            await connection.execute(
                "UPDATE mainnet_launch_sessions SET state='REAUTH_REQUIRED', "
                "max_risk_increasing_orders=NULL WHERE launch_id='preexisting-nullcap'"
            )

            await apply_migrations(connection, directory=MIGRATIONS)
            caps = await connection.fetch(
                "SELECT launch_id, max_risk_increasing_orders, reserved_orders, "
                "submitted_orders, state FROM mainnet_launch_sessions "
                "WHERE launch_id LIKE 'preexisting-%' ORDER BY launch_id"
            )
            assert [(row["max_risk_increasing_orders"], row["reserved_orders"],
                     row["submitted_orders"], row["state"]) for row in caps] == [
                (1, 1, 1, "REAUTH_REQUIRED"),
                (1, 0, 0, "REAUTH_REQUIRED"),
            ]
            with pytest.raises(asyncpg.NotNullViolationError):
                await connection.execute(
                    "UPDATE mainnet_launch_sessions SET max_risk_increasing_orders=NULL "
                    "WHERE launch_id='preexisting-nullcap'"
                )

            await _insert_pilot(connection, "fresh-session", "pilot-freshsession01")
            now = datetime.now(UTC)
            second_connection = await _connect(database)
            try:
                attempts = await asyncio.gather(
                    PersistenceRepository(connection).record_local_live_pilot_session_armed(
                        "fresh-session", armed_at=now, entry_cutoff_seconds=5400,
                        close_after_seconds=6600, end_seconds=7200,
                    ),
                    PersistenceRepository(second_connection).record_local_live_pilot_session_armed(
                        "fresh-session", armed_at=now, entry_cutoff_seconds=5400,
                        close_after_seconds=6600, end_seconds=7200,
                    ),
                    return_exceptions=True,
                )
            finally:
                await second_connection.close()
            assert sum(isinstance(result, dict) for result in attempts) == 1
            assert sum(isinstance(result, RuntimeError) for result in attempts) == 1

            armed_count = await connection.fetchval(
                "SELECT COUNT(*) FROM local_live_pilot_events "
                "WHERE campaign_id='pilot-freshsession01' AND event_type='STATE' "
                "AND payload->>'kind'='SESSION_ARMED'"
            )
            assert armed_count == 1
            readback = await PersistenceRepository(connection).get_mainnet_launch(
                "fresh-session"
            )
            assert readback["pilot_session_armed_at"] == now.isoformat().replace("+00:00", "Z")
            assert readback["pilot_session_entry_cutoff_at"] is not None
            assert readback["pilot_session_close_after_at"] is not None
            assert readback["pilot_session_end_at"] is not None

            repository = PersistenceRepository(connection)
            for launch_id in ("preexisting-populated", "preexisting-nullcap"):
                with pytest.raises(RuntimeError):
                    await repository.record_local_live_pilot_session_armed(
                        launch_id, armed_at=now, entry_cutoff_seconds=5400,
                        close_after_seconds=6600, end_seconds=7200,
                    )
        finally:
            if connection is not None:
                await connection.close()
            if created_database:
                await admin.execute(f'DROP DATABASE "{database}"')
            await admin.close()

    asyncio.run(scenario())
