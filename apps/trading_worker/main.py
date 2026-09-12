import asyncio
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, List, Optional
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator, ConfigDict

from domain.models import MarketEvent, MarketType, RiskSnapshot, utc_now
from domain.enums import RiskState
from apps.trading_worker.engines.price_action import PriceActionEngine
from apps.trading_worker.engines.market_state import MarketStateClassifier
from apps.trading_worker.engines.grid_strategy import GridStrategyEngine
from apps.trading_worker.engines.trend_strategy import TrendStrategyEngine
from apps.trading_worker.engines.shock_strategy import ShockStrategyEngine
from apps.trading_worker.engines.exposure_recovery import ExposureRecoveryEngine
from apps.trading_worker.engines.funding_carry import FundingCarryEngine
from apps.trading_worker.engines.market_scanner import MarketScannerEngine
from apps.trading_worker.engines.meta_allocator import MetaAllocator
from apps.trading_worker.engines.risk_governor import RiskGovernor

from venues.binance.public_ws import BinancePublicWebSocket
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.models import ConnectionState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("blessing.worker")

# --- Worker Runtime State Models & Execution Authority ---

class WorkerExecutionMode(str, Enum):
    """Execution environment mode determining order routing target and execution risk."""
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE = "LIVE"

class WorkerEngineState(str, Enum):
    """Authoritative operational lifecycle state of the trading engine."""
    DISARMED = "DISARMED"
    ARMING = "ARMING"
    ARMED = "ARMED"
    PAUSED_NEW_RISK = "PAUSED_NEW_RISK"
    RECOVERY_ONLY = "RECOVERY_ONLY"
    DEGRADED = "DEGRADED"
    EMERGENCY = "EMERGENCY"

class HealthIndicators(BaseModel):
    market_data_healthy: bool = False
    private_stream_healthy: bool = False
    trading_connection_healthy: bool = False
    authenticated: bool = False
    reconciliation_status: str = "UNKNOWN"
    active_symbols: List[str] = Field(default_factory=list)
    active_symbols_count: int = 0
    uptime_seconds: float = 0.0
    heartbeat_at: datetime = Field(default_factory=utc_now)
    last_heartbeat: datetime = Field(default_factory=utc_now)

