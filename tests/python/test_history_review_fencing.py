"""Offline regression tests for repository CAS and one-shot submission fences."""

import asyncio
import copy
import hashlib
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from apps.trading_worker.persistence.postgres.repositories import (
    AlgoProtectionRepository,
    BinanceHistoryRepository,
)


NOW = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)


def checkpoint(**changes):
    return {
        "runtime_target": "LOCAL", "run_id": "launch-review", "symbol": "ETHUSDC",
        "history_kind": "ALL_ORDERS", "coverage_status": "COVERED", "cursor_id": 99,
        "anchor_at": NOW - timedelta(days=2), "covered_through": NOW - timedelta(minutes=5),
        "scan_id": None, "scan_started_at": None, "scan_from_at": None, "scan_to_at": None,
        "updated_at": NOW - timedelta(minutes=5), **changes,
    }


class HistoryDB:
    def __init__(self, row, race=None):
        self.row = dict(row)
        self.race = race
        self.queries = []
        self.identities = {}
        self.observations = {}

    @asynccontextmanager
    async def transaction(self):
        before = copy.deepcopy((self.row, self.identities, self.observations))
        try:
            yield self
        except BaseException:
            self.row, self.identities, self.observations = before
            raise

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "UPDATE binance_history_checkpoints" in query:
            if self.race:
                self.row.update(self.race)
                self.race = None
            row = self.row
            if "coverage_status = 'GAP'" in query:
                expected = dict(zip(
                    ("coverage_status", "scan_id", "cursor_id", "updated_at", "scan_started_at",
                     "scan_from_at", "scan_to_at", "covered_through"), args[5:13]
                ))
                assert "scan_id IS NOT DISTINCT FROM $7::uuid" in query
                assert "coverage_status = $6" in query
                assert "cursor_id = $8 AND updated_at = $9" in query
                if any(row.get(k) != v for k, v in expected.items()):
                    return None
                row.update(coverage_status="GAP", failure_code="RETENTION_GAP", scan_id=None,
                           scan_started_at=None, scan_from_at=None, scan_to_at=None, updated_at=args[4])
                return {"coverage_status": "GAP"}
            if "SET scan_id = $5" in query:
                if (row["coverage_status"] != "SCANNING"
                        or tuple(row[k] for k in ("scan_id", "cursor_id", "updated_at", "scan_from_at",
                                                 "scan_to_at", "scan_started_at")) != args[6:12]):
                    return None
                row.update(scan_id=args[4], scan_started_at=args[5], updated_at=args[5])
            else:
                if tuple(row[k] for k in ("coverage_status", "cursor_id", "updated_at", "scan_id",
                                         "covered_through")) != args[8:13]:
                    return None
                row.update(coverage_status="SCANNING", scan_id=args[4], scan_started_at=args[5],
                           scan_from_at=args[6], scan_to_at=args[7], updated_at=args[5])
            return {"scan_id": row["scan_id"]}
        if "FROM binance_history_checkpoints" in query:
            assert "FOR SHARE" in query
            return dict(self.row)
        if "FROM binance_history_item_observations" in query:
            return self.observations.get((args[4], args[5]))
        if "FROM binance_history_items" in query:
            return self.identities.get(args[4])
        raise AssertionError(query)

    async def execute(self, query, *args):
        self.queries.append(query)
        if "INSERT INTO binance_history_items" in query:
            self.identities.setdefault(args[4], {"client_id": args[5], "event_at": args[6]})
        elif "INSERT INTO binance_history_item_observations" in query:
            self.observations.setdefault((args[4], args[7]), {
                "client_id": args[5], "event_at": args[6], "payload_sha256": args[7],
                "payload": json.loads(args[8]), "observed_at": args[9],
            })
        else:
            raise AssertionError(query)


@pytest.mark.asyncio
async def test_stale_scanning_retention_checked_before_takeover():
    row = checkpoint(coverage_status="SCANNING", scan_id=uuid.uuid4(),
                     scan_started_at=NOW - timedelta(days=4), scan_from_at=NOW - timedelta(days=4),
                     scan_to_at=NOW - timedelta(days=3), updated_at=NOW - timedelta(minutes=3))
    db = HistoryDB(row)
    with pytest.raises(RuntimeError, match="retention window"):
        await BinanceHistoryRepository(db)._begin_checkpoint_scan(row, 3 * 86400, NOW)
    assert db.row["coverage_status"] == "GAP"
    assert db.row["scan_id"] is None
    assert not any("SET scan_id = $5" in q for q in db.queries)


