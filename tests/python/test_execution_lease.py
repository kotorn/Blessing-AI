import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.trading_worker.execution_lease import (
    InMemoryExecutionLease,
    InMemoryExecutionLeaseStore,
    LeaseLostError,
)
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.models import ConnectionState
from domain.enums import EconomicRiskClass, OrderSide, OrderType, PositionSide, TimeInForce


@pytest.mark.asyncio
async def test_execution_lease_serializes_same_account_scope_and_fences_old_owner():
    store = InMemoryExecutionLeaseStore()
    first = InMemoryExecutionLease("binance:BINANCE_MAINNET:account", "worker-a", store=store)
    second = InMemoryExecutionLease("binance:BINANCE_MAINNET:account", "worker-b", store=store)

    assert await asyncio.gather(first.acquire(), second.acquire()) == [True, False]
    first_token = first.fencing_token
    assert first_token == 1
    await first.assert_valid()

    await store.lose(first.scope_key)
    with pytest.raises(LeaseLostError):
        await first.assert_valid()

    assert await second.acquire() is True
    assert second.fencing_token == first_token + 1
    with pytest.raises(LeaseLostError):
        await first.assert_valid()
    await second.assert_valid()

    await second.release()
    third = InMemoryExecutionLease(
        "binance:BINANCE_MAINNET:account", "worker-c", store=store
    )
    assert await third.acquire() is True
    assert third.fencing_token == 3


@pytest.mark.asyncio
async def test_lease_loss_is_checked_immediately_before_order_post(monkeypatch):
    monkeypatch.setenv("EXECUTION_LEASE_REQUIRED", "true")

    class FailingLease:
        async def assert_valid(self):
            raise LeaseLostError("fenced by replacement worker")

    class RestProbe:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            return {"orderId": 1}

    adapter = BinanceExecutionAdapter(
        api_key="unit-test-key",
        api_secret="unit-test-secret",
        env=BinanceEnvironment.TESTNET,
    )
    adapter.state = ConnectionState.READY
    adapter.rest_client = RestProbe()
    adapter.set_execution_lease(FailingLease(), required=True)

    async def allow_order(*args, **kwargs):
        return SimpleNamespace(
            allowed=True,
            reason="unit-test",
            prepared=SimpleNamespace(
                symbol="BTCUSDT",
                order_type="LIMIT",
                quantity=Decimal("0.001"),
                price=Decimal("100"),
                estimated_price=Decimal("100"),
                notional=Decimal("0.1"),
            ),
        )

    adapter.order_gate.check = allow_order

    authority = SimpleNamespace(
        kill_switch_active=False,
        _evaluate_execution_gate=lambda decision: (True, "unit-test"),
    )
    adapter.bind_worker_authority(authority)
    decision = SimpleNamespace(
        action="SUBMIT_ORDER",
        decision_id="LEASE-FENCE-1",
        symbol="BTCUSDT",
        risk_class=EconomicRiskClass.NEW_RISK,
        target_exposure_id=None,
        source_intent_ids=[],
        orders=[
            SimpleNamespace(
                client_order_id="LEASE-FENCE-ORDER",
                side=OrderSide.BUY,
                position_side=PositionSide.BOTH,
                time_in_force=TimeInForce.GTC,
                post_only=False,
                reduce_only=False,
            )
        ],
    )

    executed = await adapter.execute_decision(decision, authority=authority)

    assert executed == []
    assert adapter.rest_client.calls == []
    assert adapter.state == ConnectionState.DEGRADED


@pytest.mark.asyncio
async def test_risk_increasing_order_posts_only_after_durable_outbox_barrier():
    events = []

    class RestProbe:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, **kwargs):
            self.calls.append((method, path, kwargs))
            assert events == ["PENDING"]
            return {
                "orderId": 7,
                "clientOrderId": kwargs["params"]["newClientOrderId"],
                "status": "NEW",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "positionSide": "BOTH",
                "origQty": kwargs["params"]["quantity"],
                "price": kwargs["params"]["price"],
            }

    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET)
    adapter.state = ConnectionState.READY
    adapter.rest_client = RestProbe()

    async def durable_barrier(order):
        events.append(order.status)
        return True

    adapter.before_order_submission = durable_barrier
    adapter._post_mutation_reconcile = _always_verified_reconcile  # type: ignore[method-assign]

    async def allow_order(*args, **kwargs):
        return SimpleNamespace(
            allowed=True,
            reason="unit-test",
            prepared=SimpleNamespace(
                symbol="BTCUSDT",
                order_type="LIMIT",
                quantity=Decimal("0.001"),
                price=Decimal("100"),
                estimated_price=Decimal("100"),
                notional=Decimal("0.1"),
            ),
        )

    adapter.order_gate.check = allow_order
    authority = SimpleNamespace(
        kill_switch_active=False,
        _evaluate_execution_gate=lambda decision: (True, "unit-test"),
    )
    adapter.bind_worker_authority(authority)
    decision = SimpleNamespace(
        action="SUBMIT_ORDER",
        decision_id="OUTBOX-BARRIER-1",
        symbol="BTCUSDT",
        risk_class=EconomicRiskClass.NEW_RISK,
        target_exposure_id=None,
        source_intent_ids=[],
        orders=[
            SimpleNamespace(
                client_order_id="OUTBOX-ORDER-1",
                symbol="BTCUSDT",
                market_type="USDM_FUTURES",
                side=OrderSide.BUY,
                position_side=PositionSide.BOTH,
                time_in_force=TimeInForce.GTC,
                post_only=False,
                reduce_only=False,
                strategy_id="grid",
                source_intent_ids=[],
            )
        ],
    )

    executed = await adapter.execute_decision(decision, authority=authority)

    assert events == ["PENDING"]
    assert len(executed) == 1
    assert adapter.rest_client.calls[0][0:2] == ("POST", "/fapi/v1/order")


async def _always_verified_reconcile(*args, **kwargs):
    return True
