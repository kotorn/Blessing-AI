from decimal import Decimal

import pytest

from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from domain.models import ExecutionOrder, ExchangePosition, OrderSide, PositionSide

pytestmark = pytest.mark.asyncio


async def _seed_local_order(
    ledger: InMemoryLedger,
    client_order_id: str,
    *,
    position_side: PositionSide = PositionSide.BOTH,
) -> None:
    await ledger.upsert_order(
        ExecutionOrder(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.1"),
            price=Decimal("30000"),
            client_order_id=client_order_id,
            status="NEW",
            position_side=position_side,
            strategy_id="test",
            decision_id="DEC-TEST",
            source_intent_ids=["INT-TEST"],
        )
    )


async def test_duplicate_fills_idempotency():
    ledger = InMemoryLedger()
    ada = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    await _seed_local_order(ledger, "BAI-123-1", position_side=PositionSide.LONG)
    
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


async def test_malformed_trade_update_is_not_recorded_as_a_fill():
    ledger = InMemoryLedger()
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    await _seed_local_order(ledger, "BAI-MALFORMED-1")

    await adapter._on_ws_event(
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": 12345,
            "o": {
                "s": "BTCUSDT",
                "c": "BAI-MALFORMED-1",
                "X": "FILLED",
                "x": "TRADE",
                "t": 1002,
                "i": 5002,
                "S": "BUY",
                "ps": "BOTH",
                "q": "0.001",
                "p": "10000",
                "l": "0",
                "L": "10000",
                "n": "0",
                "N": "USDT",
                "rp": "0",
                "m": False,
                "T": 12340,
            },
        }
    )

    assert ledger.fills == []


async def test_unowned_order_update_is_quarantined_instead_of_adopted():
    ledger = InMemoryLedger()
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)

    await adapter._on_ws_event(
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": 12345,
            "o": {
                "s": "BTCUSDT",
                "c": "MANUAL-OR-FOREIGN-1",
                "X": "NEW",
                "i": 5004,
                "S": "BUY",
                "ps": "BOTH",
            },
        }
    )

    assert await ledger.get_order_by_client_id("MANUAL-OR-FOREIGN-1") is None
    assert adapter.reconciliation.last_status == "UNKNOWN"
    assert adapter.reconciliation.last_diffs[0].code == (
        "EXCHANGE_ORDER_EVENT_UNKNOWN_LOCALLY"
    )


async def test_trade_update_uses_transaction_time_when_event_time_is_absent():
    ledger = InMemoryLedger()
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    await _seed_local_order(ledger, "BAI-TIMESTAMP-FALLBACK")

    await adapter._on_ws_event(
        {
            "e": "ORDER_TRADE_UPDATE",
            "o": {
                "s": "BTCUSDT",
                "c": "BAI-TIMESTAMP-FALLBACK",
                "X": "FILLED",
                "x": "TRADE",
                "t": 1003,
                "i": 5003,
                "S": "BUY",
                "ps": "BOTH",
                "l": "0.1",
                "L": "30000.0",
                "n": "0.0",
                "N": "USDT",
                "rp": "0.0",
                "m": False,
                "T": 12341,
            },
        }
    )

    assert len(ledger.fills) == 1
    assert ledger.fills[0].event_time == 12341
    assert ledger.fills[0].transaction_time == 12341

async def test_incomplete_active_account_update_is_quarantined():
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

    assert ledger.positions == []
    assert ada.reconciliation.last_status == "UNKNOWN"
    assert ada.reconciliation.last_diffs[0].code == (
        "ACCOUNT_POSITION_UPDATE_INCOMPLETE"
    )


async def test_account_update_merges_delta_into_authoritative_position():
    ledger = InMemoryLedger()
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    await ledger.upsert_position(
        ExchangePosition(
            symbol="BTCUSDT",
            position_side=PositionSide.LONG,
            quantity=Decimal("1.0"),
            entry_price=Decimal("29000"),
            mark_price=Decimal("30000"),
            unrealized_pnl=Decimal("100"),
            leverage=Decimal("2"),
            margin_type="cross",
        )
    )

    await adapter._on_ws_event(
        {
            "e": "ACCOUNT_UPDATE",
            "E": 12346,
            "a": {
                "P": [
                    {
                        "s": "BTCUSDT",
                        "ps": "LONG",
                        "pa": "1.5",
                        "ep": "30000",
                        "up": "150",
                        "mt": "cross",
                    }
                ]
            },
        }
    )

    assert len(ledger.positions) == 1
    assert ledger.positions[0]["positionAmt"] == "1.5"
    assert ledger.positions[0].mark_price == Decimal("30000")
    assert ledger.positions[0].leverage == Decimal("2")


async def test_ledger_rejects_active_raw_position_without_mark_price():
    ledger = InMemoryLedger()

    with pytest.raises(ValueError, match="markPrice"):
        await ledger.upsert_position(
            {
                "symbol": "BTCUSDT",
                "positionSide": "LONG",
                "positionAmt": "1.5",
                "entryPrice": "30000",
                "unRealizedProfit": "150",
                "marginType": "cross",
                "leverage": "2",
            }
        )


async def test_account_update_does_not_treat_per_asset_balance_as_aggregate():
    ledger = InMemoryLedger()
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)
    ledger.wallet_balance = Decimal("100")
    ledger.margin_balance = Decimal("100")

    await adapter._on_ws_event(
        {
            "e": "ACCOUNT_UPDATE",
            "a": {
                "P": [],
                "B": [{"a": "USDT", "wb": "12", "cw": "11"}],
            },
        }
    )

    assert await ledger.get_balances() == (Decimal("100"), Decimal("100"))
    assert ledger.account_snapshot is None
    assert adapter.reconciliation.last_status == "UNKNOWN"
    assert adapter.reconciliation.last_diffs == []


async def test_malformed_account_update_balance_fails_closed_without_raising():
    ledger = InMemoryLedger()
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET, ledger=ledger)

    await adapter._on_ws_event(
        {
            "e": "ACCOUNT_UPDATE",
            "a": {"P": [], "B": {"a": "USDT", "wb": "12", "cw": "11"}},
        }
    )

    assert adapter.reconciliation.last_status == "UNKNOWN"
    assert adapter.reconciliation.last_diffs[0].code == (
        "ACCOUNT_BALANCE_UPDATE_INVALID"
    )
