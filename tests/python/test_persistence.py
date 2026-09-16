from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from apps.trading_worker.persistence import (
    PersistenceConfig,
    PersistenceManager,
    PersistenceMode,
)
from apps.trading_worker.persistence.manager import _PersistenceEvent
from apps.trading_worker.persistence.postgres.client import (
    PostgresSettings,
    get_postgres_client,
)
from apps.trading_worker.persistence.postgres.repositories import (
    FillRepository,
    PersistenceRepository,
    PositionRepository,
    InstrumentRulesUnavailable,
    OrderRepository,
)
from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules
from domain.enums import MarketType, OrderSide, PositionSide, RiskState, TimeInForce
from domain.models import (
    ExchangeFill,
    ExchangePosition,
    ExecutionOrder,
    Instrument,
    RiskSnapshot,
)


PERSISTED_AT = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=timezone(timedelta(hours=7)))


def _risk_snapshot(timestamp: datetime = PERSISTED_AT) -> RiskSnapshot:
    return RiskSnapshot(
        timestamp=timestamp,
        portfolio_equity=Decimal("1000"),
        unrealized_pnl=Decimal("0"),
        realized_pnl_24h=Decimal("0"),
        margin_utilization_pct=Decimal("5"),
        effective_leverage=Decimal("0.5"),
        current_drawdown_pct=Decimal("0"),
        liquidation_distance_pct=Decimal("50"),
        risk_state=RiskState.NORMAL,
    )


def _instrument() -> Instrument:
    return Instrument(
        symbol="BTCUSDT",
        venue="binance_testnet",
        market_type=MarketType.USDM_FUTURES,
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.125"),
        step_size=Decimal("0.007"),
        min_notional=Decimal("12.34"),
        price_precision=3,
        quantity_precision=3,
        max_leverage=25,
    )


class FailingDatabase:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        raise ConnectionError("database unavailable")

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


def test_database_configuration_has_no_credential_fallback():
    settings = PostgresSettings.from_environment(
        {
            "POSTGRES_HOST": "db.internal",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DB": "blessing_ai",
            "POSTGRES_USER": "worker",
        }
    )

    assert settings.dsn is None
    assert settings.missing == ("POSTGRES_PASSWORD",)
    client = get_postgres_client(
        {
            "POSTGRES_HOST": "db.internal",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DB": "blessing_ai",
            "POSTGRES_USER": "worker",
        }
    )
    assert client.config_error is not None
    assert "blessing_secret" not in client.config_error


def test_database_url_is_used_and_credentials_are_url_encoded():
    settings = PostgresSettings.from_environment(
        {
            "POSTGRES_HOST": "ignored",
            "POSTGRES_PORT": "5432",
            "POSTGRES_DB": "ignored",
            "POSTGRES_USER": "worker@desk",
            "POSTGRES_PASSWORD": "p@ss/word",
        }
    )

    assert settings.dsn == "postgresql://worker%40desk:p%40ss%2Fword@ignored:5432/ignored"
    assert "p@ss/word" not in settings.dsn


@pytest.mark.asyncio
async def test_optional_database_failure_is_degraded_and_not_durable():
    db = FailingDatabase()
    manager = PersistenceManager(
        db=db,
        config=PersistenceConfig(mode=PersistenceMode.OPTIONAL),
    )

    assert await manager.start() is False
    status = manager.readiness()

    assert status["mode"] == "OPTIONAL"
    assert status["state"] == "DEGRADED"
    assert status["ready"] is False
    assert status["durable"] is False
    assert "ConnectionError" in status["last_error"]
    await manager.stop()
    assert db.disconnect_calls >= 1


@pytest.mark.asyncio
async def test_required_database_failure_fails_closed():
    db = FailingDatabase()
    manager = PersistenceManager(
        db=db,
        config=PersistenceConfig(mode=PersistenceMode.REQUIRED),
    )

    with pytest.raises(ConnectionError, match="database unavailable"):
        await manager.start()

    status = manager.readiness()
    assert status["state"] == "FAILED"
    assert status["ready"] is False
    assert status["durable"] is False
    await manager.stop()


@pytest.mark.asyncio
async def test_disabled_persistence_is_explicit_and_does_not_connect():
    db = FailingDatabase()
    manager = PersistenceManager(
        db=db,
        config=PersistenceConfig(mode=PersistenceMode.DISABLED),
    )

    assert await manager.start() is True
    assert manager.enqueue_risk_snapshot(_risk_snapshot()) is False
    status = manager.readiness()
    assert status["mode"] == "DISABLED"
    assert status["state"] == "DISABLED"
    assert status["ready"] is True
    assert status["durable"] is False
    assert status["disabled_writes"] == 1
    assert db.connect_calls == 0
    await manager.stop()