@pytest.mark.asyncio
@pytest.mark.parametrize("race", [
    {"coverage_status": "COVERED"}, {"scan_id": uuid.uuid4()}, {"cursor_id": 100},
    {"updated_at": NOW}, {"scan_started_at": NOW}, {"scan_from_at": NOW - timedelta(hours=1)},
    {"scan_to_at": NOW}, {"covered_through": NOW},
])
async def test_stale_gap_writer_cannot_overwrite_changed_checkpoint(race):
    row = checkpoint(coverage_status="SCANNING", scan_id=uuid.uuid4(),
                     scan_started_at=NOW - timedelta(days=4), scan_from_at=NOW - timedelta(days=4),
                     scan_to_at=NOW - timedelta(days=3))
    db = HistoryDB(row, race=race)
    with pytest.raises(RuntimeError, match="changed before retention fencing"):
        await BinanceHistoryRepository(db)._begin_checkpoint_scan(row, 3 * 86400, NOW)
    assert db.row["coverage_status"] != "GAP"
    for key, value in race.items():
        assert db.row[key] == value


@pytest.mark.asyncio
async def test_stale_scan_within_retention_transfers_fence_and_keeps_window():
    row = checkpoint(coverage_status="SCANNING", scan_id=uuid.uuid4(),
                     scan_started_at=NOW - timedelta(hours=1), scan_from_at=NOW - timedelta(hours=2),
                     scan_to_at=NOW - timedelta(hours=1))
    db = HistoryDB(row)
    new_id = await BinanceHistoryRepository(db)._begin_checkpoint_scan(row, 3 * 86400, NOW)
    assert new_id != row["scan_id"]
    assert db.row["cursor_id"] == row["cursor_id"]
    assert db.row["scan_from_at"] == row["scan_from_at"]
    assert db.row["scan_to_at"] == row["scan_to_at"]


@pytest.mark.asyncio
async def test_active_scan_cannot_be_taken_over():
    row = checkpoint(coverage_status="SCANNING", scan_id=uuid.uuid4(),
                     scan_started_at=NOW - timedelta(minutes=1), scan_from_at=NOW - timedelta(hours=2),
                     scan_to_at=NOW - timedelta(hours=1), updated_at=NOW - timedelta(seconds=30))
    db = HistoryDB(row)
    with pytest.raises(RuntimeError, match="active scanner"):
        await BinanceHistoryRepository(db)._begin_checkpoint_scan(row, 3 * 86400, NOW)
    assert not db.queries


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor_age,retention,expected", [
    (48, 3 * 86400, NOW - timedelta(minutes=5, hours=24)),
    (2, 3 * 86400, NOW - timedelta(hours=2)),
    (48, 3600, NOW - timedelta(hours=1)),
])
async def test_replay_overlap_is_24_hours_bounded_by_anchor_and_retention(anchor_age, retention, expected):
    row = checkpoint(anchor_at=NOW - timedelta(hours=anchor_age))
    db = HistoryDB(row)
    await BinanceHistoryRepository(db)._begin_checkpoint_scan(row, retention, NOW)
    assert db.row["scan_from_at"] == expected
    assert db.row["scan_to_at"] == NOW
    assert db.row["cursor_id"] == 99


class EmergencyDB:
    def __init__(self, **owner_changes):
        self.now = NOW
        self.owner = {"environment": "MAINNET", "launch_runtime_target": "LOCAL",
                      "state": "PROTECTED", "basket_id": "basket-review",
                      "mainnet_launch_id": "launch-review", "state_reason": "", **owner_changes}
        self.claim = None
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            before = copy.deepcopy((self.owner, self.claim))
            try:
                yield self
            except BaseException:
                self.owner, self.claim = before
                raise

    async def fetchrow(self, query, *args):
        if "SELECT" in query and "FROM binance_algo_protections" in query:
            assert "FOR UPDATE" in query
            return dict(self.owner)
        if "SELECT * FROM binance_emergency_close_claims" in query:
            assert "FOR UPDATE" in query
            return dict(self.claim) if self.claim else None
        if "INSERT INTO binance_emergency_close_claims" in query:
            assert self.claim is None
            self.claim = {"close_client_order_id": args[3], "claimant_id": args[4],
                          "fencing_token": 1, "lease_until": self.now + timedelta(seconds=args[5]),
                          "status": args[6]}
            return dict(self.claim)
        if "UPDATE binance_emergency_close_claims" in query:
            assert "clock_timestamp()" in query
            if "SET claimant_id" in query:
                if self.claim["status"] != "RESERVED" or self.claim["lease_until"] > self.now:
                    return None
                self.claim.update(claimant_id=args[3], fencing_token=self.claim["fencing_token"] + 1,
                                  lease_until=self.now + timedelta(seconds=args[4]))
            else:
                if (self.claim is None or self.claim["status"] != "RESERVED"
                        or self.claim["lease_until"] <= self.now
                        or (self.claim["close_client_order_id"], self.claim["claimant_id"],
                            self.claim["fencing_token"]) != args[3:6]):
                    return None
                self.claim.update(status="ATTEMPTED")
            return dict(self.claim)
        if "UPDATE binance_algo_protections" in query:
            if self.owner["state"] == "CLOSED":
                return None
            if "SET state =" in query:
                self.owner["state"] = "CLOSE_PENDING"
            self.owner["state_reason"] = args[3]
            return {"entry_client_order_id": args[2]}
        raise AssertionError(query)