class WorkerRuntimeState(BaseModel):
    """Authoritative execution state model for the Python Trading Worker.
    
    Formalizes the Execution Authority contract (docs/EXECUTION-AUTHORITY.md) and
    System State Model (docs/SYSTEM-STATE-MODEL.md), guaranteeing single-authority
    execution without split-brain concurrency.
    """
    # Execution Authority & Provenance
    execution_authority: str = "PYTHON_TRADING_WORKER"
    is_execution_authority: bool = True
    execution_mode: WorkerExecutionMode = WorkerExecutionMode.PAPER
    provenance: str = "SIMULATED"
    data_source: str = "BINANCE"
    exchange_environment: str = "NONE"

    # Engine Operational State
    engine_state: WorkerEngineState = WorkerEngineState.DISARMED
    engine_status: WorkerEngineState = WorkerEngineState.DISARMED
    connection_status: str = "DISCONNECTED"
    connection_state: str = "DISCONNECTED"

    # Stream Health & Connectivity Checks
    market_data_healthy: bool = False
    private_stream_healthy: bool = False
    trading_connection_healthy: bool = False
    authenticated: bool = False
    account_synchronized: bool = False
    reconciliation_status: str = "UNKNOWN"

    # Safety Controls & Risk Governor Invariants
    kill_switch_status: bool = False
    kill_switch_active: bool = False
    pause_new_risk: bool = False
    recovery_only: bool = False

    # Configuration Details & Versioning
    config_version: str = "v0.2.0-beta"
    configuration_details: Optional[dict] = None
    active_configuration: Optional[dict] = None

    # Telemetry, Heartbeat & Timestamps
    heartbeat_at: datetime = Field(default_factory=utc_now)
    health_indicators: Optional[HealthIndicators] = None
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def sync_compatibility_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            # Sync heartbeat_at / last_heartbeat
            if "last_heartbeat" in data and "heartbeat_at" not in data:
                data["heartbeat_at"] = data["last_heartbeat"]
            elif "heartbeat_at" in data and "last_heartbeat" not in data:
                data["last_heartbeat"] = data["heartbeat_at"]
            # Sync engine_status / engine_state
            if "engine_status" in data and "engine_state" not in data:
                data["engine_state"] = data["engine_status"]
            elif "engine_state" in data and "engine_status" not in data:
                data["engine_status"] = data["engine_state"]
            # Sync connection_status / connection_state
            if "connection_status" in data and "connection_state" not in data:
                data["connection_state"] = data["connection_status"]
            elif "connection_state" in data and "connection_status" not in data:
                data["connection_status"] = data["connection_state"]
            # Sync kill_switch_status / kill_switch_active
            if "kill_switch_status" in data and "kill_switch_active" not in data:
                data["kill_switch_active"] = data["kill_switch_status"]
            elif "kill_switch_active" in data and "kill_switch_status" not in data:
                data["kill_switch_status"] = data["kill_switch_active"]
            # Sync configuration_details / active_configuration
            if "configuration_details" in data and "active_configuration" not in data:
                data["active_configuration"] = data["configuration_details"]
            elif "active_configuration" in data and "configuration_details" not in data:
                data["configuration_details"] = data["active_configuration"]
            # Sync authenticated / account_synchronized
            if "authenticated" in data and "account_synchronized" not in data:
                data["account_synchronized"] = data["authenticated"]
            elif "account_synchronized" in data and "authenticated" not in data:
                data["authenticated"] = data["account_synchronized"]
            # Derive provenance and exchange environment if not set
            if "execution_mode" in data:
                mode = data["execution_mode"]
                if mode in (WorkerExecutionMode.TESTNET, "TESTNET"):
                    data.setdefault("provenance", "BINANCE_TESTNET")
                    data.setdefault("exchange_environment", "BINANCE_TESTNET")
                elif mode in (WorkerExecutionMode.LIVE, "LIVE"):
                    data.setdefault("provenance", "BINANCE_LIVE")
                    data.setdefault("exchange_environment", "BINANCE_MAINNET")
                else:
                    data.setdefault("provenance", "SIMULATED")
                    data.setdefault("exchange_environment", "NONE")
        return data

# Global reference to the main execution engine / loop
WORKER_ENGINE: Optional[Any] = None

# Global background heartbeat tracking for Control Plane liveness monitoring
_GLOBAL_HEARTBEAT_TASK: Optional[asyncio.Task] = None
_DEFAULT_HEARTBEAT_AT: datetime = utc_now()

def update_default_heartbeat() -> datetime:
    """Monotonically update and return the default heartbeat timestamp."""
    global _DEFAULT_HEARTBEAT_AT
    _DEFAULT_HEARTBEAT_AT = utc_now()
    return _DEFAULT_HEARTBEAT_AT

