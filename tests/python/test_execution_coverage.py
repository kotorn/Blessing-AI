"""Coverage tests for BinanceExecutionAdapter submission / cancel / flatten paths.

Every async test drives the adapter's internal authorized path
(``adapter.bind_worker_authority(authority)`` followed by
``await adapter._execute_decision(decision, authority=authority)``) so the
exchange-facing fail-closed branches are exercised exactly like the real
Worker calls them.  All fakes (REST / stream / reconciliation / ledger) are
defined in this single module; no production code is substituted except the
adapter's own injectable seams (rest_client, user_stream, reconciliation,
order_gate.check, before_order_submission, on_order_submission_result).

Covers the previously untested branches:
- definitive POST rejection persistence (execution.py:1455-1475)
- identity-mismatch branches of _order_from_response (:1000-1041) plus the
  no-resubmit ambiguous recovery (:1481-1499)
- terminal-status -> BinanceDefinitiveRejection conversion (:1056-1065)
- generic-exception poll branch and order_status_known=False recovery guard
  (:1182-1186, :1193-1206)
- adapter-level rate-limit handling (:1448-1454)
- durable-outbox barrier False branches (:1391-1402)
- mainnet deterministic client ids and no-barrier LeaseLostError
  (:1323-1331, :1384-1388, :976-981)
- failing on_order_submission_result callback (:302-325)
- real cancel_all_open_orders loop (:1546-1599), local cancel guards
  (:1626-1632, :1647-1648), failed cancel-status resolution (:1937-1954)
- amendment terminal-order / side-change guards (:1774-1789)
- emergency flatten authority + auth failure + flat-account paths
  (:1969-1975, :2006-2012, :2092-2118)
- fill-recovery auth failure inside ambiguous FILLED recovery (:1148-1155)
"""

import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.trading_worker.execution_lease import InMemoryExecutionLease
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.models import (
    BinanceAuthenticationError,
    BinanceDefinitiveRejection,
    BinanceRateLimitError,
    BinanceTransportAmbiguity,
    ConnectionState,
    ExchangeAccountSnapshot,
)
from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules
from domain.enums import (
    EconomicRiskClass,
    MarketType,
    OrderSide,
    OrderType,
    PositionSide,
    TimeInForce,
)
from domain.models import (
    ExchangePosition,
    ExecutionDecision,
    ExecutionOrder,
    OrderIntent,
    utc_now,
)


class FakeStream:
    def __init__(self, connected: bool = True):
        self.is_connected = connected

    async def close(self):
        self.is_connected = False


class FakeReconciliation:
    def __init__(self, status: str = "IN_SYNC"):
        self.last_status = status
        self.next_status = status
        self.calls = 0

    async def reconcile(self):
        self.calls += 1
        self.last_status = self.next_status
        return self.last_status

    async def _recover_order_fills(self, order, response):
        return None


