from decimal import Decimal
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json

import pytest

from apps.trading_worker.persistence.postgres.repositories import AlgoProtectionRepository


def owner():
    return AlgoProtectionRepository._new_record({
        "venue": "binance_mainnet", "environment": "MAINNET", "symbol": "ETHUSDC",
        "entry_client_order_id": "entry-review", "basket_id": "basket-review",
        "mainnet_launch_id": "launch-review", "management_mode": "QUICK",
        "entry_side": "BUY", "position_side": "BOTH",
        "requested_quantity": Decimal("0.01"), "filled_quantity": Decimal("0"),
        "stop_trigger_price": Decimal("1900"), "take_profit_trigger_price": Decimal("2200"),
        "stop_client_algo_id": "stop-review", "take_profit_client_algo_id": "target-review",
        "state": "UNKNOWN",
        "state_reason": "local_close_client_order_id=close-review;close_submission=ATTEMPTED;algo_cancel=ATTEMPTED_UNKNOWN;entry_cancel=ATTEMPTED_UNKNOWN",
    })


def test_stale_diagnostic_snapshot_cannot_erase_attempt_markers():
    stored = owner()
    merged = AlgoProtectionRepository._merge_existing(stored, {"state_reason": "later read failed"})
    for part in stored["state_reason"].split(";"):
        assert part in merged["state_reason"]


def test_emergency_identity_cannot_change_in_owner_update():
    with pytest.raises(ValueError, match="identity cannot change"):
        AlgoProtectionRepository._merge_existing(owner(), {
            "state_reason": "local_close_client_order_id=different;close_submission=RESERVED",
        })


def test_attempt_state_cannot_regress_but_verified_cancel_can_advance():
    merged = AlgoProtectionRepository._merge_existing(owner(), {
        "state_reason": "close_submission=RESERVED;algo_cancel=CONFIRMED;entry_cancel=CONFIRMED",
    })
    assert "close_submission=ATTEMPTED" in merged["state_reason"]
    assert "algo_cancel=CONFIRMED" in merged["state_reason"]
    assert "entry_cancel=CONFIRMED" in merged["state_reason"]


@pytest.mark.asyncio
async def test_verified_unknown_owner_closes_with_exact_state_cas():
    record = owner()
    record.update(filled_quantity=Decimal("0.01"), entry_average_price=Decimal("2000"))

    class Database:
        @asynccontextmanager
        async def transaction(self):
            yield self

        async def fetchrow(self, query, *args):
            if "SELECT *" in query:
                return dict(record)
            assert "AND state = $7" in query
            assert args[6] == "UNKNOWN"
            record.update(state="CLOSED", state_reason=args[3], closed_at=args[4],
                          closure_evidence=json.loads(args[5]))
            return dict(record)

    proof = {
        "algo_id": "LOCAL_EMERGENCY_CLOSE", "order_id": "1001",
        "client_order_id": "close-review", "order_status": "FILLED",
        "executed_quantity": "0.01", "trade_quantity": "0.01",
        "position_quantity": "0", "open_child_order_ids": [],
        "open_owner_algo_ids": [], "verified_at": datetime.now(timezone.utc),
    }
    result = await AlgoProtectionRepository(Database()).close_mainnet_protection_with_proof(
        "ETHUSDC", "entry-review", proof,
    )
    assert result["state"] == "CLOSED"
    assert result["closure_evidence"]["kind"] == "LOCAL_EMERGENCY_CLOSE_VERIFIED"
