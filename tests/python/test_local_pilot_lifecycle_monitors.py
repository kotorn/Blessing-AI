import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.trading_worker.main import TradingWorkerApp, WorkerEngineState, WorkerExecutionMode
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import (
    BinanceExecutionAdapter,
    _local_pilot_position_mark_time_ms,
)
from apps.trading_worker.venues.binance.models import ExchangeAccountSnapshot


def test_evaluate_pilot_drawdown():
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    
    # Not a pilot session
    assert adapter.evaluate_pilot_drawdown({"policy": "STAGED_FIRST_ORDER"}, Decimal("0")) is False
    
    # Pilot session within 5 USDC drawdown
    session = {
        "policy": "LIVE_RESEARCH_PILOT",
        "pilot_peak_pnl_usdc": "10.0",
        "pilot_max_drawdown_usdc": "5.0",
        "pilot_drawdown_triggered": False,
    }
    assert adapter.evaluate_pilot_drawdown(session, Decimal("8.0")) is False  # dd = 2.0
    assert adapter.evaluate_pilot_drawdown(session, Decimal("5.1")) is False  # dd = 4.9
    
    # Pilot session breaching 5 USDC drawdown
    assert adapter.evaluate_pilot_drawdown(session, Decimal("5.0")) is True   # dd = 5.0
    assert adapter.evaluate_pilot_drawdown(session, Decimal("3.0")) is True   # dd = 7.0

    # Prior triggered flag returns True
    triggered_session = dict(session, pilot_drawdown_triggered=True)
    assert adapter.evaluate_pilot_drawdown(triggered_session, Decimal("10.0")) is True


@pytest.mark.asyncio
async def test_unprotected_local_pilot_owner_enters_close_only_before_recovery_close():
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    events = []

    class Persistence:
        async def enter_local_live_pilot_close_only(self, launch_id, *, reason):
            events.append(("close_only", launch_id, reason))
            return {
                "launch_id": launch_id,
                "pilot_status": "CLOSE_ONLY",
                "state": "PAUSED_NEW_RISK",
            }

    authority = SimpleNamespace(persistence=Persistence())

    async def close_once(intent, order, record, *, reason, authority):
        events.append(("close", order.quantity, record["filled_quantity"], reason))
        return True

    adapter._local_mainnet_close_only_once = close_once
    owner = {
        "mainnet_launch_id": "launch-recovery-1",
        "symbol": "ETHUSDC",
        "entry_client_order_id": "entry-recovery-1",
        "basket_id": "basket-recovery-1",
        "entry_side": "BUY",
        "position_side": "BOTH",
        "requested_quantity": "0.2",
        "filled_quantity": "0.1",
        "entry_average_price": "100",
        "stop_trigger_price": "90",
        "take_profit_trigger_price": "120",
        "management_mode": "QUICK",
        "state": "CLOSE_PENDING",
    }

    recovered = await adapter._recover_unprotected_local_pilot_owner(
        owner, authority=authority, launch_id="launch-recovery-1"
    )

    assert recovered is True
    assert events == [
        (
            "close_only",
            "launch-recovery-1",
            "PILOT_RECONCILIATION_UNKNOWN",
        ),
        (
            "close",
            Decimal("0.1"),
            "0.1",
            "PILOT_UNPROTECTED_OWNER_RECOVERY",
        ),
    ]
    assert authority._mainnet_launch_session["pilot_status"] == "CLOSE_ONLY"


@pytest.mark.asyncio
@pytest.mark.parametrize("pilot_status", ["CLOSE_ONLY", "EXPIRED", "REVOKED"])
async def test_unprotected_pending_owner_is_fenced_then_terminal_entry_is_read_back(
    pilot_status
):
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    events = []

    class Persistence:
        async def enter_local_live_pilot_close_only(self, *_args, **_kwargs):
            events.append("close_only")
            return {"pilot_status": pilot_status, "state": "PAUSED_NEW_RISK"}

    authority = SimpleNamespace(persistence=Persistence())

    async def cancel_and_read_back(record, *, authority):
        events.append(("cancel_and_read_back", record["entry_client_order_id"]))
        return {**record, "state": "CLOSED", "filled_quantity": "0"}

    async def forbidden_close(*_args, **_kwargs):
        pytest.fail("a verified zero-fill terminal entry needs no close order")

    adapter._cancel_and_read_back_pilot_entry = cancel_and_read_back
    adapter._local_mainnet_close_only_once = forbidden_close
    owner = {
        "mainnet_launch_id": "launch-recovery-1",
        "symbol": "ETHUSDC",
        "entry_client_order_id": "entry-recovery-1",
        "entry_side": "BUY",
        "position_side": "BOTH",
        "requested_quantity": "0.2",
        "filled_quantity": "0",
        "entry_average_price": None,
        "stop_trigger_price": "90",
        "take_profit_trigger_price": "120",
        "management_mode": "QUICK",
        "state": "PENDING",
    }

    recovered = await adapter._recover_unprotected_local_pilot_owner(
        owner, authority=authority, launch_id="launch-recovery-1"
    )

    assert recovered is True
    assert events == [
        "close_only",
        ("cancel_and_read_back", "entry-recovery-1"),
    ]
    assert authority._mainnet_launch_session["pilot_status"] == pilot_status


