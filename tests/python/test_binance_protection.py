import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time

import pytest

from apps.trading_worker.venues.binance.protection import (
    ProtectionIntent,
    verify_protection,
)
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.gates import GateResult, PreparedOrder
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.models import (
    BinanceTransportAmbiguity,
    ConnectionState,
)
from domain.enums import (
    EconomicRiskClass,
    MarketType,
    OrderSide,
    OrderType,
    PositionSide,
    TimeInForce,
)
from domain.models import ExchangeFill, ExecutionDecision, ExecutionOrder, OrderIntent


NOW = int(datetime.now(timezone.utc).timestamp() * 1000)


def fixture(side="BUY", position_side="BOTH", close=True):
    stop_trigger, take_profit_trigger = (Decimal("1900"), Decimal("2100")) if side == "BUY" else (Decimal("2100"), Decimal("1900"))
    intent = ProtectionIntent("ETHUSDC", side, position_side, Decimal("0.2"),
                              stop_trigger, take_profit_trigger, 11, "sl-11", 12, "tp-12")
    orders = []
    for ident, client, kind, price in ((11, "sl-11", "STOP_MARKET", str(stop_trigger)),
                                        (12, "tp-12", "TAKE_PROFIT_MARKET", str(take_profit_trigger))):
        orders.append(dict(algoId=ident, clientAlgoId=client, algoType="CONDITIONAL",
                           orderType=kind, symbol="ETHUSDC", side="SELL" if side == "BUY" else "BUY",
                           positionSide=position_side, triggerPrice=price, algoStatus="NEW",
                           workingType="MARK_PRICE",
                           closePosition=close, reduceOnly=False if close else True,
                           quantity="0" if close else "0.2", createTime=NOW - 10000))
    return intent, orders


def check(intent, orders, **kwargs):
    signed_position = "-0.2" if intent.entry_side == "SELL" or intent.position_side == "SHORT" else "0.2"
    params = dict(position_qty=signed_position, filled_qty="0.2", stop_query=orders[0].copy(),
                  take_profit_query=orders[1].copy(), open_algos=orders,
                  average_entry_price="2000", entry_terminal=True,
                  now_ms=NOW, query_observed_ms=NOW-100, open_observed_ms=NOW-100,
                  position_observed_ms=NOW-100)
    params.update(kwargs)
    return verify_protection(intent, **params)


@pytest.mark.parametrize("side,pos_side,close", [
    ("BUY", "BOTH", True), ("SELL", "BOTH", True),
    ("BUY", "LONG", True), ("SELL", "SHORT", True),
    ("BUY", "BOTH", False),
])
def test_protected(side, pos_side, close):
    intent, orders = fixture(side, pos_side, close)
    assert check(intent, orders).state == "PROTECTED"


@pytest.mark.parametrize("field,value", [
    ("algoStatus", "TRIGGERED"), ("orderType", "STOP"),
    ("symbol", "BTCUSDT"), ("side", "BUY"), ("positionSide", "SHORT"),
    ("triggerPrice", "1901"), ("closePosition", False),
])
def test_bad_stop_contract(field, value):
    intent, orders = fixture()
    orders[0][field] = value
    assert check(intent, orders).state == "UNPROTECTED"


def test_wrong_position_direction_and_trigger_reference_are_rejected():
    intent, orders = fixture("BUY")
    assert check(intent, orders, position_qty="-0.2").state == "UNPROTECTED"
    orders[1]["workingType"] = "CONTRACT_PRICE"
    assert "take_profit_query_working_type_mismatch" in check(intent, orders).reasons


def test_missing_open_order_and_partial_fill():
    intent, orders = fixture()
    assert check(intent, orders, open_algos=orders[:1]).state == "UNPROTECTED"
    result = check(intent, orders, filled_qty="0.1", position_qty="0.1", entry_terminal=False)
    assert result.state == "UNPROTECTED" and "entry_order_not_terminal" in result.reasons


def test_terminal_partial_fill_is_protected_when_algos_cover_actual_position():
    intent, orders = fixture()
    for order in orders:
        order["quantity"] = "0"
    assert check(intent, orders, filled_qty="0.1", position_qty="0.1").state == "PROTECTED"


def test_ambiguous_snapshots_and_identity():
    intent, orders = fixture()
    assert check(intent, orders, open_observed_ms=NOW-6000).state == "AMBIGUOUS"
    assert check(intent, orders, position_qty="0.3").state == "AMBIGUOUS"
    assert check(intent, orders, stop_query=None).state == "AMBIGUOUS"
    assert check(replace(intent, take_profit_algo_id=11), orders).state == "AMBIGUOUS"
    duplicate = orders[0].copy()
    assert check(intent, orders + [duplicate]).state == "AMBIGUOUS"


def test_unowned_open_algo_blocks_protection_even_when_expected_pair_is_valid():
    intent, orders = fixture()
    extra = {
        **orders[0],
        "algoId": 13,
        "clientAlgoId": "unowned-13",
    }

    result = check(intent, orders + [extra])

    assert result.state == "AMBIGUOUS"
    assert "unowned_open_algo_present" in result.reasons


def test_exact_reduce_only_quantity_and_hedge_restriction():
    intent, orders = fixture(close=False)
    orders[1]["quantity"] = "0.1"
    assert check(intent, orders).state == "UNPROTECTED"
    intent, orders = fixture(position_side="LONG", close=False)
    assert check(intent, orders).state == "UNPROTECTED"


def test_conflicting_query_and_open_identity():
    intent, orders = fixture()
    query = orders[0].copy()
    query["clientAlgoId"] = "other"
    assert check(intent, orders, stop_query=query).state == "AMBIGUOUS"


def test_invalid_numeric_and_creation_timestamp():
    intent, orders = fixture()
    orders[0]["createTime"] = NOW + 1
    assert check(intent, orders).state == "AMBIGUOUS"
    intent, orders = fixture()
    orders[0]["triggerPrice"] = "NaN"
    assert check(intent, orders).state == "UNPROTECTED"


@pytest.mark.asyncio
async def test_adapter_reads_authoritative_position_fill_and_algo_snapshots():
    intent, orders = fixture()
    ledger = InMemoryLedger()
    await ledger.upsert_order(ExecutionOrder(
        symbol="ETHUSDC", side=OrderSide.BUY, quantity=Decimal("0.2"),
        price=Decimal("2000"), client_order_id="entry-client-1", status="FILLED",
        exchange_order_id="entry-1", position_side=PositionSide.BOTH,
    ))
    await ledger.append_fill(ExchangeFill(
        exchange_trade_id="trade-1", exchange_order_id="entry-1",
        client_order_id="entry-client-1", symbol="ETHUSDC", side=OrderSide.BUY,
        position_side=PositionSide.BOTH, quantity=Decimal("0.2"), price=Decimal("2000"),
        commission=Decimal("0.01"), commission_asset="USDC", realized_pnl=Decimal("0"),
        maker=False, event_time=datetime.now(timezone.utc), transaction_time=NOW,
        source="BINANCE_TESTNET",
    ))

    class ReadOnlyRest:
        portfolio_margin = False

        async def request(self, method, path, **kwargs):
            assert method == "GET" and kwargs.get("signed") is True
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0.2"}]
            if path == "/fapi/v1/algoOrder":
                ident = kwargs["params"]["algoId"]
                return next(order.copy() for order in orders if order["algoId"] == ident)
            if path == "/fapi/v1/openAlgoOrders":
                return [order.copy() for order in orders]
            raise AssertionError(f"Unexpected endpoint {path}")

    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    adapter.rest_client = ReadOnlyRest()
    result = await adapter.read_back_algo_protection(intent, entry_client_order_id="entry-client-1")
    assert result.state == "PROTECTED"


