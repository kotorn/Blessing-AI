import asyncio
from decimal import Decimal

import pytest

from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.execution_lease import LeaseLostError
from apps.trading_worker.venues.binance.gates import GateResult, PreparedOrder
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.models import (
    BinanceDefinitiveRejection,
    BinanceTransportAmbiguity,
    ConnectionState,
    ExchangeAccountSnapshot,
)
from domain.enums import EconomicRiskClass, OrderSide, OrderType, PositionSide, TimeInForce
from domain.models import ExecutionDecision, MarketType, OrderIntent, utc_now

pytestmark = pytest.mark.asyncio

class MockRestClient:
    def __init__(self):
        self.calls = []
        self.before_mutation_hook = None

    async def request(self, method, path, **kwargs):
        before_mutation = kwargs.get("before_mutation")
        if callable(before_mutation):
            if self.before_mutation_hook is not None:
                await self.before_mutation_hook()
            await before_mutation()
        self.calls.append((method, path))
        if method == "POST" and "order" in path:
            raise BinanceTransportAmbiguity("Timeout ambiguity simulated")
        if method == "GET" and "order" in path:
            raise BinanceDefinitiveRejection(-2013, "Order does not exist")
        return {}
        
    async def init_session(self): pass
    async def close(self):
        pass


class MockUserStream:
    is_connected = True

    async def close(self):
        self.is_connected = False


class MockReconciliation:
    last_status = "IN_SYNC"
    authentication_failed = False

    def __init__(self):
        self.calls = 0

    async def reconcile(self):
        self.calls += 1
        return self.last_status

    async def _recover_order_fills(self, order, response):
        return None

@pytest.fixture
def adapter():
    ledger = InMemoryLedger()
    # These unit tests exercise the private generic recovery method directly;
    # worker-created Testnet adapters enable the stricter lifecycle flag.
    ada = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    ada.require_testnet_protection = False
    ada.rest_client = MockRestClient()
    ada.capabilities.account_request_succeeded = True
    ada.capabilities.authenticated = True
    ada.capabilities.trade_authorized = True
    ada.capabilities.hedge_mode = False
    ada.user_stream = MockUserStream()
    ada.reconciliation = MockReconciliation()

    # Inject a mock rule
    from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules

    rules = SymbolTradingRules("BTCUSDT")
    rules.status = "TRADING"
    rules.supported_order_types = ["LIMIT", "MARKET"]
    rules.step_size = Decimal("0.001")
    rules.tick_size = Decimal("0.1")
    rules.min_qty = Decimal("0.001")
    rules.max_qty = Decimal(100)
    rules.min_notional = Decimal(5)
    ada.capabilities.symbol_rules["BTCUSDT"] = rules
    ada.last_market_event_at["BTCUSDT"] = utc_now()
    ada.last_market_event_source["BTCUSDT"] = "BINANCE_TESTNET_WS"
    ada.last_market_event_venue["BTCUSDT"] = "BINANCE_TESTNET"
    ada.last_market_event_market_type["BTCUSDT"] = "USDM_FUTURES"
    ledger.account_snapshot = ExchangeAccountSnapshot(
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
        liquidation_safety="KNOWN",
        exchange_environment="BINANCE_TESTNET",
        valid=True,
        timestamp=utc_now(),
    )
    return ada

async def test_timeout_ambiguity_handling(adapter):
    adapter.state = ConnectionState.READY
    
    intent = OrderIntent(
        client_order_id="TEST-1",
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.001"),
        price=Decimal("10000.0"),
    )
    decision = ExecutionDecision(
        decision_id="D1",
        symbol="BTCUSDT",
        action="SUBMIT",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[intent],
    )
    
    authority = object()
    adapter.bind_worker_authority(authority)
    await adapter._execute_decision(decision, authority=authority)

    # The ambiguous POST is never blindly retried. A single authoritative query
    # confirms absence, then reconciliation is required before READY returns.
    assert adapter.state == ConnectionState.DEGRADED
    assert adapter.rest_client.calls.count(("POST", "/fapi/v1/order")) == 1
    assert adapter.rest_client.calls.count(("GET", "/fapi/v1/order")) == 3
    assert adapter.reconciliation.calls == 1


