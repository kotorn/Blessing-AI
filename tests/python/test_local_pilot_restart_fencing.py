from __future__ import annotations

import pytest

from apps.trading_worker.persistence.postgres.repositories import PersistenceRepository


class _RestartFenceDb:
    """SQL-contract double; deliberately avoids opening a database connection."""

    def __init__(self, *, pending_order_client_order_id: str | None) -> None:
        self.pending_order_client_order_id = pending_order_client_order_id
        self.state = "PAUSED_NEW_RISK"
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *args: object) -> str:
        self.calls.append((query, args))
        if "UPDATE mainnet_launch_sessions" not in query:
            raise AssertionError("unexpected SQL in restart fence")
        if "SET state = 'CLOSED'" in query:
            return "UPDATE 0"

        # The simulated persisted row is a LOCAL LIVE_RESEARCH_PILOT paused
        # before restart. Only report an update when the SQL admits that state.
        admits_paused = "AND state IN ('ACTIVE', 'PAUSED_NEW_RISK')" in query
        admits_pilot = "'LIVE_RESEARCH_PILOT'" in query
        admits_local = "'LOCAL'" in query
        if admits_paused and admits_pilot and admits_local and args == ("ETHUSDC",):
            self.state = (
                "RECONCILIATION_REQUIRED"
                if self.pending_order_client_order_id is not None
                else "REAUTH_REQUIRED"
            )
            return "UPDATE 1"
        return "UPDATE 0"


@pytest.mark.parametrize(
    ("pending_order_client_order_id", "expected_state"),
    [
        (None, "REAUTH_REQUIRED"),
        ("pilot-entry-pending", "RECONCILIATION_REQUIRED"),
    ],
)
@pytest.mark.asyncio
async def test_restart_fences_paused_live_pilot_sessions(
    pending_order_client_order_id: str | None,
    expected_state: str,
) -> None:
    db = _RestartFenceDb(
        pending_order_client_order_id=pending_order_client_order_id
    )
    repository = PersistenceRepository(db)

    changed = await repository.mark_mainnet_launches_reauth_required()

    assert changed == 1
    assert db.state == expected_state
    update_sql = next(
        query
        for query, _ in db.calls
        if "SET state = CASE" in query
    )
    assert "AND state IN ('ACTIVE', 'PAUSED_NEW_RISK')" in update_sql
    assert "WHEN pending_order_client_order_id IS NOT NULL" in update_sql
