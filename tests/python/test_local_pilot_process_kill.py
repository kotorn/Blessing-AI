"""Opt-in crash test on an already populated, isolated PostgreSQL fixture."""

import asyncio
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit
import uuid

import asyncpg
import pytest

from apps.trading_worker.execution_lease import LeaseLostError, PostgresExecutionLease


def test_local_pilot_process_kill_preserves_accounting_and_fences_lease():
    dsn = os.environ.get("BLESSING_MIGRATION_TEST_DSN", "")
    if not dsn:
        pytest.skip("Requires an isolated populated PostgreSQL fixture")
    parsed = urlsplit(dsn)
    assert parsed.hostname == "127.0.0.1" and parsed.port in {55433, 55434, 55435}
    assert parsed.scheme in {"postgres", "postgresql"} and not parsed.query and not parsed.fragment
    assert re.fullmatch(r"blessing_migration_test_[a-z0-9_]+", unquote(parsed.path[1:]))
    root = Path(__file__).resolve().parents[2]
    scope = "crash-probe-" + uuid.uuid4().hex
    child_code = """
import asyncio, os, asyncpg
async def main():
    db = await asyncpg.connect(os.environ['BLESSING_MIGRATION_TEST_DSN'])
    scope = os.environ['BLESSING_CRASH_PROBE_SCOPE']
    await db.execute("INSERT INTO local_pilot_crash_probe(id) VALUES($1)", scope)
    await db.execute("INSERT INTO execution_leases(scope_key,owner_id,fencing_token,lease_until) VALUES($1,'killed-worker',1,CURRENT_TIMESTAMP+INTERVAL '8 seconds')", scope)
    tx = db.transaction()
    await tx.start()
    changed = await db.execute("UPDATE mainnet_launch_sessions SET pilot_net_pnl_usdc=pilot_net_pnl_usdc+999, pilot_peak_pnl_usdc=pilot_peak_pnl_usdc+999 WHERE launch_id='migration-test-pilot-restart'")
    assert changed == 'UPDATE 1'
    print('TRANSACTION_OPEN', flush=True)
    await asyncio.Event().wait()
asyncio.run(main())
"""

    async def scenario():
        child = None
        connection = await asyncpg.connect(dsn, timeout=5)
        try:
            assert (await connection.fetchval("SHOW server_version")).startswith("17.")
            before = await connection.fetchrow(
                "SELECT state,pilot_net_pnl_usdc,pilot_peak_pnl_usdc FROM mainnet_launch_sessions "
                "WHERE launch_id='migration-test-pilot-restart'",
            )
            assert before is not None and before["state"] == "REAUTH_REQUIRED"
            await connection.execute("CREATE TABLE IF NOT EXISTS local_pilot_crash_probe(id TEXT PRIMARY KEY)")
            allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
            environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
            environment.update(BLESSING_MIGRATION_TEST_DSN=dsn, BLESSING_CRASH_PROBE_SCOPE=scope)
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-c", child_code, cwd=root, env=environment,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            ready = await asyncio.wait_for(child.stdout.readline(), timeout=10)
            assert ready.strip() == b"TRANSACTION_OPEN"
            assert await connection.fetchval("SELECT lease_until>CURRENT_TIMESTAMP FROM execution_leases WHERE scope_key=$1", scope)
            successor = PostgresExecutionLease(connection, scope, "restart-worker", ttl_seconds=30)
            assert await successor.acquire() is False
            child.kill()  # Only the process handle created by this test.
            await asyncio.wait_for(child.wait(), timeout=5)
            await connection.close()
            connection = await asyncpg.connect(dsn, timeout=5)
            await connection.execute("SET statement_timeout='5s'")
            async with connection.transaction():
                # NOWAIT proves the killed transaction's row lock was released.
                after = await connection.fetchrow(
                    "SELECT state,pilot_net_pnl_usdc,pilot_peak_pnl_usdc FROM mainnet_launch_sessions "
                    "WHERE launch_id='migration-test-pilot-restart' FOR UPDATE NOWAIT",
                )
            assert dict(after) == dict(before)
            assert await connection.fetchval("SELECT count(*) FROM local_pilot_crash_probe WHERE id=$1", scope) == 1
            async with asyncio.timeout(10):
                while await connection.fetchval("SELECT lease_until>CURRENT_TIMESTAMP FROM execution_leases WHERE scope_key=$1", scope):
                    await asyncio.sleep(0.1)
            successor = PostgresExecutionLease(connection, scope, "restart-worker", ttl_seconds=30)
            assert await successor.acquire() is True
            assert successor.fencing_token == 2
            stale = PostgresExecutionLease(connection, scope, "killed-worker")
            stale.fencing_token = 1
            with pytest.raises(LeaseLostError):
                await stale.assert_valid()
            assert await stale.renew() is False
            await stale.release()
            same_owner_stale = PostgresExecutionLease(connection, scope, "restart-worker")
            same_owner_stale.fencing_token = 1
            with pytest.raises(LeaseLostError):
                await same_owner_stale.assert_valid()
            assert await same_owner_stale.renew() is False
            await same_owner_stale.release()
            await successor.assert_valid()
            await successor.release()
        finally:
            if child is not None and child.returncode is None:
                child.kill()
                await child.wait()
            await connection.close()

    try:
        asyncio.run(scenario())
    except (asyncpg.PostgresError, OSError) as exc:
        pytest.fail(f"Isolated crash verification failed: {type(exc).__name__}", pytrace=False)


