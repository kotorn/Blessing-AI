from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.models import (
    BinanceDefinitiveRejection,
    BinanceTransportAmbiguity,
    ConnectionState,
)


class OfflineRest:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return await self.handler(method, path, kwargs)


class OfflineStream:
    is_connected = True


class OfflineReconciliation:
    def __init__(self):
        self.last_status = "IN_SYNC"
        self.fill_recovery_calls = []

    async def reconcile(self):
        self.last_status = "IN_SYNC"
        return self.last_status

    async def _recover_order_fills(self, order, response):
        self.fill_recovery_calls.append((order.client_order_id, response["orderId"]))


def make_adapter(rest, monkeypatch):
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    adapter = BinanceExecutionAdapter(
        api_key="unit-test-key",
        api_secret="unit-test-secret",
        env=BinanceEnvironment.MAINNET,
        ledger=InMemoryLedger(),
    )
    adapter.rest_client = rest
    adapter.user_stream = OfflineStream()
    adapter.reconciliation = OfflineReconciliation()
    adapter.state = ConnectionState.READY
    adapter.capabilities.account_request_succeeded = True
    adapter.capabilities.authenticated = True
    adapter.capabilities.trade_authorized = True
    adapter.capabilities.hedge_mode = False
    adapter.bind_worker_authority(object())
    return adapter


def response_for(client_order_id):
    return {
        "symbol": "ETHUSDC",
        "orderId": 845,
        "clientOrderId": client_order_id,
        "side": "SELL",
        "positionSide": "BOTH",
        "status": "FILLED",
        "origQty": "0.1",
        "executedQty": "0.1",
        "avgPrice": "2000.10",
        "reduceOnly": True,
    }


def claimed_session(adapter, launch_id="launch-abc", *, attempts=1):
    decision_id = f"PILOT-SESSION-CLOSE-{launch_id}"
    return {
        "client_order_id": adapter._generate_client_order_id(
            decision_id, "ETHUSDC", order_index=0
        ),
        "side": "SELL",
        "position_side": "BOTH",
        "quantity": "0.1",
        "attempt_count": attempts,
    }


async def unused_claim(**_kwargs):
    raise AssertionError("an existing session claim must not be recreated")


async def unused_attempt(**_kwargs):
    raise AssertionError("this test must not submit another close")


@pytest.mark.asyncio
async def test_restart_recovers_exact_close_id_and_never_submits_again(monkeypatch):
    launch_id = "launch-abc"
    response = None
    rest_calls = []

    async def handler(method, path, kwargs):
        nonlocal response
        rest_calls.append((method, path, kwargs))
        if method == "GET" and path == "/fapi/v1/order":
            return response
        if method == "GET" and path == "/fapi/v2/positionRisk":
            return []
        raise AssertionError(f"Unexpected REST request: {method} {path}")

    adapter = make_adapter(OfflineRest(handler), monkeypatch)
    claim = claimed_session(adapter, launch_id)
    response = response_for(claim["client_order_id"])

    async def must_not_submit(*_args, **_kwargs):
        raise AssertionError("a recovered close must never be submitted again")

    adapter._execute_decision = must_not_submit
    flattened = await adapter._emergency_flatten(
        "ETHUSDC",
        authority=adapter._worker_authority,
        pilot_session_id=launch_id,
        pilot_close_claim=claim,
        pilot_close_claim_callback=unused_claim,
        pilot_close_attempt_callback=unused_attempt,
    )

    assert len(flattened) == 1
    assert flattened[0].client_order_id == claim["client_order_id"]
    assert adapter.reconciliation.fill_recovery_calls == [(claim["client_order_id"], 845)]
    assert [(method, path) for method, path, _ in rest_calls] == [
        ("GET", "/fapi/v1/order"),
        ("GET", "/fapi/v2/positionRisk"),
        ("GET", "/fapi/v2/positionRisk"),
    ]
    assert adapter.last_emergency_result["status"] == "CONFIRMED"