@pytest.mark.asyncio
async def test_adapter_readback_fails_closed_when_exchange_data_is_ambiguous():
    intent, orders = fixture()

    class IncompleteRest:
        portfolio_margin = False

        async def request(self, method, path, **kwargs):
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0.2"}]
            if path == "/fapi/v1/algoOrder":
                return orders[0]
            return None

    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=InMemoryLedger())
    adapter.rest_client = IncompleteRest()
    result = await adapter.read_back_algo_protection(intent, entry_client_order_id="entry-client-1")
    assert result.state == "AMBIGUOUS"
    assert result.protected is False


@pytest.mark.asyncio
async def test_testnet_protected_entry_rejects_wrong_symbol_before_authorized_path():
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=InMemoryLedger())
    intent = OrderIntent(
        client_order_id="wrong-symbol-entry", symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES, side=OrderSide.BUY,
        position_side=PositionSide.BOTH, order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC, quantity=Decimal("0.001"),
        stop_loss_price=Decimal("1900"), take_profit_price=Decimal("2100"),
    )
    decision = ExecutionDecision(
        decision_id="wrong-symbol", symbol="BTCUSDT", action="SUBMIT_ORDER",
        risk_class="NEW_RISK", orders=[intent],
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("wrong-symbol entry reached exchange mutation path")

    adapter._execute_authorized_decision = forbidden
    assert await adapter.execute_protected_testnet_decision(decision, authority=object()) == []
    assert adapter.last_testnet_protection["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_testnet_protected_entry_requires_durable_owner_before_execution():
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=InMemoryLedger())
    intent = OrderIntent(
        client_order_id="durable-owner-required", symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES, side=OrderSide.BUY,
        position_side=PositionSide.BOTH, order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC, quantity=Decimal("0.01"),
        stop_loss_price=Decimal("1900"), take_profit_price=Decimal("2100"),
    )
    decision = ExecutionDecision(
        decision_id="durable-owner-required", symbol="ETHUSDC",
        action="SUBMIT_ORDER", risk_class="NEW_RISK", orders=[intent],
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("entry passed without a durable protection owner")

    adapter._execute_authorized_decision = forbidden
    assert await adapter.execute_protected_testnet_decision(decision, authority=object()) == []
    assert adapter.last_testnet_protection["reason"] == "durable_protection_store_unavailable"


@pytest.mark.asyncio
async def test_testnet_public_risk_increase_cannot_bypass_protected_entry_route():
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=InMemoryLedger())
    authority = object()
    adapter.bind_worker_authority(authority)
    decision = ExecutionDecision(
        decision_id="unprotected-testnet-entry", symbol="ETHUSDC",
        action="SUBMIT_ORDER", risk_class=EconomicRiskClass.NEW_RISK,
        orders=[OrderIntent(
            client_order_id="unprotected-entry", symbol="ETHUSDC",
            market_type=MarketType.USDM_FUTURES, side=OrderSide.BUY,
            position_side=PositionSide.BOTH, order_type=OrderType.MARKET,
            time_in_force=TimeInForce.GTC, quantity=Decimal("0.01"),
            stop_loss_price=Decimal("1900"), take_profit_price=Decimal("2100"),
        )],
    )

    assert await adapter.execute_decision(decision, authority=authority) == []
    assert await adapter._execute_decision(decision, authority=authority) == []


@pytest.mark.asyncio
async def test_ambiguous_testnet_entry_is_protected_before_reconciliation():
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=InMemoryLedger())
    authority = object()
    adapter.bind_worker_authority(authority)
    adapter.state = ConnectionState.READY
    adapter.capabilities.account_request_succeeded = True
    adapter.capabilities.authenticated = True
    adapter.capabilities.trade_authorized = True
    adapter.user_stream = type("Stream", (), {"is_connected": True})()
    intent = OrderIntent(
        client_order_id="ambiguous-protected-entry", symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES, side=OrderSide.BUY,
        position_side=PositionSide.BOTH, order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC, quantity=Decimal("0.01"),
        stop_loss_price=Decimal("1900"), take_profit_price=Decimal("2100"),
    )
    decision = ExecutionDecision(
        decision_id="ambiguous-protected-entry", symbol="ETHUSDC",
        action="SUBMIT_ORDER", risk_class=EconomicRiskClass.NEW_RISK,
        orders=[intent],
    )
    status_response = {
        "orderId": 501, "status": "FILLED", "symbol": "ETHUSDC",
        "clientOrderId": intent.client_order_id, "side": "BUY",
        "positionSide": "BOTH", "origQty": "0.01", "executedQty": "0.01",
        "price": "2000", "avgPrice": "2000", "type": "MARKET",
    }
    events = []

    class AmbiguousRest:
        async def request(self, method, path, **kwargs):
            before_mutation = kwargs.get("before_mutation")
            if callable(before_mutation):
                await before_mutation()
            if method == "GET" and path == "/fapi/v2/positionRisk":
                return [{
                    "symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0",
                }]
            if method == "POST" and path == "/fapi/v1/order":
                raise BinanceTransportAmbiguity("accepted request response was lost")
            if method == "GET" and path == "/fapi/v1/order":
                return dict(status_response)
            raise AssertionError(f"unexpected REST call {method} {path}")

    adapter.rest_client = AmbiguousRest()
    adapter.order_gate.check = lambda *args, **kwargs: _allowed_market_gate()
    adapter.symbol_rules["ETHUSDC"] = type(
        "Rules", (), {"normalize_price": lambda self, value: value}
    )()
    adapter._assert_execution_lease = lambda *args, **kwargs: _no_op()
    adapter._final_risk_increase_fence = lambda *args, **kwargs: _no_op()

    recovery_calls = 0

    async def recover_fills(*args, **kwargs):
        nonlocal recovery_calls
        recovery_calls += 1
        if recovery_calls == 1:
            raise RuntimeError("first fill lookup is transiently unavailable")
        return None

    async def reconcile():
        events.append("reconcile")
        return "IN_SYNC"

    adapter.reconciliation._recover_order_fills = recover_fills
    adapter.reconciliation.reconcile = reconcile

    async def persist_owner(record):
        assert record["state"] == "PENDING"
        events.append("owner-persisted")
        return True

    async def protect(_intent, _order, response, *, authority):
        assert authority is authority_ref
        assert response == status_response
        assert "reconcile" not in events
        events.append("protection")
        adapter.last_testnet_protection = {"status": "PROTECTED"}
        return True

    async def notify(_order, result):
        events.append(f"notify:{result}")

    async def _no_op():
        return None

    async def _allowed_market_gate():
        return GateResult(
            True, "allowed",
            PreparedOrder(
                symbol="ETHUSDC", order_type="MARKET", quantity=Decimal("0.01"),
                price=None, estimated_price=Decimal("2000"), notional=Decimal("20"),
            ),
        )

    authority_ref = authority
    adapter.on_testnet_protection_update = persist_owner
    adapter.on_order_submission_result = notify
    adapter._protect_testnet_entry = protect

    orders = await adapter._execute_decision(
        decision, authority=authority, enforce_testnet_protection=True,
    )

    assert len(orders) == 1 and orders[0].status == "FILLED"
    assert events.index("owner-persisted") < events.index("protection")
    assert events.index("protection") < events.index("reconcile")
    assert recovery_calls >= 2
    assert events[-1] == "notify:CONFIRMED"