def test_paper_worker_startup_fences_durable_pilot_in_two_processes():
    """Actual Worker startup on the isolated DB; not Local Mainnet preflight."""
    dsn = os.environ.get("BLESSING_MIGRATION_TEST_DSN", "")
    if not dsn:
        pytest.skip("Requires an isolated populated PostgreSQL fixture")
    parsed = urlsplit(dsn)
    assert parsed.scheme in {"postgres", "postgresql"}
    assert parsed.hostname == "127.0.0.1" and parsed.port in {55433, 55434, 55435}
    assert not parsed.query and not parsed.fragment
    assert re.fullmatch(r"blessing_migration_test_[a-z0-9_]+", unquote(parsed.path[1:]))
    root = Path(__file__).resolve().parents[2]
    code = """
import sys, os, asyncio, json
sys.path.insert(0, os.environ['BLESSING_CRASH_PROBE_ROOT'])
from apps.trading_worker.main import TradingWorkerApp
from apps.trading_worker.persistence.manager import PersistenceManager, PersistenceConfig, PersistenceMode
from apps.trading_worker.persistence.postgres.client import PostgresClient
async def main():
    worker = TradingWorkerApp(symbols=['ETHUSDC'])
    worker.persistence = PersistenceManager(PostgresClient(dsn=os.environ['BLESSING_MIGRATION_TEST_DSN']), config=PersistenceConfig(mode=PersistenceMode.REQUIRED))
    try:
        await worker.start()
        row = await worker.persistence.get_mainnet_launch_session('migration-test-pilot-restart')
        assert worker.persistence.is_connected
        assert worker.engine_state.value == 'DISARMED'
        assert worker.execution_mode.value == 'PAPER'
        assert worker.ws_client is None and worker.execution_adapter is None
        assert row['state'] == 'REAUTH_REQUIRED'
        print(json.dumps({'engine':'DISARMED','mode':'PAPER','state':row['state']}), flush=True)
    finally:
        await worker.stop()
asyncio.run(main())
"""

    async def scenario():
        db = await asyncpg.connect(dsn, timeout=5)
        try:
            await db.execute("UPDATE mainnet_launch_sessions SET pilot_net_pnl_usdc=2.5,pilot_peak_pnl_usdc=3.5 WHERE launch_id='migration-test-pilot-restart'")
            before = await db.fetchrow("SELECT pilot_net_pnl_usdc,pilot_peak_pnl_usdc FROM mainnet_launch_sessions WHERE launch_id='migration-test-pilot-restart'")
            assert before is not None
            for _ in range(2):
                # Only the test campaign is activated, never an exchange runtime.
                await db.execute("UPDATE mainnet_launch_sessions SET state='ACTIVE' WHERE launch_id='migration-test-pilot-restart'")
                allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
                environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
                environment.update(BLESSING_MIGRATION_TEST_DSN=dsn, BLESSING_CRASH_PROBE_ROOT=str(root), EXECUTION_MODE="PAPER", MAINNET_LIVE_APPROVED="false", PERSISTENCE_MODE="REQUIRED")
                child = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", code, cwd=root, env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                try:
                    output, _ = await asyncio.wait_for(child.communicate(), timeout=30)
                    assert child.returncode == 0, "PAPER Worker startup failed; no readiness granted"
                    assert json.loads(output.decode().strip().splitlines()[-1]) == {"engine": "DISARMED", "mode": "PAPER", "state": "REAUTH_REQUIRED"}
                finally:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()
                after = await db.fetchrow("SELECT pilot_net_pnl_usdc,pilot_peak_pnl_usdc FROM mainnet_launch_sessions WHERE launch_id='migration-test-pilot-restart'")
                assert dict(after) == dict(before)
        finally:
            # Restore the fixture fence even if child startup fails before fencing.
            await db.execute("UPDATE mainnet_launch_sessions SET state='REAUTH_REQUIRED' WHERE launch_id='migration-test-pilot-restart'")
            assert await db.fetchval("SELECT state FROM mainnet_launch_sessions WHERE launch_id='migration-test-pilot-restart'") == "REAUTH_REQUIRED"
            await db.close()

    try:
        asyncio.run(scenario())
    except (asyncpg.PostgresError, OSError) as exc:
        pytest.fail(f"Isolated Worker verification failed: {type(exc).__name__}", pytrace=False)
