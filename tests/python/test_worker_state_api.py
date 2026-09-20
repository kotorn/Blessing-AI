import asyncio
from datetime import datetime, timedelta, timezone, UTC
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from apps.trading_worker.main import (
    TradingWorkerApp,
    WorkerEngineState,
    WorkerExecutionMode,
    WorkerRuntimeState,
    _global_heartbeat_loop,
    app,
    get_default_state,
    set_worker_engine,
)
from apps.trading_worker.persistence import (
    PersistenceConfig,
    PersistenceManager,
    PersistenceMode,
)
from apps.trading_worker.venues.binance.config import BinanceEnvironment, environment_label
from apps.trading_worker.venues.binance.models import ConnectionState

client = TestClient(app)

def test_default_worker_state_endpoint():
    """Verify /state returns a valid WorkerRuntimeState even before worker app attachment."""
    set_worker_engine(None)
    response = client.get("/state")
    assert response.status_code == 200
    data = response.json()
    
    assert data["execution_mode"] == "PAPER"
    assert data["data_source"] == "SIMULATED"
    assert data["engine_state"] == "DISARMED"
    assert data["connection_state"] == "DISCONNECTED"
    assert data["market_data_healthy"] is False
    assert data["private_stream_healthy"] is False
    assert data["kill_switch_active"] is False
    assert "health_indicators" in data
    assert data["health_indicators"]["reconciliation_status"] == "DISCONNECTED"


@pytest.mark.asyncio
async def test_required_persistence_blocks_paper_readiness_and_arming():
    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    worker.persistence = PersistenceManager(
        config=PersistenceConfig(mode=PersistenceMode.REQUIRED)
    )

    preflight = worker.get_preflight("PAPER")
    persistence_check = next(
        check for check in preflight["checks"] if check["id"] == "CHK-PERSISTENCE"
    )
    assert preflight["canArm"] is False
    assert persistence_check["required"] is True
    assert persistence_check["status"] == "FAIL"

    success, message = await worker.arm(
        {
            "executionMode": "PAPER",
            "instruments": ["BTCUSDT"],
            "strategies": {"grid": True},
        }
    )
    assert success is False
    assert "Required persistence is not ready" in message