@pytest.mark.asyncio
async def test_ambiguous_testnet_entry_status_outage_runs_protection_fallback_without_retry():
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=InMemoryLedger())
    authority = object()
    adapter.bind_worker_authority(authority)
    adapter.state = ConnectionState.READY
    adapter.capabilities.account_request_succeeded = True
    adapter.capabilities.authenticated = True
    adapter.capabilities.trade_authorized = True
    adapter.user_stream = type("Stream", (), {"is_connected": True})()
    intent = OrderIntent(
        client_order_id="status-outage-entry", symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES, side=OrderSide.BUY,
        position_side=PositionSide.BOTH, order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC, quantity=Decimal("0.01"),
        stop_loss_price=Decimal("1900"), take_profit_price=Decimal("2100"),
    )
    decision = ExecutionDecision(
        decision_id="status-outage-entry", symbol="ETHUSDC",
        action="SUBMIT_ORDER", risk_class=EconomicRiskClass.NEW_RISK, orders=[intent],
    )
    events = []
    entry_posts = 0

    class StatusOutageRest:
        async def request(self, method, path, **kwargs):
            nonlocal entry_posts
            before_mutation = kwargs.get("before_mutation")
            if callable(before_mutation):
                await before_mutation()
            if method == "GET" and path == "/fapi/v2/positionRisk":
                events.append("flat-baseline")
                return [{
                    "symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0",
                }]
            if method == "POST" and path == "/fapi/v1/order":
                entry_posts += 1
                raise BinanceTransportAmbiguity("accepted request response was lost")
            if method == "GET" and path == "/fapi/v1/order":
                raise BinanceTransportAmbiguity("order status endpoint is unavailable")
            raise AssertionError(f"unexpected REST call {method} {path}")

    adapter.rest_client = StatusOutageRest()
    adapter.order_gate.check = lambda *args, **kwargs: _allowed_market_gate()
    adapter.symbol_rules["ETHUSDC"] = type(
        "Rules", (), {"normalize_price": lambda self, value: value}
    )()
    adapter._assert_execution_lease = lambda *args, **kwargs: _no_op()
    adapter._final_risk_increase_fence = lambda *args, **kwargs: _no_op()

    async def persist_owner(record):
        events.append(f"owner:{record['state']}")
        return True

    async def protection_fallback(_intent, order, response, *, authority):
        assert authority is authority_ref
        assert order.client_order_id == intent.client_order_id
        assert response == {}
        events.append("protection-fallback")
        adapter.last_testnet_protection = {"status": "FAILED_CLOSED"}
        return False

    async def notify(_order, result):
        events.append(f"notify:{result}")

    async def _no_op():
        return None

    async def _allowed_market_gate():
        return GateResult(
            True, "allowed",
            PreparedOrder(
                symbol="ETHUSDC", order_type="MARKET", quantity=Decimal("0.01"),
                price=None, estimated_price=Decimal("2000"), notional=Decimal("20"),
            ),
        )

    authority_ref = authority
    adapter.on_testnet_protection_update = persist_owner
    adapter.on_order_submission_result = notify
    adapter._protect_testnet_entry = protection_fallback

    orders = await adapter._execute_decision(
        decision, authority=authority, enforce_testnet_protection=True,
    )

    assert orders == []
    assert entry_posts == 1
    assert adapter.order_submission_attempts == 1
    assert "protection-fallback" in events
    assert events.index("flat-baseline") < events.index("protection-fallback")
    assert events[-1] == "notify:AMBIGUOUS"
    assert adapter.state == ConnectionState.DEGRADED


@pytest.mark.asyncio
async def test_protected_testnet_entry_refuses_nonflat_baseline_before_any_order_post():
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=InMemoryLedger())
    authority = object()
    adapter.bind_worker_authority(authority)
    adapter.state = ConnectionState.READY
    adapter.capabilities.account_request_succeeded = True
    adapter.capabilities.authenticated = True
    adapter.capabilities.trade_authorized = True
    adapter.user_stream = type("Stream", (), {"is_connected": True})()
    intent = OrderIntent(
        client_order_id="nonflat-baseline-entry", symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES, side=OrderSide.BUY,
        position_side=PositionSide.BOTH, order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC, quantity=Decimal("0.01"),
        stop_loss_price=Decimal("1900"), take_profit_price=Decimal("2100"),
    )
    decision = ExecutionDecision(
        decision_id="nonflat-baseline-entry", symbol="ETHUSDC",
        action="SUBMIT_ORDER", risk_class=EconomicRiskClass.NEW_RISK, orders=[intent],
    )
    events = []

    class NonflatRest:
        async def request(self, method, path, **kwargs):
            before_mutation = kwargs.get("before_mutation")
            if callable(before_mutation):
                await before_mutation()
            if method == "GET" and path == "/fapi/v2/positionRisk":
                return [{
                    "symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0.01",
                }]
            if method == "POST" and path == "/fapi/v1/order":
                events.append("entry-post")
            raise AssertionError(f"unexpected REST call {method} {path}")

    adapter.rest_client = NonflatRest()
    adapter.order_gate.check = lambda *args, **kwargs: _allowed_market_gate()
    adapter.symbol_rules["ETHUSDC"] = type(
        "Rules", (), {"normalize_price": lambda self, value: value}
    )()
    adapter._assert_execution_lease = lambda *args, **kwargs: _no_op()
    adapter._final_risk_increase_fence = lambda *args, **kwargs: _no_op()

    async def persist_owner(record):
        events.append(f"owner:{record['state']}")
        return True

    async def notify(_order, result):
        events.append(f"notify:{result}")

    async def _no_op():
        return None

    async def _allowed_market_gate():
        return GateResult(
            True, "allowed",
            PreparedOrder(
                symbol="ETHUSDC", order_type="MARKET", quantity=Decimal("0.01"),
                price=None, estimated_price=Decimal("2000"), notional=Decimal("20"),
            ),
        )

    adapter.on_testnet_protection_update = persist_owner
    adapter.on_order_submission_result = notify

    orders = await adapter._execute_decision(
        decision, authority=authority, enforce_testnet_protection=True,
    )

    assert orders == []
    assert "entry-post" not in events
    assert "owner:CLOSED" in events
    assert adapter.order_submission_attempts == 0


