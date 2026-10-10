"""PostgreSQL 17 integration: pilot launch bookkeeping around the entry fill.

Needs BLESSING_MIGRATION_TEST_DSN (isolated loopback server, see
test_local_postgres_migrations.py). It creates and drops its own database
blessing_migration_test_lifecycle on that server, so it never touches the
fixture the migration acceptance test leaves behind.
"""

import asyncio
import os
from pathlib import Path
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit, urlunsplit

import asyncpg
import pytest

from apps.trading_worker.persistence.postgres.repositories import PersistenceRepository
from scripts.apply_local_postgres_migrations import apply_migrations

DB_NAME = "blessing_migration_test_lifecycle"
INIT_SCHEMA = Path(__file__).resolve().parents[2] / "infra" / "postgres" / "init_schema.sql"


def _dsn_for(database: str) -> str:
    dsn = os.environ.get("BLESSING_MIGRATION_TEST_DSN", "")
    if not dsn:
        pytest.skip("Set BLESSING_MIGRATION_TEST_DSN for an isolated local PostgreSQL server")
    parts = urlsplit(dsn)
    if parts.hostname != "127.0.0.1":
        pytest.fail("Lifecycle test DSN must target 127.0.0.1", pytrace=False)
    return urlunsplit(parts._replace(path=f"/{database}"))


async def _connect(dsn: str):
    return await asyncpg.connect(dsn=dsn, timeout=5, server_settings={"search_path": "public"})


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
           VALUES ($1, 'lifecycle-approval', 'ETHUSDC',
            'LIVE_RESEARCH_PILOT', 'ACTIVE', 'LOCAL', $2, 1, $3,
            $4, $5, $5, $5, $5, 'test-project', '1', '2', 'QUICK',
            CURRENT_TIMESTAMP + INTERVAL '7 days', $5, 50, 2, 5, 0.25, 86400, 10, 'ACTIVE')""",
        launch_id, "d" * 64, campaign_id, "e" * 40, h,
    )


def test_entry_fill_before_submitted_mark_does_not_wedge_the_launch():
    """The user-stream FILL pauses new risk; the later CONFIRMED mark must still be recorded."""

    async def scenario():
        admin = await _connect(_dsn_for("postgres"))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}"')
            await admin.execute(f'CREATE DATABASE "{DB_NAME}"')
        finally:
            await admin.close()
        connection = await _connect(_dsn_for(DB_NAME))
        try:
            # Same bootstrap as a fresh docker volume: base schema, then migrations.
            await connection.execute(INIT_SCHEMA.read_text(encoding="utf-8"))
            await apply_migrations(connection)
            launch_id, campaign_id = "lifecycle-pilot", "pilot-lifecycle01"
            await _insert_pilot(connection, launch_id, campaign_id)
            repository = PersistenceRepository(connection)
            assert await repository.reserve_mainnet_risk_order(
                launch_id, "entry-lifecycle-1", "pilot-basket-lifecycle"
            ) is True
            now = datetime.now(UTC)
            await repository.append_local_live_pilot_event(
                campaign_id=campaign_id, launch_id=launch_id, run_id=launch_id,
                symbol="ETHUSDC", event_key="FILL:lifecycle-1", event_type="FILL",
                source="BINANCE", observed_at=now,
                payload={"run_id": launch_id, "exchange_event_id": "lifecycle-1"},
                net_pnl_delta_usdc="0",
            )
            state_after_fill = await connection.fetchval(
                "SELECT state FROM mainnet_launch_sessions WHERE launch_id = $1", launch_id
            )
            assert state_after_fill == "PAUSED_NEW_RISK"  # the premise of the race
            assert await repository.mark_mainnet_risk_order_submitted(
                launch_id, "entry-lifecycle-1"
            ) is True
            row = await connection.fetchrow(
                "SELECT state, submitted_orders, reserved_orders, pending_order_client_order_id,"
                " first_order_client_order_id FROM mainnet_launch_sessions WHERE launch_id = $1",
                launch_id,
            )
            assert row["submitted_orders"] == 1 and row["reserved_orders"] == 1
            assert row["pending_order_client_order_id"] is None
            assert row["first_order_client_order_id"] == "entry-lifecycle-1"
            # The accounting pause waiting for a fresh MARK must survive.
            assert row["state"] == "PAUSED_NEW_RISK"
        finally:
            await connection.close()
            admin = await _connect(_dsn_for("postgres"))
            try:
                await admin.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}"')
            finally:
                await admin.close()

    asyncio.run(scenario())