def test_bounded_queue_rejects_without_claiming_durability():
    manager = PersistenceManager(
        db=FailingDatabase(),
        config=PersistenceConfig(mode=PersistenceMode.OPTIONAL, queue_capacity=1),
    )
    manager._accepting = True
    manager.is_connected = True

    assert manager.enqueue_risk_snapshot(_risk_snapshot()) is True
    assert manager.enqueue_risk_snapshot(
        _risk_snapshot(PERSISTED_AT + timedelta(seconds=1))
    ) is False
    status = manager.readiness()
    assert status["queue_size"] == 1
    assert status["failed_writes"] == 1
    assert status["dropped_writes"] == 1
    assert status["durable"] is False


@pytest.mark.asyncio
async def test_retry_backoff_retries_append_and_recovers_state():
    class RetryRepository:
        def __init__(self) -> None:
            self.calls = 0

        async def append_outbox(self, event: Any) -> bool:
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("temporary outage")
            return True

    manager = PersistenceManager(
        db=FailingDatabase(),
        config=PersistenceConfig(
            mode=PersistenceMode.OPTIONAL,
            retry_base_seconds=0.001,
            retry_max_seconds=0.001,
        ),
    )
    repository = RetryRepository()
    manager.repository = repository  # type: ignore[assignment]
    event = _PersistenceEvent(
        event_id="RISK_SNAPSHOT-event-1",
        event_type="RISK_SNAPSHOT",
        idempotency_key="risk-1",
        aggregate_type="RISK_SNAPSHOT",
        aggregate_id="portfolio",
        created_at=PERSISTED_AT,
        payload={"entity": _risk_snapshot().model_dump(mode="json")},
    )

    await manager._append_with_retry(event)

    assert repository.calls == 2
    assert manager.readiness()["failed_writes"] == 1
    assert manager.readiness()["retry_count"] == 1
    assert manager.readiness()["state"] == "READY"
    assert manager.readiness()["last_error"] is None


@pytest.mark.asyncio
async def test_graceful_shutdown_drains_bounded_writer_queue(monkeypatch):
    class DrainDatabase:
        def __init__(self) -> None:
            self.connected = False
            self.disconnected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.disconnected = True

    class DrainRepository:
        def __init__(self, db: Any, instrument_rules_provider: Any = None) -> None:
            self.events: list[Any] = []
            self.orders = None
            self.fills = None
            self.positions = None
            self.risk = None

        async def append_outbox(self, event: Any) -> bool:
            self.events.append(event)
            return True

        async def pending_count(self) -> int:
            return 0

        async def dispatch_one(self) -> bool:
            return False

    import apps.trading_worker.persistence.manager as manager_module

    monkeypatch.setattr(manager_module, "PersistenceRepository", DrainRepository)
    db = DrainDatabase()
    manager = PersistenceManager(
        db=db,  # type: ignore[arg-type]
        config=PersistenceConfig(
            mode=PersistenceMode.OPTIONAL,
            drain_timeout_seconds=1,
            poll_interval_seconds=0.001,
        ),
    )

    assert await manager.start() is True
    assert manager.enqueue_risk_snapshot(_risk_snapshot()) is True
    await manager.stop()

    repository = manager.repository
    assert repository is not None
    assert len(repository.events) == 1
    assert manager.readiness()["queue_size"] == 0
    assert manager.readiness()["unflushed_writes"] == 0
    assert manager.readiness()["state"] == "STOPPED"
    assert db.disconnected is True


class RecordingDatabase:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *args: object) -> None:
        self.execute_calls.append((query, args))


@pytest.mark.asyncio
async def test_outbox_insert_is_idempotent_and_preserves_utc_timestamp():
    db = RecordingDatabase()
    repository = PersistenceRepository(db)  # type: ignore[arg-type]
    event = _PersistenceEvent(
        event_id="RISK_SNAPSHOT-event-2",
        event_type="RISK_SNAPSHOT",
        idempotency_key="risk-2",
        aggregate_type="RISK_SNAPSHOT",
        aggregate_id="portfolio",
        created_at=PERSISTED_AT,
        payload={"entity": _risk_snapshot().model_dump(mode="json")},
    )

    assert await repository.append_outbox(event) is True
    assert await repository.append_outbox(event) is True
    query, args = db.execute_calls[0]
    assert "ON CONFLICT DO NOTHING" in query
    assert args[0] == event.event_id
    assert args[2] == event.idempotency_key
    assert args[-1] == PERSISTED_AT.astimezone(UTC)