@pytest.mark.asyncio
async def test_entry_failure_flatten_is_limited_to_the_prechecked_hedge_position_side():
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=InMemoryLedger())
    authority = object()
    adapter.bind_worker_authority(authority)
    adapter.capabilities.account_request_succeeded = True
    adapter.capabilities.authenticated = True
    adapter.user_stream = type("Stream", (), {"is_connected": True})()

    class PositionRest:
        calls = 0

        async def request(self, method, path, **kwargs):
            assert (method, path) == ("GET", "/fapi/v2/positionRisk")
            self.calls += 1
            if self.calls > 1:
                return [
                    {"symbol": "ETHUSDC", "positionSide": "LONG", "positionAmt": "0.2"},
                    {"symbol": "ETHUSDC", "positionSide": "SHORT", "positionAmt": "0"},
                ]
            return [
                {"symbol": "ETHUSDC", "positionSide": "LONG", "positionAmt": "0.2"},
                {"symbol": "ETHUSDC", "positionSide": "SHORT", "positionAmt": "-0.1"},
            ]

    adapter.rest_client = PositionRest()
    adapter.ledger.replace_positions = _no_op
    flattened_sides = []

    async def execute_close(decision, **kwargs):
        flattened_sides.append(decision.orders[0].position_side)
        return [object()]

    async def reconcile():
        return "IN_SYNC"

    adapter._execute_decision = execute_close
    adapter.reconciliation.reconcile = reconcile
    flattened = await adapter._emergency_flatten(
        "ETHUSDC", authority=authority, only_position_side=PositionSide.SHORT,
    )

    assert len(flattened) == 1
    assert flattened_sides == [PositionSide.SHORT]
    assert adapter.last_emergency_result == {
        "status": "CONFIRMED", "submitted_orders": 1,
        "reconciliation": "IN_SYNC", "positions_flat_verified": True,
    }