class EntryCancelDB:
    def __init__(self):
        self.owner = {
            "environment": "MAINNET", "venue": "binance_mainnet", "symbol": "ETHUSDC",
            "entry_client_order_id": "entry-cancel", "basket_id": "basket-review",
            "mainnet_launch_id": "launch-review", "entry_side": "BUY", "position_side": "BOTH",
            "requested_quantity": Decimal("0.5"),
            "filled_quantity": Decimal("0"), "entry_average_price": None,
            "stop_trigger_price": Decimal("90"),
            "take_profit_trigger_price": Decimal("110"),
            "stop_algo_id": None, "take_profit_algo_id": None,
            "stop_client_algo_id": "stop-cancel", "take_profit_client_algo_id": "target-cancel",
            "management_mode": "QUICK", "state": "PENDING", "state_reason": None,
            "first_fill_at": None, "protection_verified_at": None,
            "last_reconciled_at": None, "closed_at": None,
        }
        self.lock = asyncio.Lock()
        self.updates = 0

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            yield self

    async def fetchrow(self, query, *args):
        if "SELECT * FROM binance_algo_protections" in query:
            assert "FOR UPDATE" in query
            return dict(self.owner)
        if "UPDATE binance_algo_protections" in query:
            self.updates += 1
            assert self.owner["state"] == args[4]
            assert self.owner["state_reason"] == args[5]
            self.owner["state_reason"] = args[3]
            self.owner["last_reconciled_at"] = NOW
            return dict(self.owner)
        raise AssertionError(query)