@pytest.mark.asyncio
async def test_mainnet_read_only_preflight_never_arms_worker_or_submits_orders(monkeypatch):
    """Signed observation evidence must not mutate the Worker lifecycle."""

    monkeypatch.setenv("BINANCE_MAINNET_API_KEY", "preflight-key")
    monkeypatch.setenv("BINANCE_MAINNET_API_SECRET", "preflight-secret")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "false")

    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    worker.persistence = SimpleNamespace(
        readiness=lambda: {
            "mode": "REQUIRED",
            "durable": True,
            "pending_outbox": 0,
            "failed_writes": 0,
        },
    )
    original_signature = (
        worker.execution_mode,
        worker.engine_state,
        worker.connection_state,
        worker.market_data_healthy,
        worker.private_stream_healthy,
        worker.authenticated,
        worker.reconciliation_status,
        worker.kill_switch_active,
        worker.pause_new_risk,
        worker.recovery_only,
        tuple(worker.symbols),
        worker.execution_adapter,
        worker.active_configuration,
    )

    now = datetime.now(UTC)
    window_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    snapshot = SimpleNamespace(
        valid=True,
        timestamp=now,
        exchange_environment=environment_label(BinanceEnvironment.MAINNET),
        wallet_balance=Decimal(100),
        margin_balance=Decimal(100),
        available_balance=Decimal(100),
        unrealized_pnl=Decimal(0),
        total_initial_margin=Decimal(0),
        total_maint_margin=Decimal(0),
        position_initial_margin=Decimal(0),
        total_position_notional=Decimal(0),
        effective_leverage=Decimal(0),
        margin_utilization_pct=Decimal(0),
        liquidation_safety="KNOWN",
        min_liquidation_distance_pct=None,
        collateral_asset="USDC",
        risk_currency="USDC",
        daily_loss_asset="USDC",
        daily_pnl_includes_fees=True,
        daily_pnl_includes_funding=True,
        daily_loss_known=True,
        daily_realized_pnl=Decimal(0),
        daily_loss_window_start=window_start,
        daily_loss_window_end=window_start + timedelta(days=1),
        configured_leverage=Decimal(5),
        configured_leverage_known=True,
        margin_mode="SINGLE_ASSET_CROSS",
        margin_mode_known=True,
    )

    class FakeAdapter:
        instances = []

        def __init__(self, **kwargs):
            assert kwargs["env"] is BinanceEnvironment.MAINNET
            assert kwargs["preflight_only"] is True
            self.env = kwargs["env"]
            self.preflight_only = kwargs["preflight_only"]
            self.connection_state = ConnectionState.READY
            self.authenticated = True
            self.private_stream_healthy = True
            self.account_snapshot = snapshot
            self.last_market_event_at = {"ETHUSDC": now}
            self.capabilities = SimpleNamespace(
                account_request_succeeded=True,
                authenticated=True,
                trade_authorized=True,
                position_mode_known=True,
            )
            self.reconciliation = SimpleNamespace(last_status="IN_SYNC")
            self.closed = False
            self.order_submission_attempts = 0
            self.__class__.instances.append(self)

        async def connect(self):
            return True

        async def refresh_market_data(self, symbols):
            assert symbols == ["ETHUSDC"]
            return True

        def is_symbol_ready_for_execution(self, symbol):
            return symbol == "ETHUSDC"

        def is_account_snapshot_fresh(self):
            return True

        def has_authoritative_market_sample(self, symbol):
            return symbol == "ETHUSDC"

        def _market_data_max_age(self):
            return 30.0

        async def close(self):
            self.closed = True

    monkeypatch.setattr("apps.trading_worker.main.BinanceExecutionAdapter", FakeAdapter)

    result = await worker.run_mainnet_read_only_preflight()

    assert result["preflightOnly"] is True
    assert result["preflightPassed"] is True
    assert result["canArm"] is False
    assert result["mainnetLiveApproved"] is False
    assert result["orderSubmissionAttempts"] == 0
    assert result["order_submission_attempts"] == 0
    assert result["orderEndpointAttempts"] == 0
    assert FakeAdapter.instances[-1].closed is True
    assert FakeAdapter.instances[-1].order_submission_attempts == 0
    assert (
        worker.execution_mode,
        worker.engine_state,
        worker.connection_state,
        worker.market_data_healthy,
        worker.private_stream_healthy,
        worker.authenticated,
        worker.reconciliation_status,
        worker.kill_switch_active,
        worker.pause_new_risk,
        worker.recovery_only,
        tuple(worker.symbols),
        worker.execution_adapter,
        worker.active_configuration,
    ) == original_signature


@pytest.mark.asyncio
async def test_mainnet_preflight_rejects_unknown_persistence_counters(monkeypatch):
    """Durable=True without explicit outbox counters is not Mainnet evidence."""

    monkeypatch.delenv("BINANCE_MAINNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_MAINNET_API_SECRET", raising=False)

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.persistence = SimpleNamespace(
        readiness=lambda: {"mode": "REQUIRED", "durable": True},
    )

    result = await worker.run_mainnet_read_only_preflight()

    assert result["preflightPassed"] is False
    persistence_check = next(
        check
        for check in result["checks"]
        if check["id"] == "CHK-PREFLIGHT-PERSISTENCE"
    )
    assert persistence_check["status"] == "FAIL"