class ScriptedRest:
    """Records every (method, path, kwargs) call, then dispatches to handler."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return await self.handler(method, path, kwargs)

    async def close(self):
        return None


def make_rules(symbol: str = "BTCUSDT") -> SymbolTradingRules:
    rules = SymbolTradingRules(symbol)
    rules.status = "TRADING"
    rules.supported_order_types = ["LIMIT", "MARKET"]
    rules.tick_size = Decimal("0.1")
    rules.step_size = Decimal("0.001")
    rules.min_qty = Decimal("0.001")
    rules.max_qty = Decimal(100)
    rules.market_step_size = Decimal("0.001")
    rules.market_min_qty = Decimal("0.001")
    rules.market_max_qty = Decimal(100)
    rules.min_notional = Decimal(5)
    return rules


def make_snapshot() -> ExchangeAccountSnapshot:
    return ExchangeAccountSnapshot(
        wallet_balance=Decimal(100),
        margin_balance=Decimal(100),
        available_balance=Decimal(90),
        unrealized_pnl=Decimal(0),
        total_initial_margin=Decimal(10),
        total_maint_margin=Decimal(5),
        position_initial_margin=Decimal(10),
        total_position_notional=Decimal(0),
        effective_leverage=Decimal(0),
        margin_utilization_pct=Decimal(10),
        min_liquidation_distance_pct=None,
        liquidation_safety="KNOWN",
        exchange_environment="BINANCE_TESTNET",
        valid=True,
        timestamp=utc_now(),
    )


async def make_adapter(rest=None):
    """READY testnet adapter with the proven authenticated/trading setup."""
    ledger = InMemoryLedger()
    adapter = BinanceExecutionAdapter(
        api_key="unit-test-key",
        api_secret="unit-test-secret",
        env=BinanceEnvironment.TESTNET,
        ledger=ledger,
    )
    adapter.state = ConnectionState.READY
    adapter.capabilities.account_request_succeeded = True
    adapter.capabilities.authenticated = True
    adapter.capabilities.trade_authorized = True
    adapter.capabilities.hedge_mode = False
    adapter.capabilities.symbol_rules["BTCUSDT"] = make_rules()
    adapter.user_stream = FakeStream()
    adapter.reconciliation = FakeReconciliation()
    if rest is not None:
        adapter.rest_client = rest
    adapter.last_market_event_at["BTCUSDT"] = utc_now()
    adapter.last_market_event_source["BTCUSDT"] = "BINANCE_TESTNET_WS"
    adapter.last_market_event_venue["BTCUSDT"] = "BINANCE_TESTNET"
    adapter.last_market_event_market_type["BTCUSDT"] = MarketType.USDM_FUTURES.value
    await ledger.set_account_snapshot(make_snapshot())
    return adapter


def make_mainnet_adapter():
    """READY mainnet adapter (requires MAINNET_LIVE_APPROVED set by the caller)."""
    adapter = BinanceExecutionAdapter(
        api_key="unit-test-mainnet-key",
        api_secret="unit-test-mainnet-secret",
        env=BinanceEnvironment.MAINNET,
        ledger=InMemoryLedger(),
    )
    adapter.state = ConnectionState.READY
    adapter.capabilities.account_request_succeeded = True
    adapter.capabilities.authenticated = True
    adapter.capabilities.trade_authorized = True
    adapter.capabilities.hedge_mode = False
    adapter.user_stream = FakeStream()
    adapter.reconciliation = FakeReconciliation()
    return adapter


async def execute_internal(adapter, decision):
    """Exercise the adapter's private path with an explicit test authority."""
    authority = object()
    adapter.bind_worker_authority(authority)
    return await adapter._execute_decision(decision, authority=authority)


def attach_recorder(adapter, notifications):
    async def record(order, outcome):
        notifications.append((order.client_order_id, outcome))

    adapter.on_order_submission_result = record


def make_limit_intent(
    *,
    client_id: str = "UNIT-1",
    symbol: str = "BTCUSDT",
    quantity: str = "0.001",
    price: str = "10000",
    reduce_only: bool = False,
):
    return OrderIntent(
        client_order_id=client_id,
        symbol=symbol,
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal(quantity),
        price=Decimal(price),
        reduce_only=reduce_only,
    )


def make_decision(risk_class, *intents, decision_id="UNIT-DECISION", source_intent_ids=None):
    return ExecutionDecision(
        decision_id=decision_id,
        symbol="BTCUSDT",
        action="SUBMIT_ORDER",
        risk_class=risk_class,
        orders=list(intents),
        source_intent_ids=list(source_intent_ids or []),
    )


def allow_order_stub(symbol: str, quantity: Decimal, price: Decimal):
    """Proven stub for adapter.order_gate.check (test_execution_lease.py pattern)."""

    async def allow_order(*args, **kwargs):
        return SimpleNamespace(
            allowed=True,
            reason="unit-test",
            prepared=SimpleNamespace(
                symbol=symbol,
                order_type="LIMIT",
                quantity=quantity,
                price=price,
                estimated_price=price,
                notional=quantity * price,
            ),
        )

    return allow_order


def order_ack(params, **overrides):
    """Successful Binance NEW-order acknowledgement echoing submitted params."""
    payload = {
        "orderId": 101,
        "clientOrderId": params["newClientOrderId"],
        "status": "NEW",
        "symbol": params["symbol"],
        "side": params["side"],
        "positionSide": "BOTH",
        "origQty": params["quantity"],
        "price": params.get("price", "0"),
        "type": params.get("type", "LIMIT"),
    }
    payload.update(overrides)
    return payload


def rest_methods(adapter):
    return [method for method, _, _ in adapter.rest_client.calls]


