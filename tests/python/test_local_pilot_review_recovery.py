"""Local-only regression coverage for emergency owner recovery and monitor exits."""

import asyncio
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.models import ConnectionState
from domain.enums import MarketType, OrderSide, OrderType, PositionSide, TimeInForce
from domain.models import ExecutionOrder, OrderIntent


@pytest.fixture(autouse=True)
def forbid_services(monkeypatch):
    """Any accidentally unmocked exchange or database client must fail locally."""
    import aiohttp
    import asyncpg

    from apps.trading_worker.venues.binance.rest_client import BinanceRestClient

    async def forbidden(*args, **kwargs):
        raise AssertionError("real exchange/network/database operations are forbidden")

    monkeypatch.setattr(aiohttp.ClientSession, "_request", forbidden)
    monkeypatch.setattr(asyncpg, "connect", forbidden)
    monkeypatch.setattr(asyncpg, "create_pool", forbidden)
    monkeypatch.setattr(BinanceRestClient, "request", forbidden)


def owner_record(**updates):
    return {
        "environment": "MAINNET", "venue": "binance_mainnet", "symbol": "ETHUSDC",
        "mainnet_launch_id": "launch-review", "entry_client_order_id": "entry-review",
        "exchange_order_id": "123", "basket_id": "basket-review", "entry_side": "BUY",
        "position_side": "BOTH", "requested_quantity": Decimal("0.1"),
        "filled_quantity": Decimal("0.1"), "entry_average_price": Decimal(100),
        "stop_trigger_price": Decimal(90), "take_profit_trigger_price": Decimal(110),
        "stop_algo_id": "1", "take_profit_algo_id": "2", "stop_client_algo_id": "sl-review",
        "take_profit_client_algo_id": "tp-review", "management_mode": "QUICK",
        "state": "PROTECTED", "state_reason": None,
        "first_fill_at": datetime.now(UTC), **updates,
    }


class OwnerRepository:
    """Model a durable, fenced claim separately from the mutable owner row."""

    def __init__(self, row):
        self.row = deepcopy(row)
        self.claims = []
        self.writes = []
        self.reservation = None

    async def get_protection(self, venue, symbol, entry_client_order_id):
        assert (venue, symbol, entry_client_order_id) == (
            self.row["venue"], self.row["symbol"], self.row["entry_client_order_id"],
        )
        return deepcopy(self.row)

    async def list_active_protections(self, **kwargs):
        return [deepcopy(self.row)]

    async def persist(self, row):
        self.writes.append(deepcopy(row))
        self.row = deepcopy(row)
        return True

    async def claim_local_emergency_close(self, symbol, entry_id, close_id, *, claimant_id, lease_seconds=30):
        self.claims.append({"expected_state_reason": self.row["state_reason"]})
        if self.reservation is not None or self.row["state"] == "CLOSED":
            return {"claimed": False, **(self.reservation or {})}
        self.reservation = {
            "status": "RESERVED", "claimant_id": claimant_id, "fencing_token": 1,
            "lease_until": datetime.now(UTC) + timedelta(seconds=lease_seconds),
            "close_client_order_id": close_id,
        }
        self.row.update(state="CLOSE_PENDING", state_reason=f"local_close_client_order_id={close_id};close_submission=RESERVED")
        return {"claimed": True, **self.reservation}

    async def mark_local_emergency_close_attempted(self, symbol, entry_id, close_id, *, claimant_id, fencing_token):
        if self.reservation is None or self.reservation["status"] != "RESERVED" or (
            self.reservation["claimant_id"] != claimant_id or self.reservation["fencing_token"] != fencing_token
        ):
            return False
        self.reservation["status"] = "ATTEMPTED"
        self.row.update(state_reason=f"local_close_client_order_id={close_id};close_submission=ATTEMPTED")
        return True


