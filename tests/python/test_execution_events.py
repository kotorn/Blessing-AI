import pytest
from decimal import Decimal
from domain.models import ExecutionOrder, ExchangeFill, OrderSide, PositionSide
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.config import BinanceEnvironment

pytestmark = pytest.mark.asyncio

async def test_duplicate_fills_idempotency():
    ledger = InMemoryLedger()
    ada = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    
    # Simulate first fill event
    event1 = {
        "e": "ORDER_TRADE_UPDATE",
        "E": 12345,
        "o": {
            "s": "BTCUSDT",
            "c": "BAI-123-1",
            "X": "FILLED",
            "x": "TRADE",
            "t": 1001,
            "i": 5001,
            "S": "BUY",
            "ps": "LONG",
            "l": "0.1",
            "L": "30000.0",
            "n": "0.0",
            "N": "USDT",
            "rp": "0.0",
            "m": False,
            "T": 12340
        }
    }
    
    await ada._on_ws_event(event1)
    
    assert len(ledger.fills) == 1
    
    # Simulate duplicate fill event
    await ada._on_ws_event(event1)
    
    # Should still be 1
    assert len(ledger.fills) == 1

async def test_account_update_positions():
    ledger = InMemoryLedger()
    ada = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    
    event = {
        "e": "ACCOUNT_UPDATE",
        "a": {
            "P": [
                {
                    "s": "BTCUSDT",
                    "ps": "LONG",
                    "pa": "1.5",
                    "ep": "30000",
                    "up": "150",
                    "mt": "cross"
                }
            ]
        }
    }
    
    await ada._on_ws_event(event)
    
    assert len(ledger.positions) == 1
    assert ledger.positions[0]["symbol"] == "BTCUSDT"
    assert ledger.positions[0]["positionSide"] == "LONG"
    assert ledger.positions[0]["positionAmt"] == "1.5"