async def _no_op(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_ambiguous_algo_post_is_resolved_by_client_id_without_resubmission():
    algo = {
        "algoId": 91, "clientAlgoId": "BAI-SL-abc", "algoType": "CONDITIONAL",
        "orderType": "STOP_MARKET", "symbol": "ETHUSDC", "algoStatus": "NEW",
    }
    calls = []

    class Authority:
        kill_switch_active = False

    class AmbiguousRest:
        portfolio_margin = False

        async def request(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            before = kwargs.get("before_mutation")
            if callable(before):
                await before()
            if method == "POST":
                raise BinanceTransportAmbiguity("simulated lost response")
            return algo

    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=InMemoryLedger())
    adapter.rest_client = AmbiguousRest()
    authority = Authority()
    adapter.bind_worker_authority(authority)
    result = await adapter._submit_testnet_protection_algo(
        symbol="ETHUSDC", side="SELL", position_side="BOTH",
        order_type="STOP_MARKET", trigger_price=Decimal("1900"),
        client_algo_id="BAI-SL-abc", authority=authority,
        deadline=time.monotonic() + 4,
    )

    assert result == algo
    assert [call[0] for call in calls] == ["POST", "GET", "GET"]
    assert calls[1][2]["params"] == {"symbol": "ETHUSDC", "clientAlgoId": "BAI-SL-abc"}
    assert calls[2][2]["params"] == {"symbol": "ETHUSDC", "algoId": 91}


@pytest.mark.asyncio
async def test_testnet_market_entry_cancels_partial_remainder_then_confirms_protection():
    ledger = InMemoryLedger()
    entry_client_id = "entry-partial-1"
    event_time = datetime.now(timezone.utc)
    order = ExecutionOrder(
        symbol="ETHUSDC", side=OrderSide.BUY, quantity=Decimal("0.2"),
        price=Decimal("2000"), client_order_id=entry_client_id,
        status="PARTIALLY_FILLED", exchange_order_id="entry-exchange-1",
        position_side=PositionSide.BOTH,
    )
    await ledger.upsert_order(order)
    await ledger.append_fill(ExchangeFill(
        exchange_trade_id="partial-trade-1", exchange_order_id="entry-exchange-1",
        client_order_id=entry_client_id, symbol="ETHUSDC", side=OrderSide.BUY,
        position_side=PositionSide.BOTH, quantity=Decimal("0.1"), price=Decimal("2000"),
        commission=Decimal("0.01"), commission_asset="USDC", realized_pnl=Decimal("0"),
        maker=False, event_time=event_time, transaction_time=event_time,
        source="BINANCE_TESTNET",
    ))
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    intent = OrderIntent(
        client_order_id=entry_client_id, symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES, side=OrderSide.BUY,
        position_side=PositionSide.BOTH, order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC, quantity=Decimal("0.2"),
        stop_loss_price=Decimal("1900"), take_profit_price=Decimal("2100"),
    )
    cancel_calls = []

    async def cancel_entry(symbol, client_order_id):
        cancel_calls.append((symbol, client_order_id))
        return {"status": "CANCELED", "executedQty": "0.1"}

    async def recover_order_fills(_order, _status):
        return 1

    submitted = []
    exchange_algos = []

    async def submit_algo(**kwargs):
        submitted.append(kwargs["order_type"])
        algo_id = 11 if kwargs["order_type"] == "STOP_MARKET" else 12
        order_side = "SELL"
        order = {
            "algoId": algo_id,
            "clientAlgoId": kwargs["client_algo_id"],
            "algoType": "CONDITIONAL",
            "orderType": kwargs["order_type"],
            "symbol": kwargs["symbol"],
            "side": order_side,
            "positionSide": kwargs["position_side"],
            "triggerPrice": str(kwargs["trigger_price"]),
            "workingType": "MARK_PRICE",
            "algoStatus": "NEW",
            "closePosition": False,
            "reduceOnly": True,
            "quantity": str(kwargs.get("quantity", "0.1")),
            "createTime": int(time.time() * 1000) - 100,
        }
        exchange_algos.append(order)
        return {"algoId": algo_id, "clientAlgoId": kwargs["client_algo_id"]}

    class ReadOnlyRest:
        portfolio_margin = False

        async def request(self, method, path, **kwargs):
            assert method == "GET" and kwargs.get("signed") is True
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0.1"}]
            if path == "/fapi/v1/algoOrder":
                ident = kwargs["params"]["algoId"]
                return next(item.copy() for item in exchange_algos if item["algoId"] == ident)
            if path == "/fapi/v1/openAlgoOrders":
                return [item.copy() for item in exchange_algos]
            raise AssertionError(f"Unexpected REST endpoint: {path}")

    adapter._cancel_testnet_entry_for_protection = cancel_entry
    adapter.reconciliation._recover_order_fills = recover_order_fills
    adapter._submit_testnet_protection_algo = submit_algo
    adapter._submit_local_mainnet_protection_algo = submit_algo
    adapter.rest_client = ReadOnlyRest()
    protection_writes = []

    async def persist_protection(record):
        protection_writes.append(dict(record))
        return True

    adapter.on_testnet_protection_update = persist_protection

    assert await adapter._protect_testnet_entry(
        intent, order, {"status": "PARTIALLY_FILLED", "executedQty": "0.1"},
        authority=object(),
    ) is True
    assert cancel_calls == [("ETHUSDC", entry_client_id)]
    assert submitted == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    assert adapter.last_testnet_protection["status"] == "PROTECTED"
    assert [row["state"] for row in protection_writes] == ["PENDING", "PENDING", "PENDING", "PROTECTED"]
    assert protection_writes[-1]["stop_algo_id"] == "11"
    assert protection_writes[-1]["take_profit_algo_id"] == "12"


def local_mainnet_intent(client_order_id="entry-local-1", quantity=Decimal("0.2")):
    return OrderIntent(
        client_order_id=client_order_id,
        symbol="ETHUSDC",
        basket_id="basket-local-1",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=quantity,
        stop_loss_price=Decimal("1900"),
        take_profit_price=Decimal("2100"),
    )


def local_mainnet_entry_order(client_order_id="entry-local-1", quantity=Decimal("0.2")):
    return ExecutionOrder(
        symbol="ETHUSDC",
        side=OrderSide.BUY,
        quantity=quantity,
        price=Decimal("2000"),
        order_type="MARKET",
        client_order_id=client_order_id,
        status="FILLED",
        exchange_order_id="entry-exchange-1",
        position_side=PositionSide.BOTH,
        risk_class=EconomicRiskClass.NEW_RISK,
    )


async def add_local_mainnet_entry_fill(
    ledger,
    client_order_id,
    *,
    event_time=None,
    requested_quantity=Decimal("0.2"),
    filled_quantity=Decimal("0.2"),
    fill_price=Decimal("2000"),
    status="FILLED",
):
    event_time = event_time or datetime.now(timezone.utc)
    entry_order = local_mainnet_entry_order(client_order_id, requested_quantity)
    entry_order.status = status
    await ledger.upsert_order(entry_order)
    await ledger.append_fill(
        ExchangeFill(
            exchange_trade_id=f"trade-{client_order_id}",
            exchange_order_id="entry-exchange-1",
            client_order_id=client_order_id,
            symbol="ETHUSDC",
            side=OrderSide.BUY,
            position_side=PositionSide.BOTH,
            quantity=filled_quantity,
            price=fill_price,
            commission=Decimal("0.01"),
            commission_asset="USDC",
            realized_pnl=Decimal("0"),
            maker=False,
            event_time=event_time,
            transaction_time=event_time,
            source="BINANCE_MAINNET_MOCK",
        )
    )


def install_close_claim_fixture(adapter, authority, initial_record=None):
    """Model a consumed durable claim; it cannot be reset by owner updates."""
    rows = dict(initial_record or {})
    claim = {}
    original_persist = adapter.on_local_mainnet_protection_update

    async def persist(record):
        rows.clear()
        rows.update(record)
        return await original_persist(record)

    class Claims:
        async def get_protection(self, *args):
            return dict(rows)

        async def claim_local_emergency_close(self, symbol, entry_id, close_id, *, claimant_id):
            if claim:
                return {"claimed": False, **claim}
            claim.update(close_client_order_id=close_id, fencing_token=1,
                         claimant_id=claimant_id, status="RESERVED")
            rows.update(state="CLOSE_PENDING", state_reason=f"local_close_client_order_id={close_id};close_submission=RESERVED")
            return {"claimed": True, **claim}

        async def mark_local_emergency_close_attempted(self, symbol, entry_id, close_id, *, claimant_id, fencing_token):
            if claim.get("status") != "RESERVED" or claim.get("claimant_id") != claimant_id or fencing_token != 1:
                return False
            claim["status"] = "ATTEMPTED"
            rows.update(state="CLOSE_PENDING", state_reason=f"local_close_client_order_id={close_id};close_submission=ATTEMPTED")
            return True

    adapter.on_local_mainnet_protection_update = persist
    authority.persistence = type("Persistence", (), {
        "repository": type("Repository", (), {"algo_protections": Claims()})(),
    })()
    return rows


def make_local_mainnet_adapter(monkeypatch, ledger):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.MAINNET, ledger=ledger)
    authority = type("Authority", (), {"kill_switch_active": False, "pause_new_risk": False})()
    adapter.bind_worker_authority(authority)
    adapter._assert_execution_lease = _no_op
    adapter.reconciliation._recover_order_fills = _no_op
    return adapter, authority


@pytest.mark.asyncio
async def test_local_mainnet_post_fill_protection_uses_actual_fill_quantity(monkeypatch):
    ledger = InMemoryLedger()
    entry_id = "entry-local-protected"
    await add_local_mainnet_entry_fill(
        ledger,
        entry_id,
        requested_quantity=Decimal("0.02"),
        filled_quantity=Decimal("0.02"),
        fill_price=Decimal("2000.10"),
        status="PARTIALLY_FILLED",
    )
    adapter, authority = make_local_mainnet_adapter(monkeypatch, ledger)
    authority._mainnet_launch_session = {"policy": "LIVE_RESEARCH_PILOT"}
    intent = local_mainnet_intent(entry_id, quantity=Decimal("0.02")).model_copy(update={
        "stop_loss_price": Decimal("1950"),
        "take_profit_price": Decimal("2100"),
        "estimated_fees_usdc": Decimal("0"),
        "estimated_funding_usdc": Decimal("0"),
        "estimated_slippage_usdc": Decimal("0"),
    })
    order = local_mainnet_entry_order(entry_id, quantity=Decimal("0.02"))
    order.status = "PARTIALLY_FILLED"
    written = []
    algos = {}
    submitted = []

    async def persist(record):
        written.append(dict(record))
        return True

    async def submit(**kwargs):
        submitted.append(dict(kwargs))
        algo_id = 31 if kwargs["order_type"] == "STOP_MARKET" else 32
        row = {
            "algoId": algo_id,
            "clientAlgoId": kwargs["client_algo_id"],
            "algoType": "CONDITIONAL",
            "orderType": kwargs["order_type"],
            "symbol": kwargs["symbol"],
            "side": kwargs["side"],
            "positionSide": "BOTH",
            "triggerPrice": str(kwargs["trigger_price"]),
            "workingType": "MARK_PRICE",
            "algoStatus": "NEW",
            "closePosition": False,
            "reduceOnly": True,
            "quantity": str(kwargs["quantity"]),
            "createTime": int(time.time() * 1000) - 100,
        }
        algos[algo_id] = row
        return dict(row)

    class MockReadBack:
        async def request(self, method, path, **kwargs):
            assert method == "GET" and kwargs.get("signed") is True
            if path == "/fapi/v2/positionRisk":
                return [{"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0.02"}]
            if path == "/fapi/v1/algoOrder":
                return dict(algos[int(kwargs["params"]["algoId"])])
            if path == "/fapi/v1/openAlgoOrders":
                return [dict(row) for row in algos.values()]
            raise AssertionError(f"Unexpected mock endpoint {path}")

    adapter.rest_client = MockReadBack()
    adapter.on_local_mainnet_protection_update = persist
    adapter._submit_local_mainnet_protection_algo = submit

    async def cancel_entry(_symbol, _client_order_id):
        return {"status": "CANCELED", "executedQty": "0.02"}

    adapter._cancel_testnet_entry_for_protection = cancel_entry

    assert await adapter._protect_local_mainnet_entry(
        intent,
        order,
        {"status": "PARTIALLY_FILLED", "executedQty": "0.02"},
        authority=authority,
    ) is True
    assert [call["order_type"] for call in submitted] == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    assert all(call["quantity"] == Decimal("0.02") for call in submitted)
    assert all(call["position_side"] == "BOTH" for call in submitted)
    assert [row["state"] for row in written][-1] == "PROTECTED"
    assert written[-1]["requested_quantity"] == Decimal("0.02")
    assert written[-1]["filled_quantity"] == Decimal("0.02")
    assert written[-1]["entry_average_price"] == Decimal("2000.10")
    assert adapter.last_local_mainnet_protection["status"] == "PROTECTED"


