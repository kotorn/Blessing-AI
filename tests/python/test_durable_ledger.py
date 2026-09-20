from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.trading_worker.persistence.postgres.ledger import (
    DurableLedgerLoadError,
    PostgresExecutionLedger,
)
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from domain.enums import OrderSide, PositionSide
from domain.models import ExchangeFill

STAMP = datetime(2026, 9, 16, 1, 2, 3, tzinfo=UTC)


class LedgerDatabase:
    def __init__(self, *, duplicate_fill: bool = False) -> None:
        self.duplicate_fill = duplicate_fill
        self.queries: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.queries.append((query, args))
        if "FROM orders" in query:
            return [
                {
                    "client_order_id": "mainnet-order-1",
                    "exchange_order_id": "1001",
                    "symbol": "ETHUSDC",
                    "venue": "BINANCE_MAINNET",
                    "side": "BUY",
                    "order_type": "LIMIT",
                    "price": Decimal(2500),
                    "quantity": Decimal("0.01"),
                    "status": "NEW",
                    "time_in_force": "GTC",
                    "position_side": "LONG",
                    "created_at": STAMP,
                    "updated_at": STAMP,
                }
            ]
        if "FROM fills" in query:
            row = {
                "fill_id": "BINANCE_MAINNET:ETHUSDC:trade-1",
                "client_order_id": "mainnet-order-1",
                "exchange_order_id": "1001",
                "exchange_trade_id": "trade-1",
                "symbol": "ETHUSDC",
                "venue": "BINANCE_MAINNET",
                "side": "BUY",
                "position_side": "LONG",
                "price": Decimal(2500),
                "quantity": Decimal("0.01"),
                "fee": Decimal("0.01"),
                "fee_asset": "USDC",
                "is_maker": False,
                "executed_at": STAMP,
            }
            return [row, dict(row)] if self.duplicate_fill else [row]
        if "FROM positions" in query:
            return [
                {
                    "venue": "BINANCE_MAINNET",
                    "symbol": "ETHUSDC",
                    "position_side": "LONG",
                    "quantity": Decimal("0.01"),
                    "entry_price": Decimal(2500),
                    "mark_price": Decimal(2501),
                    "liquidation_price": None,
                    "unrealized_pnl": Decimal("0.01"),
                    "leverage": Decimal(5),
                    "margin_type": "CROSS",
                    "updated_at": STAMP,
                }
            ]
        raise AssertionError(f"unexpected ledger query: {query}")


@pytest.mark.asyncio
async def test_postgres_ledger_loads_one_mainnet_scope_and_is_initialized() -> None:
    db = LedgerDatabase()

    ledger = await PostgresExecutionLedger.load(
        db, symbol="ETHUSDC", venue="BINANCE_MAINNET"
    )

    assert await ledger.is_initialized() is True
    assert len(await ledger.get_all_orders()) == 1
    assert (await ledger.get_all_orders())[0].position_side is PositionSide.LONG
    assert len(await ledger.get_fills()) == 1
    assert len(await ledger.get_positions()) == 1
    assert (await ledger.get_positions())[0].source == "BINANCE_MAINNET"
    assert all(args == ("ETHUSDC", "BINANCE_MAINNET") for _, args in db.queries)


@pytest.mark.asyncio
async def test_postgres_ledger_rejects_duplicate_fill_identity() -> None:
    with pytest.raises(DurableLedgerLoadError, match="duplicate fill identity"):
        await PostgresExecutionLedger.load(
            LedgerDatabase(duplicate_fill=True),
            symbol="ETHUSDC",
            venue="BINANCE_MAINNET",
        )


@pytest.mark.asyncio
async def test_postgres_ledger_rejects_rows_from_another_environment() -> None:
    db = LedgerDatabase()
    db.fetch = _foreign_scope_fetch  # type: ignore[method-assign]

    with pytest.raises(DurableLedgerLoadError, match="escaped"):
        await PostgresExecutionLedger.load(
            db, symbol="ETHUSDC", venue="BINANCE_MAINNET"
        )


@pytest.mark.asyncio
async def test_in_memory_fill_identity_is_environment_scoped() -> None:
    ledger = InMemoryLedger()

    def fill(source: str) -> ExchangeFill:
        return ExchangeFill(
            exchange_trade_id="same-trade-id",
            exchange_order_id=f"order-{source}",
            client_order_id=f"client-{source}",
            symbol="ETHUSDC",
            side=OrderSide.BUY,
            position_side=PositionSide.BOTH,
            quantity=Decimal("0.01"),
            price=Decimal(2500),
            commission=Decimal("0.01"),
            commission_asset="USDC",
            realized_pnl=Decimal(0),
            maker=False,
            event_time=STAMP,
            transaction_time=STAMP,
            source=source,
        )

    await ledger.append_fill(fill("BINANCE_TESTNET"))
    await ledger.append_fill(fill("BINANCE_MAINNET"))

    assert len(await ledger.get_fills()) == 2
    assert await ledger.has_fill("BINANCE_TESTNET:ETHUSDC:same-trade-id")
    assert await ledger.has_fill("BINANCE_MAINNET:ETHUSDC:same-trade-id")


async def _foreign_scope_fetch(
    _query: str, *_args: object
) -> list[dict[str, object]]:
    return [
        {
            "client_order_id": "testnet-order",
            "exchange_order_id": "2",
            "symbol": "ETHUSDC",
            "venue": "BINANCE_TESTNET",
            "side": OrderSide.BUY.value,
            "order_type": "LIMIT",
            "price": Decimal(2500),
            "quantity": Decimal("0.01"),
            "status": "NEW",
            "time_in_force": "GTC",
            "position_side": PositionSide.BOTH.value,
            "created_at": STAMP,
            "updated_at": STAMP,
        }
    ]
