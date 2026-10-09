from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.trading_worker.venues.binance.session import (
    ClockDriftError,
    SessionLimits,
    SessionPhase,
    SessionTimer,
)
from apps.trading_worker.main import TradingWorkerApp, WorkerExecutionMode

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def timer(*, mono=100.0):
    return SessionTimer.start(T0, wall_now=T0, monotonic_now=mono)


def test_first_successful_arm_is_t0_and_90m_without_entry_is_no_entry():
    subject = timer()

    assert subject.phase(
        wall_now=T0 + timedelta(minutes=90),
        monotonic_now=100 + 90 * 60,
        entry_count=0,
        exchange_flat=True,
        open_algo_count=0,
        ledger_in_sync=True,
    ) is SessionPhase.NO_ENTRY


def test_entry_before_cutoff_enters_close_only_at_110m():
    subject = timer()
    assert subject.phase(
        wall_now=T0 + timedelta(minutes=89), monotonic_now=100 + 89 * 60,
        entry_count=1, exchange_flat=False, open_algo_count=2, ledger_in_sync=True,
    ) is SessionPhase.ENTRY_ALLOWED
    assert subject.phase(
        wall_now=T0 + timedelta(minutes=110), monotonic_now=100 + 110 * 60,
        entry_count=1, exchange_flat=False, open_algo_count=2, ledger_in_sync=True,
    ) is SessionPhase.CLOSE_ONLY


@pytest.mark.parametrize(
    "exchange_flat,open_algo_count,ledger_in_sync",
    [(False, 0, True), (True, 1, True), (True, 0, False)],
)
def test_120m_session_end_waits_for_flat_algos_and_synced_ledger(
    exchange_flat, open_algo_count, ledger_in_sync
):
    subject = timer()
    assert subject.phase(
        wall_now=T0 + timedelta(minutes=120), monotonic_now=100 + 120 * 60,
        entry_count=1, exchange_flat=exchange_flat,
        open_algo_count=open_algo_count, ledger_in_sync=ledger_in_sync,
    ) is SessionPhase.CLOSE_ONLY
    assert subject.phase(
        wall_now=T0 + timedelta(minutes=120), monotonic_now=100 + 120 * 60,
        entry_count=1, exchange_flat=True, open_algo_count=0, ledger_in_sync=True,
    ) is SessionPhase.ENDED


@pytest.mark.parametrize(
    "wall_delta,mono_delta",
    [(timedelta(minutes=5), 10.0), (-timedelta(seconds=10), 10.0)],
)
def test_wall_monotonic_disagreement_fails_closed(wall_delta, mono_delta):
    subject = timer()
    with pytest.raises(ClockDriftError):
        subject.phase(
            wall_now=T0 + timedelta(seconds=10) + wall_delta,
            monotonic_now=110 + mono_delta,
            entry_count=1,
            exchange_flat=False,
            open_algo_count=2,
            ledger_in_sync=True,
        )


def test_restart_recovery_anchors_to_persisted_t0_without_extending_deadline():
    subject = SessionTimer.start(
        T0, wall_now=T0 + timedelta(minutes=80), monotonic_now=500.0
    )
    assert subject.phase(
        wall_now=T0 + timedelta(minutes=90), monotonic_now=1100.0,
        entry_count=0, exchange_flat=True, open_algo_count=0, ledger_in_sync=True,
    ) is SessionPhase.NO_ENTRY


def test_policy_may_tighten_but_cannot_extend_the_pinned_session_window():
    assert SessionLimits.from_policy({}).end_seconds == 7200
    with pytest.raises(ValueError, match="pinned safe window"):
        SessionLimits.from_policy(
            {
                "session_entry_cutoff_seconds": 5401,
                "session_close_after_seconds": 6600,
                "session_end_seconds": 7200,
            }
        )


@pytest.mark.asyncio
async def test_restart_after_close_does_not_submit_a_second_close_order(monkeypatch):
    class ExchangeBackedCloseAdapter:
        def __init__(self):
            self.position_open = True
            self.close_orders = 0

        async def emergency_flatten(self, _symbol, *, authority):
            assert authority is not None
            # Mirrors the adapter's authoritative positionRisk read before it
            # decides whether another reduce-only close order is needed.
            if self.position_open:
                self.close_orders += 1
                self.position_open = False
            return []

    adapter = ExchangeBackedCloseAdapter()
    recovered_session = {
        "policy": "LIVE_RESEARCH_PILOT",
        "launch_id": "launch-recovered",
        "pilot_session_armed_at": T0.isoformat().replace("+00:00", "Z"),
    }
    subject_timer = SessionTimer.start(
        T0,
        wall_now=T0,
        monotonic_now=100.0,
    )
    monkeypatch.setattr(
        "apps.trading_worker.main.utc_now",
        lambda: T0 + timedelta(minutes=111),
    )
    monkeypatch.setattr("apps.trading_worker.main.time.monotonic", lambda: 6_760.0)

    def restarted_worker():
        worker = object.__new__(TradingWorkerApp)
        worker._mainnet_launch_session = dict(recovered_session)
        worker._pilot_session_flatten_claimed = False
        worker._pilot_session_last_phase = None
        worker._pilot_session_timer_for_session = lambda: subject_timer
        worker._pilot_session_phase = AsyncMock(return_value=SessionPhase.CLOSE_ONLY)
        worker.execution_mode = WorkerExecutionMode.LIVE
        worker.execution_adapter = adapter
        worker.pause_new_risk = False
        return worker

    first_worker = restarted_worker()
    await first_worker._enforce_pilot_session_deadlines()
    assert adapter.close_orders == 1

    # Simulate a process crash after the exchange accepted the close but before
    # the worker's process-local claim could survive. Recovered T0 is unchanged;
    # the authoritative exchange read prevents a second close order.
    recovered_worker = restarted_worker()
    await recovered_worker._enforce_pilot_session_deadlines()
    assert adapter.close_orders == 1