@pytest.mark.asyncio
async def test_local_mainnet_algo_submission_is_exact_fill_sized_and_never_reposted(monkeypatch):
    ledger = InMemoryLedger()
    adapter, authority = make_local_mainnet_adapter(monkeypatch, ledger)
    captured = []
    algo = {
        "algoId": 41,
        "clientAlgoId": "BAI-SL-local",
        "algoStatus": "NEW",
        "quantity": "0.2",
        "reduceOnly": True,
        "closePosition": False,
    }

    class MockRest:
        async def request(self, method, path, **kwargs):
            captured.append((method, path, kwargs))
            before = kwargs.get("before_mutation")
            if callable(before):
                await before()
            if method == "POST":
                return {"algoId": 41, "clientAlgoId": "BAI-SL-local"}
            if method == "GET":
                return dict(algo)
            raise AssertionError(f"Unexpected mutation {method} {path}")

    adapter.rest_client = MockRest()
    result = await adapter._submit_local_mainnet_protection_algo(
        symbol="ETHUSDC",
        side="SELL",
        position_side="BOTH",
        order_type="STOP_MARKET",
        trigger_price=Decimal("1900"),
        quantity=Decimal("0.2"),
        client_algo_id="BAI-SL-local",
        authority=authority,
        deadline=time.monotonic() + 4,
    )

    assert result == algo
    assert [item[0] for item in captured] == ["POST", "GET"]
    assert captured[0][2]["params"]["quantity"] == "0.2"
    assert captured[0][2]["params"]["reduceOnly"] == "true"
    assert captured[0][2]["params"]["closePosition"] == "false"


