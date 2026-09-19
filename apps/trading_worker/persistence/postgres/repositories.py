"""Transactional outbox and idempotent persistence repositories."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Mapping, Optional, Protocol

from apps.trading_worker.persistence.postgres.client import PostgresClient, redact_error
from domain.models import (
    ExchangeFill,
    ExchangePosition,
    ExecutionOrder,
    Instrument,
    RiskSnapshot,
)

logger = logging.getLogger("blessing.persistence.repositories")

_IMMUTABLE_IMAGE_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$", re.IGNORECASE)


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
        raise ValueError("persistence timestamps must be timezone-aware")
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


def _fill_storage_id(fill: ExchangeFill, instrument: Instrument) -> str:
    """Return a bounded id for a venue-scoped exchange trade identity."""

    identity = (
        f"{instrument.venue}:{str(fill.symbol).upper()}:{fill.exchange_trade_id}"
    )
    # fills.fill_id is VARCHAR(64); a digest preserves the complete identity
    # without truncating an exchange-provided trade id.
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


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
        # The exchange trade id is not globally unique across Binance
        # environments. Store a bounded digest of the canonical venue-scoped
        # identity while preserving exchange_trade_id as the provider's
        # original identifier.
        fill_id = _fill_storage_id(fill, instrument)
        await connection.execute(
            """
            INSERT INTO fills (
                fill_id, client_order_id, exchange_order_id, exchange_trade_id, symbol, side,
                venue, position_side, price, quantity, fee, fee_asset, is_maker, executed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (venue, exchange_trade_id) DO UPDATE SET
                client_order_id = EXCLUDED.client_order_id,
                exchange_order_id = EXCLUDED.exchange_order_id,
                exchange_trade_id = EXCLUDED.exchange_trade_id,
                venue = EXCLUDED.venue,
                position_side = EXCLUDED.position_side,
                price = EXCLUDED.price,
                quantity = EXCLUDED.quantity,
                fee = EXCLUDED.fee,
                fee_asset = EXCLUDED.fee_asset,
                is_maker = EXCLUDED.is_maker,
                executed_at = EXCLUDED.executed_at
            """,
            fill_id,
            fill.client_order_id,
            fill.exchange_order_id,
            fill.exchange_trade_id,
            fill.symbol,
            str(_enum_value(fill.side)),
            instrument.venue,
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
                "realized_pnl_24h": str(snapshot.realized_pnl_24h),
                "realized_pnl_24h_known": snapshot.realized_pnl_24h_known,
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

    async def create_mainnet_launch_session(
        self,
        *,
        launch_id: str,
        approval_id: str,
        image_digest: str,
        symbol: str = "ETHUSDC",
        policy: str = "STAGED_FIRST_ORDER",
        max_risk_increasing_orders: int = 1,
    ) -> Mapping[str, Any]:
        """Create or verify the durable staged-launch session.

        Repeating ARM after a process restart is idempotent for the same
        approval, but it can never reset reserved/submitted order counters.
        """

        if not launch_id or not approval_id or not image_digest:
            raise ValueError("launch session identity is incomplete")
        if not _IMMUTABLE_IMAGE_RE.fullmatch(image_digest):
            raise ValueError("launch session requires an immutable image digest")
        if symbol.upper() != "ETHUSDC" or policy != "STAGED_FIRST_ORDER":
            raise ValueError("Mainnet launch session is bounded to ETHUSDC staged launch")
        if max_risk_increasing_orders != 1:
            raise ValueError("Mainnet staged launch permits exactly one risk-increasing order")
        await self.db.execute(
            """
            UPDATE mainnet_launch_sessions
            SET state = 'CLOSED', updated_at = CURRENT_TIMESTAMP
            WHERE symbol = $1
              AND approval_id != $2
              AND state IN ('ACTIVE', 'PAUSED_NEW_RISK', 'RECONCILIATION_REQUIRED', 'REAUTH_REQUIRED')
            """,
            symbol.upper(),
            approval_id,
        )
        await self.db.execute(
            """
            INSERT INTO mainnet_launch_sessions (
                launch_id, approval_id, image_digest, symbol, policy,
                max_risk_increasing_orders, reserved_orders, submitted_orders,
                state, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, 0, 0, 'ACTIVE', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (approval_id) DO NOTHING
            """,
            launch_id,
            approval_id,
            image_digest,
            symbol.upper(),
            policy,
            max_risk_increasing_orders,
        )
        row = await self.db.fetchrow(
            """
            SELECT launch_id, approval_id, image_digest, symbol, policy,
                   max_risk_increasing_orders, reserved_orders, submitted_orders,
                   state, continuation_approval_id, first_order_verified_at,
                   autonomous_approved_at, last_restart_at, created_at, updated_at
            FROM mainnet_launch_sessions
            WHERE approval_id = $1
            """,
            approval_id,
        )
        if row is None:
            raise RuntimeError("mainnet launch session could not be read after creation")
        if (
            str(row["launch_id"]) != launch_id
            or str(row["image_digest"]) != image_digest
            or str(row["symbol"]).upper() != symbol.upper()
            or str(row["policy"]) != policy
        ):
            raise RuntimeError("existing launch session does not match release approval")
        return dict(row)

    async def reserve_mainnet_risk_order(self, launch_id: str) -> bool:
        """Atomically reserve a risk-increasing order slot.

        Staged sessions have one slot.  Autonomous sessions deliberately have
        no session-wide count limit; the deterministic risk governor and
        exchange-derived order caps remain the limits for each order.
        """

        row = await self.db.fetchrow(
            """
            UPDATE mainnet_launch_sessions
            SET reserved_orders = reserved_orders + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1
              AND (
                (policy = 'STAGED_FIRST_ORDER'
                 AND state = 'ACTIVE'
                 AND submitted_orders = 0
                 AND reserved_orders < max_risk_increasing_orders)
                OR
                (policy = 'AUTONOMOUS_AFTER_REVIEW'
                 AND state = 'AUTONOMOUS_ACTIVE')
              )
            RETURNING launch_id
            """,
            launch_id,
        )
        return row is not None

    async def release_mainnet_risk_order_reservation(self, launch_id: str) -> bool:
        """Release a slot only after a definitive exchange rejection."""

        result = await self.db.execute(
            """
            UPDATE mainnet_launch_sessions
            SET reserved_orders = reserved_orders - 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1
              AND state IN ('ACTIVE', 'AUTONOMOUS_ACTIVE')
              AND reserved_orders > submitted_orders
            """,
            launch_id,
        )
        return str(result).upper().startswith("UPDATE 1")

    async def mark_mainnet_risk_order_submitted(self, launch_id: str) -> bool:
        """Persist an order outcome atomically with the launch lifecycle."""

        row = await self.db.fetchrow(
            """
            UPDATE mainnet_launch_sessions
            SET submitted_orders = submitted_orders + 1,
                state = CASE
                    WHEN policy = 'STAGED_FIRST_ORDER' THEN 'PAUSED_NEW_RISK'
                    ELSE 'AUTONOMOUS_ACTIVE'
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1
              AND state IN ('ACTIVE', 'AUTONOMOUS_ACTIVE')
              AND reserved_orders > submitted_orders
              AND (
                max_risk_increasing_orders IS NULL
                OR submitted_orders < max_risk_increasing_orders
              )
            RETURNING launch_id, submitted_orders, state
            """,
            launch_id,
        )
        return row is not None

    async def mark_mainnet_launch_reconciliation_required(self, launch_id: str) -> bool:
        result = await self.db.execute(
            """
            UPDATE mainnet_launch_sessions
            SET state = 'RECONCILIATION_REQUIRED', updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1
              AND state IN ('ACTIVE', 'PAUSED_NEW_RISK', 'AUTONOMOUS_ACTIVE')
            """,
            launch_id,
        )
        return str(result).upper().startswith("UPDATE 1")

    async def mark_mainnet_launch_reconciled(self, launch_id: str) -> bool:
        """Clear an exchange-ambiguity fence without resuming risk locally.

        A later authoritative reconciliation may clear the fence, but it must
        never resume autonomous risk by itself: staged sessions return to the
        paused review state, while autonomous sessions require a fresh
        continuation approval.
        """
        result = await self.db.execute(
            """
            UPDATE mainnet_launch_sessions
            SET state = CASE
                    WHEN policy = 'STAGED_FIRST_ORDER' THEN 'PAUSED_NEW_RISK'
                    ELSE 'REAUTH_REQUIRED'
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1
              AND state = 'RECONCILIATION_REQUIRED'
              AND submitted_orders >= 1
              AND reserved_orders >= submitted_orders
            """,
            launch_id,
        )
        return str(result).upper().startswith("UPDATE 1")

    async def activate_mainnet_autonomous(
        self,
        *,
        launch_id: str,
        continuation_approval_id: str,
        first_order_verified_at: Optional[datetime] = None,
        image_digest: str,
    ) -> Optional[Mapping[str, Any]]:
        """Atomically convert a verified staged launch into autonomous mode.

        The WHERE clause is the durable authorization boundary: exactly one
        submitted staged order, paused state, matching image, and a unique
        continuation approval are all required.  A retry after success returns
        no row and therefore cannot silently re-authorize a different session.
        """

        if not launch_id or not continuation_approval_id:
            raise ValueError("autonomous continuation identity is incomplete")
        if not _IMMUTABLE_IMAGE_RE.fullmatch(image_digest):
            raise ValueError("autonomous continuation requires an immutable image digest")
        verified_at = _utc_datetime(first_order_verified_at)
        row = await self.db.fetchrow(
            """
            UPDATE mainnet_launch_sessions
            SET policy = 'AUTONOMOUS_AFTER_REVIEW',
                max_risk_increasing_orders = NULL,
                continuation_approval_id = $2,
                first_order_verified_at = $3,
                autonomous_approved_at = CURRENT_TIMESTAMP,
                state = 'AUTONOMOUS_ACTIVE',
                updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1
              AND image_digest = $4
              AND symbol = 'ETHUSDC'
              AND (
                (
                    policy = 'STAGED_FIRST_ORDER'
                    AND state = 'PAUSED_NEW_RISK'
                    AND submitted_orders = 1
                    AND reserved_orders >= submitted_orders
                    AND continuation_approval_id IS NULL
                )
                OR
                (
                    policy = 'AUTONOMOUS_AFTER_REVIEW'
                    AND state = 'REAUTH_REQUIRED'
                    AND submitted_orders >= 1
                )
              )
            RETURNING launch_id, approval_id, image_digest, symbol, policy,
                      max_risk_increasing_orders, reserved_orders,
                      submitted_orders, state, continuation_approval_id,
                      first_order_verified_at, autonomous_approved_at,
                      last_restart_at, created_at, updated_at
            """,
            launch_id,
            continuation_approval_id,
            verified_at,
            image_digest,
        )
        return dict(row) if row is not None else None

    async def mark_mainnet_launches_reauth_required(
        self, symbol: str = "ETHUSDC"
    ) -> int:
        """Fence autonomous state after a process/revision restart."""

        result = await self.db.execute(
            """
            UPDATE mainnet_launch_sessions
            SET state = 'REAUTH_REQUIRED',
                last_restart_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE symbol = $1
              AND policy = 'AUTONOMOUS_AFTER_REVIEW'
              AND state = 'AUTONOMOUS_ACTIVE'
            """,
            symbol.upper(),
        )
        match = re.search(r"UPDATE\s+(\d+)", str(result).upper())
        return int(match.group(1)) if match else 0

    async def get_mainnet_launch(self, launch_id: str) -> Optional[Mapping[str, Any]]:
        row = await self.db.fetchrow(
            """
            SELECT launch_id, approval_id, image_digest, symbol, policy,
                   max_risk_increasing_orders, reserved_orders, submitted_orders,
                   state, continuation_approval_id, first_order_verified_at,
                   autonomous_approved_at, last_restart_at, created_at, updated_at
            FROM mainnet_launch_sessions
            WHERE launch_id = $1
            """,
            launch_id,
        )
        return dict(row) if row is not None else None

    async def get_active_mainnet_launch(self, symbol: str = "ETHUSDC") -> Optional[Mapping[str, Any]]:
        row = await self.db.fetchrow(
            """
            SELECT launch_id, approval_id, image_digest, symbol, policy,
                   max_risk_increasing_orders, reserved_orders, submitted_orders,
                   state, continuation_approval_id, first_order_verified_at,
                   autonomous_approved_at, last_restart_at, created_at, updated_at
            FROM mainnet_launch_sessions
            WHERE symbol = $1
              AND state IN (
                'ACTIVE', 'PAUSED_NEW_RISK', 'RECONCILIATION_REQUIRED',
                'AUTONOMOUS_ACTIVE', 'REAUTH_REQUIRED'
              )
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            symbol.upper(),
        )
        return dict(row) if row is not None else None

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
                await connection.execute(
                    """
                    UPDATE persistence_outbox
                    SET status = 'PENDING', claimed_at = NULL
                    WHERE status = 'PROCESSING'
                      AND (
                          claimed_at IS NULL
                          OR claimed_at <= CURRENT_TIMESTAMP - INTERVAL '5 minutes'
                      )
                    """
                )
                row = await connection.fetchrow(
                    """
                    UPDATE persistence_outbox
                    SET status = 'PROCESSING',
                        claimed_at = CURRENT_TIMESTAMP,
                        attempt_count = attempt_count + 1
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
                    SET status = 'PROCESSED',
                        claimed_at = NULL,
                        processed_at = CURRENT_TIMESTAMP,
                        last_error = NULL
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
                claimed_at = NULL,
                attempt_count = attempt_count + 1,
                next_attempt_at = CURRENT_TIMESTAMP + INTERVAL '1 second'
                    * LEAST(300, GREATEST(1, POWER(2, attempt_count))),
                last_error = $2
            WHERE event_id = $1
              AND status = 'PENDING'
            """,
            event_id,
            redact_error(f"{type(error).__name__}: {str(error)}"),
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
