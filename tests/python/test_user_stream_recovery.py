import pytest
import asyncio
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.models import ConnectionState

pytestmark = pytest.mark.asyncio

class MockUserStream:
    def __init__(self):
        self.is_connected = False
        self.on_disconnect = None
        self.start_called = False
    async def start(self, cb):
        self.start_called = True
        self.is_connected = True
        return True
    async def close(self):
        self.is_connected = False
        
    async def trigger_disconnect(self):
        self.is_connected = False
        if self.on_disconnect:
            await self.on_disconnect()

class MockReconciliation:
    def __init__(self):
        self.status = "IN_SYNC"
    async def reconcile(self):
        return self.status

@pytest.fixture
def adapter():
    ledger = InMemoryLedger()
    ada = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    ada.user_stream = MockUserStream()
    ada.user_stream.on_disconnect = ada._on_user_stream_disconnect
    ada.reconciliation = MockReconciliation()
    return ada

async def test_user_stream_disconnect_recovery_insync(adapter):
    adapter.state = ConnectionState.READY
    
    # Trigger disconnect
    await adapter.user_stream.trigger_disconnect()
    
    # State should have transitioned immediately, but since it awaits reconciliation,
    # and reconciliation returns IN_SYNC, it should be READY again eventually.
    # In a real event loop, it will run _on_user_stream_disconnect as a task.
    # We await it manually since we triggered it.
    
    assert adapter.state == ConnectionState.READY

async def test_user_stream_disconnect_recovery_mismatch(adapter):
    adapter.state = ConnectionState.READY
    adapter.reconciliation.status = "MISMATCH"
    
    await adapter.user_stream.trigger_disconnect()
    
    assert adapter.state == ConnectionState.DEGRADED