class FencedOwnerRepository(OwnerRepository):
    def __init__(self, row, failure=None):
        super().__init__(row)
        self.failure = failure
        self.events = []
        self.attempted = False

    async def claim_local_emergency_close(self, symbol, entry_id, close_id, *, claimant_id):
        self.events.append("reserve")
        if self.failure == "reserve_lost_ack":
            raise RuntimeError("lost reservation acknowledgement")
        self.row.update(state="CLOSE_PENDING", state_reason=f"local_close_client_order_id={close_id};close_submission=RESERVED")
        return {"claimed": not self.attempted, "close_client_order_id": close_id, "fencing_token": 1}

    async def mark_local_emergency_close_attempted(self, symbol, entry_id, close_id, *, claimant_id, fencing_token):
        self.events.append("attempt")
        assert fencing_token == 1
        if self.failure == "mark_denied":
            return False
        self.attempted = True
        self.row.update(state="CLOSE_PENDING", state_reason=f"local_close_client_order_id={close_id};close_submission=ATTEMPTED")
        if self.failure == "mark_lost_ack":
            raise RuntimeError("lost attempt acknowledgement")
        return True

    async def persist(self, row):
        self.events.append("persist_markers")
        if self.failure == "marker_write_failed":
            return False
        return await super().persist(row)

def make_adapter(row=None):
    repository = OwnerRepository(row or owner_record())
    session = {
        "policy": "LIVE_RESEARCH_PILOT", "launch_id": "launch-review",
        "pilot_campaign_id": "campaign-review", "runtime_target": "LOCAL", "symbol": "ETHUSDC",
        "pilot_status": "ACTIVE", "state": "AUTONOMOUS_ACTIVE", "pilot_quick_max_hold_seconds": 86400,
        "pilot_net_pnl_usdc": "0", "pilot_peak_pnl_usdc": "0",
    }
    authority = SimpleNamespace(
        _mainnet_launch_id="launch-review", _mainnet_launch_session=session,
        pause_new_risk=False, kill_switch_active=False,
        persistence=SimpleNamespace(
            repository=SimpleNamespace(algo_protections=repository),
            get_mainnet_launch_session=AsyncMock(return_value=session),
            enter_local_live_pilot_close_only=AsyncMock(return_value={
                **session, "pilot_status": "CLOSE_ONLY", "state": "PAUSED_NEW_RISK",
            }),
        ),
        _persist_local_live_pilot_mark=AsyncMock(return_value=True),
        get_local_live_pilot_accounting=AsyncMock(return_value=None),
    )
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.MAINNET
    adapter.preflight_only = False
    adapter._worker_authority = authority
    adapter._mutation_lock = asyncio.Lock()
    adapter._is_local_mainnet_runtime = lambda: True
    adapter._worker_authorized = lambda candidate: candidate is authority
    adapter.ledger = InMemoryLedger()
    adapter.reconciliation = SimpleNamespace(last_status="IN_SYNC", _recover_order_fills=AsyncMock())
    adapter.state = ConnectionState.READY
    adapter._assert_execution_lease = AsyncMock()
    adapter.rest_client = SimpleNamespace(portfolio_margin=False, request=AsyncMock(return_value=[{
        "symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0.1", "unRealizedProfit": "0",
        "entryPrice": "100", "markPrice": "100", "leverage": "1", "marginType": "ISOLATED",
    }]))
    adapter.query_order = AsyncMock(return_value=None)
    adapter._cancel_local_mainnet_owned_algos = AsyncMock(return_value=True)
    adapter._verify_local_mainnet_close = AsyncMock(return_value=False)
    adapter._execute_decision = AsyncMock(return_value=[])
    adapter.on_local_mainnet_protection_update = repository.persist
    adapter.on_local_mainnet_close_verified = AsyncMock(return_value=True)
    adapter._last_local_mainnet_risk_evidence = None
    return adapter, authority, repository


def entry_objects(adapter, row):
    return adapter._reconstruct_intent_and_order_from_record(row)


@pytest.mark.asyncio
async def test_close_reenters_own_mutation_scope_without_deadlock():
    adapter, authority, repository = make_adapter()
    intent, order = entry_objects(adapter, repository.row)

    async def nested():
        async with adapter._mutation_scope():
            await adapter._local_mainnet_close_only_once(
                intent, order, repository.row, reason="test", authority=authority,
            )

    await asyncio.wait_for(nested(), timeout=1)
    adapter._execute_decision.assert_awaited_once()
    assert not adapter._mutation_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "reserve_lost_ack", "mark_denied", "mark_lost_ack", "marker_write_failed"])
