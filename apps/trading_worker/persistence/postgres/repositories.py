"""Transactional outbox and idempotent persistence repositories."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Mapping, Optional, Protocol

from apps.trading_worker.persistence.postgres.client import PostgresClient
from domain.models import (
    ExchangeFill,
    ExchangePosition,
    ExecutionOrder,
    Instrument,
    RiskSnapshot,
)

logger = logging.getLogger("blessing.persistence.repositories")


class InstrumentRulesProvider(Protocol):
    def __call__(self, symbol: str) -> Optional[Instrument]: ...


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _utc_datetime(value: object) -> datetime:
    """Normalize exchange timestamps without ever falling back to naive UTC."""

    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float, Decimal)):
        numeric = float(value)
        if numeric > 100_000_000_000:
            numeric /= 1000.0
        parsed = datetime.fromtimestamp(numeric, tz=UTC)
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
    else:
        raise ValueError(f"unsupported timestamp type: {type(value).__name__}")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return _utc_datetime(value).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(_enum_value(value))


def _model_payload(model: object) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        payload = model.model_dump(mode="json")  # type: ignore[attr-defined]
    elif hasattr(model, "dict"):
        payload = model.dict()  # type: ignore[attr-defined]
    else:
        raise TypeError(f"unsupported persistence model: {type(model).__name__}")
    return json.loads(json.dumps(payload, default=_json_default))


def _instrument_payload(instrument: Optional[Instrument]) -> Optional[dict[str, Any]]:
    if instrument is None:
        return None
    payload = _model_payload(instrument)
    payload["market_type"] = str(_enum_value(instrument.market_type))
    return payload


class PersistenceEvent(Protocol):
    event_id: str
    event_type: str
    idempotency_key: str
    aggregate_type: str
    aggregate_id: str
    created_at: datetime
    payload: Mapping[str, Any]


class InstrumentRulesUnavailable(ValueError):
    """Raised when a durable event lacks exchange-derived instrument metadata."""


class OrderRepository:
    def __init__(self, db: PostgresClient) -> None:
        self.db = db

    async def save_order(
        self,
        order: ExecutionOrder,
        instrument: Instrument,
        connection: Any,
    ) -> None:
        await _ensure_instrument(connection, instrument)
        await connection.execute(
            """
            INSERT INTO orders (
                client_order_id, exchange_order_id, symbol, venue, side, order_type,
                order_role, price, quantity, status, time_in_force, position_side,
                created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $13
            ) ON CONFLICT (client_order_id) DO UPDATE SET
                exchange_order_id = EXCLUDED.exchange_order_id,
                venue = EXCLUDED.venue,
                status = EXCLUDED.status,
                price = EXCLUDED.price,
                quantity = EXCLUDED.quantity,
                time_in_force = EXCLUDED.time_in_force,
                position_side = EXCLUDED.position_side,
                updated_at = EXCLUDED.updated_at
            """,
            order.client_order_id,
            order.exchange_order_id,
            order.symbol,
            instrument.venue,
            str(_enum_value(order.side)),
            str(_enum_value(order.order_type)),
            "SYSTEM_ORDER",
            order.price,
            order.quantity,
            str(order.status),
            str(_enum_value(order.time_in_force)),
            str(_enum_value(order.position_side)),
            _utc_datetime(order.timestamp),
        )


class FillRepository:
    def __init__(self, db: PostgresClient) -> None:
        self.db = db

    async def save_fill(
        self,
        fill: ExchangeFill,
        instrument: Instrument,
        connection: Any,
    ) -> None:
        await _ensure_instrument(connection, instrument)
        await connection.execute(
            """
            INSERT INTO fills (
                fill_id, client_order_id, exchange_order_id, exchange_trade_id, symbol, side,
                position_side, price, quantity, fee, fee_asset, is_maker, executed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            ON CONFLICT (fill_id) DO UPDATE SET
                client_order_id = EXCLUDED.client_order_id,
                exchange_order_id = EXCLUDED.exchange_order_id,
                exchange_trade_id = EXCLUDED.exchange_trade_id,
                position_side = EXCLUDED.position_side,
                price = EXCLUDED.price,
                quantity = EXCLUDED.quantity,
                fee = EXCLUDED.fee,
                fee_asset = EXCLUDED.fee_asset,
                is_maker = EXCLUDED.is_maker,
                executed_at = EXCLUDED.executed_at
            """,
            fill.exchange_trade_id,
            fill.client_order_id,
            fill.exchange_order_id,
            fill.exchange_trade_id,
            fill.symbol,
            str(_enum_value(fill.side)),
            str(_enum_value(fill.position_side)),
            fill.price,
            fill.quantity,
            fill.commission,
            fill.commission_asset,
            fill.maker,
            _utc_datetime(fill.event_time or fill.transaction_time),
        )


class PositionRepository:
    def __init__(self, db: PostgresClient) -> None:
        self.db = db

    async def save_position(
        self,
        position: ExchangePosition,
        instrument: Instrument,
        connection: Any,
    ) -> None:
        await _ensure_instrument(connection, instrument)
        quantity = Decimal(str(position.quantity))
        direction = "LONG" if quantity > 0 else "SHORT" if quantity < 0 else "FLAT"
        await connection.execute(
            """
            INSERT INTO positions (
                venue, symbol, position_side, direction, quantity, entry_price, mark_price,
                liquidation_price, unrealized_pnl, leverage, margin_type,
                initial_margin, maintenance_margin, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 0.0, 0.0, $12)
            ON CONFLICT (venue, symbol, position_side) DO UPDATE SET
                direction = EXCLUDED.direction,
                quantity = EXCLUDED.quantity,
                entry_price = EXCLUDED.entry_price,
                mark_price = EXCLUDED.mark_price,
                liquidation_price = EXCLUDED.liquidation_price,
                unrealized_pnl = EXCLUDED.unrealized_pnl,
                leverage = EXCLUDED.leverage,
                margin_type = EXCLUDED.margin_type,
                updated_at = EXCLUDED.updated_at
            """,
            instrument.venue,
            position.symbol,
            str(_enum_value(position.position_side)),
            direction,
            quantity,
            position.entry_price,
            position.mark_price or Decimal("0"),
            position.liquidation_price,
            position.unrealized_pnl,
            position.leverage,
            position.margin_type,
            _utc_datetime(position.event_time),
        )


class RiskSnapshotRepository:
    def __init__(self, db: PostgresClient) -> None:
        self.db = db

    async def save_snapshot(self, snapshot: RiskSnapshot, connection: Any) -> None:
        reasons_json = json.dumps(
            {
                "hard_violations": snapshot.hard_violations,
                "soft_violations": snapshot.soft_violations,
            }
        )
        await connection.execute(
            """
            INSERT INTO portfolio_snapshots (
                timestamp, total_equity, total_balance, free_margin, used_margin,
                margin_utilization_pct, effective_leverage, current_drawdown_pct,
                risk_state, aggregate_long_exposure, aggregate_short_exposure,
                crypto_beta_exposure_pct, active_baskets_count, kill_switch_active, reasons
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            """,
            _utc_datetime(snapshot.timestamp),
            snapshot.portfolio_equity,
            snapshot.portfolio_equity,
            snapshot.portfolio_equity
            * (Decimal("1") - snapshot.margin_utilization_pct / Decimal("100")),
            snapshot.portfolio_equity * snapshot.margin_utilization_pct / Decimal("100"),
            snapshot.margin_utilization_pct,
            snapshot.effective_leverage,
            snapshot.current_drawdown_pct,
            str(_enum_value(snapshot.risk_state)),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            0,
            "KILL_SWITCH" in {item.upper() for item in snapshot.hard_violations},
            reasons_json,
        )


async def _ensure_instrument(connection: Any, instrument: Instrument) -> None:
    """Upsert only exchange-derived instrument metadata."""

    if not instrument.is_trading_enabled:
        raise InstrumentRulesUnavailable(
            f"instrument rules for {instrument.symbol} are not enabled for trading"
        )
    numeric_rules = (
        instrument.tick_size,
        instrument.step_size,
        instrument.min_notional,
    )
    if any(not value.is_finite() or value <= 0 for value in numeric_rules):
        raise InstrumentRulesUnavailable(
            f"instrument rules for {instrument.symbol} are incomplete"
        )
    await connection.execute(
        """
        INSERT INTO instruments (
            symbol, venue, market_type, base_asset, quote_asset, price_precision,
            quantity_precision, tick_size, step_size, min_notional, max_leverage,
            is_active, updated_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        ON CONFLICT (symbol) DO UPDATE SET
            venue = EXCLUDED.venue,
            market_type = EXCLUDED.market_type,
            base_asset = EXCLUDED.base_asset,
            quote_asset = EXCLUDED.quote_asset,
            price_precision = EXCLUDED.price_precision,
            quantity_precision = EXCLUDED.quantity_precision,
            tick_size = EXCLUDED.tick_size,
            step_size = EXCLUDED.step_size,
            min_notional = EXCLUDED.min_notional,
            max_leverage = EXCLUDED.max_leverage,
            is_active = EXCLUDED.is_active,
            updated_at = EXCLUDED.updated_at
        """,
        instrument.symbol,
        instrument.venue,
        str(_enum_value(instrument.market_type)),
        instrument.base_asset,
        instrument.quote_asset,
        instrument.price_precision,
        instrument.quantity_precision,
        instrument.tick_size,
        instrument.step_size,
        instrument.min_notional,
        instrument.max_leverage,
        instrument.is_trading_enabled,
        datetime.now(UTC),
    )


class PersistenceRepository:
    """Store events durably, then replay them into domain tables transactionally."""

    def __init__(
        self,
        db: PostgresClient,
        instrument_rules_provider: Optional[InstrumentRulesProvider] = None,
    ) -> None:
        self.db = db
        self.orders = OrderRepository(db)
        self.fills = FillRepository(db)
        self.positions = PositionRepository(db)
        self.risk = RiskSnapshotRepository(db)
        self.instrument_rules_provider = instrument_rules_provider

    async def append_outbox(self, event: PersistenceEvent) -> bool:
        payload = json.dumps(dict(event.payload), default=_json_default, separators=(",", ":"))
        await self.db.execute(
            """
            INSERT INTO persistence_outbox (
                event_id, event_type, idempotency_key, aggregate_type, aggregate_id,
                payload, status, attempt_count, next_attempt_at, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'PENDING', 0, $7, $8)
            ON CONFLICT DO NOTHING
            """,
            event.event_id,
            event.event_type,
            event.idempotency_key,
            event.aggregate_type,
            event.aggregate_id,
            payload,
            _utc_datetime(event.created_at),
            _utc_datetime(event.created_at),
        )
        # asyncpg returns a command tag, while test doubles and alternate
        # drivers may return nothing. No exception means the idempotent insert
        # was accepted or already present, so both are durable outcomes.
        return True

    async def pending_count(self) -> int:
        row = await self.db.fetchrow(
            """
            SELECT COUNT(*) AS count
            FROM persistence_outbox
            WHERE status IN ('PENDING', 'PROCESSING')
            """
        )
        return int(row["count"]) if row is not None else 0

    async def dispatch_one(self) -> bool:
        """Claim and apply one event; failed transactions remain replayable."""

        row: Any = None
        try:
            async with self.db.transaction() as connection:
                row = await connection.fetchrow(
                    """
                    UPDATE persistence_outbox
                    SET status = 'PROCESSING', attempt_count = attempt_count + 1
                    WHERE event_id = (
                        SELECT event_id
                        FROM persistence_outbox
                        WHERE status = 'PENDING' AND next_attempt_at <= CURRENT_TIMESTAMP
                        ORDER BY created_at, event_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    RETURNING event_id, event_type, payload
                    """
                )
                if row is None:
                    return False
                await self._apply_event(connection, row)
                await connection.execute(
                    """
                    UPDATE persistence_outbox
                    SET status = 'PROCESSED', processed_at = CURRENT_TIMESTAMP, last_error = NULL
                    WHERE event_id = $1
                    """,
                    row["event_id"],
                )
            return True
        except Exception as exc:
            if row is not None:
                await self._mark_retry(row["event_id"], exc)
            raise

    async def _mark_retry(self, event_id: str, error: Exception) -> None:
        # PostgreSQL keeps the outbox row even after a failed domain transaction.
        # The next attempt is bounded, so the dispatcher cannot hot-loop.
        await self.db.execute(
            """
            UPDATE persistence_outbox
            SET status = 'PENDING',
                attempt_count = attempt_count + 1,
                next_attempt_at = CURRENT_TIMESTAMP + INTERVAL '1 second'
                    * LEAST(300, GREATEST(1, POWER(2, attempt_count))),
                last_error = $2
            WHERE event_id = $1
              AND status = 'PENDING'
            """,
            event_id,
            f"{type(error).__name__}: {str(error)[:500]}",
        )

    async def _apply_event(self, connection: Any, row: Mapping[str, Any]) -> None:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        event_type = str(row["event_type"])
        if event_type == "ORDER":
            entity = ExecutionOrder.model_validate(payload["entity"])
            await self.orders.save_order(
                entity, self._instrument(payload, entity.symbol), connection
            )
        elif event_type == "FILL":
            entity = ExchangeFill.model_validate(payload["entity"])
            await self.fills.save_fill(
                entity, self._instrument(payload, entity.symbol), connection
            )
        elif event_type == "POSITION":
            entity = ExchangePosition.model_validate(payload["entity"])
            await self.positions.save_position(
                entity, self._instrument(payload, entity.symbol), connection
            )
        elif event_type == "RISK_SNAPSHOT":
            entity = RiskSnapshot.model_validate(payload["entity"])
            await self.risk.save_snapshot(entity, connection)
        else:
            raise ValueError(f"unsupported persistence event type: {event_type}")

    def _instrument(self, payload: Mapping[str, Any], symbol: str) -> Instrument:
        raw = payload.get("instrument")
        if raw is not None:
            return Instrument.model_validate(raw)
        if self.instrument_rules_provider is not None:
            instrument = self.instrument_rules_provider(symbol)
            if instrument is not None:
                return instrument
        raise InstrumentRulesUnavailable(
            f"exchange-derived instrument rules unavailable for {symbol}"
        )