@pytest.mark.asyncio
async def test_worker_state_transitions_and_health():
    """Verify /state returns live engine state and health indicators when attached to TradingWorkerApp."""
    worker = TradingWorkerApp(symbols=["BTCUSDT", "ETHUSDT"])
    set_worker_engine(worker)
    
    # Initial state: DISARMED
    resp = client.get("/state")
    assert resp.status_code == 200
    state = resp.json()
    assert state["engine_state"] == "DISARMED"
    assert state["execution_mode"] == "PAPER"
    assert state["data_source"] == "SIMULATED"
    assert state["market_data_healthy"] is False
    assert state["health_indicators"]["active_symbols_count"] == 2
    
    # Paper arming remains available with an explicit instrument and strategy.
    arm_resp = client.post(
        "/arm",
        json={
            "executionMode": "PAPER",
            "instruments": ["BTCUSDT"],
            "strategies": {"grid": True},
        },
    )
    assert arm_resp.status_code == 200
    
    resp = client.get("/state")
    state = resp.json()
    assert state["engine_state"] == "ARMED"
    assert state["execution_mode"] == "PAPER"
    assert state["data_source"] == "SIMULATED"
    
    # Update health indicators
    worker.market_data_healthy = True
    worker.connection_state = "READY"
    worker.authenticated = True
    worker.reconciliation_status = "IN_SYNC"
    
    resp = client.get("/state")
    state = resp.json()
    assert state["market_data_healthy"] is True
    assert state["trading_connection_healthy"] is True
    assert state["authenticated"] is True
    assert state["reconciliation_status"] == "IN_SYNC"
    assert state["health_indicators"]["trading_connection_healthy"] is True
    assert state["health_indicators"]["market_data_healthy"] is True
    
    # Disarming worker
    disarm_resp = client.post("/disarm")
    assert disarm_resp.status_code == 200
    
    resp = client.get("/state")
    state = resp.json()
    assert state["engine_state"] == "DISARMED"
    
    # Clean up
    set_worker_engine(None)


def test_worker_runtime_state_model_contract():
    """Verify WorkerExecutionMode, WorkerEngineState, and WorkerRuntimeState schema contract."""
    # Enums
    assert set(WorkerExecutionMode) == {"PAPER", "TESTNET", "LIVE"}
    assert set(WorkerEngineState) == {
        "DISARMED", "ARMING", "ARMED", "PAUSED_NEW_RISK", "RECOVERY_ONLY", "DEGRADED", "EMERGENCY"
    }

    # Model construction with explicit fields
    state = WorkerRuntimeState(
        execution_mode=WorkerExecutionMode.TESTNET,
        engine_state=WorkerEngineState.ARMED,
        engine_status=WorkerEngineState.DISARMED,
        connection_state="READY",
        connection_status="DEGRADED",
        market_data_healthy=True,
        private_stream_healthy=True,
        trading_connection_healthy=True,
        authenticated=True,
        reconciliation_status="IN_SYNC",
        kill_switch_active=False,
        kill_switch_status=True,
        active_configuration={"symbol": "BTCUSDT", "leverage": 5},
        configuration_details={"legacy": True},
    )

    assert state.execution_authority == "PYTHON_TRADING_WORKER"
    assert state.is_execution_authority is True
    assert state.provenance == "BINANCE_TESTNET"
    assert state.execution_mode == WorkerExecutionMode.TESTNET
    assert state.engine_state == WorkerEngineState.ARMED
    assert state.engine_status == WorkerEngineState.ARMED
    assert state.connection_status == "READY"
    assert state.connection_state == "READY"
    assert state.market_data_healthy is True
    assert state.private_stream_healthy is True
    assert state.reconciliation_status == "IN_SYNC"
    assert state.kill_switch_status is False
    assert state.kill_switch_active is False
    assert state.configuration_details == {"symbol": "BTCUSDT", "leverage": 5}
    assert state.active_configuration == {"symbol": "BTCUSDT", "leverage": 5}
    assert state.heartbeat_at is not None

    # Compatibility names are serialization aliases, not independently
    # mutable runtime fields. Canonical values win if both names are supplied.
    assert "engine_status" not in WorkerRuntimeState.model_fields
    assert "connection_status" not in WorkerRuntimeState.model_fields
    assert "kill_switch_status" not in WorkerRuntimeState.model_fields
    assert "configuration_details" not in WorkerRuntimeState.model_fields
    state.engine_state = WorkerEngineState.DEGRADED
    state.connection_state = "DEGRADED"
    state.kill_switch_active = True
    state.active_configuration = {"canonical": True}
    assert state.engine_status == WorkerEngineState.DEGRADED
    assert state.connection_status == "DEGRADED"
    assert state.kill_switch_status is True
    assert state.configuration_details == {"canonical": True}

    dumped = state.model_dump()
    assert dumped["execution_mode"] == "TESTNET"
    assert dumped["engine_state"] == "DEGRADED"
    assert dumped["engine_status"] == WorkerEngineState.DEGRADED
    assert dumped["connection_state"] == "DEGRADED"
    assert dumped["connection_status"] == "DEGRADED"
    assert dumped["kill_switch_active"] is True
    assert dumped["kill_switch_status"] is True
    assert dumped["configuration_details"] == {"canonical": True}
    assert "heartbeat_at" in dumped