@pytest.mark.asyncio
async def test_order_submission_barrier_acknowledges_outbox_before_returning():
    class DurableRepository:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def append_outbox(self, event: Any) -> bool:
            self.events.append(event)
            return True

        async def pending_count(self) -> int:
            return len(self.events)

    repository = DurableRepository()
    manager = PersistenceManager(
        db=FailingDatabase(),
        config=PersistenceConfig(
            mode=PersistenceMode.REQUIRED,
            pre_submission_timeout_seconds=1,
        ),
        instrument_rules_provider=lambda symbol: _instrument()
        if symbol == "BTCUSDT"
        else None,
    )
    manager.repository = repository  # type: ignore[assignment]
    manager.is_connected = True
    manager._accepting = True
    order = ExecutionOrder(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.1"),
        price=Decimal("50000"),
        client_order_id="pre-submit-1",
        status="PENDING",
        timestamp=PERSISTED_AT,
    )

    assert await manager.ensure_order_durable(order) is True
    assert len(repository.events) == 1
    assert repository.events[0].event_type == "ORDER"
    assert repository.events[0].aggregate_id == "pre-submit-1"
    assert manager.readiness()["pending_outbox"] == 1


@pytest.mark.asyncio
async def test_outbox_failed_apply_remains_replayable_and_backoff_is_recorded():
    class ReplayConnection:
        def __init__(self, row: dict[str, object]) -> None:
            self.row = row
            self.claim_queries: list[str] = []
            self.process_queries: list[tuple[str, tuple[object, ...]]] = []

        async def fetchrow(self, query: str, *args: object) -> dict[str, object]:
            self.claim_queries.append(query)
            return self.row

        async def execute(self, query: str, *args: object) -> None:
            self.process_queries.append((query, args))

    class ReplayDatabase(RecordingDatabase):
        def __init__(self, connection: ReplayConnection) -> None:
            super().__init__()
            self.connection = connection

        @asynccontextmanager
        async def transaction(self):
            yield self.connection

    row = {
        "event_id": "RISK_SNAPSHOT-event-3",
        "event_type": "RISK_SNAPSHOT",
        "payload": {},
    }
    connection = ReplayConnection(row)
    db = ReplayDatabase(connection)
    repository = PersistenceRepository(db)  # type: ignore[arg-type]
    apply_calls = 0

    async def apply_event(connection_arg: Any, row_arg: Any) -> None:
        nonlocal apply_calls
        apply_calls += 1
        if apply_calls == 1:
            raise RuntimeError("domain write failed")

    repository._apply_event = apply_event  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="domain write failed"):
        await repository.dispatch_one()

    assert "FOR UPDATE SKIP LOCKED" in connection.claim_queries[0]
    assert "attempt_count = attempt_count + 1" in db.execute_calls[0][0]
    assert "status = 'PENDING'" in db.execute_calls[0][0]

    assert await repository.dispatch_one() is True
    assert apply_calls == 2
    assert any("status = 'PROCESSED'" in query for query, _ in connection.process_queries)


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, query: str, *args: object) -> None:
        self.calls.append((query, args))