def test_signed_pilot_position_mark_uses_request_start_and_rejects_slow_response():
    assert _local_pilot_position_mark_time_ms(123_000, 5.0, 7.5) == 123_000
    with pytest.raises(ValueError, match="freshness budget"):
        _local_pilot_position_mark_time_ms(123_000, 5.0, 7.5001)


def test_evaluate_quick_hold_timeout():
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    now = datetime(2026, 9, 27, 12, 0, 0, tzinfo=UTC)

    # Within 24h
    record_recent = {
        "state": "PROTECTED",
        "first_fill_at": now - timedelta(hours=23, minutes=59),
        "quick_max_hold_seconds": 86400,
    }
    assert adapter.evaluate_quick_hold_timeout(record_recent, now=now) is False

    # Exceeded 24h (86,400s)
    record_expired = {
        "state": "PROTECTED",
        "first_fill_at": now - timedelta(hours=24, seconds=1),
        "quick_max_hold_seconds": 86400,
    }
    assert adapter.evaluate_quick_hold_timeout(record_expired, now=now) is True

    # Closed records do not timeout
    record_closed = {
        "state": "CLOSED",
        "first_fill_at": now - timedelta(days=2),
        "quick_max_hold_seconds": 86400,
    }
    assert adapter.evaluate_quick_hold_timeout(record_closed, now=now) is False

    # Persisted policy overrides cannot lengthen the approved 24h horizon.
    assert adapter.evaluate_quick_hold_timeout(
        dict(record_recent, quick_max_hold_seconds=86401), now=now
    ) is True
    assert adapter.evaluate_quick_hold_timeout(
        dict(record_recent, quick_max_hold_seconds=True), now=now
    ) is True
    assert adapter.evaluate_quick_hold_timeout(
        {"state": "PROTECTED", "quick_max_hold_seconds": 86400}, now=now
    ) is True


def test_local_pilot_monitor_status_is_independent_of_process_heartbeat():
    worker = TradingWorkerApp.__new__(TradingWorkerApp)
    worker.execution_mode = WorkerExecutionMode.LIVE
    worker._mainnet_launch_session = {"policy": "LIVE_RESEARCH_PILOT"}
    now = datetime(2026, 9, 27, 12, 0, 20, tzinfo=UTC)
    worker._pilot_lifecycle_monitor_started_at = None
    worker._pilot_lifecycle_monitor_completed_at = None
    worker._pilot_lifecycle_monitor_last_success_at = None
    worker._pilot_lifecycle_monitor_last_error = None
    assert worker._local_pilot_lifecycle_monitor_state(now)["status"] == "NOT_RUN"

    worker._pilot_lifecycle_monitor_started_at = now - timedelta(seconds=11)
    assert worker._local_pilot_lifecycle_monitor_state(now)["status"] == "STALLED"

    worker._pilot_lifecycle_monitor_completed_at = now - timedelta(seconds=10)
    worker._pilot_lifecycle_monitor_last_error = "MONITOR_ACTION_UNVERIFIED"
    assert worker._local_pilot_lifecycle_monitor_state(now)["status"] == "DEGRADED"

    worker._pilot_lifecycle_monitor_last_error = None
    worker._pilot_lifecycle_monitor_last_success_at = now - timedelta(seconds=5)
    assert worker._local_pilot_lifecycle_monitor_state(now)["status"] == "HEALTHY"

    worker._pilot_lifecycle_monitor_last_success_at = now - timedelta(seconds=16)
    assert worker._local_pilot_lifecycle_monitor_state(now)["status"] == "STALE"

    worker._pilot_lifecycle_monitor_started_at = datetime.now(UTC) - timedelta(seconds=11)
    worker._pilot_lifecycle_monitor_completed_at = None
    worker._pilot_lifecycle_monitor_last_success_at = datetime.now(UTC) - timedelta(seconds=2)
    assert worker.local_pilot_monitor_allows_new_risk() is False


