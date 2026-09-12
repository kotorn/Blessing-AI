import pytest
import asyncio
import os
from decimal import Decimal
from domain.models import ExecutionDecision, OrderIntent, OrderSide, PositionSide, MarketType
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.rest_client import BinanceRestClient
from apps.trading_worker.venues.binance.capabilities import BinanceCapabilities
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.models import ConnectionState

pytestmark = [pytest.mark.asyncio, pytest.mark.contract_mutating]

@pytest.fixture
def api_credentials():
    api_key = os.getenv("BINANCE_TESTNET_API_KEY")
    api_secret = os.getenv("BINANCE_TESTNET_API_SECRET")
    if not api_key or not api_secret:
        pytest.skip("Testnet credentials not found. Skipping mutating contract test.")
    return api_key, api_secret

async def test_bounded_mutation_place_order(api_credentials):
    api_key, api_secret = api_credentials
    ledger = InMemoryLedger()
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    adapter.rest_client = BinanceRestClient(api_key, api_secret, BinanceEnvironment.TESTNET)
    await adapter.rest_client.init_session()
    
    # Pre-flight
    success = await adapter.arm()
    assert success is True
    assert adapter.state == ConnectionState.READY
    
    # Define bounded testing rules to fail closed.
    allowed_symbols = ["BTCUSDT", "ETHUSDT"]
    symbol = "BTCUSDT"
    assert symbol in allowed_symbols
    
    # Place a microscopic passive order, wait 2 seconds, cancel it
    intent = OrderIntent(
        client_order_id="TEST_MUT_1",
        symbol=symbol,
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        order_type="LIMIT",
        time_in_force="GTC",
        quantity=Decimal("0.001"),
        price=Decimal("15000.0") # Deep out of the money
    )
    decision = ExecutionDecision(decision_id="D_TEST", symbol=symbol, action="SUBMIT", orders=[intent])
    
    orders = await adapter.execute_decision(decision)
    assert len(orders) == 1
    
    await asyncio.sleep(2)
    
    # Cancel it
    cancel_success = await adapter.cancel_order(symbol, orders[0].client_order_id)
    assert cancel_success is True
    
    await adapter.close()