async def test_ambiguous_order_terminal_without_fill_is_not_confirmed(adapter):
    class CanceledRestClient:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, **kwargs):
            self.calls.append((method, path))
            if method == "POST" and "order" in path:
                raise BinanceTransportAmbiguity("Timeout ambiguity simulated")
            if method == "GET" and "order" in path:
                return {
                    "orderId": 556,
                    "status": "CANCELED",
                    "executedQty": "0",
                    "symbol": "BTCUSDT",
                    "clientOrderId": "TEST-CANCELED",
                    "side": "BUY",
                    "positionSide": "BOTH",
                    "origQty": "0.001",
                    "price": "10000.0",
                }
            return {}

        async def init_session(self):
            pass

        async def close(self):
            pass

    adapter.rest_client = CanceledRestClient()
    adapter.state = ConnectionState.READY
    notifications: list[tuple[str, str]] = []

    async def record_notification(order, outcome):
        notifications.append((order.client_order_id, outcome))

    adapter.on_order_submission_result = record_notification
    intent = OrderIntent(
        client_order_id="TEST-CANCELED",
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.001"),
        price=Decimal("10000.0"),
    )
    decision = ExecutionDecision(
        decision_id="D-CANCELED",
        symbol="BTCUSDT",
        action="SUBMIT",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[intent],
    )

    authority = object()
    adapter.bind_worker_authority(authority)
    executed = await adapter._execute_decision(decision, authority=authority)

    assert executed == []
    assert adapter.state == ConnectionState.DEGRADED
    assert ("TEST-CANCELED", "AMBIGUOUS") in notifications
    assert ("TEST-CANCELED", "CONFIRMED") not in notifications
    assert adapter.rest_client.calls.count(("POST", "/fapi/v1/order")) == 1


async def test_ambiguous_order_confirmed_filled_notifies_confirmed(adapter):
    """An ambiguous POST later confirmed FILLED must still notify CONFIRMED."""

    class ConfirmingRestClient:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, **kwargs):
            self.calls.append((method, path))
            if method == "POST" and "order" in path:
                raise BinanceTransportAmbiguity("Timeout ambiguity simulated")
            if method == "GET" and "order" in path:
                return {
                    "orderId": 555,
                    "status": "FILLED",
                    "symbol": "BTCUSDT",
                    "clientOrderId": "TEST-1",
                    "side": "BUY",
                    "positionSide": "BOTH",
                    "origQty": "0.001",
                    "price": "10000.0",
                }
            return {}

        async def init_session(self):
            pass

        async def close(self):
            pass

    adapter.rest_client = ConfirmingRestClient()
    adapter.state = ConnectionState.READY
    notifications: list[tuple[str, str]] = []

    async def record_notification(order, outcome):
        notifications.append((order.client_order_id, outcome))

    adapter.on_order_submission_result = record_notification

    intent = OrderIntent(
        client_order_id="TEST-1",
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.001"),
        price=Decimal("10000.0"),
    )
    decision = ExecutionDecision(
        decision_id="D1",
        symbol="BTCUSDT",
        action="SUBMIT",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[intent],
    )

    authority = object()
    adapter.bind_worker_authority(authority)
    executed = await adapter._execute_decision(decision, authority=authority)

    assert adapter.state == ConnectionState.READY
    assert len(executed) == 1
    assert executed[0].status == "FILLED"
    # The order was genuinely recovered and verified -- it must be reported
    # CONFIRMED just like the direct (non-ambiguous) success path does,
    # not left as a dangling AMBIGUOUS with no follow-up.
    assert ("TEST-1", "AMBIGUOUS") in notifications
    assert ("TEST-1", "CONFIRMED") in notifications


async def test_direct_adapter_mutation_is_blocked(adapter):
    adapter.state = ConnectionState.READY
    decision = ExecutionDecision(
        decision_id="DIRECT-BLOCK",
        symbol="BTCUSDT",
        action="SUBMIT",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[],
    )

    assert await adapter.execute_decision(decision) == []
    assert adapter.rest_client.calls == []