# ---------------------------------------------------------------------------
# EX-1: definitive POST rejection is persisted, notified once, never re-queried
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_client_order_id_submission_is_persisted_as_rejected():
    async def handler(method, path, kwargs):
        if method == "POST" and path == "/fapi/v1/order":
            raise BinanceDefinitiveRejection(-2010, "Duplicate order sent")
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    notifications = []
    attach_recorder(adapter, notifications)
    intent = make_limit_intent(client_id="UNIT-1")
    decision = make_decision(
        EconomicRiskClass.NEW_RISK, intent, source_intent_ids=["INT-EX1"]
    )

    executed = await execute_internal(adapter, decision)

    assert executed == []
    record = await adapter.ledger.get_order_by_client_id("UNIT-1")
    assert record is not None
    assert record.status == "REJECTED"
    assert record.decision_id == "UNIT-DECISION"
    assert record.source_intent_ids == ["INT-EX1"]
    assert notifications == [("UNIT-1", "REJECTED")]
    # A definitive rejection is never retried nor re-queried: exactly one POST,
    # zero GETs.
    assert rest_methods(adapter) == ["POST"]
    assert adapter.state == ConnectionState.READY


# ---------------------------------------------------------------------------
# EX-2: identity-mismatched acknowledgement is ambiguous, resolved by query,
# never blindly resubmitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_response_identity_mismatch_is_ambiguous_and_resolved_not_resubmitted():
    async def handler(method, path, kwargs):
        if method == "POST" and path == "/fapi/v1/order":
            # symbol does NOT match the prepared BTCUSDT intent
            return {"orderId": 1, "status": "NEW", "symbol": "ETHUSDT", "origQty": "0.001"}
        if method == "GET" and path == "/fapi/v1/order":
            return {
                "orderId": 555,
                "status": "NEW",
                "symbol": "BTCUSDT",
                "clientOrderId": "UNIT-1",
                "side": "BUY",
                "positionSide": "BOTH",
                "origQty": "0.001",
                "price": "10000",
            }
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    notifications = []
    attach_recorder(adapter, notifications)
    decision = make_decision(
        EconomicRiskClass.NEW_RISK, make_limit_intent(client_id="UNIT-1")
    )

    executed = await execute_internal(adapter, decision)

    assert len(executed) == 1
    assert executed[0].exchange_order_id == "555"
    assert executed[0].client_order_id == "UNIT-1"
    # The mismatched acknowledgement is neither accepted nor resubmitted.
    assert rest_methods(adapter).count("POST") == 1
    assert notifications == [("UNIT-1", "AMBIGUOUS"), ("UNIT-1", "CONFIRMED")]
    assert adapter.state == ConnectionState.READY


# ---------------------------------------------------------------------------
# EX-3: terminal status in a REST ack converts to a definitive rejection
# ---------------------------------------------------------------------------


def test_terminal_status_ack_becomes_definitive_rejection():
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET)
    intent = make_limit_intent(client_id="UNIT-1")
    prepared = SimpleNamespace(
        symbol="BTCUSDT",
        order_type="LIMIT",
        quantity=Decimal("0.001"),
        price=Decimal(10000),
        estimated_price=Decimal(10000),
    )

    with pytest.raises(BinanceDefinitiveRejection) as excinfo:
        adapter._order_from_response(
            intent,
            {
                "orderId": 9,
                "status": "EXPIRED",
                "symbol": "BTCUSDT",
                "clientOrderId": "UNIT-1",
                "side": "BUY",
                "positionSide": "BOTH",
                "origQty": "0.001",
                "price": "10000",
            },
            prepared,
            "UNIT-1",
        )

    assert excinfo.value.code == -2010
    assert "terminal order status EXPIRED" in str(excinfo.value)


# ---------------------------------------------------------------------------
# EX-4: status query that fails with a generic exception keeps the adapter
# degraded even though reconciliation reports IN_SYNC
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ambiguous_resolution_query_failure_keeps_adapter_degraded():
    async def handler(method, path, kwargs):
        if method == "POST" and path == "/fapi/v1/order":
            raise BinanceTransportAmbiguity("timeout")
        if method == "GET" and path == "/fapi/v1/order":
            raise RuntimeError("rest unavailable")
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    notifications = []
    attach_recorder(adapter, notifications)
    decision = make_decision(
        EconomicRiskClass.NEW_RISK, make_limit_intent(client_id="UNIT-1")
    )

    executed = await execute_internal(adapter, decision)

    assert executed == []
    # The generic-exception branch of the resolution poll (execution.py:1182-1186)
    # abandons the (0.0, 0.1, 0.25) schedule after the first failed attempt and
    # the order is never resubmitted: one POST, one GET.
    assert rest_methods(adapter) == ["POST", "GET"]
    # order_status_known False must defeat the IN_SYNC reconciliation verdict.
    assert adapter.state == ConnectionState.DEGRADED
    assert notifications == [("UNIT-1", "AMBIGUOUS")]
    assert adapter.reconciliation.calls == 1