def test_repositories_use_exchange_rules_and_hedge_position_identity():
    connection = RecordingConnection()
    instrument = _instrument()
    order = ExecutionOrder(
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        quantity=Decimal("0.1"),
        price=Decimal("50000"),
        client_order_id="order-1",
        status="NEW",
        position_side=PositionSide.LONG,
        timestamp=PERSISTED_AT,
        time_in_force=TimeInForce.GTC,
    )
    fill = ExchangeFill(
        exchange_trade_id="trade-1",
        exchange_order_id="exchange-order-1",
        client_order_id="order-1",
        symbol="BTCUSDT",
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        quantity=Decimal("0.1"),
        price=Decimal("50000"),
        commission=Decimal("0.01"),
        commission_asset="USDT",
        realized_pnl=Decimal("0"),
        maker=True,
        event_time=PERSISTED_AT,
        transaction_time=PERSISTED_AT,
        source="BINANCE_TESTNET",
    )

    import asyncio

    async def save_all() -> None:
        await OrderRepository(None).save_order(order, instrument, connection)  # type: ignore[arg-type]
        await FillRepository(None).save_fill(fill, instrument, connection)  # type: ignore[arg-type]
        await PositionRepository(None).save_position(
            ExchangePosition(
                symbol="BTCUSDT",
                position_side=PositionSide.LONG,
                quantity=Decimal("1"),
                entry_price=Decimal("50000"),
                mark_price=Decimal("50010"),
                event_time=PERSISTED_AT,
            ),
            instrument,
            connection,
        )
        await PositionRepository(None).save_position(  # type: ignore[arg-type]
            ExchangePosition(
                symbol="BTCUSDT",
                position_side=PositionSide.SHORT,
                quantity=Decimal("-2"),
                entry_price=Decimal("50100"),
                mark_price=Decimal("50010"),
                event_time=PERSISTED_AT,
            ),
            instrument,
            connection,
        )

    asyncio.run(save_all())

    instrument_args = [
        args
        for query, args in connection.calls
        if "INSERT INTO instruments" in query
    ]
    assert instrument_args
    assert instrument_args[0][7:10] == (
        Decimal("0.125"),
        Decimal("0.007"),
        Decimal("12.34"),
    )

    order_query = next(query for query, _ in connection.calls if "INSERT INTO orders" in query)
    fill_query = next(query for query, _ in connection.calls if "INSERT INTO fills" in query)
    position_queries = [query for query, _ in connection.calls if "INSERT INTO positions" in query]
    position_args = [
        args for query, args in connection.calls if "INSERT INTO positions" in query
    ]
    assert "ON CONFLICT (client_order_id)" in order_query
    assert "ON CONFLICT (venue, exchange_trade_id)" in fill_query
    assert "exchange_trade_id" in fill_query
    assert all("ON CONFLICT (venue, symbol, position_side)" in query for query in position_queries)
    assert [args[2] for args in position_args] == ["LONG", "SHORT"]


def test_exchange_rules_conversion_uses_exchange_info_values():
    rules = SymbolTradingRules("BTCUSDT")
    rules.parse_exchange_info(
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "pricePrecision": 3,
            "quantityPrecision": 3,
            "orderTypes": ["LIMIT", "MARKET"],
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": "0.125",
                    "maxPrice": "1000000",
                    "tickSize": "0.125",
                },
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.007",
                    "maxQty": "100",
                    "stepSize": "0.007",
                },
                {"filterType": "MIN_NOTIONAL", "minNotional": "12.34"},
            ],
        }
    )

    instrument = rules.to_instrument()
    assert instrument.tick_size == Decimal("0.125")
    assert instrument.step_size == Decimal("0.007")
    assert instrument.min_notional == Decimal("12.34")
    assert instrument.base_asset == "BTC"
    assert instrument.quote_asset == "USDT"


def test_small_live_requires_required_persistence_mode():
    optional = PersistenceManager(
        db=FailingDatabase(),
        config=PersistenceConfig(mode=PersistenceMode.OPTIONAL),
    )
    with pytest.raises(RuntimeError, match="PERSISTENCE_MODE=REQUIRED"):
        optional.validate_execution_mode("SMALL_LIVE")
    optional.validate_execution_mode("TESTNET")


def test_incomplete_exchange_rules_are_rejected_before_durable_write():
    instrument = _instrument().model_copy(update={"tick_size": Decimal("0")})
    connection = RecordingConnection()
    position = ExchangePosition(symbol="BTCUSDT", quantity=Decimal("1"))

    import asyncio

    with pytest.raises(InstrumentRulesUnavailable):
        asyncio.run(PositionRepository(None).save_position(position, instrument, connection))  # type: ignore[arg-type]


def test_persistence_schema_matches_outbox_and_hedge_identity_contract():
    schema = Path("infra/postgres/init_schema.sql").read_text(encoding="utf-8")
    migration = Path(
        "infra/postgres/migrations/001_persistence_outbox_and_hedge_identity.sql"
    ).read_text(encoding="utf-8")

    assert "exchange_order_id VARCHAR(64)" in schema
    assert "UNIQUE(venue, symbol, position_side)" in schema
    assert "CREATE TABLE IF NOT EXISTS persistence_outbox" in schema
    assert "ADD COLUMN IF NOT EXISTS exchange_order_id VARCHAR(64)" in migration
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_venue_symbol_side" in migration