@pytest.mark.asyncio
async def test_private_adapter_mutation_also_requires_worker_authority(adapter):
    decision = ExecutionDecision(
        decision_id="PRIVATE-DIRECT-BLOCK",
        symbol="BTCUSDT",
        action="SUBMIT",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[],
    )

    assert await adapter._execute_decision(decision) == []
    assert adapter.rest_client.calls == []


async def test_local_mainnet_risk_increase_requires_fresh_supervisor_heartbeat(adapter, monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    adapter.env = BinanceEnvironment.MAINNET
    adapter.bind_worker_authority(
        type("StaleSupervisor", (), {"local_supervisor_heartbeat_is_fresh": lambda self: False})()
    )

    with pytest.raises(LeaseLostError, match="supervisor heartbeat"):
        await adapter._assert_execution_lease(EconomicRiskClass.NEW_RISK)

    assert adapter.rest_client.calls == []


async def test_local_mainnet_order_is_blocked_if_worker_disarms_during_rest_throttle(
    adapter, monkeypatch
):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    adapter.env = BinanceEnvironment.MAINNET
    adapter.execution_lease_required = False
    adapter.state = ConnectionState.READY
    authority_state = {"allowed": True}

    class LocalAuthority:
        def local_supervisor_heartbeat_is_fresh(self):
            return True

        def _evaluate_execution_gate(self, _decision):
            return authority_state["allowed"], "worker disarmed during REST throttle"

    authority = LocalAuthority()
    adapter.bind_worker_authority(authority)

    async def durable_outbox_barrier(_order):
        return True

    async def worker_disarms_during_throttle():
        authority_state["allowed"] = False

    adapter.before_order_submission = durable_outbox_barrier
    adapter.rest_client.before_mutation_hook = worker_disarms_during_throttle
    intent = OrderIntent(
        client_order_id="LOCAL-RACE-1",
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.001"),
        price=Decimal("10000.0"),
    )
    decision = ExecutionDecision(
        decision_id="LOCAL-RACE",
        symbol="BTCUSDT",
        action="SUBMIT",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[intent],
    )

    result = await adapter.execute_decision(decision, authority=authority)

    assert result == []
    assert adapter.rest_client.calls == []


async def test_mainnet_rest_boundary_rechecks_order_specific_risk_gate(adapter, monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    adapter.env = BinanceEnvironment.MAINNET
    adapter.execution_lease_required = False
    adapter.state = ConnectionState.READY

    class MainnetAuthority:
        _mainnet_launch_id = "unit-mainnet-launch"

        def local_supervisor_heartbeat_is_fresh(self):
            return True

        def _evaluate_execution_gate(self, _decision):
            return True, "passed"

    adapter.bind_worker_authority(MainnetAuthority())

    async def durable_protection_owner(_record):
        return True

    async def durable_close_proof(_record, _proof):
        return True

    adapter.on_local_mainnet_protection_update = durable_protection_owner
    adapter.on_local_mainnet_close_verified = durable_close_proof
    prepared = PreparedOrder(
        symbol="BTCUSDT",
        order_type="LIMIT",
        quantity=Decimal("0.001"),
        price=Decimal("10000.0"),
        estimated_price=Decimal("10000.0"),
        notional=Decimal("10.0"),
    )

    class ChangingOrderGate:
        calls = 0

        async def check(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return GateResult(True, "initially passed", prepared)
            return GateResult(False, "market/account evidence became stale")

    gate = ChangingOrderGate()
    adapter.order_gate = gate

    async def durable_outbox_barrier(_order):
        return True

    adapter.before_order_submission = durable_outbox_barrier
    intent = OrderIntent(
        client_order_id="MAINNET-FINAL-GATE-1",
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.001"),
        price=Decimal("10000.0"),
        basket_id="unit-basket",
        stop_loss_price=Decimal("9000"),
        take_profit_price=Decimal("12000"),
    )
    decision = ExecutionDecision(
        decision_id="MAINNET-FINAL-GATE",
        symbol="BTCUSDT",
        action="SUBMIT",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[intent],
    )

    result = await adapter.execute_decision(decision, authority=adapter._worker_authority)

    assert result == []
    assert gate.calls == 2
    assert adapter.rest_client.calls == []


async def test_mainnet_gate_and_durable_outbox_share_exchange_client_identity(adapter, monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    adapter.env = BinanceEnvironment.MAINNET
    adapter.execution_lease_required = False
    adapter.state = ConnectionState.READY

    class Authority:
        _mainnet_launch_id = "unit-mainnet-launch"

        def local_supervisor_heartbeat_is_fresh(self):
            return True

        def _evaluate_execution_gate(self, _decision):
            return True, "passed"

    authority = Authority()
    adapter.bind_worker_authority(authority)

    async def durable_protection_owner(_record):
        return True

    async def durable_close_proof(_record, _proof):
        return True

    adapter.on_local_mainnet_protection_update = durable_protection_owner
    adapter.on_local_mainnet_close_verified = durable_close_proof
    seen = []
    prepared = PreparedOrder(
        symbol="ETHUSDC", order_type="LIMIT", quantity=Decimal("0.01"),
        price=Decimal("2000"), estimated_price=Decimal("2000"),
        notional=Decimal("20"),
    )

    class CapturingGate:
        async def check(self, intent, *_args, **_kwargs):
            seen.append(("gate", intent.client_order_id))
            return (
                GateResult(True, "passed", prepared)
                if len([event for event in seen if event[0] == "gate"]) == 1
                else GateResult(False, "fenced before transport")
            )

    async def durable_barrier(order):
        seen.append(("outbox", order.client_order_id))
        return True

    adapter.order_gate = CapturingGate()
    adapter.before_order_submission = durable_barrier
    intent = OrderIntent(
        client_order_id="TRANSIENT-STRATEGY-ID", symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES, side=OrderSide.BUY,
        position_side=PositionSide.BOTH, order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC, quantity=Decimal("0.01"),
        price=Decimal("2000"),
        basket_id="unit-basket", stop_loss_price=Decimal("1900"),
        take_profit_price=Decimal("2200"),
    )
    decision = ExecutionDecision(
        decision_id="IDENTITY-1", symbol="ETHUSDC", action="SUBMIT",
        risk_class=EconomicRiskClass.NEW_RISK, orders=[intent],
    )
    # No transport is configured for success. This test checks the identity
    # before the network boundary, never sends an exchange order.
    await adapter.execute_decision(decision, authority=authority)

    assert seen[0][0] == "gate"
    assert seen[1][0] == "outbox"
    assert seen[0][1] == seen[1][1]
    assert seen[0][1] != "TRANSIENT-STRATEGY-ID"
    assert adapter.rest_client.calls == []


async def test_adapter_close_waits_for_active_mutation_lock(adapter):
    await adapter.mutation_lock.acquire()
    closing = asyncio.create_task(adapter.close())
    await asyncio.sleep(0)

    assert closing.done() is False
    adapter.mutation_lock.release()
    await closing

    assert adapter.state == ConnectionState.DISCONNECTED


def test_parse_order_response_handles_zero_avg_price_for_market_order(adapter):
    intent = OrderIntent(
        client_order_id="TEST-CLIENT-ORDER-ID",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.012"),
    )
    prepared = type(
        "PreparedStub",
        (),
        {
            "symbol": "ETHUSDC",
            "side": OrderSide.BUY,
            "order_type": OrderType.MARKET,
            "quantity": Decimal("0.012"),
            "price": None,
            "estimated_price": Decimal("2625.50"),
        },
    )()
    # Typical Binance response for market order acknowledgment before full match details
    response = {
        "orderId": 12345678,
        "symbol": "ETHUSDC",
        "clientOrderId": "TEST-CLIENT-ORDER-ID",
        "status": "NEW",
        "origQty": "0.012",
        "price": "0",
        "avgPrice": "0.00000",
    }
    parsed = adapter._order_from_response(intent, response, prepared, "TEST-CLIENT-ORDER-ID")
    assert parsed.price == Decimal("2625.50")
    assert parsed.exchange_order_id == "12345678"
    assert parsed.status == "NEW"