@pytest.mark.asyncio
async def test_ambiguous_exact_id_lookup_blocks_close_resend(monkeypatch):
    async def handler(method, path, _kwargs):
        if method == "GET" and path == "/fapi/v1/order":
            raise BinanceTransportAmbiguity("simulated order lookup timeout")
        raise AssertionError("position read and close POST must not follow an ambiguous lookup")

    adapter = make_adapter(OfflineRest(handler), monkeypatch)
    claim = claimed_session(adapter, attempts=1)
    sends = []

    async def should_not_send(decision, **_kwargs):
        sends.extend(decision.orders)
        return []

    adapter._execute_decision = should_not_send
    flattened = await adapter._emergency_flatten(
        "ETHUSDC",
        authority=adapter._worker_authority,
        pilot_session_id="launch-abc",
        pilot_close_claim=claim,
        pilot_close_claim_callback=unused_claim,
        pilot_close_attempt_callback=unused_attempt,
    )

    assert flattened == []
    assert sends == []
    assert adapter.last_emergency_result["status"] == "UNKNOWN"
    assert adapter.last_emergency_result["client_order_id"] == claim["client_order_id"]


@pytest.mark.asyncio
async def test_new_close_claim_and_attempt_are_durable_before_submit(monkeypatch):
    operations = []
    position_reads = 0

    async def handler(method, path, _kwargs):
        nonlocal position_reads
        if method == "GET" and path == "/fapi/v1/order":
            raise BinanceDefinitiveRejection(-2013, "Order does not exist")
        if method == "GET" and path == "/fapi/v2/positionRisk":
            position_reads += 1
            if position_reads == 1:
                return [{
                    "symbol": "ETHUSDC", "positionSide": "BOTH",
                    "positionAmt": "0.1", "markPrice": "2000",
                    "entryPrice": "1980", "unRealizedProfit": "2",
                }]
            return []
        raise AssertionError(f"Unexpected REST request: {method} {path}")

    adapter = make_adapter(OfflineRest(handler), monkeypatch)

    async def save_claim(*, client_order_id, side, position_side, quantity):
        operations.append(("claim", client_order_id, side, position_side, quantity))
        return {
            "pilot_session_close_client_order_id": client_order_id,
            "pilot_session_close_attempt_count": 0,
        }

    async def save_attempt(*, client_order_id, attempt):
        operations.append(("attempt", client_order_id, attempt))
        return {"pilot_session_close_attempt_count": attempt}

    async def submit(decision, **_kwargs):
        operations.append(("submit", decision.orders[0].client_order_id))
        return [SimpleNamespace(client_order_id=decision.orders[0].client_order_id)]

    adapter._execute_decision = submit
    await adapter._emergency_flatten(
        "ETHUSDC",
        authority=adapter._worker_authority,
        pilot_session_id="launch-new",
        pilot_close_claim=None,
        pilot_close_claim_callback=save_claim,
        pilot_close_attempt_callback=save_attempt,
    )

    assert [entry[0] for entry in operations] == ["claim", "attempt", "submit"]
    assert operations[0][1] == operations[1][1] == operations[2][1]


