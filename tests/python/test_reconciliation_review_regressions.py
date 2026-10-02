"""Offline regressions for restart, delayed fills, and terminal executions."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.reconciliation import (
    BinanceReconciliation,
    FillRecoveryError,
    _exchange_fill_from_trade,
)
from domain.enums import OrderSide
from domain.models import ExecutionOrder


class OfflineRest:
    env = BinanceEnvironment.MAINNET

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def request(self, method, path, **kwargs):
        assert method == "GET" and kwargs["signed"] is True
        self.calls.append((path, kwargs.get("params", {})))
        return self.handler(path, kwargs.get("params", {}))


class DurableHistory:
    """Payload/read-back API double; no database or exchange connections."""

    def __init__(self):
        self.anchor = datetime.now(UTC) - timedelta(minutes=5)
        self.checkpoints = {}
        self.rows = {}
        self.exact_writes = []
        self.fail_kind = None
        self.corrupt_readback = False

    async def begin_mainnet_scan(self, *, symbol, history_kind, now, **kwargs):
        checkpoint = self.checkpoints.setdefault(history_kind, {
            "runtime_target": "LOCAL", "run_id": "review-launch", "symbol": symbol,
            "history_kind": history_kind, "anchor_at": self.anchor, "cursor_id": 0,
            "covered_through": self.anchor,
        })
        checkpoint.update(
            coverage_status="SCANNING",
            scan_from_at=checkpoint["covered_through"] - timedelta(milliseconds=1),
            scan_to_at=now,
        )
        return dict(checkpoint)

    async def persist_history_page(self, *, checkpoint, items, expected_cursor_id, **kwargs):
        for item in items:
            self.rows.setdefault(checkpoint["history_kind"], {})[item["item_id"]] = dict(item)
        cursor = max([expected_cursor_id, *(item["item_id"] for item in items)])
        self.checkpoints[checkpoint["history_kind"]]["cursor_id"] = cursor
        return cursor

    async def complete_history_scan(self, checkpoint):
        current = self.checkpoints[checkpoint["history_kind"]]
        current.update(coverage_status="COVERED", covered_through=current["scan_to_at"])

    async def list_history_items(self, checkpoint):
        return list(self.rows.get(checkpoint["history_kind"], {}).values())

    async def record_algo_history_observation(self, *, checkpoint, item, **kwargs):
        self.rows.setdefault("ALL_ALGO_ORDERS", {})[item["item_id"]] = dict(item)
        return dict(item)

    async def record_history_observation(self, *, checkpoint, item, **kwargs):
        kind = checkpoint["history_kind"]
        assert self.checkpoints[kind]["coverage_status"] == "COVERED"
        if self.fail_kind == kind:
            raise RuntimeError("offline durability failure")
        self.exact_writes.append((kind, dict(item)))
        self.rows.setdefault(kind, {})[item["item_id"]] = dict(item)
        if self.corrupt_readback:
            return dict(item, payload=dict(item["payload"], symbol="BTCUSDT"))
        return dict(item)

    def seed(self, kind, payload):
        fields = {
            "ALL_ALGO_ORDERS": ("algoId", "clientAlgoId", ("createTime",)),
            "ALL_ORDERS": ("orderId", "clientOrderId", ("time",)),
            "USER_TRADES": ("id", "clientOrderId", ("time",)),
        }
        id_field, client_field, time_fields = fields[kind]
        item = BinanceReconciliation._history_item(
            payload, history_kind=kind, id_field=id_field,
            client_field=client_field, time_fields=time_fields,
        )
        self.rows.setdefault(kind, {})[item["item_id"]] = item


class OwnerRepository:
    def __init__(self, owner, history):
        self.owner = owner
        self.history = history

    async def list_protections(self, **kwargs):
        return [dict(self.owner)]

    async def list_active_protections(self, **kwargs):
        return [] if self.owner["state"] == "CLOSED" else [dict(self.owner)]

    async def get_protection(self, *args):
        return dict(self.owner)

    async def set_protection_state(self, *args, reason=None):
        self.owner["state"] = args[-1]
        return dict(self.owner)

    async def close_mainnet_protection_with_proof(self, symbol, client_id, proof):
        # Closure must only happen after exact child and delayed trade writes.
        assert self.history.rows["ALL_ORDERS"][900]["payload"]["status"] == "FILLED"
        assert sum(
            Decimal(item["payload"]["qty"])
            for item in self.history.rows["USER_TRADES"].values()
            if item["payload"]["orderId"] == 900
        ) == Decimal("0.2")
        evidence = {
            **proof, "kind": "BINANCE_ALGO_CLOSE_VERIFIED", "symbol": symbol,
            "entry_client_order_id": client_id, "mainnet_launch_id": "review-launch",
            "basket_id": self.owner["basket_id"],
            "verified_at": proof["verified_at"].isoformat(),
        }
        canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        evidence["proof_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
        self.owner.update(state="CLOSED", closure_evidence=evidence)
        return dict(self.owner)


def trade_row(*, quantity="0.2", order_id=900, trade_id=901, side="SELL", time=None):
    return {
        "id": trade_id, "orderId": order_id, "symbol": "ETHUSDC",
        "side": side, "positionSide": "BOTH", "qty": quantity, "price": "1900",
        "commission": "0.01", "commissionAsset": "USDC", "realizedPnl": "-20",
        "maker": False, "time": time or int(datetime.now(UTC).timestamp() * 1000),
    }


def owner_scenario(*, history_status="NEW", delayed=False, missing_order=False):
    history = DurableHistory()
    event_time = int((history.anchor + timedelta(minutes=1)).timestamp() * 1000)
    algo = {
        "algoId": 11, "clientAlgoId": "stop-11", "symbol": "ETHUSDC",
        "algoType": "CONDITIONAL", "orderType": "STOP_MARKET", "algoStatus": "TRIGGERED",
        "positionSide": "BOTH", "side": "SELL", "workingType": "MARK_PRICE",
        "triggerPrice": "1900", "closePosition": True, "reduceOnly": False,
        "actualOrderId": 900, "createTime": event_time,
    }
    sibling = {
        **algo, "algoId": 12, "clientAlgoId": "target-12", "orderType": "TAKE_PROFIT_MARKET",
        "algoStatus": "CANCELED", "triggerPrice": "2100", "actualOrderId": None,
    }
    child = {
        "symbol": "ETHUSDC", "orderId": 900, "clientOrderId": "child-900", "side": "SELL",
        "positionSide": "BOTH", "status": "FILLED", "origQty": "0.2", "executedQty": "0.2",
        "time": event_time, "updateTime": event_time + 1000,
    }
    fill = trade_row(time=event_time + 1000)
    owner = {
        "environment": "MAINNET", "venue": "binance_mainnet", "symbol": "ETHUSDC",
        "entry_client_order_id": "entry-1", "mainnet_launch_id": "review-launch",
        "basket_id": "basket-1", "entry_side": "BUY", "position_side": "BOTH",
        "requested_quantity": Decimal("0.2"), "filled_quantity": Decimal("0.2"),
        "entry_average_price": Decimal(2000), "stop_trigger_price": Decimal(1900),
        "take_profit_trigger_price": Decimal(2100), "stop_algo_id": "11",
        "take_profit_algo_id": "12", "stop_client_algo_id": "stop-11",
        "take_profit_client_algo_id": "target-12", "state": "CLOSE_PENDING",
    }
    history.seed("ALL_ALGO_ORDERS", algo)
    history.seed("ALL_ALGO_ORDERS", sibling)
    if not missing_order:
        history.seed("ALL_ORDERS", dict(child, status=history_status, executedQty="0"))
    if not delayed:
        history.seed("USER_TRADES", fill)
    # A previous completed scan already passed these creation/execution times.
    for kind in ("ALL_ALGO_ORDERS", "ALL_ORDERS", "USER_TRADES"):
        history.checkpoints[kind] = {
            "runtime_target": "LOCAL", "run_id": "review-launch", "symbol": "ETHUSDC",
            "history_kind": kind, "anchor_at": history.anchor, "cursor_id": 0,
            "covered_through": datetime.now(UTC) - timedelta(seconds=1),
        }
    availability = {"trades": True}

    def handler(path, params):
        if path.endswith("/algoOrder"):
            return dict(algo if str(params["algoId"]) == "11" else sibling)
        if path.endswith("/order"):
            assert str(params["orderId"]) == "900"
            return dict(child)
        if path.endswith("/userTrades") and "orderId" in params:
            return [dict(fill)] if availability["trades"] else []
        if path.endswith(("/allOrders", "/allAlgoOrders", "/userTrades")):
            assert params["startTime"] > fill["time"]
            return []
        raise AssertionError(f"Unexpected offline request {path}")

    rest = OfflineRest(handler)
    ledger = InMemoryLedger()
    owners = OwnerRepository(owner, history)
    reconciliation = BinanceReconciliation(rest, ledger)
    reconciliation.history_repository = history
    reconciliation.algo_protection_repository = owners
    return reconciliation, history, owner, availability


async def audit_owner(reconciliation):
    await reconciliation._audit_mainnet_algo_lifecycle(
        [], exchange_positions=[
            {"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0"},
        ], exchange_open_orders=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("history_status", ["NEW", "PARTIALLY_FILLED"])
async def test_restart_refreshes_stale_child_status_before_closing_owner(history_status):
    reconciliation, history, owner, _ = owner_scenario(history_status=history_status)
    await audit_owner(reconciliation)
    assert owner["state"] == "CLOSED"
    assert history.exact_writes[0][0] == "ALL_ORDERS"
    assert reconciliation._history_diffs == []


@pytest.mark.asyncio
async def test_exact_child_query_recovers_missing_create_time_history():
    reconciliation, history, owner, _ = owner_scenario(missing_order=True)
    await audit_owner(reconciliation)
    assert owner["state"] == "CLOSED"
    assert history.rows["ALL_ORDERS"][900]["payload"]["status"] == "FILLED"


@pytest.mark.asyncio
async def test_delayed_trade_behind_checkpoint_recovers_after_restart():
    reconciliation, history, owner, availability = owner_scenario(delayed=True)
    availability["trades"] = False
    with pytest.raises(FillRecoveryError, match="fills do not equal"):
        await audit_owner(reconciliation)
    assert owner["state"] == "CLOSE_PENDING"
    assert await reconciliation.ledger.get_all_orders() == []
    availability["trades"] = True
    restarted = BinanceReconciliation(reconciliation.rest_client, reconciliation.ledger)
    restarted.history_repository = history
    restarted.algo_protection_repository = reconciliation.algo_protection_repository
    await audit_owner(restarted)
    assert owner["state"] == "CLOSED"
    assert history.rows["USER_TRADES"][901]["payload"]["qty"] == "0.2"
    assert restarted._history_diffs == []
    assert await restarted._recover_recent_trades({"ETHUSDC"}) == []
    fills = await restarted.ledger.get_fills()
    assert [(fill.exchange_trade_id, fill.quantity) for fill in fills] == [("901", Decimal("0.2"))]


@pytest.mark.asyncio
async def test_delayed_trade_recovery_deduplicates_overlap_with_durable_fills():
    reconciliation, history, owner, _ = owner_scenario(delayed=True)
    event_time = int((history.anchor + timedelta(minutes=2)).timestamp() * 1000)
    first = trade_row(quantity="0.1", time=event_time)
    second = trade_row(quantity="0.1", trade_id=902, time=event_time)
    history.seed("USER_TRADES", first)
    original_handler = reconciliation.rest_client.handler

    def handler(path, params):
        if path.endswith("/userTrades") and "orderId" in params:
            return [first, second]
        return original_handler(path, params)

    reconciliation.rest_client.handler = handler
    await audit_owner(reconciliation)
    assert owner["state"] == "CLOSED"
    assert len(history.rows["USER_TRADES"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["ALL_ORDERS", "USER_TRADES"])
async def test_exact_observation_failure_prevents_adoption_and_closure(kind):
    reconciliation, history, owner, _ = owner_scenario(delayed=True)
    history.fail_kind = kind
    with pytest.raises(FillRecoveryError, match="read-back failed"):
        await audit_owner(reconciliation)
    assert owner["state"] == "CLOSE_PENDING"
    assert await reconciliation.ledger.get_all_orders() == []


@pytest.mark.asyncio
async def test_corrupt_exact_child_readback_prevents_closure():
    reconciliation, history, owner, _ = owner_scenario()
    history.corrupt_readback = True
    with pytest.raises(FillRecoveryError, match="read-back failed"):
        await audit_owner(reconciliation)
    assert owner["state"] == "CLOSE_PENDING"
    assert await reconciliation.ledger.get_all_orders() == []


@pytest.mark.asyncio
async def test_missing_persistence_api_fails_closed():
    reconciliation, history, owner, _ = owner_scenario()
    history.record_history_observation = None
    with pytest.raises(FillRecoveryError, match="storage is unavailable"):
        await audit_owner(reconciliation)
    assert owner["state"] == "CLOSE_PENDING"


async def terminal_scenario(
    status, *, local_status="NEW", quantity="0.1", trades=None, initial_fill=None,
):
    ledger = InMemoryLedger()
    order = ExecutionOrder(
        symbol="ETHUSDC", side=OrderSide.BUY, quantity=Decimal("0.2"),
        price=Decimal(2000), client_order_id="terminal-900", exchange_order_id="900",
        status=local_status,
    )
    await ledger.upsert_order(order)
    if initial_fill is not None:
        await ledger.append_fill(_exchange_fill_from_trade(
            initial_fill, order, source="BINANCE_MAINNET_RECOVERY",
        ))
    raw = {
        "symbol": "ETHUSDC", "orderId": 900, "clientOrderId": order.client_order_id,
        "side": "BUY", "positionSide": "BOTH", "status": status,
        "origQty": "0.2", "executedQty": quantity,
    }
    page = trades if trades is not None else [trade_row(quantity=quantity, side="BUY")]

    def handler(path, params):
        if path.endswith("/order"):
            return raw
        if path.endswith("/userTrades"):
            return page
        raise AssertionError(f"Unexpected offline request {path}")

    reconciliation = BinanceReconciliation(OfflineRest(handler), ledger)
    diffs = await reconciliation._collect_diffs([], [])
    return reconciliation, order, diffs


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["CANCELED", "CANCELLED", "EXPIRED", "REJECTED"])
@pytest.mark.parametrize("local_status", ["NEW", "PARTIALLY_FILLED", "CANCELED"])
async def test_terminal_execution_recovers_canonical_fills(status, local_status):
    reconciliation, order, diffs = await terminal_scenario(status, local_status=local_status)
    assert diffs == []
    assert order.status == status
    assert sum(fill.quantity for fill in await reconciliation.ledger.get_fills()) == Decimal("0.1")


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", ["0.05", "0.15"])
async def test_terminal_execution_rejects_incomplete_or_excess_fills(quantity):
    reconciliation, _, diffs = await terminal_scenario(
        "CANCELED", trades=[trade_row(quantity=quantity, side="BUY")],
    )
    assert "FILL_RECOVERY_FAILED" in {diff.code for diff in diffs}
    assert await reconciliation.ledger.get_fills() == []


@pytest.mark.asyncio
async def test_terminal_execution_missing_delayed_fill_cannot_pass():
    _, _, diffs = await terminal_scenario("EXPIRED", trades=[])
    assert "FILL_RECOVERY_FAILED" in {diff.code for diff in diffs}


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", [None, "NaN", "-0.1", "0.3"])
async def test_terminal_invalid_executed_quantity_cannot_pass(quantity):
    _, _, diffs = await terminal_scenario("CANCELED", quantity=quantity, trades=[])
    assert "FILL_RECOVERY_FAILED" in {diff.code for diff in diffs}


@pytest.mark.asyncio
async def test_unfilled_terminal_order_needs_no_trade_request():
    reconciliation, _, diffs = await terminal_scenario("CANCELED", quantity="0", trades=[])
    assert diffs == []
    assert not any(path.endswith("/userTrades") for path, _ in reconciliation.rest_client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity", ["0", "0.1"])
async def test_terminal_canonical_ledger_totals_must_equal_execution(quantity):
    _, _, diffs = await terminal_scenario(
        "CANCELED", local_status="CANCELED", quantity=quantity,
        initial_fill=trade_row(quantity="0.15", side="BUY"),
        trades=[] if quantity == "0" else None,
    )
    assert "FILL_QUANTITY_MISMATCH" in {diff.code for diff in diffs}


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [
    [trade_row(order_id=999, side="BUY")],
    [trade_row(side="BUY"), trade_row(side="BUY")],
    [trade_row(side="BUY")] * 1000,
])
async def test_exact_order_recovery_rejects_foreign_duplicate_or_saturated_page(page):
    reconciliation, _, diffs = await terminal_scenario("CANCELED", trades=page)
    assert "FILL_RECOVERY_FAILED" in {diff.code for diff in diffs}
    assert await reconciliation.ledger.get_fills() == []