@pytest.mark.asyncio
async def test_concurrent_mainnet_entry_cancel_claim_is_atomic_and_one_shot():
    db = EntryCancelDB()
    repo = AlgoProtectionRepository(db)
    results = await asyncio.gather(*(repo.claim_mainnet_entry_cancel(db.owner) for _ in range(12)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert db.updates == 1
    assert winners[0]["state_reason"] == "entry_cancel=ATTEMPTED_UNKNOWN"
    assert await repo.claim_mainnet_entry_cancel(db.owner) is None
    assert db.updates == 1


@pytest.mark.asyncio
async def test_emergency_close_cannot_race_ambiguous_entry_cancel_claim():
    db = EmergencyDB(state_reason="entry_cancel=ATTEMPTED_UNKNOWN")
    repo = AlgoProtectionRepository(db)

    with pytest.raises(RuntimeError, match="entry cancellation"):
        await claim(repo)

    assert db.claim is None
    assert db.owner["state"] == "PROTECTED"


@pytest.mark.asyncio
async def test_emergency_close_is_allowed_after_terminal_entry_cancel_readback():
    db = EmergencyDB(state_reason="entry_cancel=CONFIRMED")
    result = await claim(AlgoProtectionRepository(db))
    assert result["claimed"] is True
    assert db.owner["state"] == "CLOSE_PENDING"


async def claim(repo, claimant="worker-one", close_id="close-review"):
    return await repo.claim_local_emergency_close("ETHUSDC", "entry-review", close_id,
                                                 claimant_id=claimant)


async def attempt(repo, claimant="worker-one", fence=1, close_id="close-review"):
    return await repo.mark_local_emergency_close_attempted("ETHUSDC", "entry-review", close_id,
                                                          claimant_id=claimant, fencing_token=fence)


@pytest.mark.asyncio
async def test_concurrent_claims_and_attempts_have_single_winner():
    db = EmergencyDB()
    repo = AlgoProtectionRepository(db)
    results = await asyncio.gather(*(claim(repo, f"worker-{i}") for i in range(12)))
    winners = [result for result in results if result["claimed"]]
    assert len(winners) == 1
    winner = winners[0]
    permits = await asyncio.gather(*(attempt(repo, winner["claimant_id"], winner["fencing_token"])
                                     for _ in range(12)))
    assert sum(permits) == 1
    assert db.claim["status"] == "ATTEMPTED"
    assert "close_submission=ATTEMPTED" in db.owner["state_reason"]


@pytest.mark.asyncio
async def test_expired_unattempted_claim_transfers_and_fences_old_worker():
    db = EmergencyDB()
    repo = AlgoProtectionRepository(db)
    first = await claim(repo)
    db.now += timedelta(seconds=31)
    second = await claim(repo, "worker-two")
    assert second["claimed"] and second["fencing_token"] == first["fencing_token"] + 1
    assert not await attempt(repo, fence=first["fencing_token"])
    assert await attempt(repo, "worker-two", second["fencing_token"])


@pytest.mark.asyncio
async def test_expired_attempted_or_lost_response_never_grants_another_post():
    db = EmergencyDB()
    repo = AlgoProtectionRepository(db)
    await claim(repo)
    assert await attempt(repo)  # Response may be lost or POST outcome ambiguous.
    db.now += timedelta(days=10)
    result = await claim(repo, "worker-two")
    assert not result["claimed"] and result["status"] == "ATTEMPTED"
    assert not await attempt(repo)
    assert not await attempt(repo, "worker-two", result["fencing_token"])


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["close_submission=RESERVED", "close_submission=ATTEMPTED", "outcome=UNKNOWN"])
async def test_legacy_ambiguous_claim_never_reclaims_post(reason):
    db = EmergencyDB(state="CLOSE_PENDING", state_reason=f"local_close_client_order_id=close-review;{reason}")
    repo = AlgoProtectionRepository(db)
    result = await claim(repo)
    assert not result["claimed"] and result["status"] == "ATTEMPTED"
    assert not await attempt(repo)


@pytest.mark.asyncio
async def test_changed_close_id_is_rejected_for_same_owner():
    db = EmergencyDB()
    repo = AlgoProtectionRepository(db)
    await claim(repo)
    with pytest.raises(RuntimeError, match="identity changed"):
        await claim(repo, close_id="another-close")
    assert not await attempt(repo, close_id="another-close")


@pytest.mark.asyncio
async def test_expired_claim_cannot_submit_until_new_fence_claimed():
    db = EmergencyDB()
    repo = AlgoProtectionRepository(db)
    await claim(repo)
    db.now += timedelta(seconds=30)
    assert not await attempt(repo)
    assert db.claim["status"] == "RESERVED"


@pytest.mark.asyncio
async def test_claim_requires_local_mainnet_and_never_reopens_closed_owner():
    db = EmergencyDB(launch_runtime_target="CLOUD_RUN")
    with pytest.raises(RuntimeError, match="Local Mainnet owner"):
        await claim(AlgoProtectionRepository(db))
    db = EmergencyDB(state="CLOSED")
    assert not (await claim(AlgoProtectionRepository(db)))["claimed"]
    assert db.claim is None


def order_item(status="FILLED"):
    payload = {"orderId": 123, "clientOrderId": "close-review", "symbol": "ETHUSDC", "status": status}
    return {"item_id": 123, "client_id": "close-review", "event_at": NOW - timedelta(hours=30),
            "payload": payload, "payload_sha256": hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


@pytest.mark.asyncio
async def test_exact_order_refresh_observes_old_order_without_advancing_cursor():
    row = checkpoint()
    db = HistoryDB(row)
    repo = BinanceHistoryRepository(db)
    first = await repo.record_order_history_observation(checkpoint=row, item=order_item("NEW"), observed_at=NOW)
    second = await repo.record_order_history_observation(checkpoint=row, item=order_item(), observed_at=NOW)
    assert first["payload"]["status"] == "NEW"
    assert second["payload"]["status"] == "FILLED"
    assert len(db.observations) == 2
    assert db.row == row


@pytest.mark.asyncio
async def test_exact_order_refresh_rejects_changed_identity_and_uncovered_scope():
    row = checkpoint()
    db = HistoryDB(row)
    repo = BinanceHistoryRepository(db)
    await repo.record_order_history_observation(checkpoint=row, item=order_item(), observed_at=NOW)
    changed = order_item()
    changed["event_at"] += timedelta(seconds=1)
    with pytest.raises(RuntimeError, match="durable identity"):
        await repo.record_order_history_observation(checkpoint=row, item=changed, observed_at=NOW)
    db.row["coverage_status"] = "GAP"
    with pytest.raises(RuntimeError, match="completely covered"):
        await repo.record_order_history_observation(checkpoint=row, item=order_item(), observed_at=NOW)


def test_migration_permanently_fences_attempts_and_enforces_unique_owner_and_close_id():
    sql = (Path(__file__).resolve().parents[2] / "infra/postgres/migrations/020_emergency_close_submission_claims.sql").read_text()
    assert "PRIMARY KEY (venue, symbol, entry_client_order_id)" in sql
    assert "UNIQUE (venue, close_client_order_id)" in sql
    assert "OLD.status = 'ATTEMPTED'" in sql
    assert "NEW.fencing_token <> OLD.fencing_token + 1" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