# ---------------------------------------------------------------------------
# EX-5: rate-limited POST is not retried and degrades the adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_post_is_not_retried_and_degrades_adapter():
    async def handler(method, path, kwargs):
        if method == "POST" and path == "/fapi/v1/order":
            raise BinanceRateLimitError(-1008, "server busy")
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    notifications = []
    attach_recorder(adapter, notifications)
    decision = make_decision(
        EconomicRiskClass.NEW_RISK, make_limit_intent(client_id="UNIT-1")
    )

    executed = await execute_internal(adapter, decision)

    assert executed == []
    # No retry, no status query, and no reconcile: exactly one POST.
    assert rest_methods(adapter) == ["POST"]
    assert adapter.reconciliation.calls == 0
    assert adapter.state == ConnectionState.DEGRADED
    # submission_attempted was True, so the notification is AMBIGUOUS.
    assert notifications == [("UNIT-1", "AMBIGUOUS")]


# ---------------------------------------------------------------------------
# EX-6: risk-increasing order blocked when the durable outbox is not
# acknowledged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_increasing_order_blocked_when_durable_outbox_not_acknowledged():
    async def handler(method, path, kwargs):
        raise AssertionError(f"No REST mutation may be issued: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))

    async def deny_barrier(order):
        return False

    adapter.before_order_submission = deny_barrier
    notifications = []
    attach_recorder(adapter, notifications)
    decision = make_decision(
        EconomicRiskClass.NEW_RISK, make_limit_intent(client_id="UNIT-1")
    )

    executed = await execute_internal(adapter, decision)

    assert executed == []
    assert adapter.rest_client.calls == []
    # DEGRADED is set only by the barrier-False branch, proving it ran.
    assert adapter.state == ConnectionState.DEGRADED
    # Blocked before submission was attempted: nothing to notify.
    assert notifications == []
    # The barrier is evaluated before the pending order is journaled
    # (execution.py:1390-1407), so the rejected plan never reaches the ledger.
    assert await adapter.ledger.get_order_by_client_id("UNIT-1") is None


# ---------------------------------------------------------------------------
# EX-7: risk-reducing order proceeds when the durable outbox is unavailable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_risk_reducing_order_proceeds_when_durable_outbox_unavailable():
    async def handler(method, path, kwargs):
        if method == "POST" and path == "/fapi/v1/order":
            return order_ack(kwargs["params"], orderId=88, reduceOnly=True)
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    await adapter.ledger.upsert_position(
        ExchangePosition(
            symbol="BTCUSDT",
            position_side=PositionSide.BOTH,
            quantity=Decimal("-0.001"),
            entry_price=Decimal(10000),
            mark_price=Decimal(10000),
        )
    )

    async def deny_barrier(order):
        return False

    adapter.before_order_submission = deny_barrier
    notifications = []
    attach_recorder(adapter, notifications)
    intent = make_limit_intent(client_id="UNIT-CLOSE", reduce_only=True)
    decision = make_decision(EconomicRiskClass.CLOSE, intent)

    executed = await execute_internal(adapter, decision)

    # The reduction is NOT blocked by outbox unavailability.
    assert len(executed) == 1
    assert rest_methods(adapter).count("POST") == 1
    assert notifications == [("UNIT-CLOSE", "CONFIRMED")]


# ---------------------------------------------------------------------------
# EX-8: mainnet risk-increasing order without a durable outbox barrier raises
# LeaseLostError before any request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mainnet_risk_increasing_order_requires_durable_outbox_barrier(
    monkeypatch, caplog
):
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")

    async def unreachable(method, path, kwargs):
        raise AssertionError(f"No REST call may be issued: {method} {path}")

    adapter = make_mainnet_adapter()
    adapter.rest_client = ScriptedRest(unreachable)
    adapter.order_gate.check = allow_order_stub("ETHUSDC", Decimal("0.05"), Decimal(100))
    notifications = []
    attach_recorder(adapter, notifications)
    intent = make_limit_intent(
        client_id="MAINNET-INTENT-1", symbol="ETHUSDC", quantity="0.05", price="100"
    )
    decision = make_decision(
        EconomicRiskClass.NEW_RISK, intent, decision_id="DEC-MAINNET-BARRIER"
    )

    with caplog.at_level(logging.ERROR, logger="blessing.venues.binance.execution"):
        executed = await execute_internal(adapter, decision)

    assert executed == []
    assert adapter.rest_client.calls == []
    assert adapter.state == ConnectionState.DEGRADED
    expected_cid = adapter._generate_client_order_id("DEC-MAINNET-BARRIER", "ETHUSDC", 0)
    assert expected_cid.startswith("BAI-")
    assert notifications == [(expected_cid, "REJECTED")]
    assert "durable outbox barrier" in caplog.text