@pytest.mark.asyncio
async def test_ambiguous_post_restart_recovers_same_claim_without_second_submit(monkeypatch):
    launch_id = "launch-ambiguous"
    accepted_order = None
    claim_record = None
    attempt_count = 0
    submit_calls = []
    position_reads = 0

    async def first_handler(method, path, _kwargs):
        nonlocal position_reads
        if method == "GET" and path == "/fapi/v1/order":
            raise BinanceDefinitiveRejection(-2013, "Order does not exist")
        if method == "GET" and path == "/fapi/v2/positionRisk":
            position_reads += 1
            if position_reads == 1:
                return [{
                    "symbol": "ETHUSDC", "positionSide": "BOTH",
                    "positionAmt": "0.1", "markPrice": "2000",
                    "entryPrice": "1980", "unRealizedProfit": "2",
                }]
            return []
        raise AssertionError(f"Unexpected first-worker REST request: {method} {path}")

    first = make_adapter(OfflineRest(first_handler), monkeypatch)

    async def persist_claim(*, client_order_id, side, position_side, quantity):
        nonlocal claim_record
        claim_record = {
            "client_order_id": client_order_id,
            "side": side,
            "position_side": position_side,
            "quantity": str(quantity),
            "attempt_count": attempt_count,
        }
        return {
            "pilot_session_close_client_order_id": client_order_id,
            "pilot_session_close_attempt_count": attempt_count,
        }

    async def persist_attempt(*, client_order_id, attempt):
        nonlocal attempt_count
        assert claim_record["client_order_id"] == client_order_id
        attempt_count = attempt
        return {"pilot_session_close_attempt_count": attempt_count}

    async def lost_post_response(decision, **_kwargs):
        nonlocal accepted_order
        submit_calls.append(decision.orders[0].client_order_id)
        accepted_order = response_for(decision.orders[0].client_order_id)
        raise BinanceTransportAmbiguity("simulated accepted POST with lost response")

    first._execute_decision = lost_post_response
    with pytest.raises(BinanceTransportAmbiguity, match="lost response"):
        await first._emergency_flatten(
            "ETHUSDC",
            authority=first._worker_authority,
            pilot_session_id=launch_id,
            pilot_close_claim=None,
            pilot_close_claim_callback=persist_claim,
            pilot_close_attempt_callback=persist_attempt,
        )
    assert claim_record is not None and attempt_count == 1

    async def recovered_handler(method, path, _kwargs):
        if method == "GET" and path == "/fapi/v1/order":
            return accepted_order
        if method == "GET" and path == "/fapi/v2/positionRisk":
            return []
        raise AssertionError(f"Unexpected recovered-worker REST request: {method} {path}")

    recovered = make_adapter(OfflineRest(recovered_handler), monkeypatch)

    async def never_claim_or_submit(**_kwargs):
        raise AssertionError("recovery must use the existing durable claim")

    flattened = await recovered._emergency_flatten(
        "ETHUSDC",
        authority=recovered._worker_authority,
        pilot_session_id=launch_id,
        pilot_close_claim=claim_record,
        pilot_close_claim_callback=never_claim_or_submit,
        pilot_close_attempt_callback=never_claim_or_submit,
    )

    assert len(flattened) == 1
    assert flattened[0].client_order_id == claim_record["client_order_id"]
    assert submit_calls == [claim_record["client_order_id"]]
    assert recovered.reconciliation.fill_recovery_calls == [
        (claim_record["client_order_id"], 845)
    ]


@pytest.mark.asyncio
async def test_restart_retries_same_intent_only_after_three_definitive_absence_reads(monkeypatch):
    launch_id = "launch-abc"
    query_count = 0
    order_submissions = []
    position_reads = 0
    attempt_markers = []

    async def handler(method, path, _kwargs):
        nonlocal query_count, position_reads
        if method == "GET" and path == "/fapi/v1/order":
            query_count += 1
            raise BinanceDefinitiveRejection(-2013, "Order does not exist")
        if method == "GET" and path == "/fapi/v2/positionRisk":
            position_reads += 1
            if position_reads == 1:
                    return [{
                        "symbol": "ETHUSDC", "positionSide": "BOTH",
                        "positionAmt": "0.1", "markPrice": "2000",
                        "entryPrice": "1980", "unRealizedProfit": "2",
                    }]
            return []
        raise AssertionError(f"Unexpected REST request: {method} {path}")

    adapter = make_adapter(OfflineRest(handler), monkeypatch)
    claim = claimed_session(adapter, attempts=1)

    async def persist_attempt(*, client_order_id, attempt):
        attempt_markers.append((client_order_id, attempt))
        return {"pilot_session_close_attempt_count": attempt}

    async def submit_same_intent(decision, **_kwargs):
        order_submissions.append(decision.orders[0])
        return [SimpleNamespace(client_order_id=decision.orders[0].client_order_id)]

    adapter._execute_decision = submit_same_intent
    await adapter._emergency_flatten(
        "ETHUSDC",
        authority=adapter._worker_authority,
        pilot_session_id=launch_id,
        pilot_close_claim=claim,
        pilot_close_claim_callback=unused_claim,
        pilot_close_attempt_callback=persist_attempt,
    )

    assert query_count == 3
    assert attempt_markers == [(claim["client_order_id"], 2)], adapter.last_emergency_result
    assert len(order_submissions) == 1
    assert order_submissions[0].client_order_id == claim["client_order_id"]
    assert order_submissions[0].quantity == Decimal("0.1")
    assert adapter.last_emergency_result["status"] == "CONFIRMED"
