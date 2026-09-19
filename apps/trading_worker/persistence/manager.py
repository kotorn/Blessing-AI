"""Bounded, observable persistence manager backed by a transactional outbox."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from decimal import Decimal
from typing import Any, Mapping, Optional

from apps.trading_worker.persistence.postgres.client import (
    PostgresClient,
    get_postgres_client,
    redact_error,
)
from apps.trading_worker.execution_lease import PostgresExecutionLease
from apps.trading_worker.persistence.postgres.repositories import (
    InstrumentRulesProvider,
    PersistenceRepository,
)
from apps.trading_worker.persistence.postgres.ledger import PostgresExecutionLedger
from domain.models import ExchangeFill, ExchangePosition, ExecutionOrder, Instrument, RiskSnapshot

logger = logging.getLogger("blessing.persistence.manager")


class PersistenceMode(str, Enum):
    DISABLED = "DISABLED"
    OPTIONAL = "OPTIONAL"
    REQUIRED = "REQUIRED"


@dataclass(frozen=True, slots=True)
class PersistenceConfig:
    mode: PersistenceMode = PersistenceMode.OPTIONAL
    queue_capacity: int = 256
    retry_base_seconds: float = 0.25
    retry_max_seconds: float = 30.0
    drain_timeout_seconds: float = 10.0
    poll_interval_seconds: float = 0.25
    pre_submission_timeout_seconds: float = 5.0

    @classmethod
    def from_environment(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "PersistenceConfig":
        values = environ if environ is not None else os.environ
        raw_mode = str(values.get("PERSISTENCE_MODE", "OPTIONAL")).strip().upper()
        try:
            mode = PersistenceMode(raw_mode)
        except ValueError as exc:
            raise ValueError(
                "PERSISTENCE_MODE must be DISABLED, OPTIONAL, or REQUIRED"
            ) from exc

        def positive_int(name: str, default: int) -> int:
            raw = str(values.get(name, default)).strip()
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be a positive integer") from exc
            if value <= 0:
                raise ValueError(f"{name} must be a positive integer")
            return value

        def positive_float(name: str, default: float) -> float:
            raw = str(values.get(name, default)).strip()
            try:
                value = float(raw)
            except ValueError as exc:
                raise ValueError(f"{name} must be a positive number") from exc
            if not value > 0:
                raise ValueError(f"{name} must be a positive number")
            return value

        return cls(
            mode=mode,
            queue_capacity=positive_int("PERSISTENCE_QUEUE_CAPACITY", 256),
            retry_base_seconds=positive_float("PERSISTENCE_RETRY_BASE_SECONDS", 0.25),
            retry_max_seconds=positive_float("PERSISTENCE_RETRY_MAX_SECONDS", 30.0),
            drain_timeout_seconds=positive_float(
                "PERSISTENCE_DRAIN_TIMEOUT_SECONDS", 10.0
            ),
            poll_interval_seconds=positive_float("PERSISTENCE_POLL_INTERVAL_SECONDS", 0.25),
            pre_submission_timeout_seconds=positive_float(
                "PERSISTENCE_PRE_SUBMISSION_TIMEOUT_SECONDS", 5.0
            ),
        )


@dataclass(frozen=True, slots=True)
class _PersistenceEvent:
    event_id: str
    event_type: str
    idempotency_key: str
    aggregate_type: str
    aggregate_id: str
    created_at: datetime
    payload: Mapping[str, Any]


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _utc(value: object) -> datetime:
    """Normalize model and exchange timestamps to timezone-aware UTC."""

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


def _jsonable(model: object) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        value = model.model_dump(mode="json")  # type: ignore[attr-defined]
    elif hasattr(model, "dict"):
        value = model.dict()  # type: ignore[attr-defined]
    else:
        raise TypeError(f"unsupported persistence model: {type(model).__name__}")
    return json.loads(json.dumps(value, default=str))


def _event_id(event_type: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(
        f"{event_type}:{idempotency_key}".encode("utf-8")
    ).hexdigest()
    return f"{event_type}-{digest[:32]}"


class PersistenceManager:
    """Coordinates durable writes without blocking the execution path.

    The in-memory queue is intentionally bounded. Once an event reaches the
    PostgreSQL outbox it is durable and can be replayed independently of the
    worker process. A queue rejection or DB failure is always reflected in the
    status object and never presented as durable success.
    """

    def __init__(
        self,
        db: Optional[PostgresClient] = None,
        *,
        config: Optional[PersistenceConfig] = None,
        instrument_rules_provider: Optional[InstrumentRulesProvider] = None,
    ) -> None:
        self.config = config or PersistenceConfig.from_environment()
        self.db = db or get_postgres_client()
        self.instrument_rules_provider = instrument_rules_provider
        self.repository: Optional[PersistenceRepository] = None
        self.orders: Any = None
        self.fills: Any = None
        self.positions: Any = None
        self.risk: Any = None

        self.is_connected = False
        self._accepting = False
        self._stop_event = asyncio.Event()
        self._write_queue: asyncio.Queue[_PersistenceEvent] = asyncio.Queue(
            maxsize=self.config.queue_capacity
        )
        self._writer_task: Optional[asyncio.Task[None]] = None
        self._dispatcher_task: Optional[asyncio.Task[None]] = None
        self._stop_deadline: Optional[float] = None
        self._inflight: Optional[_PersistenceEvent] = None
        self._state = "STOPPED"
        self._last_error: Optional[str] = None
        self._failed_writes = 0
        self._dropped_writes = 0
        self._disabled_writes = 0
        self._retry_count = 0
        self._unflushed_writes = 0
        self._pending_outbox: Optional[int] = None
        self._mainnet_launch_session: Optional[dict[str, Any]] = None

    @property
    def mode(self) -> PersistenceMode:
        return self.config.mode

    def set_instrument_rules_provider(
        self, provider: Optional[InstrumentRulesProvider]
    ) -> None:
        self.instrument_rules_provider = provider
        if self.repository is not None:
            self.repository.instrument_rules_provider = provider

    def create_execution_lease(
        self,
        scope_key: str,
        *,
        owner_id: Optional[str] = None,
        ttl_seconds: float = 10.0,
    ) -> PostgresExecutionLease:
        """Create a DB-backed fencing lease; callers must acquire it explicitly."""

        if not self.is_connected or self.db.pool is None:
            raise RuntimeError("Durable persistence is required before creating an execution lease")
        return PostgresExecutionLease(
            self.db,
            scope_key,
            owner_id=owner_id,
            ttl_seconds=ttl_seconds,
        )

    def validate_execution_mode(self, execution_mode: str) -> None:
        """Require durable persistence before any future live-capital mode."""

        normalized = str(_enum_value(execution_mode)).upper()
        if normalized in {"LIVE", "SMALL_LIVE"} and self.mode is not PersistenceMode.REQUIRED:
            raise RuntimeError(
                "Small Live/Live execution requires PERSISTENCE_MODE=REQUIRED"
            )

    def _require_durable_launch_repository(self) -> PersistenceRepository:
        if (
            self.mode is not PersistenceMode.REQUIRED
            or not self.is_connected
            or not self.repository
            or not self.readiness().get("durable")
        ):
            raise RuntimeError("Mainnet launch requires durable REQUIRED persistence")
        return self.repository

    async def create_mainnet_launch_session(
        self,
        *,
        approval_id: str,
        image_digest: str,
        symbol: str = "ETHUSDC",
    ) -> dict[str, Any]:
        repository = self._require_durable_launch_repository()
        launch_id = f"launch-{approval_id}"
        session = await repository.create_mainnet_launch_session(
            launch_id=launch_id,
            approval_id=approval_id,
            image_digest=image_digest,
            symbol=symbol,
        )
        self._mainnet_launch_session = dict(session)
        return dict(session)

    async def reserve_mainnet_risk_order(self, launch_id: str) -> bool:
        repository = self._require_durable_launch_repository()
        reserved = await repository.reserve_mainnet_risk_order(launch_id)
        if reserved:
            self._mainnet_launch_session = dict(
                await repository.get_active_mainnet_launch("ETHUSDC") or {}
            )
        else:
            self._record_error("staged Mainnet risk-order reservation was unavailable")
        return reserved

    async def release_mainnet_risk_order_reservation(self, launch_id: str) -> bool:
        repository = self._require_durable_launch_repository()
        released = await repository.release_mainnet_risk_order_reservation(launch_id)
        self._mainnet_launch_session = dict(
            await repository.get_active_mainnet_launch("ETHUSDC") or {}
        )
        return released

    async def mark_mainnet_risk_order_submitted(self, launch_id: str) -> bool:
        repository = self._require_durable_launch_repository()
        marked = await repository.mark_mainnet_risk_order_submitted(launch_id)
        if not marked:
            self._record_error("staged Mainnet submission could not be durably marked")
            return False
        self._mainnet_launch_session = dict(
            await repository.get_active_mainnet_launch("ETHUSDC") or {}
        )
        return True

    async def mark_mainnet_launch_reconciliation_required(self, launch_id: str) -> bool:
        repository = self._require_durable_launch_repository()
        marked = await repository.mark_mainnet_launch_reconciliation_required(launch_id)
        self._mainnet_launch_session = dict(
            await repository.get_active_mainnet_launch("ETHUSDC") or {}
        )
        return marked

    async def mark_mainnet_launch_reconciled(self, launch_id: str) -> bool:
        """Clear an exchange-ambiguity fence without resuming local risk."""
        repository = self._require_durable_launch_repository()
        marked = await repository.mark_mainnet_launch_reconciled(launch_id)
        self._mainnet_launch_session = dict(
            await repository.get_active_mainnet_launch("ETHUSDC") or {}
        ) or None
        return marked

    async def get_mainnet_launch_session(
        self, launch_id: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Read the durable launch row without creating or resetting state."""

        repository = self._require_durable_launch_repository()
        session = (
            await repository.get_mainnet_launch(launch_id)
            if launch_id
            else await repository.get_active_mainnet_launch("ETHUSDC")
        )
        self._mainnet_launch_session = dict(session) if session else None
        return dict(session) if session else None

    async def activate_mainnet_autonomous(
        self,
        *,
        launch_id: str,
        continuation_approval_id: str,
        first_order_verified_at: Optional[datetime] = None,
        image_digest: str,
    ) -> Optional[dict[str, Any]]:
        """Atomically consume the staged session for autonomous continuation."""

        repository = self._require_durable_launch_repository()
        session = await repository.activate_mainnet_autonomous(
            launch_id=launch_id,
            continuation_approval_id=continuation_approval_id,
            first_order_verified_at=first_order_verified_at,
            image_digest=image_digest,
        )
        self._mainnet_launch_session = dict(session) if session else None
        if session is None:
            self._record_error("autonomous continuation transition was not accepted")
            return None
        return dict(session)

    async def mark_mainnet_launches_reauth_required(self) -> int:
        """Fence any autonomous session on every worker start/revision."""

        repository = self._require_durable_launch_repository()
        changed = await repository.mark_mainnet_launches_reauth_required("ETHUSDC")
        self._mainnet_launch_session = dict(
            await repository.get_active_mainnet_launch("ETHUSDC") or {}
        ) or None
        return changed

    async def create_execution_ledger(
        self,
        *,
        symbol: str,
        venue: str,
    ) -> PostgresExecutionLedger:
        """Load one fixed exchange scope from the durable SQL ledger.

        Mainnet preflight and the live adapter must not compare Binance state
        against an empty process-local ledger. Loading is read-only; later
        observations are persisted through the normal outbox callbacks.
        """

        if (
            self.mode is not PersistenceMode.REQUIRED
            or not self.is_connected
            or not self.repository
            or not self.readiness().get("durable")
        ):
            raise RuntimeError("durable execution ledger requires REQUIRED persistence")
        return await PostgresExecutionLedger.load(
            self.db,
            symbol=symbol,
            venue=venue,
        )

    async def start(self) -> bool:
        if self.mode is PersistenceMode.DISABLED:
            self._state = "DISABLED"
            self._accepting = True
            return True
        if self._writer_task and not self._writer_task.done():
            return self.is_connected

        self._stop_event = asyncio.Event()
        self._stop_deadline = None
        try:
            await self.db.connect()
            self.repository = PersistenceRepository(
                self.db,
                instrument_rules_provider=self.instrument_rules_provider,
            )
            self.orders = self.repository.orders
            self.fills = self.repository.fills
            self.positions = self.repository.positions
            self.risk = self.repository.risk
            self.is_connected = True
            self._accepting = True
            self._state = "READY"
            self._last_error = None
            self._writer_task = asyncio.create_task(self._outbox_writer())
            self._dispatcher_task = asyncio.create_task(self._outbox_dispatcher())
            await self._refresh_pending_count()
            logger.info("Persistence manager started with transactional outbox")
            return True
        except Exception as exc:
            tasks = [task for task in (self._writer_task, self._dispatcher_task) if task]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._writer_task = None
            self._dispatcher_task = None
            self.repository = None
            self.orders = None
            self.fills = None
            self.positions = None
            self.risk = None
            self.is_connected = False
            self._accepting = self.mode is PersistenceMode.OPTIONAL
            self._state = "DEGRADED" if self.mode is PersistenceMode.OPTIONAL else "FAILED"
            self._record_error(exc)
            await self.db.disconnect()
            logger.error(
                "Persistence startup failed in %s mode (%s)",
                self.mode.value,
                type(exc).__name__,
            )
            if self.mode is PersistenceMode.REQUIRED:
                raise
            return False

    async def stop(self) -> None:
        if self.mode is PersistenceMode.DISABLED:
            self._accepting = False
            self._state = "STOPPED"
            return

        self._accepting = False
        self._stop_event.set()
        self._stop_deadline = time.monotonic() + self.config.drain_timeout_seconds

        tasks = [task for task in (self._writer_task, self._dispatcher_task) if task]
        for task in tasks:
            remaining = max(0.0, self._stop_deadline - time.monotonic())
            if task.done():
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except asyncio.TimeoutError:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        queued = self._write_queue.qsize()
        self._unflushed_writes += queued
        await self._refresh_pending_count()
        self.is_connected = False
        await self.db.disconnect()
        self._writer_task = None
        self._dispatcher_task = None
        self._state = "STOPPED"
        logger.info(
            "Persistence manager stopped; unflushed=%d pending_outbox=%s",
            self._unflushed_writes,
            self._pending_outbox,
        )

    def readiness(self) -> dict[str, Any]:
        durable = self.is_connected and self._state == "READY"
        return {
            "mode": self.mode.value,
            "state": self._state,
            "connected": self.is_connected,
            "ready": self.mode is PersistenceMode.DISABLED or durable,
            "durable": durable,
            "queue_size": self._write_queue.qsize(),
            "queue_capacity": self.config.queue_capacity,
            "pending_outbox": self._pending_outbox,
            "failed_writes": self._failed_writes,
            "dropped_writes": self._dropped_writes,
            "disabled_writes": self._disabled_writes,
            "retry_count": self._retry_count,
            "unflushed_writes": self._unflushed_writes,
            "last_error": self._last_error,
            "mainnet_launch_session": self._mainnet_launch_session,
        }

    status = readiness

    def _record_error(self, error: Exception | str) -> None:
        if isinstance(error, str):
            self._last_error = redact_error(error)
        else:
            self._last_error = redact_error(
                f"{type(error).__name__}: {str(error)}"
            )

    def _record_failure(self, error: Exception) -> None:
        self._failed_writes += 1
        self._retry_count += 1
        self._state = "DEGRADED"
        self._record_error(error)
        logger.error(
            "monitor_event=persistence_outbox_failure mode=%s error_class=%s",
            self.mode.value,
            type(error).__name__,
        )

    def _enqueue(
        self,
        entity: object,
        event_type: str,
        *,
        idempotency_key: str,
        aggregate_type: str,
        aggregate_id: str,
        created_at: object,
        instrument: Optional[Instrument] = None,
    ) -> bool:
        return self._enqueue_event(
            self._event(
                entity,
                event_type,
                idempotency_key=idempotency_key,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                created_at=created_at,
                instrument=instrument,
            )
        )

    def _instrument_for(self, symbol: str) -> Optional[Instrument]:
        if self.instrument_rules_provider is None:
            return None
        try:
            return self.instrument_rules_provider(str(symbol).upper())
        except Exception as exc:
            self._failed_writes += 1
            self._dropped_writes += 1
            self._record_error(exc)
            return None

    def _require_instrument(self, symbol: str) -> Optional[Instrument]:
        instrument = self._instrument_for(symbol)
        if instrument is None:
            self._failed_writes += 1
            self._dropped_writes += 1
            self._record_error(
                f"exchange-derived instrument rules unavailable for {str(symbol).upper()}"
            )
        return instrument

    def _order_event(self, order: ExecutionOrder) -> Optional[_PersistenceEvent]:
        """Build the single canonical outbox event for an order observation."""

        instrument = self._require_instrument(order.symbol)
        if instrument is None:
            return None
        timestamp = _utc(order.timestamp)
        idempotency_key = (
            f"{order.client_order_id}:{order.status}:{timestamp.isoformat()}"
        )
        return self._event(
            order,
            "ORDER",
            idempotency_key=idempotency_key,
            aggregate_type="ORDER",
            aggregate_id=order.client_order_id,
            created_at=timestamp,
            instrument=instrument,
        )

    def _event(
        self,
        entity: object,
        event_type: str,
        *,
        idempotency_key: str,
        aggregate_type: str,
        aggregate_id: str,
        created_at: object,
        instrument: Optional[Instrument] = None,
    ) -> _PersistenceEvent:
        payload: dict[str, Any] = {"entity": _jsonable(entity)}
        if instrument is not None:
            payload["instrument"] = _jsonable(instrument)
        return _PersistenceEvent(
            event_id=_event_id(event_type, idempotency_key),
            event_type=event_type,
            idempotency_key=idempotency_key,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            created_at=_utc(created_at),
            payload=payload,
        )

    async def ensure_order_durable(self, order: ExecutionOrder) -> bool:
        """Synchronously append an order observation to PostgreSQL outbox.

        This is the pre-submission barrier for risk-increasing exchange orders.
        It intentionally bypasses the in-memory queue: returning ``True`` means
        the database acknowledged the outbox insert, so a crash or dispatcher
        outage can still be recovered by replaying the event.
        """

        if self.mode is PersistenceMode.DISABLED:
            self._disabled_writes += 1
            self._record_error("durable order submission is disabled")
            return False
        if not self._accepting or not self.is_connected or self.repository is None:
            self._dropped_writes += 1
            self._record_error("durable order submission requires a connected outbox")
            return False

        event = self._order_event(order)
        if event is None:
            return False
        try:
            await asyncio.wait_for(
                self._append_with_retry(event),
                timeout=self.config.pre_submission_timeout_seconds,
            )
            await self._refresh_pending_count()
            return True
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._record_error("durable order outbox acknowledgement timed out")
            self._state = "DEGRADED"
            return False
        except Exception as exc:
            self._record_error(exc)
            self._state = "DEGRADED"
            return False

    def enqueue_order(self, order: ExecutionOrder) -> bool:
        event = self._order_event(order)
        if event is None:
            return False
        return self._enqueue_event(event)

    def enqueue_fill(self, fill: ExchangeFill) -> bool:
        instrument = self._require_instrument(fill.symbol)
        if instrument is None:
            return False
        timestamp = (
            fill.event_time if fill.event_time is not None else fill.transaction_time
        )
        # Exchange trade ids are scoped to a venue/environment. Include the
        # exchange-derived venue in the outbox identity so Testnet and Mainnet
        # observations cannot collide in one Cloud SQL account.
        fill_identity = (
            f"{instrument.venue}:{str(fill.symbol).upper()}:{fill.exchange_trade_id}"
        )
        return self._enqueue(
            fill,
            "FILL",
            idempotency_key=fill_identity,
            aggregate_type="FILL",
            aggregate_id=fill_identity,
            created_at=timestamp,
            instrument=instrument,
        )

    def enqueue_position(self, position: ExchangePosition) -> bool:
        qty = getattr(position, "quantity", getattr(position, "position_amount", Decimal("1")))
        if qty == Decimal("0"):
            instrument = self._instrument_for(position.symbol)
            if instrument is None:
                return False
        else:
            instrument = self._require_instrument(position.symbol)
            if instrument is None:
                return False
        timestamp = _utc(position.event_time)
        position_side = str(_enum_value(position.position_side))
        # REST-refreshed positions (emergency flatten, reconciliation) never
        # carry a real source -- venues/binance/ledger.py's
        # _to_exchange_position explicitly writes the literal "UNKNOWN"
        # sentinel for them, unlike WS ACCOUNT_UPDATE-derived positions,
        # which tag it with the live environment label. Treat that sentinel
        # (and a genuinely empty value) the same: fall back to the same live
        # instrument.venue identity enqueue_fill/order already use, so the
        # same logical position never splits into two outbox identities
        # depending on which subsystem last touched it.
        venue = str(getattr(position, "source", "")).strip().upper()
        if not venue or venue == "UNKNOWN":
            venue = instrument.venue
        return self._enqueue(
            position,
            "POSITION",
            idempotency_key=(
                f"{venue}:{position.symbol.upper()}:{position_side}:"
                f"{timestamp.isoformat()}"
            ),
            aggregate_type="POSITION",
            aggregate_id=f"{venue}:{position.symbol.upper()}:{position_side}",
            created_at=timestamp,
            instrument=instrument,
        )

    def enqueue_risk_snapshot(self, snapshot: RiskSnapshot) -> bool:
        return self._enqueue(
            snapshot,
            "RISK_SNAPSHOT",
            idempotency_key=f"portfolio:{_utc(snapshot.timestamp).isoformat()}",
            aggregate_type="RISK_SNAPSHOT",
            aggregate_id="portfolio",
            created_at=snapshot.timestamp,
        )

    async def _append_with_retry(self, event: _PersistenceEvent) -> None:
        if self.repository is None:
            raise RuntimeError("persistence repository is not initialized")
        attempt = 0
        while True:
            try:
                await self.repository.append_outbox(event)
                self.is_connected = True
                self._state = "READY"
                self._last_error = None
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_failure(exc)
                attempt += 1
                if self._stop_deadline is not None and time.monotonic() >= self._stop_deadline:
                    raise
                delay = min(
                    self.config.retry_max_seconds,
                    self.config.retry_base_seconds * (2 ** min(attempt - 1, 10)),
                )
                if self._stop_deadline is not None:
                    delay = min(
                        delay,
                        max(0.0, self._stop_deadline - time.monotonic()),
                    )
                await asyncio.sleep(delay)

    def _enqueue_event(self, event: _PersistenceEvent) -> bool:
        if self.mode is PersistenceMode.DISABLED:
            self._disabled_writes += 1
            return False
        if not self._accepting or not self.is_connected:
            self._dropped_writes += 1
            self._record_error("persistence is not connected")
            return False
        try:
            self._write_queue.put_nowait(event)
        except asyncio.QueueFull:
            self._failed_writes += 1
            self._dropped_writes += 1
            self._record_error("persistence write queue is full")
            logger.error(
                "monitor_event=persistence_outbox_queue_full mode=%s queue_capacity=%d",
                self.mode.value,
                self.config.queue_capacity,
            )
            if self.mode is PersistenceMode.REQUIRED:
                self._state = "DEGRADED"
            return False
        return True

    async def _outbox_writer(self) -> None:
        while self._accepting or not self._write_queue.empty():
            try:
                event = await asyncio.wait_for(self._write_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            self._inflight = event
            durable = False
            try:
                await self._append_with_retry(event)
                durable = True
            except asyncio.CancelledError:
                raise
            except Exception:
                # The event was not acknowledged as durable. Keep the failure
                # visible and let shutdown report the remaining queue state.
                pass
            finally:
                if not durable:
                    self._unflushed_writes += 1
                self._inflight = None
                self._write_queue.task_done()

    async def _outbox_dispatcher(self) -> None:
        if self.repository is None:
            return
        while True:
            stopping = self._stop_event.is_set()
            if stopping and self._write_queue.empty():
                if self._stop_deadline is not None and time.monotonic() >= self._stop_deadline:
                    return
            try:
                processed = await self.repository.dispatch_one()
                await self._refresh_pending_count()
                if not processed:
                    if stopping and self._write_queue.empty():
                        return
                    await asyncio.sleep(self.config.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_failure(exc)
                if (
                    self._stop_event.is_set()
                    and self._stop_deadline is not None
                    and time.monotonic() >= self._stop_deadline
                ):
                    return
                await asyncio.sleep(
                    min(self.config.retry_max_seconds, self.config.retry_base_seconds)
                )

    async def _refresh_pending_count(self) -> None:
        if self.repository is None:
            self._pending_outbox = None
            return
        try:
            self._pending_outbox = await self.repository.pending_count()
        except Exception as exc:
            self._record_failure(exc)