# ---------------------------------------------------------------------------
# EX-9: mainnet submissions use deterministic decision-scoped client ids and
# ignore human-readable intent ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mainnet_submission_uses_deterministic_decision_scoped_client_order_id(
    monkeypatch,
):
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    posts = []

    async def handler(method, path, kwargs):
        if method == "POST" and path == "/fapi/v1/order":
            posts.append(kwargs["params"])
            return order_ack(kwargs["params"], orderId=200 + len(posts))
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = make_mainnet_adapter()
    adapter.rest_client = ScriptedRest(handler)
    lease = InMemoryExecutionLease("binance:BINANCE_MAINNET:unit-test", "worker-under-test")
    assert await lease.acquire() is True
    adapter.set_execution_lease(lease)

    async def allow_barrier(order):
        return True

    adapter.before_order_submission = allow_barrier
    adapter.order_gate.check = allow_order_stub("ETHUSDC", Decimal("0.05"), Decimal(100))
    notifications = []
    attach_recorder(adapter, notifications)
    decision = ExecutionDecision(
        decision_id="DEC-IDEM-1",
        symbol="ETHUSDC",
        action="SUBMIT_ORDER",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[
            make_limit_intent(
                client_id="INTENT-CID", symbol="ETHUSDC", quantity="0.05", price="100"
            ),
            make_limit_intent(
                client_id="RUNTIME-2", symbol="ETHUSDC", quantity="0.05", price="100"
            ),
        ],
    )

    executed = await execute_internal(adapter, decision)

    assert len(executed) == 2
    first_cid = adapter._generate_client_order_id("DEC-IDEM-1", "ETHUSDC", 0)
    second_cid = adapter._generate_client_order_id("DEC-IDEM-1", "ETHUSDC", 1)
    assert [params["newClientOrderId"] for params in posts] == [first_cid, second_cid]
    assert first_cid.startswith("BAI-") and second_cid.startswith("BAI-")
    # Same 12-hex decision/symbol hash, differing only in the order index.
    assert len(first_cid[4:16]) == 12
    assert first_cid[4:16] == second_cid[4:16]
    assert first_cid.endswith("-0-1") and second_cid.endswith("-1-1")
    # Mainnet must ignore runtime intent ids as the exchange idempotency key.
    assert all(params["newClientOrderId"] != "INTENT-CID" for params in posts)
    # Identical arguments regenerate the identical id (idempotent across retries).
    assert adapter._generate_client_order_id("DEC-IDEM-1", "ETHUSDC", 0) == first_cid
    assert notifications == [
        (first_cid, "CONFIRMED"),
        (second_cid, "CONFIRMED"),
    ]


# ---------------------------------------------------------------------------
# EX-10: a raising on_order_submission_result callback fails closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_submission_result_callback_failure_fails_closed():
    async def handler(method, path, kwargs):
        if method == "POST" and path == "/fapi/v1/order":
            return order_ack(kwargs["params"], orderId=301)
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))

    async def failing_recorder(order, outcome):
        raise RuntimeError("durable write failed")

    adapter.on_order_submission_result = failing_recorder
    decision = make_decision(
        EconomicRiskClass.NEW_RISK, make_limit_intent(client_id="UNIT-1")
    )
    authority = object()
    adapter.bind_worker_authority(authority)

    first = await adapter._execute_decision(decision, authority=authority)
    # The exchange mutation happened and must still be reported.
    assert len(first) == 1
    assert adapter.state == ConnectionState.DEGRADED
    assert adapter.reconciliation.last_status == "UNKNOWN"

    second = await adapter._execute_decision(decision, authority=authority)
    # The adapter is no longer READY, so the same decision yields nothing.
    assert second == []