@pytest.mark.asyncio
async def test_unverified_pilot_monitor_cycle_degrades_worker_and_blocks_new_risk():
    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE
    worker._mainnet_launch_session = {"policy": "LIVE_RESEARCH_PILOT"}
    worker.heartbeat_interval_sec = 0.01

    async def failed_cycle(*, authority):
        assert authority is worker
        return {"failed_action_count": 1, "unmatched_owner_count": 0}

    adapter = SimpleNamespace(
        execution_lease=None,
        check_and_enforce_pilot_protections=failed_cycle,
        state="READY",
        reconciliation=SimpleNamespace(last_status="IN_SYNC"),
    )
    worker.execution_adapter = adapter
    monitor_task = asyncio.create_task(worker._heartbeat_loop())
    await asyncio.sleep(0.03)
    worker.is_running = False
    await monitor_task

    assert worker.pause_new_risk is True
    assert worker.engine_state == WorkerEngineState.DEGRADED
    assert adapter.state == "DEGRADED"
    assert adapter.reconciliation.last_status == "UNKNOWN"
    assert worker._local_pilot_lifecycle_monitor_state()["status"] == "DEGRADED"


def test_independent_watchdog_degrades_worker_when_monitor_stalls():
    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE
    worker._mainnet_launch_session = {"policy": "LIVE_RESEARCH_PILOT"}
    worker._pilot_lifecycle_monitor_started_at = datetime.now(UTC) - timedelta(seconds=11)
    worker._pilot_lifecycle_monitor_completed_at = None
    worker._pilot_lifecycle_monitor_last_success_at = datetime.now(UTC) - timedelta(seconds=1)
    adapter = SimpleNamespace(
        state="READY",
        reconciliation=SimpleNamespace(last_status="IN_SYNC"),
    )
    worker.execution_adapter = adapter

    worker.enforce_local_pilot_monitor_liveness()

    assert worker.pause_new_risk is True
    assert worker.engine_state == WorkerEngineState.DEGRADED
    assert adapter.state == "DEGRADED"
    assert adapter.reconciliation.last_status == "UNKNOWN"


