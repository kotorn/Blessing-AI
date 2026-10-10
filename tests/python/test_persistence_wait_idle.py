"""The final pre-send fence needs pending_outbox == 0 and queue_size == 0, but
ensure_order_durable has just inserted an outbox row that the background
dispatcher only drains on its next poll (up to 1s). Drain it explicitly."""

from types import SimpleNamespace

import pytest

from apps.trading_worker.persistence.manager import (
    PersistenceConfig,
    PersistenceManager,
    PersistenceMode,
)


class _Repo:
    def __init__(self, pending: int, fail: bool = False):
        self.pending, self.fail = pending, fail

    async def dispatch_one(self) -> bool:
        if self.fail:
            raise RuntimeError("db down")
        if self.pending:
            self.pending -= 1
            return True
        return False

    async def pending_count(self) -> int:
        return self.pending


def _manager(repo):
    manager = PersistenceManager(
        db=SimpleNamespace(config_error=None),  # type: ignore[arg-type]
        config=PersistenceConfig(mode=PersistenceMode.OPTIONAL),
    )
    manager.repository = repo
    return manager


@pytest.mark.asyncio
async def test_outbox_is_drained_and_readiness_counts_are_zero():
    manager = _manager(_Repo(pending=2))
    assert await manager.wait_until_idle(1.0) is True
    status = manager.readiness()
    assert status["pending_outbox"] == 0
    assert status["queue_size"] == 0


@pytest.mark.asyncio
async def test_returns_false_when_the_outbox_cannot_be_drained():
    manager = _manager(_Repo(pending=1, fail=True))
    assert await manager.wait_until_idle(0.2) is False


@pytest.mark.asyncio
async def test_returns_false_without_a_repository():
    manager = _manager(None)
    assert await manager.wait_until_idle(0.1) is False