@pytest.mark.asyncio
async def test_worker_heartbeat_background_task():
    """Verify that TradingWorkerApp heartbeat loop updates heartbeat_at for Control Plane liveness."""
    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    worker.heartbeat_interval_sec = 0.05
    initial_heartbeat = worker.heartbeat_at
    assert initial_heartbeat is not None

    # Launch heartbeat loop task
    worker.heartbeat_task = asyncio.create_task(worker._heartbeat_loop())
    await asyncio.sleep(0.15)

    # State must reflect updated heartbeat_at
    state = worker.get_state()
    assert state.heartbeat_at > initial_heartbeat
    if state.health_indicators:
        assert state.health_indicators.heartbeat_at == state.heartbeat_at

    # Graceful stop cancels heartbeat task
    await worker.stop()
    assert worker.is_running is False
    await asyncio.sleep(0.01)
    assert worker.heartbeat_task.done()


def test_state_endpoint_serialization_contract():
    """Verify that GET /state returns the fully serialized WorkerRuntimeState for the Control Plane."""
    worker = TradingWorkerApp(symbols=["BTCUSDT", "ETHUSDT"])
    worker.execution_mode = WorkerExecutionMode.TESTNET
    worker.engine_state = WorkerEngineState.ARMED
    worker.connection_state = "READY"
    worker.market_data_healthy = True
    worker.private_stream_healthy = True
    worker.authenticated = True
    worker.reconciliation_status = "IN_SYNC"
    worker.active_configuration = {"symbol": "BTCUSDT", "leverage": 10}
    set_worker_engine(worker)

    response = client.get("/state")
    assert response.status_code == 200
    payload = response.json()

    # Core Execution Authority fields
    assert payload["execution_authority"] == "PYTHON_TRADING_WORKER"
    assert payload["is_execution_authority"] is True
    assert payload["execution_mode"] == "TESTNET"
    assert payload["provenance"] == "BINANCE_TESTNET"
    assert payload["data_source"] == "BINANCE"
    assert payload["exchange_environment"] == "BINANCE_TESTNET"

    # Engine Status & Lifecycle
    assert payload["engine_state"] == "ARMED"
    assert payload["engine_status"] == "ARMED"
    assert payload["connection_status"] == "READY"
    assert payload["connection_state"] == "READY"

    # Stream Health & Synchronization
    assert payload["market_data_healthy"] is True
    assert payload["private_stream_healthy"] is True
    assert payload["trading_connection_healthy"] is True
    assert payload["authenticated"] is True
    assert payload["account_synchronized"] is True
    assert payload["reconciliation_status"] == "IN_SYNC"

    # Risk Invariants
    assert payload["kill_switch_status"] is False
    assert payload["kill_switch_active"] is False
    assert payload["pause_new_risk"] is False
    assert payload["recovery_only"] is False

    # Liveness & Telemetry
    assert "heartbeat_at" in payload
    assert payload["heartbeat_at"] is not None
    assert "health_indicators" in payload
    assert payload["health_indicators"]["heartbeat_at"] is not None
    assert payload["health_indicators"]["active_symbols"] == ["BTCUSDT", "ETHUSDT"]
    assert payload["health_indicators"]["active_symbols_count"] == 2

    # Configuration
    assert payload["config_version"] == "v0.2.0-beta"
    assert payload["configuration_details"] == {"symbol": "BTCUSDT", "leverage": 10}
    assert payload["active_configuration"] == {"symbol": "BTCUSDT", "leverage": 10}

    # Clean up
    set_worker_engine(None)