async def test_fenced_repository_consumes_attempt_before_mutation(failure):
    adapter, authority, original = make_adapter()
    repository = FencedOwnerRepository(original.row, failure)
    authority.persistence.repository.algo_protections = repository
    adapter.on_local_mainnet_protection_update = repository.persist
    intent, order = entry_objects(adapter, repository.row)
    await adapter._local_mainnet_close_only_once(intent, order, repository.row, reason="test", authority=authority)
    if failure is None:
        adapter._execute_decision.assert_awaited_once()
        assert repository.events[:3] == ["reserve", "attempt", "persist_markers"]
        assert "algo_cancel=CONFIRMED" in repository.row["state_reason"]
    else:
        adapter._execute_decision.assert_not_awaited()
        adapter._cancel_local_mainnet_owned_algos.assert_not_awaited()
        assert authority.pause_new_risk
        if failure in {"mark_lost_ack", "marker_write_failed"}:
            assert repository.attempted
            assert "close_submission=ATTEMPTED" in repository.row["state_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("acknowledgement", [None, 1, "true"])
async def test_only_literal_true_attempt_acknowledgement_permits_post(acknowledgement):
    adapter, authority, repository = make_adapter()
    mark_attempted = repository.mark_local_emergency_close_attempted

    async def wrong_ack(*args, **kwargs):
        assert await mark_attempted(*args, **kwargs) is True
        return acknowledgement

    repository.mark_local_emergency_close_attempted = wrong_ack
    intent, order = entry_objects(adapter, repository.row)
    await adapter._local_mainnet_close_only_once(intent, order, repository.row, reason="test", authority=authority)
    adapter._execute_decision.assert_not_awaited()
    adapter._cancel_local_mainnet_owned_algos.assert_not_awaited()
    assert repository.reservation["status"] == "ATTEMPTED"


@pytest.mark.asyncio
async def test_concurrent_close_reload_and_atomic_claim_allow_one_submission():
    adapter, authority, repository = make_adapter()
    intent, order = entry_objects(adapter, repository.row)
    stale = deepcopy(repository.row)
    await asyncio.wait_for(asyncio.gather(*(
        adapter._local_mainnet_close_only_once(intent, order, deepcopy(stale), reason="test", authority=authority)
        for _ in range(2)
    )), timeout=1)
    adapter._execute_decision.assert_awaited_once()
    adapter._cancel_local_mainnet_owned_algos.assert_awaited_once()
    assert len(repository.claims) == 1
    assert repository.claims[0]["expected_state_reason"] is None
    assert "close_submission=ATTEMPTED" in repository.row["state_reason"]


@pytest.mark.asyncio
async def test_independent_adapters_need_atomic_claim_not_only_local_lock():
    first, first_authority, repository = make_adapter()
    second, second_authority, _ = make_adapter()
    second_authority.persistence.repository.algo_protections = repository
    second.on_local_mainnet_protection_update = repository.persist
    queries = 0
    both_absent = asyncio.Event()

    async def absent(*args):
        nonlocal queries
        queries += 1
        if queries == 2:
            both_absent.set()
        await both_absent.wait()

    first.query_order = second.query_order = absent
    intent, order = entry_objects(first, repository.row)
    snapshot = deepcopy(repository.row)
    await asyncio.wait_for(asyncio.gather(
        first._local_mainnet_close_only_once(intent, order, snapshot, reason="first", authority=first_authority),
        second._local_mainnet_close_only_once(intent, order, snapshot, reason="second", authority=second_authority),
    ), timeout=1)
    assert len(repository.claims) == 2
    assert first._execute_decision.await_count + second._execute_decision.await_count == 1
    assert "close_submission=ATTEMPTED" in repository.row["state_reason"]


@pytest.mark.asyncio
async def test_child_task_cannot_inherit_parent_mutation_ownership():
    adapter, _, _ = make_adapter()
    entered = asyncio.Event()

    async def child():
        async with adapter._mutation_scope():
            entered.set()

    async with adapter._mutation_scope():
        pending = asyncio.create_task(child())
        await asyncio.sleep(0)
        assert not entered.is_set()
    await asyncio.wait_for(pending, timeout=1)
    assert entered.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("claim_result", ["missing", "denied", "lost_ack"])
async def test_claim_failure_never_submits_cancels_or_overwrites_attempt(claim_result):
    adapter, authority, repository = make_adapter()
    intent, order = entry_objects(adapter, repository.row)
    if claim_result == "missing":
        repository.claim_local_emergency_close = None
    else:
        async def fail_claim(symbol, entry_id, close_id, **kwargs):
            repository.row["state_reason"] = f"local_close_client_order_id={close_id};close_submission=ATTEMPTED"
            repository.row["state"] = "CLOSE_PENDING"
            if claim_result == "lost_ack":
                raise RuntimeError("lost claim acknowledgement")
        repository.claim_local_emergency_close = fail_claim
    await adapter._local_mainnet_close_only_once(intent, order, repository.row, reason="test", authority=authority)
    adapter._execute_decision.assert_not_awaited()
    adapter._cancel_local_mainnet_owned_algos.assert_not_awaited()
    assert repository.writes == []
    assert authority.pause_new_risk is True
    if claim_result != "missing":
        assert "close_submission=ATTEMPTED" in repository.row["state_reason"]


@pytest.mark.asyncio
async def test_close_owner_sizing_mismatch_blocks_claim_and_mutation():
    adapter, authority, repository = make_adapter()
    adapter.rest_client.request.return_value[0]["positionAmt"] = "0.2"
    intent, order = entry_objects(adapter, repository.row)
    await adapter._local_mainnet_close_only_once(intent, order, repository.row, reason="test", authority=authority)
    assert repository.claims == []
    adapter._execute_decision.assert_not_awaited()
    adapter._cancel_local_mainnet_owned_algos.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_reserved_owner_is_claimed_against_exact_reason():
    adapter, authority, repository = make_adapter()
    intent, order = entry_objects(adapter, repository.row)
    close_id = adapter._generate_client_order_id(adapter._local_close_decision_id(order.client_order_id), intent.symbol, order_index=0)
    reason = f"local_close_client_order_id={close_id};close_submission=RESERVED"
    repository.row.update(state="CLOSE_PENDING", state_reason=reason)
    await adapter._local_mainnet_close_only_once(intent, order, repository.row, reason="restart", authority=authority)
    assert repository.claims[0]["expected_state_reason"] == reason
    adapter._execute_decision.assert_awaited_once()


@pytest.mark.asyncio
async def test_ambiguous_algo_cancel_survives_unknown_and_restart():
    adapter, authority, repository = make_adapter()
    adapter._cancel_local_mainnet_owned_algos.return_value = False
    intent, order = entry_objects(adapter, repository.row)
    await adapter._local_mainnet_close_only_once(intent, order, repository.row, reason="test", authority=authority)
    assert "algo_cancel=ATTEMPTED_UNKNOWN" in repository.row["state_reason"]
    await adapter._local_mainnet_close_only_once(intent, order, repository.row, reason="restart", authority=authority)
    assert "algo_cancel=ATTEMPTED_UNKNOWN" in repository.row["state_reason"]
    adapter._execute_decision.assert_awaited_once()
    adapter._cancel_local_mainnet_owned_algos.assert_awaited_once()
    assert "algo_cancel=ATTEMPTED_UNKNOWN" in adapter._verify_local_mainnet_close.await_args.args[1]["state_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_filled", ["0", "0.1"])
async def test_unknown_zero_fill_owner_reconciles_terminal_entry(terminal_filled):
    adapter, authority, repository = make_adapter(owner_record(state="UNKNOWN", filled_quantity=Decimal(0)))
    updated = {**repository.row, "filled_quantity": Decimal(terminal_filled),
               "state": "CLOSED" if terminal_filled == "0" else "UNKNOWN"}
    adapter._cancel_and_read_back_pilot_entry = AsyncMock(return_value=updated)
    adapter._local_mainnet_close_only_once = AsyncMock(return_value=True)
    assert await adapter._recover_unprotected_local_pilot_owner(repository.row, authority=authority, launch_id="launch-review")
    adapter._cancel_and_read_back_pilot_entry.assert_awaited_once()
    if terminal_filled == "0":
        adapter._local_mainnet_close_only_once.assert_not_awaited()
    else:
        adapter._local_mainnet_close_only_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_ambiguous_entry_cancel_is_durable_and_not_repeated():
    adapter, authority, repository = make_adapter(owner_record(state="UNKNOWN", filled_quantity=Decimal(0)))
    adapter.state = ConnectionState.DEGRADED
    adapter.query_order.return_value = {
        "clientOrderId": "entry-review", "symbol": "ETHUSDC", "side": "BUY", "origQty": "0.1",
        "executedQty": "0", "status": "NEW", "orderId": "123",
    }
    adapter.cancel_order = AsyncMock(return_value=False)
    assert await adapter._cancel_and_read_back_pilot_entry(repository.row, authority=authority) is None
    assert "entry_cancel=ATTEMPTED_UNKNOWN" in repository.row["state_reason"]
    assert await adapter._cancel_and_read_back_pilot_entry(repository.row, authority=authority) is None
    adapter.cancel_order.assert_awaited_once()
    assert adapter.cancel_order.await_args.kwargs["allow_emergency_fallback"] is True


@pytest.mark.asyncio
async def test_unknown_entry_terminal_readback_closes_without_repeating_cancel():
    adapter, authority, repository = make_adapter(owner_record(
        state="UNKNOWN", filled_quantity=Decimal(0), state_reason="entry_cancel=ATTEMPTED_UNKNOWN",
    ))
    adapter.query_order.return_value = {
        "clientOrderId": "entry-review", "symbol": "ETHUSDC", "side": "BUY", "origQty": "0.1",
        "executedQty": "0", "status": "CANCELED", "orderId": "123",
    }
    adapter.cancel_order = AsyncMock()
    result = await adapter._cancel_and_read_back_pilot_entry(repository.row, authority=authority)
    assert result["state"] == "CLOSED"
    assert "entry_cancel=CONFIRMED" in result["state_reason"]
    adapter.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_fenced_close_marker_prevents_entry_cancel_replay_after_lost_ack():
    adapter, authority, repository = make_adapter(owner_record(
        state="CLOSE_PENDING", filled_quantity=Decimal(0),
        state_reason="local_close_client_order_id=close-review;close_submission=RESERVED",
    ))
    adapter.query_order.return_value = {
        "clientOrderId": "entry-review", "symbol": "ETHUSDC", "side": "BUY", "origQty": "0.1",
        "executedQty": "0", "status": "NEW", "orderId": "123",
    }
    adapter.cancel_order = AsyncMock()
    assert await adapter._cancel_and_read_back_pilot_entry(repository.row, authority=authority) is None
    adapter.cancel_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mark_available", [True, False])
async def test_quick_timeout_closes_despite_unavailable_accounting(mark_available):
    adapter, authority, repository = make_adapter(owner_record(first_fill_at=datetime.now(UTC) - timedelta(hours=25)))
    adapter.read_back_algo_protection = AsyncMock(return_value=SimpleNamespace(protected=True))
    authority._persist_local_live_pilot_mark.return_value = mark_available
    adapter._cancel_and_read_back_pilot_entry = AsyncMock(return_value=repository.row)
    adapter._local_mainnet_close_only_once = AsyncMock(return_value=True)
    result = await adapter.check_and_enforce_pilot_protections(authority)
    assert result["quick_expired_count"] == 1
    assert result["failed_action_count"] >= 1
    adapter._local_mainnet_close_only_once.assert_awaited_once()
    assert adapter._local_mainnet_close_only_once.await_args.kwargs["reason"] == "QUICK_MAX_HOLD_EXPIRED"
    authority.persistence.enter_local_live_pilot_close_only.assert_awaited_once()


def protection_objects(adapter):
    intent = OrderIntent(
        client_order_id="entry-review", symbol="ETHUSDC", basket_id="basket-review",
        market_type=MarketType.USDM_FUTURES, side=OrderSide.BUY, position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET, time_in_force=TimeInForce.GTC, quantity=Decimal("0.1"),
        stop_loss_price=Decimal(90), take_profit_price=Decimal(110),
    )
    order = ExecutionOrder(
        client_order_id=intent.client_order_id, symbol=intent.symbol, side=intent.side,
        quantity=intent.quantity, price=Decimal(100), order_type=OrderType.MARKET,
        status="FILLED", exchange_order_id="123", position_side=PositionSide.BOTH,
    )
    adapter._local_mainnet_close_only_once = AsyncMock(return_value=False)
    return intent, order


def cached_fill(age_seconds):
    return SimpleNamespace(
        client_order_id="entry-review", symbol="ETHUSDC", quantity=Decimal("0.1"),
        price=Decimal(100), event_time=datetime.now(UTC) - timedelta(seconds=age_seconds),
    )


@pytest.mark.asyncio
async def test_old_first_fill_falls_back_before_any_preparation_await():
    adapter, authority, _ = make_adapter()
    intent, order = protection_objects(adapter)
    adapter.ledger.fills = [cached_fill(6)]
    adapter.ledger.get_fills = AsyncMock(return_value=adapter.ledger.fills)
    await adapter._protect_local_mainnet_entry(intent, order, {"executedQty": "0.1"}, authority=authority)
    adapter.ledger.get_fills.assert_not_awaited()
    adapter.reconciliation._recover_order_fills.assert_not_awaited()
    adapter._local_mainnet_close_only_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_remaining_fill_budget_bounds_recovery_and_cancels_stall():
    adapter, authority, _ = make_adapter()
    intent, order = protection_objects(adapter)
    adapter.ledger.fills = [cached_fill(4.8)]
    cancelled = asyncio.Event()
    async def stall(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    adapter.reconciliation._recover_order_fills = stall
    started = time.monotonic()
    await asyncio.wait_for(adapter._protect_local_mainnet_entry(
        intent, order, {"executedQty": "0.1"}, authority=authority,
    ), timeout=1)
    assert time.monotonic() - started < 0.8
    assert cancelled.is_set()
    adapter._local_mainnet_close_only_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_first_fill_timestamp_never_receives_new_five_second_budget():
    adapter, authority, _ = make_adapter()
    intent, order = protection_objects(adapter)
    adapter.ledger.get_fills = AsyncMock()
    await adapter._protect_local_mainnet_entry(intent, order, {"executedQty": "0.1"}, authority=authority)
    adapter.ledger.get_fills.assert_not_awaited()
    adapter._local_mainnet_close_only_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_signed_order_creation_time_bounds_fill_recovery_without_cache():
    adapter, authority, _ = make_adapter()
    intent, order = protection_objects(adapter)
    adapter.ledger.get_fills = AsyncMock()
    await adapter._protect_local_mainnet_entry(intent, order, {
        "executedQty": "0.1", "time": int((time.time() - 6) * 1000),
    }, authority=authority)
    adapter.ledger.get_fills.assert_not_awaited()
    adapter.reconciliation._recover_order_fills.assert_not_awaited()
    adapter._local_mainnet_close_only_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_pre_send_deadline_survives_delayed_response_without_fill_cache():
    adapter, authority, _ = make_adapter()
    intent, order = protection_objects(adapter)
    adapter._local_mainnet_entry_deadlines = {order.client_order_id: time.monotonic() - 1}
    adapter.ledger.get_fills = AsyncMock()
    await adapter._protect_local_mainnet_entry(intent, order, {"executedQty": "0.1"}, authority=authority)
    adapter.ledger.get_fills.assert_not_awaited()
    adapter._local_mainnet_close_only_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_verified_close_never_repeats_algo_cancellation():
    adapter, authority, repository = make_adapter()
    intent, _ = entry_objects(adapter, repository.row)
    close_id = "close-review"
    adapter.query_order.return_value = {
        "clientOrderId": close_id, "symbol": "ETHUSDC", "side": "SELL", "positionSide": "BOTH",
        "reduceOnly": True, "status": "FILLED", "executedQty": "0.1", "origQty": "0.1", "orderId": "124",
    }
    adapter.ledger = SimpleNamespace(
        get_order_by_client_id=AsyncMock(return_value=SimpleNamespace(exchange_order_id="124")),
        get_fills=AsyncMock(return_value=[SimpleNamespace(
            client_order_id=close_id, symbol="ETHUSDC", quantity=Decimal("0.1"), side="SELL", exchange_order_id="124",
        )]),
    )
    adapter.rest_client.request.side_effect = [
        [{"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0"}], [], [],
    ]
    adapter._owned_algos_are_absent = AsyncMock(return_value=True)
    assert await BinanceExecutionAdapter._verify_local_mainnet_close(
        adapter, intent, repository.row, close_id, authority=authority,
    )
    adapter._cancel_local_mainnet_owned_algos.assert_not_awaited()
    adapter._owned_algos_are_absent.assert_awaited_once()


def test_recovery_reason_preserves_attempt_markers_when_diagnostics_are_long():
    result = BinanceExecutionAdapter._local_recovery_reason(
        {"state_reason": "local_close_client_order_id=close-review;close_submission=ATTEMPTED;algo_cancel=ATTEMPTED_UNKNOWN;entry_cancel=ATTEMPTED_UNKNOWN"},
        cause="x" * 400, outcome="UNKNOWN",
    )
    assert len(result) <= 256
    assert "algo_cancel=ATTEMPTED_UNKNOWN" in result
    assert "entry_cancel=ATTEMPTED_UNKNOWN" in result
