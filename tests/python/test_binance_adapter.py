from decimal import Decimal

import pytest

from domain.enums import EconomicRiskClass, OrderSide, OrderType, PositionSide, TimeInForce
from domain.models import ExecutionDecision, OrderIntent, MarketType, utc_now
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.models import (
    BinanceDefinitiveRejection,
    BinanceTransportAmbiguity,
    ConnectionState,
    ExchangeAccountSnapshot,
)

pytestmark = pytest.mark.asyncio

class MockRestClient:
    def __init__(self):
        self.calls = []

    async def request(self, method, path, **kwargs):
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

@pytest.fixture
def adapter():
    ledger = InMemoryLedger()
    ada = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    ada.rest_client = MockRestClient()
    ada.capabilities.account_request_succeeded = True
    ada.capabilities.authenticated = True
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
    rules.max_qty = Decimal("100")
    rules.min_notional = Decimal("5")
    ada.capabilities.symbol_rules["BTCUSDT"] = rules
    ada.last_market_event_at["BTCUSDT"] = utc_now()
    ada.last_market_event_source["BTCUSDT"] = "BINANCE_TESTNET_WS"
    ada.last_market_event_venue["BTCUSDT"] = "BINANCE_TESTNET"
    ada.last_market_event_market_type["BTCUSDT"] = "USDM_FUTURES"
    ledger.account_snapshot = ExchangeAccountSnapshot(
        wallet_balance=Decimal("100"),
        margin_balance=Decimal("100"),
        available_balance=Decimal("90"),
        unrealized_pnl=Decimal("0"),
        total_initial_margin=Decimal("10"),
        total_maint_margin=Decimal("5"),
        position_initial_margin=Decimal("10"),
        total_position_notional=Decimal("0"),
        effective_leverage=Decimal("0"),
        margin_utilization_pct=Decimal("10"),
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
    assert adapter.state == ConnectionState.READY
    assert adapter.rest_client.calls.count(("POST", "/fapi/v1/order")) == 1
    assert adapter.rest_client.calls.count(("GET", "/fapi/v1/order")) == 3
    assert adapter.reconciliation.calls == 1


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