# ---------------------------------------------------------------------------
# EX-11: cancel_all_open_orders fails closed on an invalid openOrders response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_all_open_orders_reports_unknown_for_invalid_openorders_response():
    async def handler(method, path, kwargs):
        if method == "GET" and path == "/fapi/v1/openOrders":
            return {}  # not a list
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    authority = object()
    adapter.bind_worker_authority(authority)

    result = await adapter.cancel_all_open_orders(authority=authority)

    assert result == {
        "status": "UNKNOWN",
        "reason": "Authoritative openOrders response is invalid",
    }
    assert rest_methods(adapter) == ["GET"]
    assert "DELETE" not in rest_methods(adapter)


# ---------------------------------------------------------------------------
# EX-12: cancel_all_open_orders counts failed cancellations and reports PARTIAL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_all_open_orders_counts_failed_cancellations_as_partial():
    open_orders_queries = []

    async def handler(method, path, kwargs):
        if method == "GET" and path == "/fapi/v1/openOrders":
            open_orders_queries.append(1)
            if len(open_orders_queries) == 1:
                return [
                    {"symbol": "BTCUSDT", "orderId": 1, "clientOrderId": "A"},
                    {"symbol": "BTCUSDT", "orderId": 2, "clientOrderId": "B"},
                ]
            return []
        if method == "DELETE" and path == "/fapi/v1/order":
            if kwargs["params"]["orderId"] == 1:
                return {"status": "CANCELED", "orderId": 1}
            raise BinanceTransportAmbiguity("timeout")
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    authority = object()
    adapter.bind_worker_authority(authority)

    result = await adapter.cancel_all_open_orders(authority=authority)

    assert result == {"status": "PARTIAL", "remaining_orders": 0, "cancel_failures": 1}


# ---------------------------------------------------------------------------
# EX-13: cancel_order local fail-closed guards (kill switch, non-READY state)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_order_blocked_by_kill_switch_and_degraded_state():
    async def handler(method, path, kwargs):
        raise AssertionError(f"No REST mutation may be issued: {method} {path}")

    # (a) kill switch active on the bound authority
    kill_adapter = await make_adapter(rest=ScriptedRest(handler))
    kill_authority = SimpleNamespace(kill_switch_active=True)
    kill_adapter.bind_worker_authority(kill_authority)
    assert await kill_adapter.cancel_order("BTCUSDT", "UNIT-1", authority=kill_authority) is False
    assert kill_adapter.rest_client.calls == []

    # (b) adapter is not READY
    syncing_adapter = await make_adapter(rest=ScriptedRest(handler))
    authority = object()
    syncing_adapter.bind_worker_authority(authority)
    syncing_adapter.state = ConnectionState.SYNCING
    assert await syncing_adapter.cancel_order("BTCUSDT", "UNIT-1", authority=authority) is False
    assert syncing_adapter.rest_client.calls == []


# ---------------------------------------------------------------------------
# EX-14: cancel ambiguity whose status query fails keeps the adapter degraded
# and never flips the ledger order to CANCELED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_ambiguity_with_failed_status_query_degrades_adapter():
    async def handler(method, path, kwargs):
        if method == "DELETE" and path == "/fapi/v1/order":
            raise BinanceTransportAmbiguity("timeout")
        if method == "GET" and path == "/fapi/v1/order":
            raise RuntimeError("rest unavailable")
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    await adapter.ledger.upsert_order(
        ExecutionOrder(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.001"),
            price=Decimal(10000),
            order_type="LIMIT",
            client_order_id="UNIT-1",
            status="NEW",
            exchange_order_id="12",
        )
    )
    authority = object()
    adapter.bind_worker_authority(authority)

    assert await adapter.cancel_order("BTCUSDT", "UNIT-1", authority=authority) is False
    # status_known False must defeat the IN_SYNC reconciliation verdict.
    assert adapter.state == ConnectionState.DEGRADED
    assert adapter.reconciliation.calls == 1
    record = await adapter.ledger.get_order_by_client_id("UNIT-1")
    assert record is not None
    assert record.status == "NEW"