@pytest.mark.asyncio
async def test_worker_start_heartbeat_lifecycle():
    """Verify that start_heartbeat and stop_heartbeat manage background task cleanly and update heartbeat_at."""
    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    t0 = worker.heartbeat_at
    task = worker.start_heartbeat(interval_sec=0.03)
    assert worker.heartbeat_task is task
    assert not task.done()

    await asyncio.sleep(0.10)
    t1 = worker.heartbeat_at
    assert t1 > t0

    worker.stop_heartbeat()
    await asyncio.sleep(0.01)
    assert task.done()


@pytest.mark.asyncio
async def test_global_heartbeat_background_updater():
    """Verify that the background heartbeat loop updates default state heartbeat_at when no engine is attached."""
    set_worker_engine(None)
    initial_default = get_default_state()
    initial_hb = initial_default.heartbeat_at

    # Run the background updater loop task for a short interval
    task = asyncio.create_task(_global_heartbeat_loop(interval_sec=0.03))
    try:
        await asyncio.sleep(0.08)
        updated_default = get_default_state()
        assert updated_default.heartbeat_at > initial_hb
        assert updated_default.health_indicators.heartbeat_at == updated_default.heartbeat_at
    finally:
        task.cancel()
        await asyncio.sleep(0.01)


def test_worker_preflight_and_live_release_gate():
    """Verify LIVE is release-gated and TESTNET still validates readiness."""
    worker = TradingWorkerApp(symbols=["BTCUSDT", "ETHUSDT"])
    set_worker_engine(worker)

    # 1. LIVE mode preflight must fail
    live_resp = client.get("/preflight?execution_mode=LIVE")
    assert live_resp.status_code == 200
    live_data = live_resp.json()
    assert live_data["canArm"] is False
    assert any(c["id"] == "CHK-MAINNET-APPROVAL" for c in live_data["checks"])
    assert any("MAINNET_LIVE_APPROVED" in c["message"] for c in live_data["checks"])

    # 2. Arming in LIVE mode remains fail-closed without release credentials.
    arm_live = client.post("/arm", json={"executionMode": "LIVE", "instruments": ["ETHUSDC"]})
    assert arm_live.status_code == 400
    assert "MAINNET_LIVE_APPROVED" in str(arm_live.json()) or "credential" in str(arm_live.json()).lower()

    # 3. TESTNET preflight when unconfigured / unauthenticated
    testnet_resp = client.get("/preflight?execution_mode=TESTNET")
    assert testnet_resp.status_code == 200
    testnet_data = testnet_resp.json()
    assert testnet_data["canArm"] is False

    # 4. Strict arming in TESTNET fails if preflight fails
    arm_testnet_strict = client.post(
        "/arm",
        json={
            "executionMode": "TESTNET",
            "instruments": ["BTCUSDT"],
            "strategies": {"grid": True},
            "enforcePreflight": True,
        },
    )
    assert arm_testnet_strict.status_code == 400
    assert "Configuration Preflight Failed" in arm_testnet_strict.json()["detail"]

    # 5. Local flags and hand-written health fields cannot manufacture Testnet
    # readiness without a real adapter, account snapshot, rules, and market data.
    worker.authenticated = True
    worker.connection_state = "READY"
    worker.reconciliation_status = "IN_SYNC"
    worker.private_stream_healthy = True
    
    ready_preflight = client.get("/preflight?execution_mode=TESTNET")
    assert ready_preflight.status_code == 200
    assert ready_preflight.json()["canArm"] is False
    set_worker_engine(None)


def test_reconcile_endpoint_integration():
    """Verify that POST /reconcile triggers reconciliation and updates state."""
    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    set_worker_engine(worker)

    worker.reconciliation_status = "UNKNOWN"
    rec_resp = client.post("/reconcile")
    assert rec_resp.status_code == 200
    assert rec_resp.json()["status"] == "SIMULATED_SYNC"

    state_resp = client.get("/state")
    assert state_resp.status_code == 200
    assert state_resp.json()["reconciliation_status"] == "SIMULATED_SYNC"
    assert state_resp.json()["account_synchronized"] is False

    set_worker_engine(None)


