import asyncio
import pytest
from fastapi.testclient import TestClient
from apps.trading_worker.main import (
    app,
    set_worker_engine,
    WORKER_ENGINE,
    WorkerExecutionMode,
    WorkerEngineState,
    WorkerRuntimeState,
    HealthIndicators,
    TradingWorkerApp,
    get_default_state,
    _global_heartbeat_loop,
)

client = TestClient(app)

def test_default_worker_state_endpoint():
    """Verify /state returns a valid WorkerRuntimeState even before worker app attachment."""
    set_worker_engine(None)
    response = client.get("/state")
    assert response.status_code == 200
    data = response.json()
    
    assert data["execution_mode"] == "PAPER"
    assert data["engine_state"] == "DISARMED"
    assert data["connection_state"] == "DISCONNECTED"
    assert data["market_data_healthy"] is False
    assert data["private_stream_healthy"] is False
    assert data["kill_switch_active"] is False
    assert "health_indicators" in data
    assert data["health_indicators"]["reconciliation_status"] == "DISCONNECTED"


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
    assert state["market_data_healthy"] is False
    assert state["health_indicators"]["active_symbols_count"] == 2
    
    # Arming worker in TESTNET mode
    arm_resp = client.post("/arm", json={"executionMode": "TESTNET"})
    assert arm_resp.status_code == 200
    
    resp = client.get("/state")
    state = resp.json()
    assert state["engine_state"] == "ARMED"
    assert state["execution_mode"] == "TESTNET"
    
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
        connection_status="READY",
        market_data_healthy=True,
        private_stream_healthy=True,
        trading_connection_healthy=True,
        authenticated=True,
        reconciliation_status="IN_SYNC",
        kill_switch_status=False,
        configuration_details={"symbol": "BTCUSDT", "leverage": 5},
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

    dumped = state.model_dump()
    assert dumped["execution_mode"] == "TESTNET"
    assert dumped["engine_state"] == "ARMED"
    assert dumped["engine_status"] == "ARMED"
    assert dumped["connection_status"] == "READY"
    assert dumped["kill_switch_status"] == False
    assert dumped["configuration_details"] == {"symbol": "BTCUSDT", "leverage": 5}
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
    worker.stop()
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