@pytest.mark.asyncio
async def test_pilot_account_snapshot_mark_persists_and_updates_pnl(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")

    events = []

    async def mock_append(**kwargs):
        events.append(kwargs)
        return {
            "launch_id": kwargs["launch_id"],
            "pilot_campaign_id": kwargs["campaign_id"],
            "pilot_net_pnl_usdc": Decimal("2.5"),
            "pilot_peak_pnl_usdc": Decimal("3.0"),
            "pilot_drawdown_triggered": False,
            "pilot_status": "ACTIVE",
            "state": "ACTIVE",
        }

    app = TradingWorkerApp.__new__(TradingWorkerApp)
    app.execution_mode = WorkerExecutionMode.LIVE
    app._mainnet_launch_session = {
        "launch_id": "launch-pilot-snap-1",
        "policy": "LIVE_RESEARCH_PILOT",
        "pilot_campaign_id": "campaign-snap-1",
        "pilot_last_account_snapshot_at": None,
    }
    app.persistence = SimpleNamespace(
        repository=SimpleNamespace(append_local_live_pilot_event=mock_append),
        is_connected=True,
    )
    app.pause_new_risk = False
    app.engine_state = WorkerEngineState.ARMED

    snapshot = ExchangeAccountSnapshot(
        wallet_balance=Decimal("100"),
        margin_balance=Decimal("102.5"),
        available_balance=Decimal("90"),
        unrealized_pnl=Decimal("2.5"),
        total_initial_margin=Decimal("10"),
        total_maint_margin=Decimal("5"),
        position_initial_margin=Decimal("10"),
        total_position_notional=Decimal("25"),
        effective_leverage=Decimal("0.25"),
        margin_utilization_pct=Decimal("10"),
        valid=True,
        timestamp=datetime.now(UTC),
    )

    await app._on_account_snapshot_update(snapshot)

    assert len(events) == 1
    event = events[0]
    assert event["event_type"] == "MARK"
    assert event["source"] == "BINANCE"
    assert event["symbol"] == "ETHUSDC"
    assert event["payload"]["unrealized_pnl_usdc"] == "2.5"
    assert event["payload"]["run_id"] == "launch-pilot-snap-1"


@pytest.mark.asyncio
async def test_pilot_drawdown_breach_enforces_close_only(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.MAINNET
    adapter.reconciliation = SimpleNamespace(last_status="IN_SYNC")

    async def verified_protection(*_args, **_kwargs):
        return SimpleNamespace(protected=True, state="PROTECTED", reasons=())

    adapter.read_back_algo_protection = verified_protection

    closed_intents = []
    lifecycle_events = []

    async def mock_close_only_once(intent, order, record, *, reason, authority):
        lifecycle_events.append("close")
        closed_intents.append((record["entry_client_order_id"], reason))
        return True

    adapter._local_mainnet_close_only_once = mock_close_only_once

    async def readback_entry(record, *, authority):
        lifecycle_events.append("cancel-entry")
        return dict(record)

    adapter._cancel_and_read_back_pilot_entry = readback_entry

    now = datetime.now(UTC)
    active_protections = [
        {
            "entry_client_order_id": "client-ord-1",
            "mainnet_launch_id": "launch-dd-1",
            "venue": "binance_mainnet",
            "symbol": "ETHUSDC",
            "management_mode": "QUICK",
            "state": "PROTECTED",
            "side": "BUY",
            "position_side": "BOTH",
            "filled_quantity": "0.1",
            "stop_trigger_price": "90",
            "take_profit_trigger_price": "120",
            "stop_algo_id": 21,
            "stop_client_algo_id": "stop-1",
            "take_profit_algo_id": 22,
            "take_profit_client_algo_id": "target-1",
            "first_fill_at": now - timedelta(hours=1),
            "quick_max_hold_seconds": 86400,
        }
    ]

    async def signed_positions(*_args, **_kwargs):
        return [{
            "symbol": "ETHUSDC", "positionSide": "BOTH",
            "positionAmt": "0.1", "unRealizedProfit": "0.0",
        }]

    adapter.rest_client = SimpleNamespace(request=signed_positions)

    class MockProtectionsStore:
        async def list_active_protections(self, venue, symbol):
            return active_protections

    class MockPersistence:
        def __init__(self):
            self.repository = SimpleNamespace(algo_protections=MockProtectionsStore())
            self.drawdown_triggered = False

        async def get_mainnet_launch_session(self, launch_id):
            assert launch_id == "launch-dd-1"
            return {
                "launch_id": "launch-dd-1", "pilot_campaign_id": "pilot-dd-1",
                "policy": "LIVE_RESEARCH_PILOT", "runtime_target": "LOCAL",
                "symbol": "ETHUSDC", "pilot_peak_pnl_usdc": "10.0",
                "pilot_net_pnl_usdc": "4.0", "pilot_max_drawdown_usdc": "5.0",
                "pilot_drawdown_triggered": False,
            }

        async def trigger_pilot_drawdown(self, launch_id, reason=""):
            self.drawdown_triggered = True
            return {}

    persistence = MockPersistence()

    async def persist_mark(*_args, **_kwargs):
        return True

    authority = SimpleNamespace(
        _mainnet_launch_session={
            "launch_id": "launch-dd-1",
            "pilot_campaign_id": "pilot-dd-1",
            "policy": "LIVE_RESEARCH_PILOT",
            "pilot_peak_pnl_usdc": "10.0",
            "pilot_net_pnl_usdc": "4.0",  # dd = 6.0 >= 5.0
            "pilot_max_drawdown_usdc": "5.0",
            "pilot_drawdown_triggered": False,
        },
        _mainnet_launch_id="launch-dd-1",
        persistence=persistence,
        _persist_local_live_pilot_mark=persist_mark,
        get_local_live_pilot_accounting=lambda: _async_value({
            "status": "VERIFIED", "campaign_id": "pilot-dd-1",
            "launch_id": "launch-dd-1", "net_pnl_usdc": "4.0",
            "peak_net_pnl_usdc": "10.0",
        }),
    )

    actions = await adapter.check_and_enforce_pilot_protections(authority=authority)

    assert actions["drawdown_triggered"] is True
    assert persistence.drawdown_triggered is True
    assert ("client-ord-1", "PILOT_DRAWDOWN_LIMIT_REACHED") in closed_intents


@pytest.mark.asyncio
async def test_quick_24h_hold_expiry_enforces_close(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.MAINNET
    adapter.reconciliation = SimpleNamespace(last_status="IN_SYNC")

    async def verified_protection(*_args, **_kwargs):
        return SimpleNamespace(protected=True, state="PROTECTED", reasons=())

    adapter.read_back_algo_protection = verified_protection

    closed_intents = []
    lifecycle_events = []

    async def mock_close_only_once(intent, order, record, *, reason, authority):
        lifecycle_events.append("close")
        closed_intents.append((record["entry_client_order_id"], reason))
        return True

    adapter._local_mainnet_close_only_once = mock_close_only_once

    async def readback_entry(record, *, authority):
        lifecycle_events.append("cancel-entry")
        return dict(record)

    adapter._cancel_and_read_back_pilot_entry = readback_entry

    close_only_calls = []

    async def enter_close_only(launch_id, *, reason, drawdown=False):
        lifecycle_events.append("close-only")
        close_only_calls.append((launch_id, reason, drawdown))
        return {
            "launch_id": launch_id,
            "policy": "LIVE_RESEARCH_PILOT",
            "pilot_status": "CLOSE_ONLY",
        }

    now = datetime.now(UTC)
    active_protections = [
        {
            "entry_client_order_id": "client-ord-old",
            "mainnet_launch_id": "launch-qh-1",
            "venue": "binance_mainnet",
            "symbol": "ETHUSDC",
            "management_mode": "QUICK",
            "state": "PROTECTED",
            "side": "BUY",
            "position_side": "BOTH",
            "filled_quantity": "0.1",
            "stop_trigger_price": "90",
            "take_profit_trigger_price": "120",
            "stop_algo_id": 31,
            "stop_client_algo_id": "stop-old",
            "take_profit_algo_id": 32,
            "take_profit_client_algo_id": "target-old",
            "first_fill_at": now - timedelta(hours=25),  # 25h > 24h
            "quick_max_hold_seconds": 86400,
        }
    ]

    async def signed_positions(*_args, **_kwargs):
        return [{
            "symbol": "ETHUSDC", "positionSide": "BOTH",
            "positionAmt": "0.1", "unRealizedProfit": "0.0",
        }]

    adapter.rest_client = SimpleNamespace(request=signed_positions)

    class MockProtectionsStore:
        async def list_active_protections(self, venue, symbol):
            return active_protections

    pilot_session = {
        "launch_id": "launch-qh-1",
        "pilot_campaign_id": "pilot-qh-1",
        "policy": "LIVE_RESEARCH_PILOT",
        "runtime_target": "LOCAL",
        "symbol": "ETHUSDC",
        "pilot_peak_pnl_usdc": "2.0",
        "pilot_net_pnl_usdc": "1.0",
        "pilot_max_drawdown_usdc": "5.0",
        "pilot_quick_max_hold_seconds": 86400,
        "pilot_drawdown_triggered": False,
    }
    persistence = SimpleNamespace(
        repository=SimpleNamespace(algo_protections=MockProtectionsStore()),
        trigger_pilot_drawdown=None,
        enter_local_live_pilot_close_only=enter_close_only,
        get_mainnet_launch_session=lambda _launch_id: _async_value(pilot_session),
    )

    async def persist_mark(*_args, **_kwargs):
        return True

    authority = SimpleNamespace(
        _mainnet_launch_session={
            **pilot_session,
        },
        _mainnet_launch_id="launch-qh-1",
        persistence=persistence,
        _persist_local_live_pilot_mark=persist_mark,
        get_local_live_pilot_accounting=lambda: _async_value({
            "status": "VERIFIED", "campaign_id": "pilot-qh-1",
            "launch_id": "launch-qh-1", "net_pnl_usdc": "1.0",
            "peak_net_pnl_usdc": "2.0",
        }),
    )

    actions = await adapter.check_and_enforce_pilot_protections(authority=authority)

    assert actions["quick_expired_count"] == 1
    assert ("client-ord-old", "QUICK_MAX_HOLD_EXPIRED") in closed_intents
    assert close_only_calls == [("launch-qh-1", "QUICK_MAX_HOLD_EXPIRED", False)]
    assert lifecycle_events == ["close-only", "cancel-entry", "close"]
    assert authority._mainnet_launch_session["policy"] == "LIVE_RESEARCH_PILOT"
    assert authority._mainnet_launch_session["pilot_status"] == "CLOSE_ONLY"


@pytest.mark.asyncio
async def test_drawdown_never_closes_another_launch_campaign_owner(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.MAINNET
    close_calls = []
    degraded = []

    async def degrade(worker):
        degraded.append(worker)

    async def should_not_cancel(*_args, **_kwargs):
        raise AssertionError("unmatched owner must not be adopted or cancelled")

    adapter._degrade_local_mainnet_protection = degrade
    adapter._cancel_and_read_back_pilot_entry = should_not_cancel
    adapter._local_mainnet_close_only_once = lambda *args, **kwargs: close_calls.append(args)

    owner = {
        "entry_client_order_id": "other-campaign-entry",
        "mainnet_launch_id": "launch-other-campaign",
        "pilot_campaign_id": "pilot-other-campaign",
        "venue": "binance_mainnet",
        "symbol": "ETHUSDC",
        "management_mode": "QUICK",
        "state": "PROTECTED",
    }

    class Owners:
        async def list_active_protections(self, venue, symbol):
            assert venue == "binance_mainnet" and symbol == "ETHUSDC"
            return [owner]

    session = {
        "launch_id": "launch-current-campaign",
        "pilot_campaign_id": "pilot-current-campaign",
        "policy": "LIVE_RESEARCH_PILOT",
        "runtime_target": "LOCAL",
        "symbol": "ETHUSDC",
        "pilot_peak_pnl_usdc": "10",
        "pilot_net_pnl_usdc": "0",
        "pilot_max_drawdown_usdc": "5",
        "pilot_drawdown_triggered": False,
    }
    authority = SimpleNamespace(
        _mainnet_launch_id="launch-current-campaign",
        _mainnet_launch_session=session,
        persistence=SimpleNamespace(
            get_mainnet_launch_session=lambda _launch_id: _async_value(session),
            repository=SimpleNamespace(algo_protections=Owners()),
        ),
    )

    actions = await adapter.check_and_enforce_pilot_protections(authority)

    assert actions["unmatched_owner_count"] == 1
    assert actions["drawdown_triggered"] is False
    assert degraded == [authority]
    assert close_calls == []


@pytest.mark.asyncio
async def test_ambiguous_protection_readback_uses_one_durable_close_only_attempt(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    now = datetime.now(UTC)
    owner = {
        "entry_client_order_id": "ambiguous-entry-1",
        "mainnet_launch_id": "launch-ambiguous-1",
        "venue": "binance_mainnet",
        "symbol": "ETHUSDC",
        "management_mode": "QUICK",
        "state": "PROTECTED",
        "entry_side": "BUY",
        "position_side": "BOTH",
        "filled_quantity": "0.01",
        "requested_quantity": "0.01",
        "first_fill_at": now - timedelta(minutes=1),
        "stop_trigger_price": "1900",
        "take_profit_trigger_price": "2200",
        "stop_algo_id": 41,
        "stop_client_algo_id": "ambiguous-stop-1",
        "take_profit_algo_id": 42,
        "take_profit_client_algo_id": "ambiguous-target-1",
    }
    session = {
        "launch_id": "launch-ambiguous-1",
        "pilot_campaign_id": "pilot-ambiguous-1",
        "policy": "LIVE_RESEARCH_PILOT",
        "runtime_target": "LOCAL",
        "symbol": "ETHUSDC",
        "pilot_status": "ACTIVE",
        "pilot_max_drawdown_usdc": "5",
        "pilot_quick_max_hold_seconds": 86400,
        "pilot_net_pnl_usdc": "0",
        "pilot_peak_pnl_usdc": "0",
        "pilot_drawdown_triggered": False,
    }

    class Protections:
        async def list_active_protections(self, **_kwargs):
            return [owner]

    class Persistence:
        repository = SimpleNamespace(algo_protections=Protections())

        async def get_mainnet_launch_session(self, _launch_id):
            return session

    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.MAINNET
    adapter.state = "READY"
    adapter.reconciliation = SimpleNamespace(last_status="IN_SYNC")
    adapter._is_local_mainnet_runtime = lambda: True
    degraded = []
    close_attempts = []

    async def degrade(authority):
        degraded.append(authority)

    async def ambiguous(*_args, **_kwargs):
        return SimpleNamespace(protected=False, state="AMBIGUOUS", reasons=("read_timeout",))

    async def close_once(intent, order, record, *, reason, authority):
        close_attempts.append((record["entry_client_order_id"], reason, authority))
        return True

    adapter._degrade_local_mainnet_protection = degrade
    adapter.read_back_algo_protection = ambiguous
    adapter._reconstruct_intent_and_order_from_record = lambda _record: (object(), object())
    adapter._local_mainnet_close_only_once = close_once
    authority = SimpleNamespace(
        _mainnet_launch_id=session["launch_id"],
        _mainnet_launch_session=session,
        persistence=Persistence(),
    )

    result = await adapter.check_and_enforce_pilot_protections(authority)

    assert result["failed_action_count"] == 1
    assert close_attempts == [(
        "ambiguous-entry-1", "PILOT_PROTECTION_UNVERIFIED", authority
    )]
    assert degraded == [authority]


def _pilot_entry_owner(*, filled="0", status="NEW"):
    return {
        "entry_client_order_id": "pilot-entry-pending-1",
        "mainnet_launch_id": "launch-cancel-1",
        "venue": "binance_mainnet",
        "symbol": "ETHUSDC",
        "entry_side": "BUY",
        "position_side": "BOTH",
        "requested_quantity": Decimal("0.5"),
        "filled_quantity": Decimal(filled),
        "state": "PENDING" if status == "NEW" else "PROTECTED",
    }


def _pilot_cancel_adapter(monkeypatch, before, after, fills=(), cancel_result=True):
    monkeypatch.setattr(
        BinanceExecutionAdapter, "_is_local_mainnet_runtime", lambda _self: True
    )
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.MAINNET
    authority = SimpleNamespace(
        _mainnet_launch_session={
            "launch_id": "launch-cancel-1", "policy": "LIVE_RESEARCH_PILOT"
        }
    )
    adapter._worker_authority = authority
    adapter._worker_authorized = lambda candidate: candidate is authority
    adapter._degrade_local_mainnet_protection = _async_return_none
    queries = iter([before, after])
    adapter.query_order = lambda *_args: _async_next(queries)
    cancellations = []

    async def cancel(symbol, client_order_id, *, authority):
        cancellations.append((symbol, client_order_id, authority))
        return cancel_result

    adapter.cancel_order = cancel
    persisted = []

    async def persist(record):
        persisted.append(dict(record))
        return True

    adapter._persist_local_mainnet_protection = persist
    cancel_claim_lock = asyncio.Lock()
    cancel_claimed = False

    async def claim_cancel(record):
        nonlocal cancel_claimed
        async with cancel_claim_lock:
            if cancel_claimed or "entry_cancel=" in str(record.get("state_reason") or ""):
                return False
            cancel_claimed = True
            claimed = dict(record)
            claimed["state_reason"] = adapter._local_recovery_reason(
                claimed, entry_cancel="ATTEMPTED_UNKNOWN"
            )
            persisted.append(claimed)
            return True

    adapter.on_local_mainnet_entry_cancel_claim = claim_cancel
    order = SimpleNamespace()
    adapter.ledger = SimpleNamespace(
        get_order_by_client_id=lambda _client_id: _async_value(order),
        get_fills=lambda: _async_value(list(fills)),
    )

    async def recover(_order, _response):
        return None

    adapter.reconciliation = SimpleNamespace(_recover_order_fills=recover)
    return adapter, authority, cancellations, persisted


async def _async_return_none(*_args, **_kwargs):
    return None


async def _async_next(iterator):
    return next(iterator)


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_pending_zero_fill_entry_is_cancelled_and_read_back_without_close(monkeypatch):
    owner = _pilot_entry_owner()
    before = {
        "clientOrderId": owner["entry_client_order_id"], "symbol": "ETHUSDC",
        "side": "BUY", "origQty": "0.5", "executedQty": "0",
        "status": "NEW", "orderId": 101,
    }
    after = {**before, "status": "CANCELED"}
    adapter, authority, cancellations, persisted = _pilot_cancel_adapter(
        monkeypatch, before, after
    )

    result = await adapter._cancel_and_read_back_pilot_entry(owner, authority=authority)

    assert result is not None
    assert result["filled_quantity"] == 0
    assert result["state"] == "CLOSED"
    assert cancellations == [("ETHUSDC", owner["entry_client_order_id"], authority)]
    assert persisted[-1]["closed_at"] is not None
    assert persisted[-1]["unfilled_order_proof"] == {
        "order_id": "101",
        "client_order_id": owner["entry_client_order_id"],
        "order_status": "CANCELED",
        "executed_quantity": "0",
        "original_quantity": "0.5",
        "symbol": "ETHUSDC",
        "entry_side": "BUY",
        "position_side": "BOTH",
        "verified_at": persisted[-1]["unfilled_order_proof"]["verified_at"],
    }


@pytest.mark.asyncio
async def test_partial_fill_entry_remainder_is_cancelled_and_fills_reconciled(monkeypatch):
    owner = _pilot_entry_owner(filled="0.1", status="PARTIALLY_FILLED")
    before = {
        "clientOrderId": owner["entry_client_order_id"], "symbol": "ETHUSDC",
        "side": "BUY", "origQty": "0.5", "executedQty": "0.2",
        "status": "PARTIALLY_FILLED", "orderId": 102,
    }
    after = {**before, "status": "CANCELED"}
    recovered_fill = SimpleNamespace(
        client_order_id=owner["entry_client_order_id"],
        quantity=Decimal("0.2"), price=Decimal("100"),
    )
    adapter, authority, cancellations, persisted = _pilot_cancel_adapter(
        monkeypatch, before, after, fills=[recovered_fill]
    )

    result = await adapter._cancel_and_read_back_pilot_entry(owner, authority=authority)

    assert result is not None
    assert result["filled_quantity"] == Decimal("0.2")
    assert result["entry_average_price"] == Decimal("100")
    assert cancellations == [("ETHUSDC", owner["entry_client_order_id"], authority)]
    assert persisted[-1]["state"] == "PROTECTED"


@pytest.mark.asyncio
async def test_multi_level_entry_average_is_bounded_to_protection_scale(monkeypatch):
    # 0.01 @ 2610.55 + 0.009 @ 2610.61 averages to a non-terminating decimal
    # (2610.578421052631...). The stored NUMERIC(28, 10) owner must accept it.
    owner = _pilot_entry_owner(filled="0.019", status="PARTIALLY_FILLED")
    before = {
        "clientOrderId": owner["entry_client_order_id"], "symbol": "ETHUSDC",
        "side": "BUY", "origQty": "0.5", "executedQty": "0.019",
        "status": "PARTIALLY_FILLED", "orderId": 103,
    }
    after = {**before, "status": "CANCELED"}
    fills = [
        SimpleNamespace(
            client_order_id=owner["entry_client_order_id"],
            quantity=Decimal("0.01"), price=Decimal("2610.55"),
        ),
        SimpleNamespace(
            client_order_id=owner["entry_client_order_id"],
            quantity=Decimal("0.009"), price=Decimal("2610.61"),
        ),
    ]
    adapter, authority, _cancellations, persisted = _pilot_cancel_adapter(
        monkeypatch, before, after, fills=fills
    )

    result = await adapter._cancel_and_read_back_pilot_entry(owner, authority=authority)

    assert result is not None
    assert result["entry_average_price"] == Decimal("2610.5784210526")
    assert persisted[-1]["state"] == "PROTECTED"
    stored = persisted[-1]["entry_average_price"]
    assert stored == stored.quantize(Decimal("0.0000000001"))
    # The strict NUMERIC(28, 10) validator used by the real writer must accept it.
    from apps.trading_worker.persistence.postgres.repositories import _protection_decimal

    assert _protection_decimal(stored, "entry_average_price") == stored
    # The validator itself is unchanged: an unquantized value from an untrusted source
    # still fails closed.
    with pytest.raises(ValueError, match="NUMERIC"):
        _protection_decimal(Decimal("2610.578421052631578947368421"), "entry_average_price")


@pytest.mark.asyncio
async def test_ambiguous_entry_cancel_is_read_back_once_and_never_retried(monkeypatch):
    owner = _pilot_entry_owner()
    still_open = {
        "clientOrderId": owner["entry_client_order_id"], "symbol": "ETHUSDC",
        "side": "BUY", "origQty": "0.5", "executedQty": "0",
        "status": "NEW", "orderId": 103,
    }
    adapter, authority, cancellations, persisted = _pilot_cancel_adapter(
        monkeypatch, still_open, still_open, cancel_result=False
    )

    result = await adapter._cancel_and_read_back_pilot_entry(owner, authority=authority)

    assert result is None
    assert len(cancellations) == 1
    assert len(persisted) == 1
    assert "entry_cancel=ATTEMPTED_UNKNOWN" in persisted[0]["state_reason"]
    # A restart with the durable marker must query, not repeat cancellation.
    recovered = {**owner, **persisted[0]}
    assert await adapter._cancel_and_read_back_pilot_entry(recovered, authority=authority) is None
    assert len(cancellations) == 1


@pytest.mark.asyncio
async def test_concurrent_entry_cancel_claim_has_one_mutation_winner(monkeypatch):
    owner = _pilot_entry_owner()
    open_order = {
        "clientOrderId": owner["entry_client_order_id"], "symbol": "ETHUSDC",
        "side": "BUY", "origQty": "0.5", "executedQty": "0",
        "status": "NEW", "orderId": 104,
    }
    terminal_order = {**open_order, "status": "CANCELED"}
    adapter, authority, cancellations, persisted = _pilot_cancel_adapter(
        monkeypatch, open_order, terminal_order
    )
    second_adapter, second_authority, _, _ = _pilot_cancel_adapter(
        monkeypatch, open_order, terminal_order
    )
    read_count = 0
    read_lock = asyncio.Lock()
    both_initial_reads = asyncio.Event()

    async def query_order(_symbol, _client_order_id):
        nonlocal read_count
        async with read_lock:
            read_count += 1
            current = read_count
            if current == 2:
                both_initial_reads.set()
        if current <= 2:
            # Distinct Worker instances both observe the live order before
            # racing for the durable PostgreSQL claim.
            await both_initial_reads.wait()
            return open_order
        return terminal_order

    adapter.query_order = query_order
    second_adapter.query_order = query_order
    claim_lock = asyncio.Lock()
    claim_calls = 0
    claim_won = False

    async def claim_cancel(record):
        nonlocal claim_calls, claim_won
        claim_calls += 1
        async with claim_lock:
            if claim_won or "entry_cancel=ATTEMPTED_UNKNOWN" in str(record.get("state_reason") or ""):
                return False
            claim_won = True
            claimed = dict(record)
            claimed["state_reason"] = adapter._local_recovery_reason(
                claimed, entry_cancel="ATTEMPTED_UNKNOWN"
            )
            persisted.append(claimed)
            return True

    adapter.on_local_mainnet_entry_cancel_claim = claim_cancel
    second_adapter.on_local_mainnet_entry_cancel_claim = claim_cancel
    second_adapter.cancel_order = adapter.cancel_order

    outcomes = await asyncio.gather(
        adapter._cancel_and_read_back_pilot_entry(owner, authority=authority),
        second_adapter._cancel_and_read_back_pilot_entry(owner, authority=second_authority),
    )

    assert claim_calls == 2
    assert len(cancellations) == 1
    assert all(outcome is None or outcome["state"] == "CLOSED" for outcome in outcomes)
    assert read_count >= 4  # loser resolves only with exact order read-back
