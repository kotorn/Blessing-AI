"""Read-only reconstruction of the exchange ledger from Cloud SQL.

The Binance adapter keeps a mutable in-process ledger while it is running, but
Mainnet preflight must compare the exchange with the durable state that will
survive a restart.  This module loads only one fixed symbol/environment scope
from PostgreSQL into the adapter's normal ledger interface.  It deliberately
does not write to the database; exchange observations remain disposable until
the normal outbox callbacks are attached by the Worker.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from apps.trading_worker.persistence.postgres.client import PostgresClient
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from domain.enums import EconomicRiskClass, MarketType, OrderSide, PositionSide, TimeInForce
from domain.models import ExchangeFill, ExchangePosition, ExecutionOrder


class DurableLedgerLoadError(RuntimeError):
    """Raised when Cloud SQL cannot provide a trustworthy scoped ledger."""


def _row_dict(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    try:
        return dict(row)
    except (TypeError, ValueError) as exc:
        raise DurableLedgerLoadError("durable ledger row is not a mapping") from exc


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if value in (None, ""):
        raise DurableLedgerLoadError(f"durable ledger row is missing {field}")
    text = str(value).strip()
    if not text:
        raise DurableLedgerLoadError(f"durable ledger row is missing {field}")
    return text


def _decimal(row: Mapping[str, Any], field: str, *, allow_none: bool = False) -> Decimal | None:
    value = row.get(field)
    if value in (None, ""):
        if allow_none:
            return None
        raise DurableLedgerLoadError(f"durable ledger row is missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise DurableLedgerLoadError(f"durable ledger row has invalid {field}") from exc
    if not parsed.is_finite():
        raise DurableLedgerLoadError(f"durable ledger row has non-finite {field}")
    return parsed


def _timestamp(row: Mapping[str, Any], field: str) -> datetime:
    value = row.get(field)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise DurableLedgerLoadError(f"durable ledger row has invalid {field}") from exc
    else:
        raise DurableLedgerLoadError(f"durable ledger row is missing {field}")
    if parsed.tzinfo is None:
        raise DurableLedgerLoadError(f"durable ledger {field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _enum_text(row: Mapping[str, Any], field: str, default: str | None = None) -> str:
    raw = row.get(field, default)
    if raw in (None, ""):
        raise DurableLedgerLoadError(f"durable ledger row is missing {field}")
    return str(raw).strip().upper()


def _parse_time_in_force(value: str) -> TimeInForce:
    if value == "GTX":
        return TimeInForce.POST_ONLY
    try:
        return TimeInForce(value)
    except ValueError as exc:
        raise DurableLedgerLoadError(f"durable ledger row has unsupported time_in_force: {value}") from exc


def _parse_position_side(value: str) -> PositionSide:
    try:
        return PositionSide(value)
    except ValueError as exc:
        raise DurableLedgerLoadError(f"durable ledger row has unsupported position_side: {value}") from exc


def _strict_bool(value: object, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise DurableLedgerLoadError(f"durable ledger row has invalid {field}")


class PostgresExecutionLedger(InMemoryLedger):
    """Scoped, restart-safe ledger snapshot used by the Binance adapter.

    Mutations after loading are intentionally in-memory.  The Worker attaches
    its transactional-outbox callbacks before connecting the live adapter, so
    newly observed exchange state is persisted through the existing authority.
    """

    def __init__(self, *, symbol: str, venue: str) -> None:
        super().__init__()
        self.symbol = str(symbol).strip().upper()
        self.venue = str(venue).strip().upper()
        self.durable_snapshot = True

    @classmethod
    async def load(
        cls,
        db: PostgresClient,
        *,
        symbol: str = "ETHUSDC",
        venue: str,
    ) -> "PostgresExecutionLedger":
        normalized_symbol = str(symbol).strip().upper()
        normalized_venue = str(venue).strip().upper()
        if not normalized_symbol or not normalized_venue:
            raise DurableLedgerLoadError("durable ledger scope is incomplete")

        orders = await db.fetch(
            """
            SELECT client_order_id, exchange_order_id, symbol, venue, side,
                   order_type, price, quantity, status, time_in_force,
                   position_side, created_at, updated_at
            FROM orders
            WHERE UPPER(symbol) = $1 AND UPPER(venue) = $2
            ORDER BY created_at, client_order_id
            """,
            normalized_symbol,
            normalized_venue,
        )
        fills = await db.fetch(
            """
            SELECT fill_id, client_order_id, exchange_order_id,
                   exchange_trade_id, symbol, venue, side, position_side,
                   price, quantity, fee, fee_asset, is_maker, executed_at
            FROM fills
            WHERE UPPER(symbol) = $1 AND UPPER(venue) = $2
            ORDER BY executed_at, fill_id
            """,
            normalized_symbol,
            normalized_venue,
        )
        positions = await db.fetch(
            """
            SELECT venue, symbol, position_side, quantity, entry_price,
                   mark_price, liquidation_price, unrealized_pnl, leverage,
                   margin_type, updated_at
            FROM positions
            WHERE UPPER(symbol) = $1 AND UPPER(venue) = $2
            ORDER BY position_side
            """,
            normalized_symbol,
            normalized_venue,
        )

        ledger = cls(symbol=normalized_symbol, venue=normalized_venue)
        for raw in orders:
            order = ledger._order_from_row(_row_dict(raw), normalized_symbol, normalized_venue)
            await ledger.upsert_order(order)
        for raw in fills:
            fill = ledger._fill_from_row(_row_dict(raw), normalized_symbol, normalized_venue)
            if await ledger.has_fill(
                f"{normalized_venue}:{fill.symbol}:{fill.exchange_trade_id}"
            ):
                raise DurableLedgerLoadError("durable ledger contains duplicate fill identity")
            ledger._fill_keys.add(ledger._get_fill_key(fill))
            ledger.fills.append(fill)
        for raw in positions:
            position = ledger._position_from_row(_row_dict(raw), normalized_symbol, normalized_venue)
            # Use the normal parser for the same active-position invariants as
            # exchange REST/WS observations.
            await ledger.upsert_position(position)
        ledger._validate_lineage()
        ledger._initialized = True
        return ledger

    @staticmethod
    def _check_scope(row: Mapping[str, Any], symbol: str, venue: str) -> None:
        row_symbol = _required_text(row, "symbol").upper()
        row_venue = _required_text(row, "venue").upper()
        if row_symbol != symbol or row_venue != venue:
            raise DurableLedgerLoadError("durable ledger row escaped the requested exchange scope")

    @classmethod
    def _order_from_row(
        cls, row: Mapping[str, Any], symbol: str, venue: str
    ) -> ExecutionOrder:
        cls._check_scope(row, symbol, venue)
        side = _enum_text(row, "side")
        try:
            parsed_side = OrderSide(side)
        except ValueError as exc:
            raise DurableLedgerLoadError(f"durable order has unsupported side: {side}") from exc
        order_type = _enum_text(row, "order_type")
        price = _decimal(row, "price", allow_none=order_type == "MARKET")
        if price is None:
            price = Decimal("0")
        if order_type != "MARKET" and price <= 0:
            raise DurableLedgerLoadError("durable non-market order has unusable price")
        quantity = _decimal(row, "quantity")
        if quantity is None or quantity <= 0:
            raise DurableLedgerLoadError("durable order has unusable quantity")
        status = _required_text(row, "status").upper()
        if status not in {
            "PENDING",
            "SUBMITTED",
            "NEW",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCELLED",
            "CANCELED",
            "REJECTED",
            "EXPIRED",
        }:
            raise DurableLedgerLoadError(f"durable order has unsupported status: {status}")
        return ExecutionOrder(
            symbol=symbol,
            side=parsed_side,
            quantity=quantity,
            price=price,
            order_type=order_type,
            client_order_id=_required_text(row, "client_order_id"),
            status=status,
            exchange_order_id=(
                str(row["exchange_order_id"]).strip()
                if row.get("exchange_order_id") not in (None, "")
                else None
            ),
            timestamp=_timestamp(row, "updated_at"),
            market_type=MarketType.USDM_FUTURES,
            position_side=_parse_position_side(_enum_text(row, "position_side", "BOTH")),
            reduce_only=False,
            time_in_force=_parse_time_in_force(_enum_text(row, "time_in_force", "GTC")),
            risk_class=EconomicRiskClass.NOOP,
        )

    @classmethod
    def _fill_from_row(
        cls, row: Mapping[str, Any], symbol: str, venue: str
    ) -> ExchangeFill:
        cls._check_scope(row, symbol, venue)
        try:
            side = OrderSide(_enum_text(row, "side"))
        except ValueError as exc:
            raise DurableLedgerLoadError("durable fill has unsupported side") from exc
        timestamp = _timestamp(row, "executed_at")
        quantity = _decimal(row, "quantity")
        price = _decimal(row, "price")
        commission = _decimal(row, "fee")
        if quantity is None or quantity <= 0 or price is None or price <= 0:
            raise DurableLedgerLoadError("durable fill has unusable quantity or price")
        if commission is None or commission < 0:
            raise DurableLedgerLoadError("durable fill has unusable fee")
        return ExchangeFill(
            exchange_trade_id=_required_text(row, "exchange_trade_id"),
            exchange_order_id=_required_text(row, "exchange_order_id"),
            client_order_id=_required_text(row, "client_order_id"),
            symbol=symbol,
            side=side,
            position_side=_parse_position_side(_enum_text(row, "position_side", "BOTH")),
            quantity=quantity,
            price=price,
            commission=commission,
            commission_asset=_required_text(row, "fee_asset"),
            realized_pnl=Decimal("0"),
            maker=_strict_bool(row.get("is_maker", False), "is_maker"),
            event_time=timestamp,
            transaction_time=timestamp,
            source=venue,
        )

    @classmethod
    def _position_from_row(
        cls, row: Mapping[str, Any], symbol: str, venue: str
    ) -> ExchangePosition:
        cls._check_scope(row, symbol, venue)
        quantity = _decimal(row, "quantity") or Decimal("0")
        mark_price = _decimal(row, "mark_price")
        if quantity != 0 and (mark_price is None or mark_price <= 0):
            raise DurableLedgerLoadError("active durable position has unusable mark_price")
        entry_price = _decimal(row, "entry_price")
        leverage = _decimal(row, "leverage")
        if quantity != 0 and (
            entry_price is None
            or entry_price <= 0
            or leverage is None
            or leverage <= 0
        ):
            raise DurableLedgerLoadError(
                "active durable position has incomplete entry or leverage"
            )
        return ExchangePosition(
            symbol=symbol,
            position_side=_parse_position_side(_enum_text(row, "position_side", "BOTH")),
            quantity=quantity,
            entry_price=entry_price or Decimal("0"),
            mark_price=mark_price,
            liquidation_price=_decimal(row, "liquidation_price", allow_none=True),
            unrealized_pnl=_decimal(row, "unrealized_pnl") or Decimal("0"),
            leverage=leverage or Decimal("0"),
            margin_type=_required_text(row, "margin_type").upper(),
            event_time=_timestamp(row, "updated_at"),
            source=venue,
        )

    def _validate_lineage(self) -> None:
        orders_by_client = set(self.orders)
        orders_by_exchange = {
            str(order.exchange_order_id)
            for order in self.orders.values()
            if order.exchange_order_id
        }
        for fill in self.fills:
            if (
                fill.client_order_id not in orders_by_client
                and str(fill.exchange_order_id) not in orders_by_exchange
            ):
                raise DurableLedgerLoadError(
                    "durable fill has no matching order lineage"
                )