@pytest.mark.asyncio
async def test_local_mainnet_expired_first_fill_uses_one_durable_close_identity(monkeypatch):
    ledger = InMemoryLedger()
    entry_id = "entry-local-close"
    old_fill = datetime.now(timezone.utc) - timedelta(seconds=6)
    await add_local_mainnet_entry_fill(ledger, entry_id, event_time=old_fill)
    adapter, authority = make_local_mainnet_adapter(monkeypatch, ledger)
    intent = local_mainnet_intent(entry_id)
    order = local_mainnet_entry_order(entry_id)
    writes = []
    close_submissions = []
    close_proofs = []
    positions = [{
        "symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0.2",
        "entryPrice": "2000", "markPrice": "2000", "leverage": "1",
        "marginType": "ISOLATED", "unRealizedProfit": "0",
    }]
    close_response = {}

    async def persist(record):
        writes.append(dict(record))
        return True

    async def persist_close(record, proof):
        close_proofs.append((dict(record), dict(proof)))
        writes.append(
            {
                **record,
                "state": "CLOSED",
                "closure_evidence": {"kind": "LOCAL_EMERGENCY_CLOSE_VERIFIED"},
            }
        )
        return True

    class MockRest:
        async def request(self, method, path, **kwargs):
            assert kwargs.get("signed") is True
            if method == "GET" and path == "/fapi/v1/algoOrder":
                return None
            if method == "GET" and path == "/fapi/v1/openAlgoOrders":
                return []
            if method == "GET" and path == "/fapi/v1/openOrders":
                return []
            if method == "GET" and path == "/fapi/v2/positionRisk":
                return [dict(row) for row in positions]
            if method == "GET" and path == "/fapi/v1/order":
                return dict(close_response) if close_response else None
            raise AssertionError(f"Unexpected mock request {method} {path}")

    adapter.rest_client = MockRest()
    adapter.on_local_mainnet_protection_update = persist
    adapter.on_local_mainnet_close_verified = persist_close
    install_close_claim_fixture(adapter, authority)

    async def close_once(decision, *, allow_emergency_fallback, authority):
        close_submissions.append(decision)
        close_intent = decision.orders[0]
        close_order = ExecutionOrder(
            symbol="ETHUSDC",
            side=close_intent.side,
            quantity=close_intent.quantity,
            price=Decimal("1999"),
            order_type="MARKET",
            client_order_id=close_intent.client_order_id,
            status="FILLED",
            exchange_order_id="9001",
            position_side=PositionSide.BOTH,
            reduce_only=True,
            risk_class=EconomicRiskClass.EMERGENCY,
        )
        await ledger.upsert_order(close_order)
        now = datetime.now(timezone.utc)
        await ledger.append_fill(
            ExchangeFill(
                exchange_trade_id="close-trade-1",
                exchange_order_id="9001",
                client_order_id=close_order.client_order_id,
                symbol="ETHUSDC",
                side=OrderSide.SELL,
                position_side=PositionSide.BOTH,
                quantity=Decimal("0.2"),
                price=Decimal("1999"),
                commission=Decimal("0.01"),
                commission_asset="USDC",
                realized_pnl=Decimal("0"),
                maker=False,
                event_time=now,
                transaction_time=now,
                source="BINANCE_MAINNET_MOCK",
            )
        )
        close_response.update(
            {
                "symbol": "ETHUSDC",
                "orderId": "9001",
                "clientOrderId": close_order.client_order_id,
                "side": "SELL",
                "positionSide": "BOTH",
                "reduceOnly": True,
                "status": "FILLED",
                "origQty": "0.2",
                "executedQty": "0.2",
            }
        )
        positions[:] = [{"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0"}]
        return [close_order]

    adapter._execute_decision = close_once
    adapter._submit_local_mainnet_protection_algo = lambda **kwargs: pytest.fail(
        "expired protection deadline must not submit an Algo"
    )

    assert await adapter._protect_local_mainnet_entry(
        intent,
        order,
        {"status": "FILLED", "executedQty": "0.2"},
        authority=authority,
    ) is True
    assert len(close_submissions) == 1
    close_id = close_submissions[0].orders[0].client_order_id
    assert any(
        row["state"] == "CLOSE_PENDING"
        and f"local_close_client_order_id={close_id}" in row["state_reason"]
        for row in writes
    )
    assert writes[-1]["state"] == "CLOSED"
    assert close_proofs[-1][1]["algo_id"] == "LOCAL_EMERGENCY_CLOSE"
    assert close_proofs[-1][1]["order_id"] == "9001"
    assert writes[-1]["state_reason"].startswith(
        f"local_close_client_order_id={close_id};"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("live_amount", ["0.3", "-0.2"])
async def test_local_mainnet_emergency_close_blocks_unowned_position_mismatch(monkeypatch, live_amount):
    ledger = InMemoryLedger()
    entry_id = "entry-local-close-mismatch"
    await add_local_mainnet_entry_fill(ledger, entry_id)
    adapter, authority = make_local_mainnet_adapter(monkeypatch, ledger)
    intent = local_mainnet_intent(entry_id)
    order = local_mainnet_entry_order(entry_id)
    record = adapter._local_mainnet_protection_record(intent, order)
    record.update(filled_quantity=Decimal("0.2"), entry_average_price=Decimal("2000"))
    writes = []
    forbidden_mutations = []

    async def persist(updated):
        writes.append(dict(updated))
        return True

    class MockRest:
        async def request(self, method, path, **kwargs):
            if method == "GET" and path == "/fapi/v1/order":
                return None
            if method == "GET" and path == "/fapi/v2/positionRisk":
                # Either larger same-side exposure or the opposite direction
                # must fail before canceling protection or submitting a close.
                return [{"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": live_amount}]
            forbidden_mutations.append((method, path))
            raise AssertionError("mismatched owner must block before exchange mutation")

    adapter.rest_client = MockRest()
    adapter.on_local_mainnet_protection_update = persist

    async def forbidden_close(*_args, **_kwargs):
        forbidden_mutations.append(("CLOSE", "decision"))
        raise AssertionError("mismatched owner must not submit a close")

    adapter._execute_decision = forbidden_close
    assert await adapter._local_mainnet_close_only_once(
        intent, order, record, reason="protection_timeout", authority=authority
    ) is False
    assert forbidden_mutations == []
    assert writes[-1]["state"] == "UNKNOWN"
    assert writes[-1]["state_reason"] == "emergency_close_owner_position_mismatch"
    assert adapter.reconciliation.last_status == "UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.parametrize("known_exchange_id", ["9000", "", "9999"])
async def test_local_mainnet_emergency_close_can_reduce_exact_exchange_fill_without_trade_history(
    monkeypatch, known_exchange_id,
):
    ledger = InMemoryLedger()
    entry_id = "entry-local-fill-history-outage"
    adapter, authority = make_local_mainnet_adapter(monkeypatch, ledger)
    authority._mainnet_launch_id = "launch-fill-history-outage"
    intent = local_mainnet_intent(entry_id)
    order = local_mainnet_entry_order(entry_id)
    order.exchange_order_id = known_exchange_id or None
    record = adapter._local_mainnet_protection_record(intent, order)
    record["exchange_order_id"] = known_exchange_id
    writes = []
    close_submissions = []

    async def persist(updated):
        writes.append(dict(updated))
        return True

    terminal_entry = {
        "symbol": "ETHUSDC",
        "orderId": "9000",
        "clientOrderId": entry_id,
        "side": "BUY",
        "positionSide": "BOTH",
        "status": "CANCELED",
        "origQty": "0.2",
        "executedQty": "0.1",
    }

    class ReadOnlyRest:
        async def request(self, method, path, **kwargs):
            assert method == "GET" and kwargs.get("signed") is True
            if path == "/fapi/v1/order":
                client_id = kwargs["params"]["origClientOrderId"]
                if client_id == entry_id:
                    return dict(terminal_entry)
                assert client_id.startswith("BAI-")
                return None
            if path == "/fapi/v2/positionRisk":
                return [{
                    "symbol": "ETHUSDC",
                    "positionSide": "BOTH",
                    "positionAmt": "0.1",
                    "entryPrice": "2000",
                    "markPrice": "2000",
                    "leverage": "1",
                    "marginType": "ISOLATED",
                    "unRealizedProfit": "0",
                }]
            if path == "/fapi/v1/algoOrder":
                return None
            if path == "/fapi/v1/openAlgoOrders":
                return []
            raise AssertionError(f"Unexpected read-only request {path}")

    async def capture_close(decision, *, allow_emergency_fallback, authority):
        close_submissions.append(decision.orders[0])
        return []

    adapter.rest_client = ReadOnlyRest()
    adapter.on_local_mainnet_protection_update = persist
    adapter._execute_decision = capture_close
    install_close_claim_fixture(adapter, authority, record)

    verified = await adapter._local_mainnet_close_only_once(
        intent,
        order,
        record,
        reason="entry_fill_history_unavailable",
        authority=authority,
    )

    assert verified is False  # Without entry fills, never claim the owner CLOSED.
    assert len(close_submissions) == (1 if known_exchange_id == "9000" else 0)
    if close_submissions:
        assert close_submissions[0].quantity == Decimal("0.1")
        assert close_submissions[0].reduce_only is True
        assert any(
            "entry_order_id=9000;entry_executed_qty=0.1" in str(row.get("state_reason"))
            for row in writes
        )
    assert writes[-1]["state"] == "UNKNOWN"
    assert adapter.reconciliation.last_status == "UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_status", ["NEW", "PARTIALLY_FILLED"])
async def test_local_mainnet_emergency_close_rejects_nonterminal_unfilled_owner(
    monkeypatch, entry_status
):
    ledger = InMemoryLedger()
    entry_id = f"entry-local-nonterminal-{entry_status.lower()}"
    adapter, authority = make_local_mainnet_adapter(monkeypatch, ledger)
    authority._mainnet_launch_id = "launch-nonterminal-entry"
    intent = local_mainnet_intent(entry_id)
    order = local_mainnet_entry_order(entry_id)
    order.exchange_order_id = "9100"
    record = adapter._local_mainnet_protection_record(intent, order)
    record["exchange_order_id"] = "9100"
    close_submissions = []
    writes = []

    async def persist(updated):
        writes.append(dict(updated))
        return True

    class ReadOnlyRest:
        async def request(self, method, path, **kwargs):
            assert method == "GET" and kwargs.get("signed") is True
            if path == "/fapi/v1/order":
                client_id = kwargs["params"]["origClientOrderId"]
                if client_id == entry_id:
                    return {
                        "symbol": "ETHUSDC",
                        "orderId": "9100",
                        "clientOrderId": entry_id,
                        "side": "BUY",
                        "positionSide": "BOTH",
                        "status": entry_status,
                        "origQty": "0.2",
                        "executedQty": "0.1",
                    }
                return None
            if path == "/fapi/v2/positionRisk":
                pytest.fail("must not close while the entry can still fill")
            raise AssertionError(f"Unexpected request {path}")

    async def forbidden_close(decision, **_kwargs):
        close_submissions.append(decision)
        pytest.fail("nonterminal entry must not be followed by a close")

    adapter.rest_client = ReadOnlyRest()
    adapter.on_local_mainnet_protection_update = persist
    adapter._execute_decision = forbidden_close

    verified = await adapter._local_mainnet_close_only_once(
        intent,
        order,
        record,
        reason="entry_fill_history_unavailable",
        authority=authority,
    )

    assert verified is False
    assert close_submissions == []
    assert writes[-1]["state"] == "UNKNOWN"
    assert adapter.reconciliation.last_status == "UNKNOWN"


