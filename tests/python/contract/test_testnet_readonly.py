import pytest
import asyncio
import os
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.rest_client import BinanceRestClient
from apps.trading_worker.venues.binance.capabilities import BinanceCapabilities

pytestmark = [pytest.mark.asyncio, pytest.mark.contract_readonly]

@pytest.fixture
def api_credentials():
    api_key = os.getenv("BINANCE_TESTNET_API_KEY")
    api_secret = os.getenv("BINANCE_TESTNET_API_SECRET")
    if not api_key or not api_secret:
        pytest.skip("Testnet credentials not found. Skipping contract test.")
    return api_key, api_secret

async def test_capabilities_discovery(api_credentials):
    api_key, api_secret = api_credentials
    rest_client = BinanceRestClient(api_key, api_secret, BinanceEnvironment.TESTNET)
    await rest_client.init_session()
    
    cap = BinanceCapabilities()
    success = await cap.discover(rest_client)
    
    assert success is True
    assert cap.authenticated is True
    assert len(cap.symbol_rules) > 0
    assert "BTCUSDT" in cap.symbol_rules
    
    await rest_client.close()
