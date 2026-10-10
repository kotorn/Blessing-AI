"""The pilot risk context needs an account snapshot <= 5s old for the whole
multi-second entry chain (clamp, gate #1, final fence), but the worker only
reconciled when the snapshot was older than 30s. Refresh just in time."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from apps.trading_worker.main import TradingWorkerApp
from domain.models import utc_now


class _Reconciliation:
    def __init__(self, adapter, result="IN_SYNC"):
        self.adapter, self.result, self.calls = adapter, result, 0

    async def reconcile(self):
        self.calls += 1
        if self.result == "IN_SYNC":
            self.adapter.account_snapshot = SimpleNamespace(timestamp=utc_now())
        return self.result


def _worker(snapshot_age_s, result="IN_SYNC"):
    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    adapter = SimpleNamespace(
        account_snapshot=SimpleNamespace(timestamp=utc_now() - timedelta(seconds=snapshot_age_s)),
    )
    adapter.reconciliation = _Reconciliation(adapter, result)
    worker.execution_adapter = adapter
    return worker, adapter


@pytest.mark.asyncio
async def test_stale_snapshot_is_reconciled_before_the_entry_chain():
    worker, adapter = _worker(10.0)
    assert await worker._ensure_fresh_pilot_account_snapshot() is True
    assert adapter.reconciliation.calls == 1


@pytest.mark.asyncio
async def test_fresh_snapshot_is_not_reconciled_again():
    worker, adapter = _worker(0.4)
    assert await worker._ensure_fresh_pilot_account_snapshot() is True
    assert adapter.reconciliation.calls == 0


@pytest.mark.asyncio
async def test_out_of_sync_reconciliation_blocks_the_entry():
    worker, adapter = _worker(10.0, result="MISMATCH")
    assert await worker._ensure_fresh_pilot_account_snapshot() is False


@pytest.mark.asyncio
async def test_reconciliation_error_blocks_the_entry():
    worker, adapter = _worker(10.0)

    async def boom():
        raise RuntimeError("exchange unavailable")

    adapter.reconciliation.reconcile = boom
    assert await worker._ensure_fresh_pilot_account_snapshot() is False


@pytest.mark.asyncio
async def test_missing_snapshot_is_reconciled():
    worker, adapter = _worker(0.0)
    adapter.account_snapshot = None
    assert await worker._ensure_fresh_pilot_account_snapshot() is True
    assert adapter.reconciliation.calls == 1
