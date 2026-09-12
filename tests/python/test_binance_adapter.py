import pytest
from decimal import Decimal
import asyncio
from domain.models import ExecutionDecision, OrderIntent, OrderSide, PositionSide, MarketType
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.models import ConnectionState

pytestmark = pytest.mark.asyncio

class MockRestClient:
    async def request(self, method, path, **kwargs):
        if method == "POST" and "order" in path:
            raise Exception("Timeout Ambiguity Simulated")
        if method == "GET" and "order" in path:
            raise Exception("Order does not exist -2013")
        return {}
        
    async def init_session(self): pass
    async def close(self): pass

class MockCapabilities:
    hedge_mode = False
    symbol_rules = {}
    async def discover(self, client): return True

@pytest.fixture
def adapter():
    ledger = InMemoryLedger()
    ada = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    ada.rest_client = MockRestClient()
    ada.capabilities = MockCapabilities()
    
    # Inject a mock rule
    from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules
    rules = SymbolTradingRules("BTCUSDT")
    rules.step_size = Decimal("0.001")
    rules.tick_size = Decimal("0.1")
    ada.capabilities.symbol_rules["BTCUSDT"] = rules
    return ada

async def test_timeout_ambiguity_handling(adapter):
    adapter.state = ConnectionState.READY
    
    intent = OrderIntent(
        client_order_id="TEST-1",
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        order_type="LIMIT",
        time_in_force="GTC",
        quantity=Decimal("1.0"),
        price=Decimal("30000.0")
    )
    decision = ExecutionDecision(decision_id="D1", symbol="BTCUSDT", action="SUBMIT", orders=[intent])
    
    await adapter.execute_decision(decision)
    
    # Should be READY again because "Order does not exist" confirms absence.
    assert adapter.state == ConnectionState.READY

