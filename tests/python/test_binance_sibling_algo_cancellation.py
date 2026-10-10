"""Tests for sibling algo cancellation after exit in Binance reconciliation (WP6 / Gap B)."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List

import pytest

from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.reconciliation import BinanceReconciliation
from tests.python.test_testnet_safety import MemoryHistoryRepository, ScriptedRest


def _make_owner(history: MemoryHistoryRepository) -> Dict[str, Any]:
    return {
        "environment": "MAINNET",
        "venue": "binance_mainnet",
        "symbol": "ETHUSDC",
        "entry_client_order_id": "entry-wp6",
        "mainnet_launch_id": history.run_id,
        "basket_id": "basket-wp6",
        "entry_side": "BUY",
        "position_side": "BOTH",
        "requested_quantity": Decimal("0.2"),
        "filled_quantity": Decimal("0.2"),
        "entry_average_price": Decimal("2000"),
        "stop_trigger_price": Decimal("1900"),
        "take_profit_trigger_price": Decimal("2100"),
        "stop_algo_id": "11",
        "take_profit_algo_id": "12",
        "stop_client_algo_id": "stop-11",
        "take_profit_client_algo_id": "target-12",
        "state": "PROTECTED",
    }


class OwnerRepository:
    def __init__(self, owner: Dict[str, Any]):
        self.owner = owner

    async def list_protections(self, *, venue):
        assert venue == "binance_mainnet"
        return [dict(self.owner)]

    async def list_active_protections(self, *, venue):
        return [dict(self.owner)] if self.owner["state"] != "CLOSED" else []

    async def get_protection(self, venue, symbol, client_id):
        assert (venue, symbol, client_id) == ("binance_mainnet", "ETHUSDC", "entry-wp6")
        return dict(self.owner)

    async def set_protection_state(self, venue, symbol, client_id, state, reason=None):
        assert (venue, symbol, client_id) == ("binance_mainnet", "ETHUSDC", "entry-wp6")
        self.owner["state"] = state
        self.owner["state_reason"] = reason
        return dict(self.owner)

    async def close_mainnet_protection_with_proof(self, symbol, client_id, proof):
        assert (symbol, client_id) == ("ETHUSDC", "entry-wp6")
        evidence = {
            "kind": "BINANCE_ALGO_CLOSE_VERIFIED",
            "symbol": self.owner["symbol"],
            "entry_client_order_id": self.owner["entry_client_order_id"],
            "mainnet_launch_id": self.owner["mainnet_launch_id"],
            "basket_id": self.owner["basket_id"],
            "algo_id": str(proof["algo_id"]),
            "order_id": str(proof["order_id"]),
            "client_order_id": str(proof["client_order_id"]),
            "order_status": str(proof["order_status"]),
            "executed_quantity": str(proof["executed_quantity"]),
            "trade_quantity": str(proof["trade_quantity"]),
            "owner_filled_quantity": str(self.owner["filled_quantity"]),
            "position_quantity": str(proof["position_quantity"]),
            "open_child_order_ids": list(proof["open_child_order_ids"]),
            "open_owner_algo_ids": list(proof["open_owner_algo_ids"]),
            "verified_at": proof["verified_at"].isoformat(),
        }
        canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
        evidence["proof_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.owner["closure_evidence"] = evidence
        self.owner["state"] = "CLOSED"
        self.owner["state_reason"] = f"{proof['order_status']} child fill verified"
        return dict(self.owner)


@pytest.mark.asyncio
@pytest.mark.parametrize("portfolio_margin", [False, True])
async def test_mainnet_triggered_algo_cancels_open_sibling_and_reaches_in_sync(portfolio_margin):
    """When an algo triggers and fills flat, reconciliation must cancel the sibling algo and reach IN_SYNC."""
    history = MemoryHistoryRepository()
    owner = _make_owner(history)
    owner_repo = OwnerRepository(owner)

    algo_order_path = "/papi/v1/um/algo/order" if portfolio_margin else "/fapi/v1/algoOrder"
    algo_query_path = "/papi/v1/um/algo/algoOrder" if portfolio_margin else "/fapi/v1/algoOrder"
    open_algos_path = "/papi/v1/um/algo/openAlgoOrders" if portfolio_margin else "/fapi/v1/openAlgoOrders"
    all_algos_path = "/papi/v1/um/algo/allAlgoOrders" if portfolio_margin else "/fapi/v1/allAlgoOrders"
    order_path = "/papi/v1/um/order" if portfolio_margin else "/fapi/v1/order"
    all_orders_path = "/papi/v1/um/allOrders" if portfolio_margin else "/fapi/v1/allOrders"
    user_trades_path = "/papi/v1/um/userTrades" if portfolio_margin else "/fapi/v1/userTrades"

    triggered = {
        "algoId": 11,
        "clientAlgoId": "stop-11",
        "symbol": "ETHUSDC",
        "algoType": "CONDITIONAL",
        "orderType": "STOP_MARKET",
        "algoStatus": "TRIGGERED",
        "positionSide": "BOTH",
        "side": "SELL",
        "workingType": "MARK_PRICE",
        "triggerPrice": "1900",
        "closePosition": False,
        "reduceOnly": True,
        "quantity": "0.2",
        "actualOrderId": 900,
        "createTime": int((history.anchor_at + timedelta(minutes=1)).timestamp() * 1000),
    }
    sibling = {
        "algoId": 12,
        "clientAlgoId": "target-12",
        "symbol": "ETHUSDC",
        "algoType": "CONDITIONAL",
        "orderType": "TAKE_PROFIT_MARKET",
        "algoStatus": "NEW",  # Still open on exchange!
        "positionSide": "BOTH",
        "side": "SELL",
        "workingType": "MARK_PRICE",
        "triggerPrice": "2100",
        "closePosition": False,
        "reduceOnly": True,
        "quantity": "0.2",
        "actualOrderId": None,
        "createTime": int((history.anchor_at + timedelta(minutes=1)).timestamp() * 1000),
    }
    child_order = {
        "symbol": "ETHUSDC",
        "orderId": 900,
        "clientOrderId": "algo-child-900",
        "side": "SELL",
        "positionSide": "BOTH",
        "status": "FILLED",
        "origQty": "0.2",
        "executedQty": "0.2",
        "time": int((history.anchor_at + timedelta(minutes=2)).timestamp() * 1000),
        "updateTime": int((history.anchor_at + timedelta(minutes=2)).timestamp() * 1000),
    }
    child_trade = {
        "id": 901,
        "orderId": 900,
        "clientOrderId": "algo-child-900",
        "symbol": "ETHUSDC",
        "side": "SELL",
        "positionSide": "BOTH",
        "qty": "0.2",
        "price": "1900",
        "commission": "0.01",
        "commissionAsset": "USDC",
        "realizedPnl": "-20",
        "time": child_order["time"],
    }

    deleted_algos = []
    sibling_canceled = False

    async def handler(method, path, kwargs):
        nonlocal sibling_canceled
        assert kwargs.get("signed") is True

        if method == "DELETE" and path == algo_order_path:
            algo_id = kwargs.get("params", {}).get("algoId")
            deleted_algos.append(algo_id)
            if algo_id in (12, "12"):
                sibling_canceled = True
            return {"code": 200, "msg": "success"}

        if method == "GET":
            if path == algo_query_path:
                algo_id = kwargs.get("params", {}).get("algoId")
                client_id = kwargs.get("params", {}).get("clientAlgoId")
                if algo_id == "11" or client_id == "stop-11":
                    return dict(triggered)
                if algo_id == "12" or client_id == "target-12":
                    s = dict(sibling)
                    if sibling_canceled:
                        s["algoStatus"] = "CANCELED"
                    return s
                raise AssertionError(f"Unknown query params: {kwargs}")

            if path == open_algos_path:
                if sibling_canceled:
                    return []
                return [dict(sibling)]

            if path == all_algos_path:
                return [dict(triggered), dict(sibling)]

            if path == all_orders_path:
                return [dict(child_order)]

            if path == user_trades_path:
                return [dict(child_trade)]

            if path == order_path:
                return dict(child_order)

        raise AssertionError(f"Unexpected endpoint: {method} {path} kwargs={kwargs}")

    rest = ScriptedRest(handler)
    rest.env = BinanceEnvironment.MAINNET
    rest.portfolio_margin = portfolio_margin
    reconciliation = BinanceReconciliation(rest, InMemoryLedger())
    reconciliation.history_repository = history
    reconciliation.algo_protection_repository = owner_repo

    await reconciliation._audit_mainnet_algo_lifecycle(
        [dict(sibling)],  # Sibling algo is initially open
        exchange_positions=[
            {"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0"}
        ],
        exchange_open_orders=[],
    )

    # 1. Sibling algo was deleted via the correct path
    assert 12 in deleted_algos or "12" in deleted_algos
    # 2. Owner transitioned to CLOSED
    assert owner["state"] == "CLOSED"
    assert "FILLED" in owner["state_reason"]
    # 3. Reconciliation reached IN_SYNC with zero history diffs
    assert reconciliation._history_diffs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("portfolio_margin", [False, True])
async def test_mainnet_triggered_algo_fails_closed_when_sibling_cancellation_fails(portfolio_margin):
    """When sibling cancellation fails or sibling remains open, owner remains CLOSE_PENDING with diff."""
    history = MemoryHistoryRepository()
    owner = _make_owner(history)
    owner_repo = OwnerRepository(owner)

    algo_order_path = "/papi/v1/um/algo/order" if portfolio_margin else "/fapi/v1/algoOrder"
    algo_query_path = "/papi/v1/um/algo/algoOrder" if portfolio_margin else "/fapi/v1/algoOrder"
    open_algos_path = "/papi/v1/um/algo/openAlgoOrders" if portfolio_margin else "/fapi/v1/openAlgoOrders"
    all_algos_path = "/papi/v1/um/algo/allAlgoOrders" if portfolio_margin else "/fapi/v1/allAlgoOrders"
    order_path = "/papi/v1/um/order" if portfolio_margin else "/fapi/v1/order"
    all_orders_path = "/papi/v1/um/allOrders" if portfolio_margin else "/fapi/v1/allOrders"
    user_trades_path = "/papi/v1/um/userTrades" if portfolio_margin else "/fapi/v1/userTrades"

    triggered = {
        "algoId": 11,
        "clientAlgoId": "stop-11",
        "symbol": "ETHUSDC",
        "algoType": "CONDITIONAL",
        "orderType": "STOP_MARKET",
        "algoStatus": "TRIGGERED",
        "positionSide": "BOTH",
        "side": "SELL",
        "workingType": "MARK_PRICE",
        "triggerPrice": "1900",
        "closePosition": False,
        "reduceOnly": True,
        "quantity": "0.2",
        "actualOrderId": 900,
        "createTime": int((history.anchor_at + timedelta(minutes=1)).timestamp() * 1000),
    }
    sibling = {
        "algoId": 12,
        "clientAlgoId": "target-12",
        "symbol": "ETHUSDC",
        "algoType": "CONDITIONAL",
        "orderType": "TAKE_PROFIT_MARKET",
        "algoStatus": "NEW",  # Still open on exchange!
        "positionSide": "BOTH",
        "side": "SELL",
        "workingType": "MARK_PRICE",
        "triggerPrice": "2100",
        "closePosition": False,
        "reduceOnly": True,
        "quantity": "0.2",
        "actualOrderId": None,
        "createTime": int((history.anchor_at + timedelta(minutes=1)).timestamp() * 1000),
    }
    child_order = {
        "symbol": "ETHUSDC",
        "orderId": 900,
        "clientOrderId": "algo-child-900",
        "side": "SELL",
        "positionSide": "BOTH",
        "status": "FILLED",
        "origQty": "0.2",
        "executedQty": "0.2",
        "time": int((history.anchor_at + timedelta(minutes=2)).timestamp() * 1000),
        "updateTime": int((history.anchor_at + timedelta(minutes=2)).timestamp() * 1000),
    }
    child_trade = {
        "id": 901,
        "orderId": 900,
        "clientOrderId": "algo-child-900",
        "symbol": "ETHUSDC",
        "side": "SELL",
        "positionSide": "BOTH",
        "qty": "0.2",
        "price": "1900",
        "commission": "0.01",
        "commissionAsset": "USDC",
        "realizedPnl": "-20",
        "time": child_order["time"],
    }

    async def handler(method, path, kwargs):
        assert kwargs.get("signed") is True

        if method == "DELETE" and path == algo_order_path:
            # Simulate rejection or outage
            raise RuntimeError("Binance 500 internal server error")

        if method == "GET":
            if path == algo_query_path:
                algo_id = kwargs.get("params", {}).get("algoId")
                client_id = kwargs.get("params", {}).get("clientAlgoId")
                if algo_id == "11" or client_id == "stop-11":
                    return dict(triggered)
                if algo_id == "12" or client_id == "target-12":
                    return dict(sibling)  # Sibling remains NEW
                raise AssertionError(f"Unknown query params: {kwargs}")

            if path == open_algos_path:
                return [dict(sibling)]

            if path == all_algos_path:
                return [dict(triggered), dict(sibling)]

            if path == all_orders_path:
                return [dict(child_order)]

            if path == user_trades_path:
                return [dict(child_trade)]

            if path == order_path:
                return dict(child_order)

        raise AssertionError(f"Unexpected endpoint: {method} {path}")

    rest = ScriptedRest(handler)
    rest.env = BinanceEnvironment.MAINNET
    rest.portfolio_margin = portfolio_margin
    reconciliation = BinanceReconciliation(rest, InMemoryLedger())
    reconciliation.history_repository = history
    reconciliation.algo_protection_repository = owner_repo

    await reconciliation._audit_mainnet_algo_lifecycle(
        [dict(sibling)],
        exchange_positions=[
            {"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0"}
        ],
        exchange_open_orders=[],
    )

    # Owner must not transition to CLOSED
    assert owner["state"] == "CLOSE_PENDING"
    # Diff recorded
    assert len(reconciliation._history_diffs) == 1
    assert reconciliation._history_diffs[0].code == "ALGO_TRIGGER_REMAINS_CLOSE_ONLY"
