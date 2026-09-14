import asyncio
from datetime import UTC, datetime

import pytest

from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.models import (
    BinanceAuthenticationError,
    ConnectionState,
)
from apps.trading_worker.venues.binance.user_stream import BinanceUserStream

pytestmark = pytest.mark.asyncio


async def test_private_stream_needs_event_or_transport_heartbeat():
    stream = BinanceUserStream(None, BinanceEnvironment.TESTNET)
    stream.running = True
    stream.is_connected = True
    stream.connected_at = datetime.now(UTC)

    assert stream.is_healthy() is False

    stream.last_event_at = datetime.now(UTC)
    assert stream.is_healthy() is True

    stream.last_event_at = None
    stream.last_transport_heartbeat_at = datetime.now(UTC)
    assert stream.is_healthy() is True


async def test_transport_heartbeat_requires_pong():
    class ResponsiveWebSocket:
        def __init__(self):
            self.ping_calls = 0

        async def ping(self):
            self.ping_calls += 1
            pong = asyncio.get_running_loop().create_future()
            pong.set_result(0.001)
            return pong

    stream = BinanceUserStream(None, BinanceEnvironment.TESTNET)
    stream.ws = ResponsiveWebSocket()
    stream.STREAM_HEARTBEAT_TIMEOUT_SEC = 0.1

    assert await stream._transport_heartbeat() is True
    assert stream.ws.ping_calls == 1
    assert stream.last_transport_heartbeat_at is not None


async def test_transport_heartbeat_fails_when_pong_is_missing():
    class UnresponsiveWebSocket:
        async def ping(self):
            return asyncio.get_running_loop().create_future()

    stream = BinanceUserStream(None, BinanceEnvironment.TESTNET)
    stream.ws = UnresponsiveWebSocket()
    stream.STREAM_HEARTBEAT_TIMEOUT_SEC = 0.01

    assert await stream._transport_heartbeat() is False
    assert stream.last_transport_heartbeat_at is None


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
    
    # A disconnect is degraded. Reconciliation alone cannot claim the private
    # stream invariant has recovered.
    assert adapter.state == ConnectionState.DEGRADED

async def test_user_stream_disconnect_recovery_mismatch(adapter):
    adapter.state = ConnectionState.READY
    adapter.reconciliation.status = "MISMATCH"
    
    await adapter.user_stream.trigger_disconnect()
    
    assert adapter.state == ConnectionState.DEGRADED

async def test_user_stream_reconnect_requires_stream_auth_and_sync(adapter):
    adapter.state = ConnectionState.DEGRADED
    adapter.capabilities.account_request_succeeded = True
    adapter.capabilities.authenticated = True
    adapter.capabilities.trade_authorized = True
    adapter.user_stream.is_connected = True
    adapter.user_stream.on_reconnected = adapter._on_user_stream_reconnected

    await adapter._on_user_stream_reconnected()

    assert adapter.state == ConnectionState.READY


async def test_user_stream_authentication_failure_clears_adapter_truth():
    class AuthFailureRest:
        async def request(self, method, path, **kwargs):
            raise BinanceAuthenticationError(-2015, "Invalid API key")

    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET)
    adapter.capabilities.account_request_succeeded = True
    adapter.capabilities.authenticated = True
    adapter.capabilities.trade_authorized = True
    adapter.state = ConnectionState.READY
    adapter.user_stream.rest_client = AuthFailureRest()

    await adapter.user_stream._get_listen_key()

    assert adapter.authenticated is False
    assert adapter.state == ConnectionState.DEGRADED
    assert adapter.user_stream.listen_key is None