# ---------------------------------------------------------------------------
# EX-15: modify_order rejects terminal orders and side changes without a PUT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_modify_order_rejects_terminal_orders_and_side_changes():
    async def handler(method, path, kwargs):
        raise AssertionError(f"No amendment PUT may be issued: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    await adapter.ledger.upsert_order(
        ExecutionOrder(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.001"),
            price=Decimal(10000),
            order_type="LIMIT",
            client_order_id="AMEND-1",
            status="CANCELED",
            exchange_order_id="13",
        )
    )
    await adapter.ledger.upsert_order(
        ExecutionOrder(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.001"),
            price=Decimal(10000),
            order_type="LIMIT",
            client_order_id="AMEND-2",
            status="NEW",
            exchange_order_id="14",
        )
    )
    authority = object()
    adapter.bind_worker_authority(authority)

    terminal = await adapter.modify_order(
        "BTCUSDT", "AMEND-1", Decimal(9000), Decimal("0.001"), "BUY", authority=authority
    )
    side_change = await adapter.modify_order(
        "BTCUSDT", "AMEND-2", Decimal(9000), Decimal("0.001"), "SELL", authority=authority
    )

    assert terminal is None
    assert side_change is None
    assert adapter.rest_client.calls == []
    assert "PUT" not in rest_methods(adapter)


# ---------------------------------------------------------------------------
# EX-16: emergency flatten requires worker authority and fails closed when the
# position read fails authentication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emergency_flatten_requires_worker_authority_and_fails_closed_on_position_read_errors():
    # (a) no authority passed at all
    async def unreachable(method, path, kwargs):
        raise AssertionError(f"No REST call may be issued: {method} {path}")

    unbound = await make_adapter(rest=ScriptedRest(unreachable))
    assert await unbound.emergency_flatten(authority=None) == []
    assert unbound.last_emergency_result == {
        "status": "UNKNOWN",
        "reason": "Worker authority is required for emergency execution",
    }

    # (b) bound authority but the position read fails authentication
    async def handler(method, path, kwargs):
        if method == "GET" and path == "/fapi/v2/positionRisk":
            raise BinanceAuthenticationError(-2015, "Invalid API-key")
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    authority = object()
    adapter.bind_worker_authority(authority)

    assert await adapter.emergency_flatten(authority=authority) == []
    assert adapter.last_emergency_result["status"] == "UNKNOWN"
    assert "authentication failed while reading positions" in adapter.last_emergency_result["reason"]
    assert adapter.authenticated is False
    assert adapter.state == ConnectionState.DEGRADED


# ---------------------------------------------------------------------------
# EX-17: emergency flatten on a flat account reports CONFIRMED with zero
# submitted orders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emergency_flatten_flat_account_reports_confirmed():
    async def handler(method, path, kwargs):
        if method == "GET" and path == "/fapi/v2/positionRisk":
            return []  # flat account
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    authority = object()
    adapter.bind_worker_authority(authority)

    flattened = await adapter.emergency_flatten(authority=authority)

    assert flattened == []
    assert adapter.last_emergency_result == {
        "status": "CONFIRMED",
        "submitted_orders": 0,
        "reconciliation": "IN_SYNC",
    }
    assert adapter.state == ConnectionState.READY


# ---------------------------------------------------------------------------
# EX-18: ambiguous POST resolved FILLED but fill recovery failing with an auth
# error returns no execution success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filled_ambiguity_recovery_with_fill_recovery_auth_failure_returns_none():
    async def handler(method, path, kwargs):
        if method == "POST" and path == "/fapi/v1/order":
            raise BinanceTransportAmbiguity("timeout")
        if method == "GET" and path == "/fapi/v1/order":
            return {
                "orderId": 555,
                "status": "FILLED",
                "symbol": "BTCUSDT",
                "clientOrderId": "UNIT-1",
                "side": "BUY",
                "positionSide": "BOTH",
                "origQty": "0.001",
                "price": "10000",
            }
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))

    class FailingFillRecovery(FakeReconciliation):
        async def _recover_order_fills(self, order, response):
            raise BinanceAuthenticationError(-2015, "invalid key")

    adapter.reconciliation = FailingFillRecovery()
    notifications = []
    attach_recorder(adapter, notifications)
    decision = make_decision(
        EconomicRiskClass.NEW_RISK, make_limit_intent(client_id="UNIT-1")
    )

    executed = await execute_internal(adapter, decision)

    assert executed == []
    # invalidate_authentication ran inside fill recovery.
    assert adapter.authenticated is False
    assert adapter.state is not ConnectionState.READY
    assert ("UNIT-1", "CONFIRMED") not in notifications
