"""Disposable PostgreSQL integration for pilot session ARM and cap backfills.

Requires BLESSING_MIGRATION_TEST_DSN pointed at a disposable loopback PostgreSQL
instance. Each test creates a random database and removes only that database.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from apps.trading_worker.persistence.postgres.repositories import PersistenceRepository
from scripts.apply_local_postgres_migrations import (
    apply_migrations,
    migration_checksum,
    validate_migration_ledger,
    verify_schema,
)

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


async def _insert_pilot(
    connection, launch_id: str, campaign_id: str, *, entry_cap: int | None = 1
) -> None:
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
        entry_cap,
        campaign_id,
        "e" * 40,
        h,
    )


async def _apply_staged_migrations(connection, migrations: list[Path]) -> None:
    """Build an historical migration state without final-schema verification.

    The production runner verifies the latest schema after every invocation.
    This fixture deliberately stops at 020 so it can seed a legacy NULL cap
    before exercising 021/022; applying a prefix through the runner would
    incorrectly ask the latest-schema verifier to accept the pre-021 shape.
    """
    recorded_rows = await connection.fetch(
        "SELECT version, checksum FROM public.local_schema_migrations ORDER BY version"
    )
    validate_migration_ledger(recorded_rows, migrations)
    for migration in migrations[len(recorded_rows):]:
        async with connection.transaction():
            await connection.execute(migration.read_text(encoding="utf-8"))
            await connection.execute(
                "INSERT INTO public.local_schema_migrations (version, checksum) VALUES ($1, $2)",
                migration.name,
                migration_checksum(migration),
            )


def test_fresh_020_population_021_022_and_concurrent_session_arm():
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
            migrations_through_020 = [
                path for path in sorted(MIGRATIONS.glob("*.sql"))
                if int(path.name[:3]) <= 20
            ]
            await _apply_staged_migrations(connection, migrations_through_020)

            # Seed rows that resemble sessions present before the non-null cap
            # migrations. Startup fencing preserves their counters and REAUTH state.
            await _insert_pilot(
                connection, "preexisting-populated", "pilot-preexisting01", entry_cap=None
            )
            await connection.execute(
                "UPDATE mainnet_launch_sessions SET state='REAUTH_REQUIRED', "
                "max_risk_increasing_orders=NULL, reserved_orders=1, submitted_orders=1 "
                "WHERE launch_id='preexisting-populated'"
            )
            migrations_through_022 = sorted(MIGRATIONS.glob("*.sql"))
            await _apply_staged_migrations(connection, migrations_through_022)
            await verify_schema(connection)
            caps = await connection.fetch(
                "SELECT launch_id, max_risk_increasing_orders, reserved_orders, "
                "submitted_orders, state FROM mainnet_launch_sessions "
                "WHERE launch_id='preexisting-populated'"
            )
            assert [(row["max_risk_increasing_orders"], row["reserved_orders"],
                     row["submitted_orders"], row["state"]) for row in caps] == [
                (1, 1, 1, "REAUTH_REQUIRED"),
            ]
            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    "UPDATE mainnet_launch_sessions SET max_risk_increasing_orders=NULL "
                    "WHERE launch_id='preexisting-populated'"
                )

            repository = PersistenceRepository(connection)
            now = datetime.now(UTC)
            with pytest.raises(RuntimeError):
                await repository.record_local_live_pilot_session_armed(
                    "preexisting-populated", armed_at=now, entry_cutoff_seconds=5400,
                    close_after_seconds=6600, end_seconds=7200,
                )
            await connection.execute(
                "UPDATE mainnet_launch_sessions SET state='CLOSED' "
                "WHERE launch_id='preexisting-populated'"
            )

            await _insert_pilot(connection, "fresh-session", "pilot-freshsession01")
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

            await connection.execute(
                "UPDATE mainnet_launch_sessions SET state='REAUTH_REQUIRED' "
                "WHERE launch_id='fresh-session'"
            )
            close_readback = await repository.record_local_live_pilot_session_close_claim(
                "fresh-session",
                client_order_id="BAI-abcdef012345-0-1",
                side="SELL",
                position_side="BOTH",
                quantity=Decimal("0.1"),
                claimed_at=now,
            )
            assert close_readback["pilot_session_close_client_order_id"] == "BAI-abcdef012345-0-1"
            attempt_readback = await repository.record_local_live_pilot_session_close_attempt(
                "fresh-session",
                client_order_id="BAI-abcdef012345-0-1",
                attempt=1,
                attempted_at=now,
            )
            assert attempt_readback["pilot_session_close_attempt_count"] == 1

        finally:
            if connection is not None:
                await connection.close()
            if created_database:
                await admin.execute(f'DROP DATABASE "{database}"')
            await admin.close()

    asyncio.run(scenario())
