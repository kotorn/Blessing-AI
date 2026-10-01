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
    InstrumentRulesUnavailable,
    OrderRepository,
    PersistenceRepository,
    PositionRepository,
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
        portfolio_equity=Decimal(1000),
        unrealized_pnl=Decimal(0),
        realized_pnl_24h=Decimal(0),
        margin_utilization_pct=Decimal(5),
        effective_leverage=Decimal("0.5"),
        current_drawdown_pct=Decimal(0),
        liquidation_distance_pct=Decimal(50),
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


@pytest.mark.asyncio
async def test_reduce_only_close_keeps_existing_basket_direction():
    class Connection:
        def __init__(self):
            self.execute_calls = []
            self.fetchrow_calls = []

        async def execute(self, query, *args):
            self.execute_calls.append((query, args))

        async def fetchrow(self, query, *args):
            self.fetchrow_calls.append((query, args))
            assert "SELECT basket_id FROM baskets" in query
            return {"basket_id": "basket-long"}

    instrument = Instrument(
        symbol="ETHUSDC",
        venue="binance_mainnet",
        market_type=MarketType.USDM_FUTURES,
        base_asset="ETH",
        quote_asset="USDC",
        tick_size=Decimal("0.1"),
        step_size=Decimal("0.001"),
        min_notional=Decimal("5"),
        price_precision=1,
        quantity_precision=3,
        max_leverage=10,
    )
    close = ExecutionOrder(
        symbol="ETHUSDC",
        side=OrderSide.SELL,
        quantity=Decimal("0.1"),
        price=Decimal("2000"),
        order_type="MARKET",
        client_order_id="close-long-basket",
        basket_id="basket-long",
        status="FILLED",
        market_type=MarketType.USDM_FUTURES,
        position_side=PositionSide.BOTH,
        reduce_only=True,
    )
    connection = Connection()

    await OrderRepository(None).save_order(close, instrument, connection)

    assert connection.fetchrow_calls[0][1] == (
        "basket-long", "binance_mainnet", "ETHUSDC"
    )
    assert not any("INSERT INTO baskets" in query for query, _ in connection.fetchrow_calls)
    order_insert = next(
        (query, args)
        for query, args in connection.execute_calls
        if "INSERT INTO orders" in query
    )
    assert order_insert[1][2] == "basket-long"
    assert order_insert[1][5] == "SELL"


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
    async def verified_schema(self, *, require_local_ledger: bool) -> None:
        self._schema_verified = True

    monkeypatch.setattr(PersistenceManager, "_verify_postgres_schema", verified_schema)
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
        price=Decimal(50000),
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
        price=Decimal(50000),
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
        price=Decimal(50000),
        commission=Decimal("0.01"),
        commission_asset="USDT",
        realized_pnl=Decimal(0),
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
                quantity=Decimal(1),
                entry_price=Decimal(50000),
                mark_price=Decimal(50010),
                event_time=PERSISTED_AT,
            ),
            instrument,
            connection,
        )
        await PositionRepository(None).save_position(  # type: ignore[arg-type]
            ExchangePosition(
                symbol="BTCUSDT",
                position_side=PositionSide.SHORT,
                quantity=Decimal(-2),
                entry_price=Decimal(50100),
                mark_price=Decimal(50010),
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
    instrument = _instrument().model_copy(update={"tick_size": Decimal(0)})
    connection = RecordingConnection()
    position = ExchangePosition(symbol="BTCUSDT", quantity=Decimal(1))

    import asyncio

    with pytest.raises(InstrumentRulesUnavailable):
        asyncio.run(PositionRepository(None).save_position(position, instrument, connection))  # type: ignore[arg-type]


def test_rest_refreshed_position_reuses_instrument_venue_not_unknown():
    """A REST-refreshed position (no .source) must not fragment outbox identity."""
    manager = PersistenceManager(
        db=FailingDatabase(),
        config=PersistenceConfig(mode=PersistenceMode.REQUIRED),
        instrument_rules_provider=lambda symbol: _instrument()
        if symbol == "BTCUSDT"
        else None,
    )
    manager.is_connected = True
    manager._accepting = True

    # apps/trading_worker/venues/binance/ledger.py's _to_exchange_position
    # explicitly writes source="UNKNOWN" when parsing a raw REST position
    # dict (emergency flatten, reconciliation) that never carried a "source"
    # key -- unlike WebSocket ACCOUNT_UPDATE-derived positions, which tag it
    # with the live environment label.
    position = ExchangePosition(symbol="BTCUSDT", quantity=Decimal(1), source="UNKNOWN")

    assert manager.enqueue_position(position) is True
    event = manager._write_queue.get_nowait()
    assert event.aggregate_id == f"{_instrument().venue}:BTCUSDT:BOTH"
    assert "UNKNOWN" not in event.aggregate_id


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
    assert "runtime_target VARCHAR(32) NOT NULL DEFAULT 'CLOUD_RUN'" in schema
    assert "runtime_fingerprint VARCHAR(64)" in schema
    assert "pending_order_client_order_id VARCHAR(64)" in schema
    assert "CONSTRAINT mainnet_launch_runtime_identity_check" in schema
    assert "CONSTRAINT mainnet_launch_order_identity_check" in schema

    algo_schema = Path(
        "infra/postgres/migrations/009_binance_algo_protection_ownership.sql"
    ).read_text(encoding="utf-8")
    table_start = "CREATE TABLE IF NOT EXISTS binance_algo_protections"
    index_start = "CREATE INDEX IF NOT EXISTS idx_binance_algo_protections_nonterminal"
    migration_ddl = algo_schema[
        algo_schema.index(table_start) : algo_schema.index(index_start)
    ].strip()
    schema_ddl = schema[schema.index(table_start) : schema.index(index_start)].strip()
    assert migration_ddl == schema_ddl
    for field in (
        "environment VARCHAR(8) NOT NULL",
        "entry_client_order_id VARCHAR(64) NOT NULL",
        "requested_quantity NUMERIC(28, 10) NOT NULL",
        "filled_quantity NUMERIC(28, 10) NOT NULL",
        "entry_average_price NUMERIC(28, 10)",
        "stop_algo_id VARCHAR(64)",
        "take_profit_algo_id VARCHAR(64)",
        "stop_client_algo_id VARCHAR(64) NOT NULL",
        "take_profit_client_algo_id VARCHAR(64) NOT NULL",
        "state IN ('PENDING', 'PROTECTED', 'CLOSE_PENDING', 'CLOSED', 'DEGRADED', 'UNKNOWN')",
        "PRIMARY KEY (venue, symbol, entry_client_order_id)",
    ):
        assert field in algo_schema
        assert field in schema


def _algo_protection_record(
    *, venue: str = "binance_testnet", environment: str = "TESTNET", entry_id: str = "entry-1"
) -> dict[str, Any]:
    return {
        "venue": venue,
        "environment": environment,
        "symbol": "ETHUSDC",
        "entry_client_order_id": entry_id,
        "basket_id": "mainnet-basket-1" if environment == "MAINNET" else None,
        "mainnet_launch_id": "launch-mainnet-1" if environment == "MAINNET" else None,
        "management_mode": "QUICK" if environment == "MAINNET" else None,
        "entry_side": "BUY",
        "position_side": "BOTH",
        "requested_quantity": "1.0000000000",
        "filled_quantity": "0",
        "entry_average_price": None,
        "stop_trigger_price": "1900",
        "take_profit_trigger_price": "2200",
        "stop_client_algo_id": f"sl-{entry_id}",
        "take_profit_client_algo_id": f"tp-{entry_id}",
        "state": "PENDING",
    }


class _AlgoProtectionMemoryDatabase:
    """Small SQL-shaped unit fake; PostgreSQL behavior is exercised opt-in below."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], dict[str, Any]] = {}

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, query: str, *args: object):
        if "SELECT * FROM binance_algo_protections" in query:
            key = (str(args[0]), str(args[1]), str(args[2]))
            row = self.rows.get(key)
            return dict(row) if row is not None else None
        if "INSERT INTO binance_algo_protections" in query:
            names = (
                "environment", "venue", "symbol", "entry_client_order_id", "basket_id",
                "mainnet_launch_id", "entry_side", "position_side", "requested_quantity",
                "filled_quantity", "entry_average_price",
                "stop_trigger_price", "take_profit_trigger_price", "stop_algo_id",
                "take_profit_algo_id", "stop_client_algo_id", "take_profit_client_algo_id",
                "management_mode", "state", "state_reason", "first_fill_at", "protection_verified_at",
                "last_reconciled_at", "closed_at",
            )
            row = dict(zip(names, args, strict=True))
            key = (str(row["venue"]), str(row["symbol"]), str(row["entry_client_order_id"]))
            if key in self.rows:
                return None
            now = datetime.now(UTC)
            row.update(created_at=now, updated_at=now)
            self.rows[key] = row
            return dict(row)
        if "UPDATE binance_algo_protections" in query:
            key = (str(args[0]), str(args[1]), str(args[2]))
            row = self.rows.get(key)
            if row is None:
                return None
            if "SET state = 'CLOSE_PENDING'" in query:
                if not (
                    row["environment"] == "TESTNET" and row["state"] == "PROTECTED"
                    and row["entry_side"] == args[3] and row["position_side"] == args[4]
                    and row["filled_quantity"] == args[5] and row["stop_algo_id"] == args[6]
                    and row["take_profit_algo_id"] == args[8]
                ):
                    return None
                row.update(state="CLOSE_PENDING", state_reason=args[7])
                row["updated_at"] = datetime.now(UTC)
                return dict(row)
            if "SET state_reason = $5" in query:
                if row["environment"] != "TESTNET" or row["state"] != "CLOSE_PENDING" \
                        or row["state_reason"] != args[3]:
                    return None
                row.update(state_reason=args[4], updated_at=datetime.now(UTC))
                return dict(row)
            if "filled_quantity = $4" in query:
                names = (
                    "filled_quantity", "entry_average_price", "stop_algo_id", "take_profit_algo_id",
                    "state", "state_reason", "first_fill_at", "protection_verified_at",
                    "last_reconciled_at", "closed_at",
                )
                row.update(zip(names, args[3:], strict=True))
            else:
                row.update(
                    state=args[3],
                    state_reason=args[4],
                    protection_verified_at=args[5],
                    closed_at=args[6],
                )
            row["updated_at"] = datetime.now(UTC)
            return dict(row)
        raise AssertionError(f"Unexpected protection SQL: {query}")

    async def fetch(self, query: str, *args: object):
        venue = str(args[0])
        symbol = str(args[1]) if len(args) > 1 else None
        active_only = "state <> 'CLOSED'" in query
        return [
            dict(row)
            for key, row in self.rows.items()
            if key[0] == venue
            and (symbol is None or key[1] == symbol)
            and (not active_only or row["state"] != "CLOSED")
        ]


@pytest.mark.asyncio
async def test_algo_protection_repository_identity_transitions_and_environment_isolation():
    db = _AlgoProtectionMemoryDatabase()
    repo = PersistenceRepository(db).algo_protections  # type: ignore[arg-type]
    original = _algo_protection_record()

    created = await repo.upsert_protection(original)
    assert created["state"] == "PENDING"
    assert created["environment"] == "TESTNET"
    repeated = await repo.upsert_protection(original)
    assert repeated["entry_client_order_id"] == "entry-1"
    assert repeated["updated_at"] == created["updated_at"]

    key_patch = {
        "venue": "binance_testnet",
        "environment": "TESTNET",
        "symbol": "ETHUSDC",
        "entry_client_order_id": "entry-1",
    }
    partial = await repo.upsert_protection(
        {**key_patch, "filled_quantity": "0.5", "entry_average_price": "2000"}
    )
    assert partial["first_fill_at"] is not None
    assert partial["filled_quantity"] == Decimal("0.5")

    assigned = await repo.upsert_protection(
        {**key_patch, "stop_algo_id": "1001", "take_profit_algo_id": "1002"}
    )
    assert assigned["stop_algo_id"] == "1001"
    protected = await repo.set_protection_state(
        "binance_testnet", "ETHUSDC", "entry-1", "PROTECTED"
    )
    assert protected is not None
    assert protected["protection_verified_at"] is not None

    with pytest.raises(ValueError, match="identity field stop_trigger_price"):
        await repo.upsert_protection({**key_patch, "stop_trigger_price": "1950"})
    with pytest.raises(ValueError, match="assigned stop_algo_id is immutable"):
        await repo.upsert_protection({**key_patch, "stop_algo_id": "2001"})
    with pytest.raises(ValueError, match="cannot decrease"):
        await repo.upsert_protection(
            {**key_patch, "filled_quantity": "0.4", "entry_average_price": "2000"}
        )
    with pytest.raises(ValueError, match="unsafe protection state transition"):
        await repo.set_protection_state("binance_testnet", "ETHUSDC", "entry-1", "CLOSED")

    closing = await repo.set_protection_state(
        "binance_testnet", "ETHUSDC", "entry-1", "CLOSE_PENDING", reason="close requested"
    )
    assert closing is not None and closing["state_reason"] == "close requested"
    closed = await repo.set_protection_state("binance_testnet", "ETHUSDC", "entry-1", "CLOSED")
    assert closed is not None and closed["closed_at"] is not None
    assert await repo.list_active_protections("binance_testnet") == []
    closed_history = await repo.list_protections("binance_testnet")
    assert len(closed_history) == 1 and closed_history[0]["state"] == "CLOSED"
    with pytest.raises(ValueError, match="unsafe protection state transition"):
        await repo.set_protection_state("binance_testnet", "ETHUSDC", "entry-1", "PROTECTED")
    with pytest.raises(ValueError, match="cannot change execution facts"):
        await repo.upsert_protection(
            {**key_patch, "filled_quantity": "0.6", "entry_average_price": "2010"}
        )

    # The same exchange client ID is independent in Testnet and Mainnet because
    # environment-specific Binance venue names are part of the durable key.
    mainnet = _algo_protection_record(
        venue="binance_mainnet", environment="MAINNET", entry_id="entry-1"
    )
    await repo.upsert_protection(mainnet)
    assert await repo.get_protection("binance_testnet", "ETHUSDC", "entry-1") is not None
    assert await repo.get_protection("binance_mainnet", "ETHUSDC", "entry-1") is not None
    assert len(await repo.list_active_protections("binance_mainnet", "ethusdc")) == 1

    reconciled_at = datetime.now(UTC)
    await repo.upsert_protection({
        "venue": "binance_mainnet",
        "environment": "MAINNET",
        "symbol": "ETHUSDC",
        "entry_client_order_id": "entry-1",
        "basket_id": "mainnet-basket-1",
        "mainnet_launch_id": "launch-mainnet-1",
        "management_mode": "QUICK",
        "last_reconciled_at": reconciled_at,
    })
    with pytest.raises(ValueError, match="last_reconciled_at cannot be cleared"):
        await repo.upsert_protection({
            "venue": "binance_mainnet",
            "environment": "MAINNET",
            "symbol": "ETHUSDC",
            "entry_client_order_id": "entry-1",
            "basket_id": "mainnet-basket-1",
            "mainnet_launch_id": "launch-mainnet-1",
            "management_mode": "QUICK",
            "last_reconciled_at": None,
        })


@pytest.mark.asyncio
async def test_algo_protection_repository_rejects_invalid_identity_and_risk_fields():
    repo = PersistenceRepository(_AlgoProtectionMemoryDatabase()).algo_protections  # type: ignore[arg-type]

    bad_records = [
        {**_algo_protection_record(), "environment": "MAINNET"},
        {**_algo_protection_record(), "entry_side": "HOLD"},
        {**_algo_protection_record(), "position_side": "SHORT"},
        {**_algo_protection_record(), "requested_quantity": "0"},
        {**_algo_protection_record(), "filled_quantity": "2", "entry_average_price": "2000"},
        {**_algo_protection_record(), "filled_quantity": "0.1"},
        {
            **_algo_protection_record(),
            "stop_trigger_price": "2200",
            "take_profit_trigger_price": "2200",
        },
        {
            **_algo_protection_record(),
            "stop_client_algo_id": "same-id",
            "take_profit_client_algo_id": "same-id",
        },
        {
            **_algo_protection_record(),
            "filled_quantity": "0.1",
            "entry_average_price": "2000",
            "state": "PROTECTED",
            "stop_algo_id": "1001",
            "take_profit_algo_id": "1001",
        },
    ]
    for record in bad_records:
        with pytest.raises(ValueError):
            await repo.upsert_protection(record)


@pytest.mark.asyncio
async def test_testnet_close_claim_and_submission_marker_are_atomic_one_use_transitions():
    repo = PersistenceRepository(_AlgoProtectionMemoryDatabase()).algo_protections  # type: ignore[arg-type]
    record = _algo_protection_record()
    await repo.upsert_protection(record)
    identity = {key: record[key] for key in (
        "venue", "environment", "symbol", "entry_client_order_id",
    )}
    await repo.upsert_protection({**identity, "filled_quantity": "0.5", "entry_average_price": "2000"})
    await repo.upsert_protection({**identity, "stop_algo_id": "1001", "take_profit_algo_id": "1002"})
    await repo.set_protection_state("binance_testnet", "ETHUSDC", "entry-1", "PROTECTED")

    claim_args = {
        "venue": "binance_testnet", "symbol": "ETHUSDC", "entry_client_order_id": "entry-1",
        "entry_side": "BUY", "position_side": "BOTH", "filled_quantity": Decimal("0.5"),
        "stop_algo_id": "1001", "take_profit_algo_id": "1002",
        "state_reason": "protected_ethusdc_testnet_trial_close:close-1:CLAIMED",
    }
    claimed = await repo.claim_testnet_protection_close(**claim_args)
    assert claimed is not None and claimed["state"] == "CLOSE_PENDING"
    assert await repo.claim_testnet_protection_close(**claim_args) is None

    marker_args = {
        "venue": "binance_testnet", "symbol": "ETHUSDC", "entry_client_order_id": "entry-1",
        "claimed_reason": claim_args["state_reason"],
        "submitting_reason": "protected_ethusdc_testnet_trial_close:close-1:SUBMITTING",
    }
    submitting = await repo.mark_testnet_protection_close_submitting(**marker_args)
    assert submitting is not None and submitting["state_reason"].endswith(":SUBMITTING")
    assert await repo.mark_testnet_protection_close_submitting(**marker_args) is None


@pytest.mark.asyncio
async def test_create_mainnet_launch_session_rejects_arming_when_prior_has_unreviewed_orders():
    """Arming a new session while prior session has submitted_orders > 0 must raise and preserve prior."""

    class MockDb:
        def __init__(self):
            self.closed_called = False
            self.prior_session = {
                "launch_id": "launch-approval-prior",
                "approval_id": "approval-prior",
                "submitted_orders": 1,
                "state": "PAUSED_NEW_RISK",
            }

        async def execute(self, query: str, *args: object) -> str:
            if "UPDATE mainnet_launch_sessions" in query:
                if "submitted_orders = 0" in query:
                    return "UPDATE 0"
                self.closed_called = True
                return "UPDATE 1"
            return "INSERT 0 1"

        async def fetchrow(self, query: str, *args: object):
            if "submitted_orders > 0" in query:
                return self.prior_session
            return self.prior_session

    db = MockDb()
    repo = PersistenceRepository(db)  # type: ignore[arg-type]

    image_digest = "asia-southeast1-docker.pkg.dev/demo/trading-worker@sha256:" + "b" * 64
    with pytest.raises(
        RuntimeError,
        match="existing session launch-approval-prior has a submitted order pending review",
    ):
        await repo.create_mainnet_launch_session(
            launch_id="launch-approval-new",
            approval_id="approval-new",
            image_digest=image_digest,
            symbol="ETHUSDC",
        )

    assert not db.closed_called


@pytest.mark.asyncio
async def test_binance_history_anchor_and_checkpoint_bind_explicit_runtime_scope():
    class HistoryDatabase:
        def __init__(self):
            self.anchors = {}
            self.checkpoints = {}

        async def execute(self, query, *args):
            if "INSERT INTO binance_history_anchors" in query:
                target, run_id, symbol, anchor_at = args[:4]
                source = args[4] if len(args) > 4 else "TESTNET_READONLY_START"
                launch_id = args[5] if len(args) > 5 else None
                self.anchors[(target, run_id, symbol)] = {
                    "runtime_target": target,
                    "run_id": run_id,
                    "symbol": symbol,
                    "anchor_at": anchor_at,
                    "anchor_source": source,
                    "mainnet_launch_id": launch_id,
                }
                return "INSERT 0 1"
            if "INSERT INTO binance_history_checkpoints" in query:
                target, run_id, symbol, history_kind = args
                self.checkpoints[(target, run_id, symbol, history_kind)] = {
                    "runtime_target": target,
                    "run_id": run_id,
                    "symbol": symbol,
                    "history_kind": history_kind,
                    "cursor_id": 0,
                    "coverage_status": "NOT_STARTED",
                }
                return "INSERT 0 1"
            raise AssertionError(f"Unexpected history SQL: {query}")

        async def fetchrow(self, query, *args):
            if "FROM binance_history_checkpoints AS c" in query:
                target, run_id, symbol, history_kind = args
                checkpoint = self.checkpoints.get((target, run_id, symbol, history_kind))
                if checkpoint is None:
                    return None
                return {
                    **checkpoint,
                    "anchor_at": self.anchors[(target, run_id, symbol)]["anchor_at"],
                }
            if "FROM binance_history_anchors" in query:
                return self.anchors.get(tuple(args[:3]))
            raise AssertionError(f"Unexpected history query: {query}")

    db = HistoryDatabase()
    history = PersistenceRepository(db).binance_history  # type: ignore[arg-type]
    anchor_at = datetime.now(UTC) - timedelta(minutes=1)
    anchor = await history.register_testnet_anchor(
        run_id="testnet-run-1", symbol="ETHUSDC", anchor_at=anchor_at
    )
    assert anchor["anchor_source"] == "TESTNET_READONLY_START"
    assert anchor["mainnet_launch_id"] is None

    checkpoint = await history._ensure_checkpoint(
        runtime_target="TESTNET",
        run_id="testnet-run-1",
        symbol="ETHUSDC",
        history_kind="ALL_ALGO_ORDERS",
        anchor_at=anchor_at,
    )
    assert checkpoint["runtime_target"] == "TESTNET"
    assert checkpoint["run_id"] == "testnet-run-1"
    assert checkpoint["history_kind"] == "ALL_ALGO_ORDERS"

    mainnet_db = HistoryDatabase()
    mainnet_history = PersistenceRepository(mainnet_db).binance_history  # type: ignore[arg-type]
    await mainnet_history._ensure_checkpoint(
        runtime_target="LOCAL",
        run_id="local-launch-1",
        symbol="ETHUSDC",
        history_kind="USER_TRADES",
        anchor_at=anchor_at,
    )
    mainnet_anchor = mainnet_db.anchors[("LOCAL", "local-launch-1", "ETHUSDC")]
    assert mainnet_anchor["anchor_source"] == "MAINNET_LAUNCH_SESSION"
    assert mainnet_anchor["mainnet_launch_id"] == "local-launch-1"


@pytest.mark.asyncio
async def test_preexisting_algo_baseline_rejects_proof_without_durable_anchor():
    class MissingAnchorDatabase:
        async def fetchrow(self, query, *args):
            return None

    repository = PersistenceRepository(MissingAnchorDatabase()).binance_history  # type: ignore[arg-type]
    now = datetime.now(UTC)
    proof = {
        "run_id": "testnet-run-1",
        "symbol": "ETHUSDC",
        "algo_id": 1,
        "client_algo_id": "old-protection-1",
        "algo_created_at": now - timedelta(days=1),
        "terminal_status": "CANCELED",
        "snapshot_observed_at": now,
        "position_snapshot": [{"symbol": "ETHUSDC", "positionAmt": "0"}],
        "open_orders_snapshot": [],
        "open_algo_orders_snapshot": [],
    }
    with pytest.raises(ValueError, match="existing durable Testnet run anchor"):
        await repository.record_preexisting_algo_baseline(proof)


@pytest.mark.asyncio
async def test_preexisting_algo_baseline_requires_target_symbol_and_nonfuture_snapshot():
    now = datetime.now(UTC)
    anchor_at = now - timedelta(minutes=5)

    class AnchoredDatabase:
        async def fetchrow(self, query, *args):
            return {
                "anchor_at": anchor_at,
                "anchor_source": "TESTNET_READONLY_START",
            }

    repository = PersistenceRepository(AnchoredDatabase()).binance_history  # type: ignore[arg-type]
    base = {
        "run_id": "testnet-run-2",
        "symbol": "ETHUSDC",
        "algo_id": 2,
        "client_algo_id": "legacy-algo-2",
        "algo_created_at": anchor_at - timedelta(days=1),
        "terminal_status": "CANCELED",
        "snapshot_observed_at": now,
        "position_snapshot": [{"symbol": "ETHUSDC", "positionAmt": "0"}],
        "open_orders_snapshot": [],
        "open_algo_orders_snapshot": [],
    }
    with pytest.raises(ValueError, match="no target-symbol position snapshot"):
        await repository.record_preexisting_algo_baseline(
            {**base, "position_snapshot": [{"symbol": "BTCUSDC", "positionAmt": "0"}]}
        )
    with pytest.raises(ValueError, match="timestamp is in the future"):
        await repository.record_preexisting_algo_baseline(
            {**base, "snapshot_observed_at": now + timedelta(minutes=1)}
        )


class _LocalPilotAccountingDatabase:
    """In-memory SQL-shaped fake for Local Pilot event accounting semantics."""

    def __init__(self) -> None:
        self.session = {
            "launch_id": "launch-accounting-test",
            "symbol": "ETHUSDC",
            "policy": "LIVE_RESEARCH_PILOT",
            "runtime_target": "LOCAL",
            "pilot_campaign_id": "campaign-accounting-test",
            "pilot_net_pnl_usdc": Decimal("0"),
            "pilot_peak_pnl_usdc": Decimal("0"),
            "pilot_drawdown_triggered": False,
            "pilot_status": "ACTIVE",
            "pilot_last_account_snapshot_at": None,
            "state": "ACTIVE",
            "updated_at": datetime.now(UTC),
        }
        self.events: dict[tuple[str, str], dict[str, Any]] = {}
        self.payloads: dict[int, dict[str, Any]] = {}

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchrow(self, query: str, *args: object):
        if "FROM mainnet_launch_sessions WHERE launch_id = $1 FOR UPDATE" in query:
            return dict(self.session)
        if "FROM mainnet_launch_sessions WHERE launch_id = $1" in query:
            return dict(self.session)
        if "SELECT event_type, event_id" in query:
            accounting_events = [
                event for event in self.events.values()
                if event["event_type"] in {"MARK", "FILL", "FEE", "FUNDING"}
            ]
            latest = max(accounting_events, key=lambda event: event["event_id"], default=None)
            return None if latest is None else {
                "event_type": latest["event_type"], "event_id": latest["event_id"]
            }
        if "FROM local_live_pilot_events" in query:
            return self.events.get((str(args[0]), str(args[1])))
        if "INSERT INTO local_live_pilot_events" in query:
            import json
            if "VALUES ($1,$2,$3,'STATE','WORKER',$4,NULL,$5,$6::jsonb)" in query:
                campaign_id, launch_id, event_key, observed_at, payload_hash, payload = args
                event_type, source, delta = "STATE", "WORKER", None
            else:
                (campaign_id, launch_id, event_key, event_type, source, observed_at,
                 delta, payload_hash, payload) = args
            key = (str(campaign_id), str(event_key))
            if key in self.events:
                return None
            event = {
                "event_id": len(self.events) + 1,
                "campaign_id": campaign_id,
                "launch_id": launch_id,
                "event_key": event_key,
                "event_type": event_type,
                "source": source,
                "observed_at": observed_at,
                "net_pnl_delta_usdc": delta,
                "payload_sha256": payload_hash,
            }
            self.events[key] = event
            self.payloads[event["event_id"]] = json.loads(str(payload))
            return {"event_id": event["event_id"]}
        raise AssertionError(f"Unexpected pilot accounting query: {query}")

    async def fetchval(self, query: str, *args: object):
        if "payload->>'resume_eligible'" in query:
            markers = [
                event for event in self.events.values()
                if event["event_type"] == "STATE"
                and self.payloads[event["event_id"]].get("kind") == "ACCOUNTING_SNAPSHOT_PENDING"
            ]
            if not markers:
                return None
            marker = max(markers, key=lambda event: event["event_id"])
            return str(self.payloads[marker["event_id"]].get("resume_eligible", False)).lower()
        if "COALESCE(SUM(net_pnl_delta_usdc)" in query:
            return sum(
                (event["net_pnl_delta_usdc"] or Decimal("0"))
                for event in self.events.values()
                if event["event_type"] in {"FILL", "FEE", "FUNDING"}
            )
        if "SELECT MAX(observed_at)" in query:
            financial_events = [
                event for event in self.events.values()
                if event["event_type"] in {"FILL", "FEE", "FUNDING"}
            ]
            return max((event["observed_at"] for event in financial_events), default=None)
        component_events = [
            event for event in self.events.values()
            if event["event_type"] in {"MARK", "FILL", "FEE", "FUNDING"}
        ]
        if not component_events:
            return None
        if "SELECT event_type" in query:
            latest = max(component_events, key=lambda event: event["event_id"])
            return latest["event_type"]
        if "event_type = 'MARK'" in query:
            marks = [event for event in component_events if event["event_type"] == "MARK"]
            if not marks:
                return None
            latest_mark = max(marks, key=lambda event: event["event_id"])
            return self.payloads[latest_mark["event_id"]]["unrealized_pnl_usdc"]
        raise AssertionError(f"Unexpected pilot accounting scalar query: {query}")

    async def execute(self, query: str, *args: object):
        if "UPDATE mainnet_launch_sessions" not in query:
            raise AssertionError(f"Unexpected pilot accounting execute: {query}")
        (launch_id, net, peak, triggered, status, launch_state,
         is_mark, observed_at) = args
        assert launch_id == self.session["launch_id"]
        self.session.update(
            pilot_net_pnl_usdc=net,
            pilot_peak_pnl_usdc=peak,
            pilot_drawdown_triggered=triggered,
            pilot_status=status,
            state=launch_state or self.session["state"],
            pilot_last_account_snapshot_at=(
                observed_at if is_mark else self.session["pilot_last_account_snapshot_at"]
            ),
            updated_at=datetime.now(UTC),
        )
        return "UPDATE 1"


def _local_pilot_accounting_repository():
    db = _LocalPilotAccountingDatabase()
    return PersistenceRepository(db), db


@pytest.mark.asyncio
async def test_local_pilot_fee_event_rejects_positive_rebate_sign_as_fee():
    repository, _ = _local_pilot_accounting_repository()
    with pytest.raises(ValueError, match="fee deltas must be non-positive"):
        await repository.append_local_live_pilot_event(**_local_pilot_event(
            "FEE", "positive-fee", datetime.now(UTC), pnl="0.01"
        ))


def _local_pilot_event(event_type: str, event_id: str, observed_at: datetime, *, pnl: str):
    if event_type == "MARK":
        payload = {
            "run_id": "launch-accounting-test",
            "account_snapshot_id": event_id,
            "unrealized_pnl_usdc": pnl,
        }
        delta = None
    else:
        payload = {
            "run_id": "launch-accounting-test",
            "exchange_event_id": event_id,
            "realized_pnl_usdc": pnl,
        }
        delta = Decimal(pnl)
    return {
        "campaign_id": "campaign-accounting-test",
        "launch_id": "launch-accounting-test",
        "run_id": "launch-accounting-test",
        "symbol": "ETHUSDC",
        "event_key": f"{event_type}:{event_id}",
        "event_type": event_type,
        "source": "BINANCE",
        "observed_at": observed_at,
        "payload": payload,
        "net_pnl_delta_usdc": delta,
    }


@pytest.mark.asyncio
async def test_local_pilot_mark_fill_transitions_keep_realized_and_unrealized_pnl_separate():
    repository, db = _local_pilot_accounting_repository()
    start = datetime.now(UTC) - timedelta(minutes=1)

    async def append(event):
        return await repository.append_local_live_pilot_event(**event)

    opened = await append(_local_pilot_event("MARK", "snapshot-open", start, pnl="10"))
    assert Decimal(str(opened["pilot_net_pnl_usdc"])) == Decimal("10")
    assert Decimal(str(opened["pilot_peak_pnl_usdc"])) == Decimal("10")

    partial_close = await append(_local_pilot_event(
        "FILL", "partial-close", start + timedelta(seconds=1), pnl="5"
    ))
    assert Decimal(str(partial_close["pilot_net_pnl_usdc"])) == Decimal("10")
    assert Decimal(str(partial_close["pilot_peak_pnl_usdc"])) == Decimal("10")
    assert partial_close["pilot_status"] == "ACTIVE"
    assert partial_close["state"] == "PAUSED_NEW_RISK"

    partial_mark = await append(_local_pilot_event(
        "MARK", "snapshot-partial", start + timedelta(seconds=2), pnl="5"
    ))
    assert Decimal(str(partial_mark["pilot_net_pnl_usdc"])) == Decimal("10")
    assert Decimal(str(partial_mark["pilot_peak_pnl_usdc"])) == Decimal("10")

    full_close = await append(_local_pilot_event(
        "FILL", "full-close", start + timedelta(seconds=3), pnl="5"
    ))
    assert Decimal(str(full_close["pilot_net_pnl_usdc"])) == Decimal("10")
    assert Decimal(str(full_close["pilot_peak_pnl_usdc"])) == Decimal("10")

    closed_mark = await append(_local_pilot_event(
        "MARK", "snapshot-closed", start + timedelta(seconds=4), pnl="0"
    ))
    assert Decimal(str(closed_mark["pilot_net_pnl_usdc"])) == Decimal("10")
    assert Decimal(str(closed_mark["pilot_peak_pnl_usdc"])) == Decimal("10")
    assert len(db.events) == 7  # five accounting events plus two durable pause markers


@pytest.mark.asyncio
async def test_local_pilot_negative_mark_partial_fill_freezes_peak_until_fresh_snapshot():
    repository, db = _local_pilot_accounting_repository()
    start = datetime.now(UTC) - timedelta(minutes=1)

    marked = await repository.append_local_live_pilot_event(**_local_pilot_event(
        "MARK", "snapshot-negative", start, pnl="-2"
    ))
    assert Decimal(str(marked["pilot_net_pnl_usdc"])) == Decimal("-2")
    assert Decimal(str(marked["pilot_peak_pnl_usdc"])) == Decimal("0")

    partial = await repository.append_local_live_pilot_event(**_local_pilot_event(
        "FILL", "partial-negative-close", start + timedelta(seconds=1), pnl="1"
    ))
    # The old -2 mark remains explicitly stale in the interim net. It is
    # neither cleared to zero nor allowed to raise the sticky peak.
    assert Decimal(str(partial["pilot_net_pnl_usdc"])) == Decimal("-2")
    assert Decimal(str(partial["pilot_peak_pnl_usdc"])) == Decimal("0")
    assert partial["pilot_status"] == "ACTIVE"
    assert partial["state"] == "PAUSED_NEW_RISK"

    refreshed = await repository.append_local_live_pilot_event(**_local_pilot_event(
        "MARK", "snapshot-negative-residual", start + timedelta(seconds=2), pnl="-1"
    ))
    assert Decimal(str(refreshed["pilot_net_pnl_usdc"])) == Decimal("0")
    assert Decimal(str(refreshed["pilot_peak_pnl_usdc"])) == Decimal("0")
    assert db.session["pilot_status"] == "ACTIVE"
    assert db.session["state"] == "ACTIVE"
    assert refreshed["pilot_accounting_resumed"] is True


@pytest.mark.asyncio
async def test_local_pilot_fee_and_funding_each_require_a_fresh_mark_before_resume():
    repository, db = _local_pilot_accounting_repository()
    start = datetime.now(UTC) - timedelta(minutes=1)
    await repository.append_local_live_pilot_event(**_local_pilot_event(
        "MARK", "initial-cost-test", start, pnl="1"
    ))
    fee = await repository.append_local_live_pilot_event(**_local_pilot_event(
        "FEE", "commission-cost-test", start + timedelta(seconds=1), pnl="-0.1"
    ))
    assert fee["state"] == "PAUSED_NEW_RISK"
    assert fee["pilot_accounting_resume_eligible"] is True
    funding = await repository.append_local_live_pilot_event(**_local_pilot_event(
        "FUNDING", "funding-cost-test", start + timedelta(seconds=2), pnl="-0.02"
    ))
    assert funding["state"] == "PAUSED_NEW_RISK"
    assert funding["pilot_accounting_resume_eligible"] is True
    assert [payload["resume_eligible"] for payload in db.payloads.values()
            if payload.get("kind") == "ACCOUNTING_SNAPSHOT_PENDING"] == [True, True]
    refreshed = await repository.append_local_live_pilot_event(**_local_pilot_event(
        "MARK", "after-costs-test", start + timedelta(seconds=3), pnl="0.5"
    ))
    assert refreshed["pilot_net_pnl_usdc"] == Decimal("0.38")
    assert refreshed["pilot_accounting_resumed"] is True
    assert refreshed["state"] == "ACTIVE"
    assert db.session["state"] == "ACTIVE"


@pytest.mark.asyncio
async def test_local_pilot_duplicate_events_are_idempotent_and_new_out_of_order_marks_reject():
    repository, db = _local_pilot_accounting_repository()
    now = datetime.now(UTC) - timedelta(minutes=1)
    first_mark = _local_pilot_event("MARK", "snapshot-1", now, pnl="3")
    original = await repository.append_local_live_pilot_event(**first_mark)

    duplicate = await repository.append_local_live_pilot_event(**first_mark)
    assert duplicate["pilot_net_pnl_usdc"] == original["pilot_net_pnl_usdc"]
    assert duplicate["pilot_peak_pnl_usdc"] == original["pilot_peak_pnl_usdc"]
    assert len(db.events) == 1

    newer = await repository.append_local_live_pilot_event(**_local_pilot_event(
        "MARK", "snapshot-2", now + timedelta(seconds=2), pnl="4"
    ))
    with pytest.raises(RuntimeError, match="out-of-order pilot account snapshot"):
        await repository.append_local_live_pilot_event(**_local_pilot_event(
            "MARK", "snapshot-old", now + timedelta(seconds=1), pnl="99"
        ))
    assert db.session["pilot_net_pnl_usdc"] == newer["pilot_net_pnl_usdc"]
    assert db.session["pilot_peak_pnl_usdc"] == newer["pilot_peak_pnl_usdc"]
    assert len(db.events) == 2