@pytest.mark.asyncio
async def test_local_mainnet_zero_fill_terminal_owner_has_reconciliation_proof(monkeypatch):
    ledger = InMemoryLedger()
    entry_id = "entry-local-unfilled"
    adapter, authority = make_local_mainnet_adapter(monkeypatch, ledger)
    authority._mainnet_launch_id = "launch-zero-fill-test"
    intent = local_mainnet_intent(entry_id)
    order = local_mainnet_entry_order(entry_id)
    order.status = "CANCELED"
    writes = []

    async def persist(record):
        writes.append(dict(record))
        return True

    class ReadOnlyRest:
        async def request(self, method, path, **kwargs):
            raise AssertionError(f"zero-fill proof must not need extra exchange calls: {method} {path}")

    adapter.rest_client = ReadOnlyRest()
    adapter.on_local_mainnet_protection_update = persist
    terminal_order = {
        "symbol": "ETHUSDC",
        "orderId": "9002",
        "clientOrderId": entry_id,
        "side": "BUY",
        "positionSide": "BOTH",
        "status": "CANCELED",
        "origQty": "0.2",
        "executedQty": "0",
    }
    assert await adapter._protect_local_mainnet_entry(
        intent, order, terminal_order, authority=authority
    ) is True
    owner = writes[-1]
    assert owner["state"] == "CLOSED"
    assert owner["filled_quantity"] == 0
    assert owner["state_reason"] == (
        "unfilled_entry_order_id=9002;unfilled_entry_status=CANCELED;"
        "unfilled_entry_executed_qty=0"
    )
    assert order.exchange_order_id == "9002"

    class ReadBackRest:
        async def request(self, method, path, **kwargs):
            assert method == "GET" and path == "/fapi/v1/order"
            assert kwargs["params"] == {"symbol": "ETHUSDC", "orderId": 9002}
            return dict(terminal_order)

    adapter.reconciliation.rest_client = ReadBackRest()
    assert await adapter.reconciliation._mainnet_unfilled_entry_proof_matches_owner(
        owner, launch_id=str(owner["mainnet_launch_id"])
    ) is True
    terminal_order["executedQty"] = "0.01"
    assert await adapter.reconciliation._mainnet_unfilled_entry_proof_matches_owner(
        owner, launch_id=str(owner["mainnet_launch_id"])
    ) is False


@pytest.mark.asyncio
async def test_local_mainnet_ambiguous_close_is_degraded_and_never_resubmitted(monkeypatch):
    ledger = InMemoryLedger()
    entry_id = "entry-local-ambiguous-close"
    await add_local_mainnet_entry_fill(ledger, entry_id)
    adapter, authority = make_local_mainnet_adapter(monkeypatch, ledger)
    intent = local_mainnet_intent(entry_id)
    order = local_mainnet_entry_order(entry_id)
    writes = []
    close_submissions = []

    async def persist(record):
        writes.append(dict(record))
        return True

    class MockRest:
        async def request(self, method, path, **kwargs):
            if method == "GET" and path == "/fapi/v1/algoOrder":
                return None
            if method == "GET" and path == "/fapi/v1/openAlgoOrders":
                return []
            if method == "GET" and path == "/fapi/v1/openOrders":
                return []
            if method == "GET" and path == "/fapi/v2/positionRisk":
                return [{
                    "symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0.2",
                    "entryPrice": "2000", "markPrice": "2000", "leverage": "1",
                    "marginType": "ISOLATED", "unRealizedProfit": "0",
                }]
            if method == "GET" and path == "/fapi/v1/order":
                return None
            raise AssertionError(f"Unexpected mock request {method} {path}")

    adapter.rest_client = MockRest()
    adapter.on_local_mainnet_protection_update = persist

    async def ambiguous_close(decision, **_kwargs):
        close_submissions.append(decision)
        return []

    adapter._execute_decision = ambiguous_close
    record = adapter._local_mainnet_protection_record(intent, order)
    record.update(filled_quantity=Decimal("0.2"), entry_average_price=Decimal("2000"))
    install_close_claim_fixture(adapter, authority, record)
    assert await adapter._local_mainnet_close_only_once(
        intent, order, record, reason="stop_timeout", authority=authority
    ) is False
    assert writes[-1]["state"] == "UNKNOWN"

    persisted_record = dict(writes[-1])
    assert await adapter._local_mainnet_close_only_once(
        intent, order, persisted_record, reason="restart_recovery", authority=authority
    ) is False
    assert len(close_submissions) == 1
    assert writes[-1]["state"] == "UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.parametrize("stalled_stage", ["cancel", "fill_recovery"])
async def test_local_protection_bounds_work_before_bracket_placement(monkeypatch, stalled_stage):
    ledger = InMemoryLedger()
    adapter, authority = make_local_mainnet_adapter(monkeypatch, ledger)
    intent = local_mainnet_intent("entry-stalled-preparation")
    order = local_mainnet_entry_order("entry-stalled-preparation")
    order.status = "PARTIALLY_FILLED" if stalled_stage == "cancel" else "FILLED"
    canceled = asyncio.Event()
    close_calls = []

    async def stalled(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    async def persist(record):
        return True

    async def close(*args, **kwargs):
        close_calls.append(kwargs["reason"])
        return False

    adapter.on_local_mainnet_protection_update = persist
    adapter._local_mainnet_close_only_once = close
    if stalled_stage == "cancel":
        adapter._cancel_testnet_entry_for_protection = stalled
    else:
        adapter.reconciliation._recover_order_fills = stalled
    started = time.monotonic()
    result = await asyncio.wait_for(adapter._protect_local_mainnet_entry(
        intent, order, {
            "executedQty": "0.1", "status": order.status,
            "time": int(time.time() * 1000),
        }, authority=authority,
    ), timeout=7)
    assert result is False
    assert canceled.is_set()
    assert close_calls == ["TimeoutError"]
    assert time.monotonic() - started < 7
    assert authority.pause_new_risk is True