async def _global_heartbeat_loop(interval_sec: float = 1.0):
    """Background task periodically refreshing heartbeat_at in the worker state for Control Plane liveness."""
    while True:
        update_default_heartbeat()
        if WORKER_ENGINE is not None and hasattr(WORKER_ENGINE, "record_heartbeat"):
            WORKER_ENGINE.record_heartbeat()
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error("Global heartbeat loop error: %s", e)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager launching periodic background heartbeat loop for the worker."""
    global _GLOBAL_HEARTBEAT_TASK
    _GLOBAL_HEARTBEAT_TASK = asyncio.create_task(_global_heartbeat_loop())
    logger.info("Worker FastAPI lifespan: Background heartbeat loop started.")
    try:
        yield
    finally:
        if _GLOBAL_HEARTBEAT_TASK and not _GLOBAL_HEARTBEAT_TASK.done():
            _GLOBAL_HEARTBEAT_TASK.cancel()
        logger.info("Worker FastAPI lifespan: Background heartbeat loop stopped.")

def set_worker_engine(engine: Any) -> None:
    global WORKER_ENGINE
    WORKER_ENGINE = engine
    if engine is not None and hasattr(engine, "start_heartbeat"):
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running() and (not getattr(engine, "heartbeat_task", None) or engine.heartbeat_task.done()):
                engine.start_heartbeat()
        except RuntimeError:
            pass

def get_default_state() -> WorkerRuntimeState:
    now = _DEFAULT_HEARTBEAT_AT
    health = HealthIndicators(
        market_data_healthy=False,
        private_stream_healthy=False,
        trading_connection_healthy=False,
        authenticated=False,
        reconciliation_status="DISCONNECTED",
        active_symbols=[],
        active_symbols_count=0,
        uptime_seconds=0.0,
        heartbeat_at=now,
        last_heartbeat=now,
    )
    return WorkerRuntimeState(
        execution_authority="PYTHON_TRADING_WORKER",
        is_execution_authority=True,
        execution_mode=WorkerExecutionMode.PAPER,
        provenance="SIMULATED",
        data_source="BINANCE",
        exchange_environment="NONE",
        engine_state=WorkerEngineState.DISARMED,
        engine_status=WorkerEngineState.DISARMED,
        connection_status="DISCONNECTED",
        connection_state="DISCONNECTED",
        market_data_healthy=False,
        private_stream_healthy=False,
        trading_connection_healthy=False,
        authenticated=False,
        account_synchronized=False,
        reconciliation_status="DISCONNECTED",
        kill_switch_status=False,
        kill_switch_active=False,
        pause_new_risk=False,
        recovery_only=False,
        heartbeat_at=now,
        health_indicators=health,
        config_version="v0.2.0-beta",
        configuration_details=None,
        active_configuration=None,
        updated_at=utc_now()
    )

# FastAPI Control Plane API
app = FastAPI(title="Blessing AI Worker Control API", lifespan=lifespan)

@app.get("/health")
def health_check():
    return {"status": "ok", "timestamp": utc_now()}

@app.get(
    "/state",
    response_model=WorkerRuntimeState,
    summary="Get Authoritative Worker Runtime State",
    description=(
        "Returns the complete serialized WorkerRuntimeState for consumption by the Control Plane. "
        "Encompasses execution authority, execution mode, engine state, connection status, "
        "market/private stream health indicators, reconciliation status, heartbeat timestamp, "
        "kill switch status, and active configuration details."
    ),
    tags=["Control Plane"],
)
def get_state() -> WorkerRuntimeState:
    """Return the authoritative serialized WorkerRuntimeState for the Control Plane."""
    if WORKER_ENGINE is not None and hasattr(WORKER_ENGINE, "get_state"):
        return WORKER_ENGINE.get_state()
    return get_default_state()

@app.get("/capabilities")
def get_capabilities():
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    return WORKER_ENGINE.get_capabilities()

@app.post("/arm")
async def arm(config: dict):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    success, msg = await WORKER_ENGINE.arm(config)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    state = WORKER_ENGINE.get_state().model_dump()
    state["status"] = "ARMED"
    return state

@app.get("/readiness")
def get_readiness_endpoint():
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    return WORKER_ENGINE.get_launch_readiness()

@app.post("/disarm")
async def disarm():
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    await WORKER_ENGINE.disarm()
    return {"status": "DISARMED"}

@app.get("/preflight")
def get_preflight_endpoint(execution_mode: str = "PAPER"):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    return WORKER_ENGINE.get_preflight(execution_mode)

@app.post("/pause-new-risk")
async def pause_new_risk_endpoint(req: dict):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    active = req.get("active", True)
    await WORKER_ENGINE.set_pause_new_risk(active)
    return {"status": "ok"}

@app.post("/recovery-only")
async def recovery_only_endpoint(req: dict):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    active = req.get("active", True)
    await WORKER_ENGINE.set_recovery_only(active)
    return {"status": "ok"}

@app.post("/kill-switch")
async def kill_switch_endpoint(req: dict):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    active = req.get("active", True)
    await WORKER_ENGINE.set_kill_switch(active)
    return {"status": "ok"}

@app.post("/reconcile")
async def reconcile_endpoint():
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    res = await WORKER_ENGINE.trigger_reconciliation()
    return {"status": res}

class TradingWorkerApp:
    def __init__(self, symbols: List[str] = ["BTCUSDT", "ETHUSDT"]):
        self.symbols = symbols
        self.is_running = True
        
        # Engines
        self.pa_engine = PriceActionEngine()
        self.market_state_engine = MarketStateClassifier()
        self.grid_engine = GridStrategyEngine()
        self.trend_engine = TrendStrategyEngine()
        self.shock_engine = ShockStrategyEngine()
        self.carry_engine = FundingCarryEngine()
        self.recovery_engine = ExposureRecoveryEngine()
        self.meta_allocator = MetaAllocator()
        self.risk_governor = RiskGovernor()
        self.scanner = MarketScannerEngine(top_n=8)
        self.scan_task = None
        
        self.ws_client = None
        self.execution_adapter: Optional[BinanceExecutionAdapter] = None
        
        # Runtime State
        self.start_time = utc_now()
        self.heartbeat_at = utc_now()
        self.heartbeat_interval_sec: float = 1.0
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.execution_mode = WorkerExecutionMode.PAPER
        self.engine_state = WorkerEngineState.DISARMED
        self.connection_state = "DISCONNECTED"
        self.market_data_healthy = False
        self.private_stream_healthy = False
        self.authenticated = False
        self.reconciliation_status = "UNKNOWN"
        self.kill_switch_active = False
        self.pause_new_risk = False
        self.recovery_only = False
        self.active_configuration = None
        self.updated_at = utc_now()

    def record_heartbeat(self) -> datetime:
        """Update and return the current heartbeat timestamp."""
        self.heartbeat_at = utc_now()
        return self.heartbeat_at

    def start_heartbeat(self, interval_sec: Optional[float] = None) -> asyncio.Task:
        """Start or restart the background heartbeat loop task."""
        if interval_sec is not None:
            self.heartbeat_interval_sec = interval_sec
        self.stop_heartbeat()
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return self.heartbeat_task

    def stop_heartbeat(self) -> None:
        """Gracefully cancel the background heartbeat loop task."""
        if self.heartbeat_task and not self.heartbeat_task.done():
            self.heartbeat_task.cancel()

    def get_state(self) -> WorkerRuntimeState:
        uptime = (utc_now() - self.start_time).total_seconds()
        trading_healthy = self.connection_state == "READY"
        health = HealthIndicators(
            market_data_healthy=self.market_data_healthy,
            private_stream_healthy=self.private_stream_healthy,
            trading_connection_healthy=trading_healthy,
            authenticated=self.authenticated,
            reconciliation_status=self.reconciliation_status,
            active_symbols=list(self.symbols) if self.symbols else [],
            active_symbols_count=len(self.symbols) if self.symbols else 0,
            uptime_seconds=round(uptime, 2),
            heartbeat_at=self.heartbeat_at,
            last_heartbeat=self.heartbeat_at
        )
        provenance = "SIMULATED"
        exchange_env = "NONE"
        if self.execution_mode == WorkerExecutionMode.TESTNET:
            provenance = "BINANCE_TESTNET"
            exchange_env = "BINANCE_TESTNET"
        elif self.execution_mode == WorkerExecutionMode.LIVE:
            provenance = "BINANCE_LIVE"
            exchange_env = "BINANCE_MAINNET"

        return WorkerRuntimeState(
            execution_authority="PYTHON_TRADING_WORKER",
            is_execution_authority=True,
            execution_mode=self.execution_mode,
            provenance=provenance,
            data_source="BINANCE",
            exchange_environment=exchange_env,
            engine_state=self.engine_state,
            engine_status=self.engine_state,
            connection_status=self.connection_state,
            connection_state=self.connection_state,
            market_data_healthy=self.market_data_healthy,
            private_stream_healthy=self.private_stream_healthy,
            trading_connection_healthy=trading_healthy,
            authenticated=self.authenticated,
            account_synchronized=self.reconciliation_status == "IN_SYNC",
            reconciliation_status=self.reconciliation_status,
            kill_switch_status=self.kill_switch_active,
            kill_switch_active=self.kill_switch_active,
            pause_new_risk=self.pause_new_risk,
            recovery_only=self.recovery_only,
            heartbeat_at=self.heartbeat_at,
            health_indicators=health,
            config_version="v0.2.0-beta",
            configuration_details=self.active_configuration,
            active_configuration=self.active_configuration,
            updated_at=utc_now()
        )
        
    def get_capabilities(self) -> dict:
        testnet_configured = bool(
            os.getenv("BINANCE_TESTNET_API_KEY") and os.getenv("BINANCE_TESTNET_API_SECRET")
        )
        adapter_ready = (
            self.execution_adapter is not None
            and getattr(self.execution_adapter, "state", None) == ConnectionState.READY
        )
        symbol_rules_loaded = (
            self.execution_adapter is not None
            and bool(getattr(self.execution_adapter.capabilities, "symbol_rules", {}))
        )
        testnet_ready = (
            testnet_configured
            and self.authenticated
            and self.connection_state == "READY"
            and self.private_stream_healthy
            and self.reconciliation_status == "IN_SYNC"
            and not self.kill_switch_active
        )
        return {
            "paper": True,
            "testnetConfigured": testnet_configured,
            "testnetAuthenticated": self.authenticated,
            "testnetPrivateStreamHealthy": self.private_stream_healthy,
            "testnetReconciliationInSync": self.reconciliation_status == "IN_SYNC",
            "testnetSymbolRulesLoaded": symbol_rules_loaded,
            "testnetAdapterReady": adapter_ready,
            "testnetExecutionReady": testnet_ready,
            "liveConfigured": False,
            "liveExecutionReady": False,
            "spotSupported": False,
            "usdmFuturesSupported": True,
            "hedgeModeSupported": False
        }

    def get_launch_readiness(self) -> dict:
        testnet_configured = bool(
            os.getenv("BINANCE_TESTNET_API_KEY") and os.getenv("BINANCE_TESTNET_API_SECRET")
        )
        auto_flag = os.getenv("AUTONOMOUS_TESTNET_EXECUTION", "false").lower() in ("true", "1", "yes")
        read_only_ready = testnet_configured and self.authenticated and self.market_data_healthy
        manual_ready = (
            read_only_ready
            and self.reconciliation_status == "IN_SYNC"
            and self.private_stream_healthy
            and not self.kill_switch_active
        )
        autonomous_ready = manual_ready and auto_flag and self.engine_state == WorkerEngineState.ARMED
        return {
            "PAPER_READY": not self.kill_switch_active,
            "TESTNET_READ_ONLY_READY": read_only_ready,
            "TESTNET_MANUAL_READY": manual_ready,
            "TESTNET_AUTONOMOUS_READY": autonomous_ready,
            "SMALL_LIVE_READY": False,
        }

    def get_preflight(self, execution_mode: str) -> dict:
        mode_upper = str(execution_mode).upper()
        if mode_upper == "LIVE":
            return {
                "executionMode": "LIVE",
                "canArm": False,
                "checks": [
                    {
                        "id": "CHK-LIVE-BLOCKED",
                        "name": "Live Execution Mode",
                        "required": True,
                        "status": "FAIL",
                        "message": "LIVE execution mode is permanently blocked in this sprint."
                    }
                ]
            }

        if mode_upper == "TESTNET":
            testnet_configured = bool(
                os.getenv("BINANCE_TESTNET_API_KEY") and os.getenv("BINANCE_TESTNET_API_SECRET")
            )
            checks = [
                {
                    "id": "CHK-CREDS",
                    "name": "Testnet Credentials",
                    "required": True,
                    "status": "PASS" if testnet_configured else "FAIL",
                    "message": "Configured in environment" if testnet_configured else "BINANCE_TESTNET_API_KEY / SECRET missing"
                },
                {
                    "id": "CHK-AUTH",
                    "name": "Authentication",
                    "required": True,
                    "status": "PASS" if self.authenticated else "FAIL",
                    "message": "Authenticated with Binance Testnet" if self.authenticated else "Not authenticated"
                },
                {
                    "id": "CHK-CONN",
                    "name": "Trading Connection",
                    "required": True,
                    "status": "PASS" if self.connection_state == "READY" else "FAIL",
                    "message": f"Connection state: {self.connection_state}"
                },
                {
                    "id": "CHK-SYNC",
                    "name": "Reconciliation",
                    "required": True,
                    "status": "PASS" if self.reconciliation_status == "IN_SYNC" else "FAIL",
                    "message": f"Reconciliation status: {self.reconciliation_status}"
                },
                {
                    "id": "CHK-STREAM",
                    "name": "Private User Stream",
                    "required": True,
                    "status": "PASS" if self.private_stream_healthy else "FAIL",
                    "message": "Private user stream active" if self.private_stream_healthy else "Stream offline"
                },
                {
                    "id": "CHK-KILL",
                    "name": "Kill Switch",
                    "required": True,
                    "status": "FAIL" if self.kill_switch_active else "PASS",
                    "message": "Kill switch is active" if self.kill_switch_active else "Kill switch inactive"
                }
            ]
            can_arm = all(c["status"] == "PASS" for c in checks if c["required"])
            return {
                "executionMode": "TESTNET",
                "canArm": can_arm,
                "checks": checks
            }

        # Default PAPER mode
        return {
            "executionMode": "PAPER",
            "canArm": not self.kill_switch_active,
            "checks": [
                {
                    "id": "CHK-SIMULATION",
                    "name": "Paper Simulation Engine",
                    "required": True,
                    "status": "PASS",
                    "message": "Local simulation ready"
                },
                {
                    "id": "CHK-KILL",
                    "name": "Kill Switch",
                    "required": True,
                    "status": "FAIL" if self.kill_switch_active else "PASS",
                    "message": "Kill switch is active" if self.kill_switch_active else "Kill switch inactive"
                }
            ]
        }

    async def set_pause_new_risk(self, active: bool):
        self.pause_new_risk = active
        if active:
            self.engine_state = WorkerEngineState.PAUSED_NEW_RISK
        else:
            self.engine_state = WorkerEngineState.ARMED if self.active_configuration else WorkerEngineState.DISARMED

    async def set_recovery_only(self, active: bool):
        self.recovery_only = active
        if active:
            self.engine_state = WorkerEngineState.RECOVERY_ONLY
        else:
            self.engine_state = WorkerEngineState.ARMED if self.active_configuration else WorkerEngineState.DISARMED

    async def set_kill_switch(self, active: bool):
        self.kill_switch_active = active
        if active:
            self.engine_state = WorkerEngineState.EMERGENCY
        else:
            self.engine_state = WorkerEngineState.DISARMED
            
    async def trigger_reconciliation(self) -> str:
        if hasattr(self, "execution_adapter") and self.execution_adapter is not None:
            res = await self.execution_adapter.reconciliation.reconcile()
            self.reconciliation_status = res
            if res == "IN_SYNC" and self.execution_adapter.user_stream.is_connected:
                self.connection_state = "READY"
            else:
                self.connection_state = "DEGRADED"
            return res
            
        self.reconciliation_status = "IN_SYNC"
        self.connection_state = "READY"
        self.private_stream_healthy = True
        self.authenticated = True
        return self.reconciliation_status

    async def arm(self, config: dict):
        if self.kill_switch_active:
            return False, "Cannot arm: Kill switch is active"

        mode = str(config.get("executionMode") or config.get("execution_mode") or "PAPER").upper()
        if mode == "LIVE":
            return False, "LIVE execution mode is permanently blocked in this sprint."

        enforce = config.get("enforcePreflight", config.get("enforce_preflight", False))
        if mode == "TESTNET":
            preflight = self.get_preflight("TESTNET")
            if enforce and not preflight["canArm"]:
                failures = [c["message"] for c in preflight["checks"] if c["status"] == "FAIL"]
                return False, f"TESTNET preflight failed: {'; '.join(failures)}"

            self.engine_state = WorkerEngineState.ARMING
            api_key = os.getenv("BINANCE_TESTNET_API_KEY", "")
            api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "")
            
            # If not yet authenticated and real credentials exist, connect execution adapter
            if not self.authenticated and api_key and api_secret:
                try:
                    if self.execution_adapter is None:
                        self.execution_adapter = BinanceExecutionAdapter(
                            api_key=api_key,
                            api_secret=api_secret,
                            env=BinanceEnvironment.TESTNET
                        )
                        connected = await self.execution_adapter.connect()
                        if not connected or self.execution_adapter.state != ConnectionState.READY:
                            self.engine_state = WorkerEngineState.DISARMED
                            return False, f"Failed to initialize Testnet Execution Adapter. State: {self.execution_adapter.state}"
                    self.authenticated = True
                    self.connection_state = "READY"
                    self.private_stream_healthy = self.execution_adapter.user_stream.is_connected
                    self.reconciliation_status = self.execution_adapter.reconciliation.last_status
                except Exception as e:
                    logger.error("Error connecting Testnet Execution Adapter: %s", e)
                    self.engine_state = WorkerEngineState.DISARMED
                    return False, f"Adapter connection failed: {e}"

            self.execution_mode = WorkerExecutionMode.TESTNET
            self.engine_state = WorkerEngineState.ARMED
            self.active_configuration = config
            logger.info("Worker ARMED in TESTNET mode")
            return True, ""
        else:
            self.execution_mode = WorkerExecutionMode.PAPER
            self.engine_state = WorkerEngineState.ARMED
            self.active_configuration = config
            logger.info("Worker ARMED in PAPER mode")
            return True, ""

    async def disarm(self):
        if self.execution_adapter is not None:
            try:
                await self.execution_adapter.close()
            except Exception as e:
                logger.warning("Error closing execution adapter on disarm: %s", e)
            self.execution_adapter = None
        self.engine_state = WorkerEngineState.DISARMED
        self.connection_state = "DISCONNECTED"
        self.private_stream_healthy = False
        self.authenticated = False
        self.reconciliation_status = "DISCONNECTED"
        logger.info("Worker DISARMED")

    async def handle_market_event(self, event: MarketEvent):
        if not self.market_data_healthy:
            self.market_data_healthy = True
            
        pa_state = self.pa_engine.process_event(event)
        if not pa_state:
            return
            
        market_state = self.market_state_engine.classify(pa_state)
        
        grid_intent = self.grid_engine.evaluate(pa_state, market_state)
        trend_intent = self.trend_engine.evaluate(pa_state, market_state)
        shock_intent = self.shock_engine.evaluate(pa_state, market_state)
        carry_intent = self.carry_engine.evaluate(event, market_state)
        
        intents = [i for i in [grid_intent, trend_intent, shock_intent, carry_intent] if i]
        
        # Real or simulated RiskSnapshot
        if self.execution_mode == WorkerExecutionMode.TESTNET and self.execution_adapter is not None:
            try:
                positions = await self.execution_adapter.ledger.get_positions()
                total_unrealized = sum((p.unrealized_pnl for p in positions), Decimal("0.0"))
                total_notional = sum((abs(p.quantity) * (p.mark_price or p.entry_price or Decimal("0.0")) for p in positions), Decimal("0.0"))
                equity = Decimal("100000.0") + total_unrealized
                margin_util = (total_notional / equity * Decimal("100.0")) if equity > 0 else Decimal("0.0")
                risk_snapshot = RiskSnapshot(
                    portfolio_equity=equity,
                    unrealized_pnl=total_unrealized,
                    realized_pnl_24h=Decimal("0.0"),
                    margin_utilization_pct=margin_util,
                    effective_leverage=total_notional / equity if equity > 0 else Decimal("0.0"),
                    current_drawdown_pct=Decimal("0.0"),
                    liquidation_distance_pct=Decimal("50.0"),
                    risk_state=RiskState.NORMAL
                )
                sym_pos = next((p for p in positions if p.symbol == event.symbol), None)
                current_position_qty = sym_pos.quantity if sym_pos else Decimal("0.0")
            except Exception as e:
                logger.error("Error extracting Testnet risk snapshot: %s", e)
                risk_snapshot = RiskSnapshot(
                    portfolio_equity=Decimal("100000.0"),
                    unrealized_pnl=Decimal("0.0"),
                    realized_pnl_24h=Decimal("0.0"),
                    margin_utilization_pct=Decimal("5.0"),
                    effective_leverage=Decimal("0.5"),
                    current_drawdown_pct=Decimal("1.2"),
                    liquidation_distance_pct=Decimal("45.0"),
                    risk_state=RiskState.NORMAL
                )
                current_position_qty = Decimal("0.0")
        else:
            risk_snapshot = RiskSnapshot(
                portfolio_equity=Decimal("100000.0"),
                unrealized_pnl=Decimal("0.0"),
                realized_pnl_24h=Decimal("0.0"),
                margin_utilization_pct=Decimal("5.0"),
                effective_leverage=Decimal("0.5"),
                current_drawdown_pct=Decimal("1.2"),
                liquidation_distance_pct=Decimal("45.0"),
                risk_state=RiskState.NORMAL
            )
            current_position_qty = Decimal("1.2") # [RESEARCH] Mock
        
        raw_target_exposure = self.meta_allocator.allocate(intents, event.symbol)
        
        target_exposure = self.recovery_engine.process(
            target=raw_target_exposure,
            risk=risk_snapshot,
            current_position_qty=current_position_qty
        )
        
        decision = self.risk_governor.evaluate(target_exposure, risk_snapshot, current_position_qty=current_position_qty)
        
        if decision.action != "NOOP":
            if self.engine_state == WorkerEngineState.ARMED:
                if self.execution_mode == WorkerExecutionMode.TESTNET and self.execution_adapter is not None:
                    autonomous_enabled = os.getenv("AUTONOMOUS_TESTNET_EXECUTION", "false").lower() in ("true", "1", "yes")
                    if autonomous_enabled and not self.pause_new_risk and not self.kill_switch_active:
                        logger.info(f"[TESTNET][AUTONOMOUS_EXEC] Executing decision {decision.decision_id} for {decision.symbol}")
                        await self.execution_adapter.execute_decision(decision)
                    else:
                        logger.info(f"[TESTNET][MONITOR_ONLY] Decision {decision.decision_id} for {decision.symbol} (Autonomous execution disabled)")
                else:
                    logger.info(f"[{self.execution_mode.value}][SIMULATED] EXECUTION DECISION: {decision.symbol} | Action: {decision.action}")

    async def start(self):
        logger.info("Initializing Blessing AI Trading Worker v0.2...")
        self.symbols = await self.scanner.scan_active_symbols()
        
        logger.info("Connecting to Binance WS for: %s", self.symbols)
        self.ws_client = BinancePublicWebSocket(
            symbols=self.symbols,
            event_callback=self.handle_market_event,
        )
        self.start_heartbeat()
        self.scan_task = asyncio.create_task(self._periodic_scanner())
        
        # We don't sleep forever here, we just start tasks.
        
    async def _heartbeat_loop(self):
        """Background task periodically updating heartbeat_at for Control Plane liveness monitoring."""
        while self.is_running:
            self.record_heartbeat()
            try:
                await asyncio.sleep(self.heartbeat_interval_sec)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat loop error: %s", e)

    async def _periodic_scanner(self):
        while self.is_running:
            await asyncio.sleep(self.scanner.refresh_interval_sec)
            try:
                new_symbols = await self.scanner.scan_active_symbols()
                if set(new_symbols) != set(self.symbols):
                    self.symbols = new_symbols
            except Exception as e:
                logger.error("Periodic scanner failed: %s", e)

    def stop(self):
        logger.info("Gracefully stopping Trading Worker...")
        self.is_running = False
        self.stop_heartbeat()
        if self.scan_task and not self.scan_task.done():
            self.scan_task.cancel()

def serve_api(app_instance):
    set_worker_engine(app_instance)
    config = uvicorn.Config(app, host="0.0.0.0", port=8080, log_level="info")
    server = uvicorn.Server(config)
    return server.serve()

async def main():
    app_instance = TradingWorkerApp()
    await app_instance.start()
    
    # Run API server
    await serve_api(app_instance)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
