import asyncio
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, List, Optional, Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    field_validator,
    model_validator,
)

from domain.enums import RiskState
from domain.models import Instrument, MarketEvent, MarketType, RiskSnapshot, utc_now

from apps.trading_worker.engines.exposure_recovery import ExposureRecoveryEngine
from apps.trading_worker.engines.funding_carry import (
    FundingCarryCostInputs,
    FundingCarryEngine,
)
from apps.trading_worker.engines.grid_strategy import GridStrategyEngine
from apps.trading_worker.engines.market_scanner import MarketScannerEngine
from apps.trading_worker.engines.market_state import MarketStateClassifier
from apps.trading_worker.engines.meta_allocator import MetaAllocator
from apps.trading_worker.engines.price_action import PriceActionEngine
from apps.trading_worker.engines.risk_governor import RiskGovernor
from apps.trading_worker.engines.shock_strategy import ShockStrategyEngine
from apps.trading_worker.engines.trend_strategy import TrendStrategyEngine
from apps.trading_worker.evidence import BuildEvidence
from apps.trading_worker.venues.binance.config import (
    BinanceEnvironment,
    environment_label,
    get_ws_url,
)
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.gates import DecisionExecutionGate
from apps.trading_worker.venues.binance.models import (
    BinanceAuthenticationError,
    ConnectionState,
    TestnetSafetyLimits,
)
from apps.trading_worker.persistence.manager import PersistenceManager
from venues.binance.public_ws import BinancePublicWebSocket

class StrategyEnablement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grid: bool = False
    trend: bool = False
    shock: bool = False
    carry: bool = False

class ArmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    executionMode: Literal["PAPER", "TESTNET", "LIVE"] = "PAPER"
    instruments: List[str] = Field(default_factory=list)
    strategies: StrategyEnablement = Field(default_factory=StrategyEnablement)
    riskProfile: Literal["CONSERVATIVE", "BALANCED", "AGGRESSIVE"] = "CONSERVATIVE"
    enforcePreflight: bool = False
    # LIVE ARM is accepted only with a release-controller approval that has
    # already been consumed. This is an identifier, never a token or secret.
    releaseApprovalId: Optional[str] = None
    launchPolicy: Literal["STAGED_FIRST_ORDER"] = "STAGED_FIRST_ORDER"

    @field_validator("instruments")
    @classmethod
    def normalize_instruments(cls, instruments: List[str]) -> List[str]:
        return [str(symbol).strip().upper() for symbol in instruments if str(symbol).strip()]


class ContinuationRequest(BaseModel):
    """One-time, server-authorized transition from staged to autonomous LIVE."""

    model_config = ConfigDict(extra="forbid")
    executionMode: Literal["LIVE"] = "LIVE"
    instruments: List[str] = Field(default_factory=lambda: ["ETHUSDC"])
    strategies: StrategyEnablement = Field(default_factory=StrategyEnablement)
    riskProfile: Literal["CONSERVATIVE", "BALANCED", "AGGRESSIVE"] = "CONSERVATIVE"
    enforcePreflight: bool = True
    continuationApprovalId: str
    launchId: str
    # The Control Plane normally resolves this from the durable staged row.
    # Supplying it lets the Worker bind the continuation to the original
    # release approval without accepting any token or secret.
    initialApprovalId: Optional[str] = None

    @field_validator("instruments")
    @classmethod
    def normalize_instruments(cls, instruments: List[str]) -> List[str]:
        return [str(symbol).strip().upper() for symbol in instruments if str(symbol).strip()]

    @field_validator("continuationApprovalId", "launchId", "initialApprovalId")
    @classmethod
    def validate_identifiers(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        normalized = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}", normalized):
            raise ValueError("launch identifiers must be opaque bounded identifiers")
        return normalized


class ToggleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool = True


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


MAINNET_LAUNCH_STAGED = "STAGED_FIRST_ORDER"
MAINNET_LAUNCH_AUTONOMOUS = "AUTONOMOUS_AFTER_REVIEW"


# These states may process an already-approved decision.  The decision gate
# still rejects NEW_RISK/INCREASE_RISK while paused or recovery-only, while
# allowing reductions, recovery, close, and emergency actions through.
EXECUTABLE_ENGINE_STATES = {
    WorkerEngineState.ARMED,
    WorkerEngineState.PAUSED_NEW_RISK,
    WorkerEngineState.RECOVERY_ONLY,
}

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

class LaunchReadiness(BaseModel):
    paper_ready: bool
    local_non_secret_tests_verified: bool
    ci_verified: bool
    testnet_credentials_verified: bool
    testnet_trade_authorized: bool = False
    testnet_readonly_contract_verified: bool
    testnet_manual_trial_verified: bool
    testnet_soak_verified: bool
    adapter_ready: bool
    private_stream_healthy: bool
    reconciliation_in_sync: bool
    account_snapshot_ready: bool
    symbol_rules_ready: bool
    market_data_fresh: bool
    autonomous_flag_enabled: bool
    autonomous_soak_flag_enabled: bool = False
    launch_approved: bool
    testnet_autonomous_soak_ready: bool = False
    testnet_autonomous_ready: bool
    small_live_ready: bool = False
    mainnet_credentials_verified: bool = False
    mainnet_live_approved: bool = False
    mainnet_account_risk_ready: bool = False
    mainnet_preflight_ready: bool = False
    mainnet_autonomous_ready: bool = False
    mainnet_launch_policy: Optional[str] = None
    mainnet_launch_id: Optional[str] = None
    mainnet_launch_state: Optional[str] = None
    mainnet_continuation_approval_id: Optional[str] = None
    persistence: Dict[str, Any] = Field(default_factory=dict)

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
    data_source: str = "SIMULATED"
    exchange_environment: str = "NONE"
    # Non-secret release bindings. Cloud Run's K_REVISION is used until a
    # release promotion pins WORKER_REVISION to the preflighted source
    # revision; neither value contains credentials.
    worker_image_digest: str = ""
    worker_revision: str = ""
    # Numeric Secret Manager version metadata only.  Values are never exposed
    # and the secret payloads remain injected directly by Cloud Run.
    secret_versions: Dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(extra="ignore")

    # Engine Operational State. These are the only stored runtime values;
    # compatibility names below are computed at the serialization boundary.
    engine_state: WorkerEngineState = WorkerEngineState.DISARMED
    connection_state: str = "DISCONNECTED"

    # Stream Health & Connectivity Checks
    market_data_healthy: bool = False
    private_stream_healthy: bool = False
    trading_connection_healthy: bool = False
    authenticated: bool = False
    account_synchronized: bool = False
    reconciliation_status: str = "UNKNOWN"

    # Safety Controls & Risk Governor Invariants
    kill_switch_active: bool = False
    pause_new_risk: bool = False
    recovery_only: bool = False
    # Observable counter used by release approval and staged-launch evidence.
    # It is incremented only at the Binance REST order boundary.
    order_submission_attempts: int = 0

    # Readiness
    launch_readiness: Optional[LaunchReadiness] = None
    # Explicit release evidence for the control plane. Presence of credentials
    # alone is not enough to claim that Mainnet is ready.
    mainnet_credentials_verified: bool = False
    mainnet_live_approved: bool = False
    mainnet_preflight_ready: bool = False
    mainnet_launch_policy: Optional[str] = None
    mainnet_launch_id: Optional[str] = None
    mainnet_launch_state: Optional[str] = None
    mainnet_continuation_approval_id: Optional[str] = None

    # Configuration Details & Versioning
    config_version: str = "v0.2.0-beta"
    active_configuration: Optional[dict] = None

    # Telemetry, Heartbeat & Timestamps
    heartbeat_at: datetime = Field(default_factory=utc_now)
    health_indicators: Optional[HealthIndicators] = None
    updated_at: datetime = Field(default_factory=utc_now)

    @computed_field
    @property
    def engine_status(self) -> WorkerEngineState:
        """Legacy serialization alias for ``engine_state``."""
        return self.engine_state

    @computed_field
    @property
    def connection_status(self) -> str:
        """Legacy serialization alias for ``connection_state``."""
        return self.connection_state

    @computed_field
    @property
    def kill_switch_status(self) -> bool:
        """Legacy serialization alias for ``kill_switch_active``."""
        return self.kill_switch_active

    @computed_field
    @property
    def last_heartbeat(self) -> datetime:
        """Legacy serialization alias for ``heartbeat_at``."""
        return self.heartbeat_at

    @computed_field
    @property
    def configuration_details(self) -> Optional[dict]:
        """Legacy serialization alias for ``active_configuration``."""
        return self.active_configuration

    @model_validator(mode="before")
    @classmethod
    def normalize_compatibility_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            normalized = dict(data)
            # Accept old request/fixture names, but discard them after mapping
            # so the model never stores two independently mutable values. When
            # both names are present, the canonical field wins.
            compatibility_pairs = (
                ("heartbeat_at", "last_heartbeat"),
                ("engine_state", "engine_status"),
                ("connection_state", "connection_status"),
                ("kill_switch_active", "kill_switch_status"),
                ("active_configuration", "configuration_details"),
            )
            for canonical, compatibility in compatibility_pairs:
                if canonical not in normalized and compatibility in normalized:
                    normalized[canonical] = normalized[compatibility]
                normalized.pop(compatibility, None)

            # Derive provenance and exchange environment if not set
            if "execution_mode" in normalized:
                mode = normalized["execution_mode"]
                if mode in (WorkerExecutionMode.TESTNET, "TESTNET"):
                    normalized.setdefault("provenance", "BINANCE_TESTNET")
                    normalized.setdefault("data_source", "BINANCE")
                    normalized.setdefault("exchange_environment", "BINANCE_TESTNET")
                elif mode in (WorkerExecutionMode.LIVE, "LIVE"):
                    normalized.setdefault("provenance", "BINANCE_MAINNET")
                    normalized.setdefault("data_source", "BINANCE")
                    normalized.setdefault("exchange_environment", "BINANCE_MAINNET")
                else:
                    normalized.setdefault("provenance", "SIMULATED")
                    normalized.setdefault("data_source", "SIMULATED")
                    normalized.setdefault("exchange_environment", "NONE")
            return normalized
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


def configured_worker_revision() -> str:
    """Return the release-bound revision without trusting browser input."""

    return (
        os.getenv("WORKER_REVISION", "").strip()
        or os.getenv("K_REVISION", "").strip()
    )


def configured_secret_versions() -> Dict[str, str]:
    """Return only numeric Secret Manager version metadata, never values."""

    values = {
        "sql": os.getenv("CLOUD_SQL_PASSWORD_VERSION", "").strip(),
        "apiKey": os.getenv("BINANCE_MAINNET_API_KEY_VERSION", "").strip(),
        "apiSecret": os.getenv("BINANCE_MAINNET_API_SECRET_VERSION", "").strip(),
    }
    return {
        key: value if re.fullmatch(r"[1-9][0-9]*", value) else ""
        for key, value in values.items()
    }

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
        data_source="SIMULATED",
        exchange_environment="NONE",
        worker_image_digest=os.getenv("WORKER_IMAGE_DIGEST", "").strip(),
        worker_revision=configured_worker_revision(),
        secret_versions=configured_secret_versions(),
        engine_state=WorkerEngineState.DISARMED,
        connection_state="DISCONNECTED",
        market_data_healthy=False,
        private_stream_healthy=False,
        trading_connection_healthy=False,
        authenticated=False,
        account_synchronized=False,
        reconciliation_status="DISCONNECTED",
        kill_switch_active=False,
        pause_new_risk=False,
        recovery_only=False,
        mainnet_launch_policy=None,
        mainnet_launch_id=None,
        mainnet_launch_state=None,
        mainnet_continuation_approval_id=None,
        heartbeat_at=now,
        health_indicators=health,
        config_version="v0.2.0-beta",
        active_configuration=None,
        updated_at=utc_now()
    )

# FastAPI Control Plane API
app = FastAPI(title="Blessing AI Worker Control API", lifespan=lifespan)

@app.get("/health")
def health_check():
    return {"status": "ok", "timestamp": utc_now()}

@app.get("/ready")
def readiness_probe():
    """Cloud Run readiness: expose durable persistence truth, never a fixture."""
    if WORKER_ENGINE is None:
        raise HTTPException(status_code=503, detail="Worker is not initialized")
    readiness = WORKER_ENGINE.get_launch_readiness()
    persistence = readiness.get("persistence", {})
    if not persistence.get("ready", False):
        logger.error(
            "monitor_event=readiness_degraded persistence_mode=%s state=%s",
            persistence.get("mode", "UNKNOWN"),
            persistence.get("state", "UNKNOWN"),
        )
    persistence_ready = (
        persistence.get("mode") != "REQUIRED" or persistence.get("durable") is True
    )
    if not persistence_ready:
        raise HTTPException(
            status_code=503,
            detail={"status": "DEGRADED", "reason": "Required persistence is unavailable", "readiness": readiness},
        )
    return {"status": "ready", "readiness": readiness}

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
async def arm(config: ArmRequest):
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

@app.post("/continuation/readiness")
async def continuation_readiness_endpoint(launch_id: Optional[str] = None):
    """Verify first-order evidence without activating autonomous execution."""

    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    runner = getattr(WORKER_ENGINE, "run_mainnet_continuation_readiness", None)
    if not callable(runner):
        raise HTTPException(status_code=503, detail="Autonomous continuation is unavailable")
    return await runner(launch_id=launch_id)

@app.post("/continue")
async def continue_endpoint(config: ContinuationRequest):
    """Activate autonomous continuation only after a verified approval."""

    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    runner = getattr(WORKER_ENGINE, "continue_autonomous", None)
    if not callable(runner):
        raise HTTPException(status_code=503, detail="Autonomous continuation is unavailable")
    success, message = await runner(config)
    if not success:
        raise HTTPException(status_code=409, detail=message)
    state = WORKER_ENGINE.get_state().model_dump()
    state["status"] = "AUTONOMOUS_ACTIVE"
    return state

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

@app.post("/preflight/read-only")
async def read_only_preflight_endpoint():
    """Run a signed Mainnet observation without arming or submitting orders."""
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    runner = getattr(WORKER_ENGINE, "run_mainnet_read_only_preflight", None)
    if not callable(runner):
        raise HTTPException(status_code=503, detail="Read-only preflight is unavailable")
    return await runner()

@app.post("/pause-new-risk")
async def pause_new_risk_endpoint(req: ToggleRequest):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    accepted = await WORKER_ENGINE.set_pause_new_risk(req.active)
    if accepted is False:
        raise HTTPException(
            status_code=409,
            detail="LIVE new-risk pause can only be cleared by an approved autonomous continuation",
        )
    return {
        "status": "ok",
        "active": req.active,
        "engine_state": WORKER_ENGINE.get_state().engine_state.value,
    }

@app.post("/recovery-only")
async def recovery_only_endpoint(req: ToggleRequest):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    await WORKER_ENGINE.set_recovery_only(req.active)
    return {
        "status": "ok",
        "active": req.active,
        "engine_state": WORKER_ENGINE.get_state().engine_state.value,
    }

@app.post("/kill-switch")
async def kill_switch_endpoint(req: ToggleRequest):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    result = await WORKER_ENGINE.set_kill_switch(req.active)
    return {
        **result,
        "kill_switch_active": WORKER_ENGINE.kill_switch_active,
        "engine_state": WORKER_ENGINE.get_state().engine_state.value,
    }

@app.post("/reconcile")
async def reconcile_endpoint():
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    res = await WORKER_ENGINE.trigger_reconciliation()
    return {"status": res}

class TradingWorkerApp:
    def __init__(self, symbols: Optional[List[str]] = None):
        configured_mode = str(os.getenv("EXECUTION_MODE", "PAPER")).strip().upper()
        default_symbols = ["ETHUSDC"] if configured_mode == "LIVE" else ["BTCUSDT"]
        self.symbols = [str(symbol).upper() for symbol in (symbols or default_symbols)]
        self.is_running = True
        
        self.session_start_equity: Optional[Decimal] = None
        self.session_peak_equity: Optional[Decimal] = None
        
        # Engines
        self.pa_engine = PriceActionEngine()
        self.market_state_engine = MarketStateClassifier()
        self.grid_engine = GridStrategyEngine()
        self.trend_engine = TrendStrategyEngine()
        self.shock_engine = ShockStrategyEngine()
        self.carry_engine = FundingCarryEngine(
            cost_inputs=FundingCarryCostInputs.from_environment()
        )
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
        self.last_market_event_at: Dict[str, datetime] = {}
        self.decision_execution_gate = DecisionExecutionGate(self)
        self.persistence = PersistenceManager(
            instrument_rules_provider=self._persistence_instrument_rules
        )
        self._mainnet_preflight_lock = asyncio.Lock()
        self._execution_lease_owner_id = uuid.uuid4().hex
        self._execution_lease_ttl_seconds = 10.0
        self._execution_lease_last_renewed_at = 0.0
        self._mainnet_launch_id: Optional[str] = None
        self._mainnet_launch_session: Optional[dict[str, Any]] = None

    def _set_mainnet_launch_session(self, session: Optional[dict[str, Any]]) -> None:
        """Project durable launch identity into the process-local API state."""

        self._mainnet_launch_session = dict(session) if session else None
        if session:
            launch_id = session.get("launch_id")
            self._mainnet_launch_id = str(launch_id) if launch_id else None
        else:
            self._mainnet_launch_id = None

    def _launch_session_value(self, key: str, default: Any = None) -> Any:
        if self._mainnet_launch_session is None:
            return default
        return self._mainnet_launch_session.get(key, default)

    async def _fence_autonomous_launch(self, reason: str) -> bool:
        """Move an active autonomous launch behind fresh authorization.

        This is used by restart, DISARM, kill-switch, and failed continuation
        paths. It never resumes trading; failure to persist the fence clears
        the process-local identity and leaves the Worker blocked.
        """

        if not (
            self.execution_mode == WorkerExecutionMode.LIVE
            and self._launch_session_value("policy") == MAINNET_LAUNCH_AUTONOMOUS
            and self._launch_session_value("state") == "AUTONOMOUS_ACTIVE"
        ):
            return True

        launch_id = self._mainnet_launch_id
        try:
            fenced = await self.persistence.mark_mainnet_launches_reauth_required()
            session = await self.persistence.get_mainnet_launch_session(launch_id)
            if not session or session.get("state") != "REAUTH_REQUIRED":
                raise RuntimeError("durable launch fence was not read back as REAUTH_REQUIRED")
            self._set_mainnet_launch_session(session)
            logger.warning(
                "monitor_event=autonomous_launch_fenced reason=%s launch_id=%s changed=%s",
                reason,
                launch_id or "unknown",
                fenced,
            )
            return True
        except Exception as exc:
            logger.error(
                "Unable to durably fence autonomous launch reason=%s: %s",
                reason,
                type(exc).__name__,
            )
            self._set_mainnet_launch_session(None)
            return False

    def _persistence_instrument_rules(self, symbol: str) -> Optional[Instrument]:
        """Expose only exchange-discovered Binance rules to persistence."""

        adapter = self.execution_adapter
        if adapter is None:
            return None
        rules = getattr(adapter, "symbol_rules", {}).get(str(symbol).upper())
        if rules is None:
            return None
        try:
            return rules.to_instrument(
                venue=environment_label(self._current_exchange_environment()),
                market_type=MarketType.USDM_FUTURES,
            )
        except (TypeError, ValueError, AttributeError) as exc:
            logger.error(
                "Exchange-derived persistence rules unavailable for %s: %s",
                symbol,
                type(exc).__name__,
            )
            return None

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

    @staticmethod
    def _env_flag(name: str, default: bool = False) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def _testnet_configured(cls) -> bool:
        return bool(
            os.getenv("BINANCE_TESTNET_API_KEY", "").strip()
            and os.getenv("BINANCE_TESTNET_API_SECRET", "").strip()
            and cls._env_flag("BINANCE_TESTNET", False)
        )

    @classmethod
    def _mainnet_configured(cls) -> bool:
        """Return true only when the explicit Secret Manager env injection exists."""

        return bool(
            os.getenv("BINANCE_MAINNET_API_KEY", "").strip()
            and os.getenv("BINANCE_MAINNET_API_SECRET", "").strip()
        )

    async def _before_order_submission(self, order: Any) -> bool:
        """Apply the durable outbox barrier and launch-session reservation."""

        risk_class = getattr(order, "risk_class", None)
        risk_value = getattr(risk_class, "value", risk_class)
        is_risk_increasing = str(risk_value).upper() in {"NEW_RISK", "INCREASE_RISK"}
        durable = await self.persistence.ensure_order_durable(order)
        if not is_risk_increasing or self.execution_mode != WorkerExecutionMode.LIVE:
            return durable or not is_risk_increasing
        if not durable:
            self.pause_new_risk = True
            self._refresh_engine_state()
            return False
        if not self._mainnet_launch_id:
            self.pause_new_risk = True
            self._refresh_engine_state()
            logger.error(
                "monitor_event=launch_session_violation reason=launch_session_missing"
            )
            logger.error("LIVE risk-increasing order blocked: durable launch session is missing")
            return False
        launch_policy = str(
            self._launch_session_value("policy", MAINNET_LAUNCH_STAGED)
        )
        launch_state = str(self._launch_session_value("state", ""))
        if launch_policy == MAINNET_LAUNCH_AUTONOMOUS and launch_state != "AUTONOMOUS_ACTIVE":
            self.pause_new_risk = True
            self._refresh_engine_state()
            logger.error(
                "monitor_event=autonomous_resume_denied reason=launch_state_%s",
                launch_state or "unknown",
            )
            return False
        if launch_policy not in {MAINNET_LAUNCH_STAGED, MAINNET_LAUNCH_AUTONOMOUS}:
            self.pause_new_risk = True
            self._refresh_engine_state()
            logger.error(
                "monitor_event=launch_session_violation reason=unknown_launch_policy"
            )
            return False
        try:
            reserved = await self.persistence.reserve_mainnet_risk_order(self._mainnet_launch_id)
        except Exception as exc:
            self.pause_new_risk = True
            self._refresh_engine_state()
            logger.error(
                "monitor_event=launch_session_violation reason=reservation_failed error_class=%s",
                type(exc).__name__,
            )
            logger.error("LIVE order reservation failed: %s", type(exc).__name__)
            return False
        if not reserved:
            self.pause_new_risk = True
            self._refresh_engine_state()
            logger.warning(
                "monitor_event=launch_session_violation reason=order_slot_unavailable"
            )
            logger.warning("LIVE launch has no available risk-increasing order slot")
            return False
        return True

    async def _on_order_submission_result(self, order: Any, outcome: str) -> None:
        """Persist order outcome and preserve the autonomous lifecycle."""

        if self.execution_mode != WorkerExecutionMode.LIVE or not self._mainnet_launch_id:
            return
        risk_class = getattr(order, "risk_class", None)
        risk_value = getattr(risk_class, "value", risk_class)
        if str(risk_value).upper() not in {"NEW_RISK", "INCREASE_RISK"}:
            return
        normalized = str(outcome).upper()
        if normalized == "REJECTED":
            await self.persistence.release_mainnet_risk_order_reservation(self._mainnet_launch_id)
            return
        if normalized == "CONFIRMED":
            marked = await self.persistence.mark_mainnet_risk_order_submitted(self._mainnet_launch_id)
            if not marked:
                self.kill_switch_active = True
                self.connection_state = ConnectionState.DEGRADED.value
                self.reconciliation_status = "UNKNOWN"
                logger.error(
                    "monitor_event=launch_session_violation reason=submission_mark_failed"
                )
                logger.error("LIVE order was not durably marked; local kill switch is active")
                return
            if self._launch_session_value("policy", MAINNET_LAUNCH_STAGED) == MAINNET_LAUNCH_STAGED:
                self.pause_new_risk = True
                self._refresh_engine_state()
                logger.warning(
                    "monitor_event=staged_first_order_confirmed pause_new_risk=true"
                )
                logger.warning("LIVE staged first risk-increasing order confirmed; new risk is paused")
            else:
                self.pause_new_risk = False
                self._refresh_engine_state()
                logger.info(
                    "monitor_event=autonomous_order_confirmed launch_state=AUTONOMOUS_ACTIVE"
                )
            return
        await self.persistence.mark_mainnet_launch_reconciliation_required(self._mainnet_launch_id)
        self.pause_new_risk = True
        self.connection_state = ConnectionState.DEGRADED.value
        self.reconciliation_status = "UNKNOWN"
        logger.error(
            "monitor_event=launch_session_violation reason=ambiguous_outcome"
        )
        logger.error("LIVE order outcome is ambiguous; reconciliation is required")

    def _current_exchange_environment(self) -> BinanceEnvironment:
        return (
            BinanceEnvironment.MAINNET
            if self.execution_mode == WorkerExecutionMode.LIVE
            else BinanceEnvironment.TESTNET
        )

    def _current_exchange_label(self) -> str:
        return environment_label(self._current_exchange_environment())

    async def _restart_public_market_stream(self) -> bool:
        """Rebind the public stream to the worker's current mode and symbols.

        Binance's combined stream URL is fixed when the client is created.  A
        mode switch therefore must stop the old stream before starting a new
        one; leaving the previous client alive could feed Mainnet decisions
        with Testnet symbols (or vice versa).
        """

        await self._stop_public_market_stream()

        exchange_environment = self._current_exchange_environment()
        symbols = [str(symbol).upper() for symbol in self.symbols if str(symbol).strip()]
        self.symbols = symbols
        self.ws_client = BinancePublicWebSocket(
            symbols=symbols,
            base_ws_url=get_ws_url(exchange_environment),
            event_callback=self.handle_market_event,
            venue=environment_label(exchange_environment),
        )
        started = await self.ws_client.start()
        if not started:
            self.market_data_healthy = False
            logger.error(
                "Binance %s public market stream could not be started for %s",
                exchange_environment.value,
                symbols,
            )
        return started

    async def _stop_public_market_stream(self) -> None:
        """Stop and detach any exchange stream before entering a non-exchange mode."""

        stream = self.ws_client
        self.ws_client = None
        if stream is None:
            return
        try:
            await stream.stop()
        except Exception as exc:
            logger.warning("Error stopping public market stream: %s", type(exc).__name__)

    async def _resolve_startup_symbols(self, configured_mode: str) -> List[str]:
        """Resolve startup symbols without letting research scanning rewrite them."""

        configured = [str(symbol).upper() for symbol in self.symbols if str(symbol).strip()]
        if configured_mode == "LIVE":
            if set(configured) != {"ETHUSDC"}:
                logger.error(
                    "LIVE startup requires the explicitly bounded ETHUSDC instrument; refusing configured symbols %s",
                    configured,
                )
                return []
            return ["ETHUSDC"]
        if configured_mode == "TESTNET":
            return configured or ["BTCUSDT"]
        return [str(symbol).upper() for symbol in await self.scanner.scan_active_symbols()]

    def _active_instruments(self) -> List[str]:
        if isinstance(self.active_configuration, dict):
            configured = self.active_configuration.get("instruments")
            if configured:
                return [str(symbol).upper() for symbol in configured]
        return [str(symbol).upper() for symbol in self.symbols]

    def _enabled_strategies(self) -> set[str]:
        """Return only strategies explicitly enabled by the active ARM config.

        Strategy engines are stateful and may emit executable intents.  A
        disabled strategy must therefore not even be evaluated; filtering
        after evaluation would still let it influence allocation or audit
        output.  No active configuration means no strategy authority.
        """

        if not isinstance(self.active_configuration, dict):
            return set()
        configured = self.active_configuration.get("strategies")
        if not isinstance(configured, dict):
            return set()
        return {
            str(strategy).strip().lower()
            for strategy, enabled in configured.items()
            if enabled is True
            and str(strategy).strip().lower() in {"grid", "trend", "shock", "carry"}
        }

    @staticmethod
    def _has_grid_lineage(value: object) -> bool:
        return any(
            str(intent_id).upper().startswith("GRID-")
            for intent_id in (value or [])
        )

    async def _observed_grid_depth(self, symbol: str) -> int:
        """Read grid depth from Worker-owned ledger lineage before expansion."""

        if self.execution_mode not in {
            WorkerExecutionMode.TESTNET,
            WorkerExecutionMode.LIVE,
        }:
            # Paper mode has no exchange fills and must remain explicitly
            # simulated; it cannot claim observed grid inventory.
            return 0
        adapter = self.execution_adapter
        if adapter is None:
            # No authoritative ledger means no safe assumption about depth.
            return self.grid_engine.max_grid_levels
        normalized_symbol = str(symbol).upper()
        try:
            positions = await adapter.ledger.get_positions()
            position_qty = sum(
                (
                    abs(position.quantity)
                    for position in positions
                    if str(position.symbol).upper() == normalized_symbol
                    and position.quantity != 0
                ),
                Decimal("0"),
            )
            all_orders = await adapter.ledger.get_all_orders()
            grid_orders = [
                order
                for order in all_orders
                if str(order.symbol).upper() == normalized_symbol
                and self._has_grid_lineage(order.source_intent_ids)
            ]
            open_grid_orders = sum(
                1
                for order in grid_orders
                if str(order.status).upper() in {"NEW", "PARTIALLY_FILLED"}
            )
            grid_order_ids = {order.client_order_id for order in grid_orders}
            fills = await adapter.ledger.get_fills()
            filled_grid_order_ids = {
                str(fill.client_order_id)
                for fill in fills
                if str(fill.symbol).upper() == normalized_symbol
                and (
                    str(fill.client_order_id) in grid_order_ids
                    or self._has_grid_lineage(fill.source_intent_ids)
                )
            }
            return self.grid_engine.observed_depth(
                position_qty=position_qty,
                open_grid_orders=open_grid_orders,
                filled_grid_orders=len(filled_grid_order_ids),
            )
        except Exception as exc:
            logger.error(
                "Unable to prove observed grid depth for %s; blocking grid expansion: %s",
                normalized_symbol,
                exc,
            )
            return self.grid_engine.max_grid_levels

    def _symbol_rules_ready(self) -> bool:
        """Require complete, selected-environment exchange rules for all symbols."""
        active_symbols = self._active_instruments()
        adapter = self.execution_adapter
        return bool(
            adapter
            and active_symbols
            and all(
                adapter.is_symbol_ready_for_execution(symbol)
                if hasattr(adapter, "is_symbol_ready_for_execution")
                else (
                    symbol in adapter.symbol_rules
                    and adapter.symbol_rules[symbol].is_ready_for("LIMIT")
                    and adapter.symbol_rules[symbol].is_ready_for("MARKET")
                )
                for symbol in active_symbols
            )
        )

    @staticmethod
    def _positive_env_float(name: str, fallback: float) -> float:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            return fallback
        try:
            value = float(raw)
        except ValueError:
            return fallback
        return value if math.isfinite(value) and value > 0 else fallback

    def is_account_snapshot_ready(self) -> bool:
        adapter = self.execution_adapter
        if adapter is None:
            return False
        snapshot = getattr(adapter, "account_snapshot", None)
        if snapshot is None:
            snapshot = getattr(getattr(adapter, "ledger", None), "account_snapshot", None)
        if snapshot is None or not getattr(snapshot, "valid", False):
            return False
        if getattr(snapshot, "exchange_environment", None) != self._current_exchange_label():
            return False
        timestamp = getattr(snapshot, "timestamp", None)
        if not isinstance(timestamp, datetime):
            return False
        if timestamp.tzinfo is None:
            return False
        age = (utc_now() - timestamp).total_seconds()
        if age < 0 or age > self._positive_env_float("ACCOUNT_SNAPSHOT_MAX_AGE_SEC", 30.0):
            return False
        required = (
            "wallet_balance",
            "margin_balance",
            "available_balance",
            "unrealized_pnl",
            "total_initial_margin",
            "total_maint_margin",
            "position_initial_margin",
            "total_position_notional",
            "effective_leverage",
            "margin_utilization_pct",
        )
        try:
            finite = all(
                getattr(snapshot, field, None) is not None
                and Decimal(str(getattr(snapshot, field))).is_finite()
                for field in required
            )
            nonnegative = all(
                Decimal(str(getattr(snapshot, field))) >= 0
                for field in (
                    "wallet_balance",
                    "margin_balance",
                    "available_balance",
                    "total_initial_margin",
                    "total_maint_margin",
                    "position_initial_margin",
                    "total_position_notional",
                    "effective_leverage",
                    "margin_utilization_pct",
                )
            )
            return finite and nonnegative
        except (InvalidOperation, TypeError, ValueError):
            return False

    def _is_mainnet_snapshot_risk_ready(
        self,
        snapshot: Any,
        adapter: Any,
        *,
        freshness_verified: Optional[bool] = None,
        require_execution_lease: bool = True,
    ) -> bool:
        """Apply the immutable Mainnet account/risk limits to one snapshot.

        ``run_mainnet_read_only_preflight`` uses this same validator with the
        temporary observation adapter and without an execution lease.  That
        keeps preflight useful while ensuring it can never satisfy the
        autonomous execution lease gate.
        """

        if snapshot is None or not getattr(snapshot, "valid", False):
            return False
        if getattr(adapter, "env", None) != BinanceEnvironment.MAINNET:
            return False
        if freshness_verified is None:
            freshness_check = getattr(adapter, "is_account_snapshot_fresh", None)
            freshness_verified = bool(callable(freshness_check) and freshness_check())
        if not freshness_verified:
            return False

        lease = getattr(adapter, "execution_lease", None)
        if require_execution_lease and bool(getattr(adapter, "execution_lease_required", True)) and (
            lease is None or getattr(lease, "fencing_token", None) is None
        ):
            return False
        if str(getattr(snapshot, "collateral_asset", "")).upper() != "USDC":
            return False
        if str(getattr(snapshot, "risk_currency", "")).upper() != "USDC":
            return False
        if str(getattr(snapshot, "daily_loss_asset", "")).upper() != "USDC":
            return False
        if not bool(getattr(snapshot, "daily_loss_known", False)):
            return False
        if not bool(getattr(snapshot, "daily_pnl_includes_fees", False)):
            return False
        if not bool(getattr(snapshot, "daily_pnl_includes_funding", False)):
            return False
        if not bool(getattr(snapshot, "configured_leverage_known", False)):
            return False
        if not bool(getattr(snapshot, "margin_mode_known", False)):
            return False
        if str(getattr(snapshot, "margin_mode", "")).upper() not in {
            "CROSS",
            "ISOLATED",
            "SINGLE_ASSET_CROSS",
        }:
            return False
        if str(getattr(snapshot, "liquidation_safety", "")).upper() != "KNOWN":
            return False

        try:
            limits = TestnetSafetyLimits.from_environment(BinanceEnvironment.MAINNET)
            collateral = Decimal(str(getattr(snapshot, "margin_balance", None)))
            wallet_balance = Decimal(str(getattr(snapshot, "wallet_balance", None)))
            available_balance = Decimal(str(getattr(snapshot, "available_balance", None)))
            effective_leverage = Decimal(str(getattr(snapshot, "effective_leverage", None)))
            configured_leverage = Decimal(str(getattr(snapshot, "configured_leverage", None)))
            daily_pnl = Decimal(str(getattr(snapshot, "daily_realized_pnl", None)))
            unrealized_pnl = Decimal(str(getattr(snapshot, "unrealized_pnl", None)))
            total_position_notional = Decimal(
                str(getattr(snapshot, "total_position_notional", None))
            )
        except (InvalidOperation, TypeError, ValueError):
            return False

        if (
            not collateral.is_finite()
            or collateral <= 0
            or collateral > limits.max_collateral
            or not wallet_balance.is_finite()
            or wallet_balance <= 0
            or wallet_balance > limits.max_collateral
            or not available_balance.is_finite()
            or available_balance <= 0
            or not effective_leverage.is_finite()
            or effective_leverage < 0
            or effective_leverage > limits.max_leverage
            or not total_position_notional.is_finite()
            or total_position_notional < 0
            or total_position_notional > limits.max_total_open_notional
        ):
            return False
        if (
            not configured_leverage.is_finite()
            or configured_leverage <= 0
            or configured_leverage > limits.max_leverage
        ):
            return False
        if not daily_pnl.is_finite() or not unrealized_pnl.is_finite():
            return False
        daily_loss = max(Decimal("0"), -(daily_pnl + unrealized_pnl))
        if not daily_loss.is_finite() or daily_loss >= limits.max_daily_loss:
            return False

        liquidation_distance = getattr(snapshot, "min_liquidation_distance_pct", None)
        if total_position_notional != 0:
            try:
                if liquidation_distance is None or not Decimal(str(liquidation_distance)).is_finite() or Decimal(str(liquidation_distance)) <= 0:
                    return False
            except (InvalidOperation, TypeError, ValueError):
                return False

        window_start = getattr(snapshot, "daily_loss_window_start", None)
        window_end = getattr(snapshot, "daily_loss_window_end", None)
        if not isinstance(window_start, datetime) or not isinstance(window_end, datetime):
            return False
        if window_start.tzinfo is None or window_end.tzinfo is None:
            return False
        start_utc = window_start.astimezone(timezone.utc)
        end_utc = window_end.astimezone(timezone.utc)
        now_utc = utc_now()
        return (
            start_utc.hour == 0
            and start_utc.minute == 0
            and start_utc.second == 0
            and start_utc.microsecond == 0
            and end_utc >= start_utc
            and end_utc <= start_utc + timedelta(days=1)
            and start_utc <= now_utc < end_utc
        )

    def is_mainnet_account_risk_ready(self) -> bool:
        """Require independently observed Mainnet collateral, mode, leverage, and PnL."""

        if self.execution_mode != WorkerExecutionMode.LIVE:
            return False
        adapter = self.execution_adapter
        snapshot = getattr(adapter, "account_snapshot", None) if adapter else None
        if snapshot is None:
            snapshot = getattr(getattr(adapter, "ledger", None), "account_snapshot", None) if adapter else None
        if snapshot is None or not self.is_account_snapshot_ready():
            return False
        return self._is_mainnet_snapshot_risk_ready(
            snapshot,
            adapter,
            freshness_verified=True,
            require_execution_lease=True,
        )

    def _derive_testnet_risk_state(
        self,
        snapshot: Any,
        drawdown_pct: Decimal,
    ) -> RiskState:
        """Derive the Testnet risk state from authoritative account metrics.

        A valid snapshot is not automatically safe for more exposure.  This
        state is consumed by ``RiskGovernor`` so an unsafe account blocks
        ``NEW_RISK``/``INCREASE_RISK`` while explicit reductions remain
        available.  Unknown or malformed risk data fails closed.
        """

        try:
            drawdown = Decimal(str(drawdown_pct))
            available_balance = Decimal(str(snapshot.available_balance))
            effective_leverage = Decimal(str(snapshot.effective_leverage))
            margin_utilization = Decimal(str(snapshot.margin_utilization_pct))
            total_notional = Decimal(str(snapshot.total_position_notional))
            liquidation_safety = str(snapshot.liquidation_safety).upper()
            liquidation_distance = snapshot.min_liquidation_distance_pct
            if liquidation_distance is not None:
                liquidation_distance = Decimal(str(liquidation_distance))

            max_drawdown = Decimal(str(self.risk_governor.max_drawdown_pct))
            max_leverage = Decimal(str(self.risk_governor.max_leverage))
            max_margin_raw = os.getenv("MAX_MARGIN_UTILIZATION_PCT", "")
            max_margin = Decimal(
                max_margin_raw.strip()
                if max_margin_raw.strip()
                else str(self.risk_governor.max_margin_utilization_pct)
            )
        except (AttributeError, InvalidOperation, TypeError, ValueError):
            return RiskState.NO_NEW_RISK

        numeric_values = (
            drawdown,
            available_balance,
            effective_leverage,
            margin_utilization,
            total_notional,
            max_drawdown,
            max_leverage,
            max_margin,
        )
        if any(not value.is_finite() or value < 0 for value in numeric_values):
            return RiskState.NO_NEW_RISK
        if max_drawdown <= 0 or max_leverage <= 0 or max_margin <= 0 or max_margin > 100:
            return RiskState.NO_NEW_RISK

        if (
            available_balance <= 0
            or drawdown >= max_drawdown
            or effective_leverage >= max_leverage
            or margin_utilization >= max_margin
        ):
            return RiskState.NO_NEW_RISK

        if liquidation_safety != "KNOWN":
            return RiskState.NO_NEW_RISK
        if liquidation_distance is None:
            # A missing distance is only meaningful for a proven flat account.
            if total_notional != 0:
                return RiskState.NO_NEW_RISK
        elif (
            not liquidation_distance.is_finite()
            or liquidation_distance <= 0
        ):
            return RiskState.NO_NEW_RISK

        return RiskState.NORMAL

    def is_market_data_fresh(self, symbols: Optional[List[str]] = None) -> bool:
        if not self.market_data_healthy:
            return False
        max_age = self._positive_env_float("MAX_MARKET_DATA_AGE_SEC", 3.0)
        adapter_timestamps = getattr(self.execution_adapter, "last_market_event_at", {})
        for raw_symbol in symbols or self._active_instruments():
            symbol = str(raw_symbol).upper()
            has_authoritative_sample = getattr(
                self.execution_adapter, "has_authoritative_market_sample", None
            )
            if callable(has_authoritative_sample) and not has_authoritative_sample(symbol):
                return False
            timestamp = self.last_market_event_at.get(symbol) or adapter_timestamps.get(symbol)
            if not isinstance(timestamp, datetime):
                return False
            if timestamp.tzinfo is None:
                return False
            age = (utc_now() - timestamp).total_seconds()
            if age < 0 or age > max_age:
                return False
        return True

    def _sync_adapter_state(self) -> None:
        if self.execution_adapter is None:
            return
        if getattr(self.execution_adapter.reconciliation, "authentication_failed", False):
            self.execution_adapter.invalidate_authentication()
        adapter_state = self.execution_adapter.connection_state
        self.connection_state = getattr(adapter_state, "value", str(adapter_state))
        self.authenticated = self.execution_adapter.authenticated
        stream_health = getattr(self.execution_adapter, "private_stream_healthy", None)
        if stream_health is None:
            stream = getattr(self.execution_adapter, "user_stream", None)
            stream_health = bool(stream and getattr(stream, "is_connected", False))
        self.private_stream_healthy = bool(stream_health)
        self.reconciliation_status = getattr(
            self.execution_adapter.reconciliation, "last_status", "UNKNOWN"
        )
        self._refresh_engine_state()

    def _adapter_trade_authorized(self) -> bool:
        adapter = self.execution_adapter
        return bool(
            adapter
            and getattr(getattr(adapter, "capabilities", None), "trade_authorized", False)
        )

    def _refresh_engine_state(self) -> None:
        """Derive the single operational state from canonical control flags."""
        if self.kill_switch_active:
            self.engine_state = WorkerEngineState.EMERGENCY
        elif (
            self.execution_mode in {
                WorkerExecutionMode.TESTNET,
                WorkerExecutionMode.LIVE,
            }
            and self.active_configuration is not None
            and (
                self.connection_state != ConnectionState.READY.value
                or not self.authenticated
                or not self._adapter_trade_authorized()
                or not self.private_stream_healthy
                or self.reconciliation_status != "IN_SYNC"
            )
        ):
            # A worker cannot remain visibly ARMED while its sole exchange
            # adapter is degraded, disconnected, or reconciling.  This is a
            # control-plane state only; emergency reduction remains available
            # through its explicit Worker-owned fallback path.
            self.engine_state = WorkerEngineState.DEGRADED
        elif self.recovery_only:
            self.engine_state = WorkerEngineState.RECOVERY_ONLY
        elif self.pause_new_risk:
            self.engine_state = WorkerEngineState.PAUSED_NEW_RISK
        else:
            self.engine_state = (
                WorkerEngineState.ARMED
                if self.active_configuration
                else WorkerEngineState.DISARMED
            )

    def get_state(self) -> WorkerRuntimeState:
        self._sync_adapter_state()
        launch_readiness = self.get_launch_readiness()
        uptime = (utc_now() - self.start_time).total_seconds()
        trading_healthy = bool(
            self.connection_state == "READY"
            and not self.kill_switch_active
            and (
                self.execution_mode == WorkerExecutionMode.PAPER
                or (
                    self.authenticated
                    and self.private_stream_healthy
                    and self.reconciliation_status == "IN_SYNC"
                )
            )
        )
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
            provenance = "BINANCE_MAINNET"
            exchange_env = "BINANCE_MAINNET"

        data_source = "BINANCE" if self.execution_mode != WorkerExecutionMode.PAPER else "SIMULATED"

        return WorkerRuntimeState(
            execution_authority="PYTHON_TRADING_WORKER",
            is_execution_authority=True,
            execution_mode=self.execution_mode,
            provenance=provenance,
            data_source=data_source,
            exchange_environment=exchange_env,
            worker_image_digest=os.getenv("WORKER_IMAGE_DIGEST", "").strip(),
            worker_revision=configured_worker_revision(),
            secret_versions=configured_secret_versions(),
            engine_state=self.engine_state,
            connection_state=self.connection_state,
            market_data_healthy=self.market_data_healthy,
            private_stream_healthy=self.private_stream_healthy,
            trading_connection_healthy=trading_healthy,
            authenticated=self.authenticated,
            account_synchronized=self.reconciliation_status == "IN_SYNC",
            reconciliation_status=self.reconciliation_status,
            kill_switch_active=self.kill_switch_active,
            pause_new_risk=self.pause_new_risk,
            recovery_only=self.recovery_only,
            order_submission_attempts=int(
                getattr(self.execution_adapter, "order_submission_attempts", 0) or 0
            ),
            mainnet_credentials_verified=bool(
                launch_readiness.get("mainnet_credentials_verified", False)
            ),
            mainnet_live_approved=bool(
                launch_readiness.get("mainnet_live_approved", False)
            ),
            mainnet_preflight_ready=bool(
                launch_readiness.get("mainnet_preflight_ready", False)
            ),
            mainnet_launch_policy=(
                str(self._launch_session_value("policy"))
                if self._launch_session_value("policy") is not None
                else None
            ),
            mainnet_launch_id=self._mainnet_launch_id,
            mainnet_launch_state=(
                str(self._launch_session_value("state"))
                if self._launch_session_value("state") is not None
                else None
            ),
            mainnet_continuation_approval_id=(
                str(self._launch_session_value("continuation_approval_id"))
                if self._launch_session_value("continuation_approval_id") is not None
                else None
            ),
            heartbeat_at=self.heartbeat_at,
            health_indicators=health,
            config_version="v0.2.0-beta",
            active_configuration=self.active_configuration,
            updated_at=utc_now()
        )
        
    def get_capabilities(self) -> dict:
        testnet_configured = self._testnet_configured()
        mainnet_configured = self._mainnet_configured()
        self._sync_adapter_state()
        adapter_ready = (
            self.execution_adapter is not None
            and self.execution_adapter.connection_state == ConnectionState.READY
            and self._adapter_trade_authorized()
        )
        trade_authorized = self._adapter_trade_authorized()
        symbol_rules_loaded = self._symbol_rules_ready()
        account_snapshot_ready = self.is_account_snapshot_ready()
        mainnet_account_risk_ready = self.is_mainnet_account_risk_ready()
        market_data_fresh = self.is_market_data_fresh()
        testnet_ready = (
            self.execution_mode == WorkerExecutionMode.TESTNET
            and testnet_configured
            and self.authenticated
            and trade_authorized
            and adapter_ready
            and symbol_rules_loaded
            and self.private_stream_healthy
            and self.reconciliation_status == "IN_SYNC"
            and account_snapshot_ready
            and market_data_fresh
            and not self.kill_switch_active
        )
        mainnet_preflight_ready = (
            self.execution_mode == WorkerExecutionMode.LIVE
            and mainnet_configured
            and self._env_flag("MAINNET_LIVE_APPROVED", False)
            and self.authenticated
            and trade_authorized
            and adapter_ready
            and symbol_rules_loaded
            and self.private_stream_healthy
            and self.reconciliation_status == "IN_SYNC"
            and account_snapshot_ready
            and mainnet_account_risk_ready
            and market_data_fresh
            and self.persistence.readiness()["durable"]
            and not self.kill_switch_active
        )
        launch_session = self.persistence.readiness().get("mainnet_launch_session") or self._mainnet_launch_session
        mainnet_ready = bool(
            mainnet_preflight_ready
            and isinstance(launch_session, dict)
            and launch_session.get("policy") == MAINNET_LAUNCH_AUTONOMOUS
            and launch_session.get("state") == "AUTONOMOUS_ACTIVE"
            and self.engine_state == WorkerEngineState.ARMED
            and not self.pause_new_risk
        )
        return {
            "paper": True,
            "testnetConfigured": testnet_configured,
            "testnetAuthenticated": self.authenticated,
            "testnetTradeAuthorized": trade_authorized,
            "testnetPrivateStreamHealthy": self.private_stream_healthy,
            "testnetReconciliationInSync": self.reconciliation_status == "IN_SYNC",
            "testnetSymbolRulesLoaded": symbol_rules_loaded,
            "testnetAdapterReady": bool(
                self.execution_mode == WorkerExecutionMode.TESTNET and adapter_ready
            ),
            "testnetExecutionAdapterReady": bool(
                self.execution_mode == WorkerExecutionMode.TESTNET and adapter_ready
            ),
            "testnetAccountSnapshotReady": account_snapshot_ready,
            "testnetMarketDataFresh": market_data_fresh,
            "testnetExecutionReady": testnet_ready,
            "liveConfigured": mainnet_configured,
            "liveExecutionReady": mainnet_ready,
            "mainnetCredentialsVerified": bool(mainnet_configured and self.authenticated),
            "mainnetLiveApproved": self._env_flag("MAINNET_LIVE_APPROVED", False),
            "mainnetAccountRiskReady": mainnet_account_risk_ready,
            "mainnetPreflightReady": mainnet_preflight_ready,
            "spotSupported": False,
            "usdmFuturesSupported": True,
            "hedgeModeSupported": bool(
                self.execution_adapter and self.execution_adapter.capabilities.hedge_mode
            ),
            "persistence": self.persistence.readiness(),
        }

    def get_launch_readiness(self) -> dict:
        self._sync_adapter_state()
        testnet_configured = self._testnet_configured()
        mainnet_configured = self._mainnet_configured()
        auto_flag = self._env_flag("AUTONOMOUS_TESTNET_EXECUTION", False)
        auto_soak_flag = self._env_flag("AUTONOMOUS_TESTNET_SOAK_APPROVED", False)

        ci_verified = False
        local_non_secret_tests_verified = False
        readonly_contract_verified = False
        manual_trial_verified = False
        soak_verified = False

        current_build_sha = None
        try:
            import subprocess

            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            working_tree = subprocess.run(
                ["git", "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            # Evidence is for the exact source that was tested. A commit SHA
            # alone is insufficient while tracked source changes are pending.
            current_build_sha = head_sha if not working_tree else None
        except Exception:
            # A caller-supplied SHA is not evidence of the source that is
            # actually running.  Fail closed if the repository identity cannot
            # be read and keep all launch evidence disabled.
            current_build_sha = None

        metadata_paths = ("build_metadata.json", os.path.join("artifacts", "build-evidence.json"))
        meta: dict = {}
        for metadata_path in metadata_paths:
            if not os.path.exists(metadata_path):
                continue
            try:
                with open(metadata_path, encoding="utf-8") as evidence_file:
                    evidence = BuildEvidence.model_validate(json.load(evidence_file))
                    meta = evidence.model_dump()
                    break
            except Exception:
                pass

        evidence_matches_build = bool(current_build_sha and meta.get("build_sha") == current_build_sha)
        local_non_secret_tests_verified = bool(
            evidence_matches_build and meta.get("local_non_secret_tests_verified", False)
        )
        ci_verified = bool(evidence_matches_build and meta.get("github_ci_verified", False))
        readonly_contract_verified = bool(
            evidence_matches_build and meta.get("readonly_contract_verified", False)
        )
        manual_trial_verified = bool(
            evidence_matches_build
            and meta.get("manual_trial_verified", False)
            and meta.get("manual_trial_sha") == current_build_sha
        )
        soak_verified = bool(evidence_matches_build and meta.get("testnet_soak_verified", False))

        adapter_ready = bool(
            self.execution_adapter
            and self.execution_adapter.connection_state == ConnectionState.READY
            and self._adapter_trade_authorized()
        )
        trade_authorized = self._adapter_trade_authorized()
        rules_ready = self._symbol_rules_ready()
        account_ready = self.is_account_snapshot_ready()
        mainnet_account_risk_ready = self.is_mainnet_account_risk_ready()
        market_data_fresh = self.is_market_data_fresh()
        persistence = self.persistence.readiness()
        launch_session = persistence.get("mainnet_launch_session") or self._mainnet_launch_session
        persistence_required_ready = (
            self.persistence.mode.value != "REQUIRED" or persistence["durable"]
        )

        readiness = LaunchReadiness(
            paper_ready=not self.kill_switch_active and persistence_required_ready,
            local_non_secret_tests_verified=local_non_secret_tests_verified,
            ci_verified=ci_verified,
            testnet_credentials_verified=testnet_configured and self.authenticated,
            testnet_trade_authorized=trade_authorized,
            testnet_readonly_contract_verified=readonly_contract_verified,
            testnet_manual_trial_verified=manual_trial_verified,
            testnet_soak_verified=soak_verified,
            adapter_ready=adapter_ready,
            private_stream_healthy=self.private_stream_healthy,
            reconciliation_in_sync=self.reconciliation_status == "IN_SYNC",
            account_snapshot_ready=account_ready,
            symbol_rules_ready=rules_ready,
            market_data_fresh=market_data_fresh,
            autonomous_flag_enabled=auto_flag,
            autonomous_soak_flag_enabled=auto_soak_flag,
            launch_approved=self._env_flag("TESTNET_LAUNCH_APPROVED", False),
            testnet_autonomous_soak_ready=False,
            testnet_autonomous_ready=False,
            small_live_ready=False,
            mainnet_credentials_verified=mainnet_configured and self.authenticated,
            mainnet_live_approved=self._env_flag("MAINNET_LIVE_APPROVED", False),
            mainnet_account_risk_ready=mainnet_account_risk_ready,
            mainnet_preflight_ready=False,
            mainnet_autonomous_ready=False,
            mainnet_launch_policy=(
                str(launch_session.get("policy"))
                if isinstance(launch_session, dict) and launch_session.get("policy")
                else None
            ),
            mainnet_launch_id=(
                str(launch_session.get("launch_id"))
                if isinstance(launch_session, dict) and launch_session.get("launch_id")
                else None
            ),
            mainnet_launch_state=(
                str(launch_session.get("state"))
                if isinstance(launch_session, dict) and launch_session.get("state")
                else None
            ),
            mainnet_continuation_approval_id=(
                str(launch_session.get("continuation_approval_id"))
                if isinstance(launch_session, dict) and launch_session.get("continuation_approval_id")
                else None
            ),
            persistence=persistence,
        )
        
        readiness.testnet_autonomous_soak_ready = (
            self.execution_mode == WorkerExecutionMode.TESTNET and
            readiness.local_non_secret_tests_verified and
            readiness.ci_verified and
            readiness.testnet_credentials_verified and
            readiness.testnet_trade_authorized and
            self.authenticated and
            readiness.testnet_readonly_contract_verified and
            readiness.testnet_manual_trial_verified and
            readiness.adapter_ready and
            readiness.symbol_rules_ready and
            readiness.account_snapshot_ready and
            readiness.private_stream_healthy and
            readiness.reconciliation_in_sync and
            readiness.market_data_fresh and
            persistence_required_ready and
            readiness.autonomous_soak_flag_enabled and
            readiness.autonomous_flag_enabled and
            readiness.launch_approved and
            not self.kill_switch_active
        )

        readiness.testnet_autonomous_ready = (
            readiness.testnet_autonomous_soak_ready and
            readiness.testnet_soak_verified and
            readiness.autonomous_flag_enabled
        )

        readiness.mainnet_preflight_ready = (
            self.execution_mode == WorkerExecutionMode.LIVE
            and readiness.mainnet_credentials_verified
            and readiness.mainnet_live_approved
            and readiness.adapter_ready
            and readiness.private_stream_healthy
            and readiness.reconciliation_in_sync
            and readiness.account_snapshot_ready
            and mainnet_account_risk_ready
            and readiness.symbol_rules_ready
            and readiness.market_data_fresh
            and persistence_required_ready
            and not self.kill_switch_active
        )
        # A successful read-only preflight is not autonomous authorization.
        # The durable session must have consumed a separate continuation
        # approval and be in AUTONOMOUS_ACTIVE state.
        readiness.mainnet_autonomous_ready = bool(
            readiness.mainnet_preflight_ready
            and readiness.mainnet_launch_policy == MAINNET_LAUNCH_AUTONOMOUS
            and readiness.mainnet_launch_state == "AUTONOMOUS_ACTIVE"
            and self.engine_state == WorkerEngineState.ARMED
            and not self.pause_new_risk
        )
        
        return readiness.model_dump()

    def get_preflight(self, execution_mode: str) -> dict:
        mode_upper = str(execution_mode).upper()
        if mode_upper == "LIVE":
            self._sync_adapter_state()
            adapter_ready = bool(
                self.execution_adapter
                and self.execution_adapter.env == BinanceEnvironment.MAINNET
                and self.execution_adapter.connection_state == ConnectionState.READY
                and self._adapter_trade_authorized()
            )
            persistence = self.persistence.readiness()
            checks = [
                {
                    "id": "CHK-MAINNET-CREDS",
                    "name": "Mainnet Credentials",
                    "required": True,
                    "status": "PASS" if self._mainnet_configured() else "FAIL",
                    "message": "Secret Manager Mainnet credential pair is injected"
                    if self._mainnet_configured()
                    else "BINANCE_MAINNET_API_KEY/SECRET are missing",
                },
                {
                    "id": "CHK-MAINNET-APPROVAL",
                    "name": "Deployment Launch Approval",
                    "required": True,
                    "status": "PASS" if self._env_flag("MAINNET_LIVE_APPROVED", False) else "FAIL",
                    "message": "MAINNET_LIVE_APPROVED is enabled for this revision"
                    if self._env_flag("MAINNET_LIVE_APPROVED", False)
                    else "Deployment is disarmed until MAINNET_LIVE_APPROVED=true",
                },
                {
                    "id": "CHK-MAINNET-ADAPTER",
                    "name": "Mainnet Adapter",
                    "required": True,
                    "status": "PASS" if adapter_ready else "FAIL",
                    "message": "Fixed Binance Mainnet adapter is READY"
                    if adapter_ready
                    else "Mainnet adapter is not READY",
                },
                {
                    "id": "CHK-MAINNET-AUTH",
                    "name": "Signed Authentication",
                    "required": True,
                    "status": "PASS" if self.authenticated else "FAIL",
                    "message": "Signed Mainnet account request succeeded"
                    if self.authenticated
                    else "Mainnet authentication is not verified",
                },
                {
                    "id": "CHK-MAINNET-TRADE-PERMISSION",
                    "name": "Mainnet Trade Permission",
                    "required": True,
                    "status": "PASS" if self._adapter_trade_authorized() else "FAIL",
                    "message": "Account canTrade is true"
                    if self._adapter_trade_authorized()
                    else "Account canTrade is false or unverified",
                },
                {
                    "id": "CHK-MAINNET-RULES",
                    "name": "ETHUSDC Contract Rules",
                    "required": True,
                    "status": "PASS" if self._symbol_rules_ready() else "FAIL",
                    "message": "Runtime exchangeInfo proves a TRADING USDC perpetual"
                    if self._symbol_rules_ready()
                    else "ETHUSDC exchange-derived contract/filter rules are incomplete",
                },
                {
                    "id": "CHK-MAINNET-SYNC",
                    "name": "Reconciliation",
                    "required": True,
                    "status": "PASS" if self.reconciliation_status == "IN_SYNC" else "FAIL",
                    "message": f"Reconciliation status: {self.reconciliation_status}",
                },
                {
                    "id": "CHK-MAINNET-STREAM",
                    "name": "Private User Stream",
                    "required": True,
                    "status": "PASS" if self.private_stream_healthy else "FAIL",
                    "message": "Private Mainnet user stream active"
                    if self.private_stream_healthy
                    else "Private stream offline",
                },
                {
                    "id": "CHK-MAINNET-ACCOUNT",
                    "name": "Account Risk Snapshot",
                    "required": True,
                    "status": "PASS" if self.is_mainnet_account_risk_ready() else "FAIL",
                    "message": "Fresh Mainnet USDC collateral, margin mode, leverage, and daily PnL are verified"
                    if self.is_mainnet_account_risk_ready()
                    else "Mainnet risk snapshot is missing explicit USDC, mode, leverage, or fee/funding-inclusive daily PnL evidence",
                },
                {
                    "id": "CHK-MAINNET-MARKET",
                    "name": "Market Data Freshness",
                    "required": True,
                    "status": "PASS" if self.is_market_data_fresh() else "FAIL",
                    "message": "Fresh ETHUSDC market data is available"
                    if self.is_market_data_fresh()
                    else "ETHUSDC market data is missing or stale",
                },
                {
                    "id": "CHK-MAINNET-PERSISTENCE",
                    "name": "Required SQL Outbox",
                    "required": True,
                    "status": "PASS" if persistence["durable"] else "FAIL",
                    "message": "Cloud SQL transactional outbox is durable"
                    if persistence["durable"]
                    else "LIVE requires PERSISTENCE_MODE=REQUIRED and a durable outbox",
                },
                {
                    "id": "CHK-MAINNET-KILL",
                    "name": "Kill Switch",
                    "required": True,
                    "status": "FAIL" if self.kill_switch_active else "PASS",
                    "message": "Kill switch is active" if self.kill_switch_active else "Kill switch inactive",
                },
            ]
            return {
                "executionMode": "LIVE",
                "canArm": all(check["status"] == "PASS" for check in checks if check["required"]),
                "checks": checks,
            }

        if mode_upper == "TESTNET":
            self._sync_adapter_state()
            testnet_configured = self._testnet_configured()
            adapter_ready = bool(
                self.execution_adapter
                and self.execution_adapter.connection_state == ConnectionState.READY
                and self._adapter_trade_authorized()
            )
            trade_authorized = self._adapter_trade_authorized()
            rules_ready = self._symbol_rules_ready()
            account_ready = self.is_account_snapshot_ready()
            market_data_fresh = self.is_market_data_fresh()
            persistence = self.persistence.readiness()
            persistence_required = self.persistence.mode.value == "REQUIRED"
            checks = [
                {
                    "id": "CHK-CREDS",
                    "name": "Testnet Credentials",
                    "required": True,
                    "status": "PASS" if testnet_configured else "FAIL",
                    "message": "Configured for Binance Testnet" if testnet_configured else "Testnet credentials or BINANCE_TESTNET=true missing"
                },
                {
                    "id": "CHK-ADAPTER",
                    "name": "Testnet Adapter",
                    "required": True,
                    "status": "PASS" if adapter_ready else "FAIL",
                    "message": "Adapter READY" if adapter_ready else "Adapter is not READY",
                },
                {
                    "id": "CHK-AUTH",
                    "name": "Signed Authentication",
                    "required": True,
                    "status": "PASS" if self.authenticated else "FAIL",
                    "message": "Signed Testnet account request succeeded" if self.authenticated else "Not authenticated",
                },
                {
                    "id": "CHK-TRADE-PERMISSION",
                    "name": "Testnet Trade Permission",
                    "required": True,
                    "status": "PASS"
                    if bool(
                        trade_authorized
                    )
                    else "FAIL",
                    "message": "Account canTrade is true"
                    if trade_authorized
                    else "Account canTrade is false or unverified",
                },
                {
                    "id": "CHK-RULES",
                    "name": "Symbol Rules",
                    "required": True,
                    "status": "PASS" if rules_ready else "FAIL",
                    "message": "Rules loaded for every active instrument" if rules_ready else "Trading rules missing or incomplete",
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
                    "id": "CHK-ACCOUNT",
                    "name": "Account Snapshot",
                    "required": True,
                    "status": "PASS" if account_ready else "FAIL",
                    "message": "Fresh verified Testnet account snapshot" if account_ready else "Account snapshot missing, stale, invalid, or wrong environment",
                },
                {
                    "id": "CHK-MARKET",
                    "name": "Market Data Freshness",
                    "required": True,
                    "status": "PASS" if market_data_fresh else "FAIL",
                    "message": "Fresh data for every active instrument" if market_data_fresh else "Market data missing or stale for an active instrument",
                },
                {
                    "id": "CHK-KILL",
                    "name": "Kill Switch",
                    "required": True,
                    "status": "FAIL" if self.kill_switch_active else "PASS",
                    "message": "Kill switch is active" if self.kill_switch_active else "Kill switch inactive"
                },
                {
                    "id": "CHK-PERSISTENCE",
                    "name": "Persistence Outbox",
                    "required": persistence_required,
                    "status": (
                        "PASS"
                        if persistence["durable"]
                        else "FAIL"
                        if persistence_required
                        else "DEGRADED"
                    ),
                    "message": (
                        "Transactional outbox is connected and durable"
                        if persistence["durable"]
                        else "REQUIRED persistence is unavailable; execution is blocked"
                        if persistence_required
                        else "OPTIONAL persistence is degraded; events are not durable"
                    ),
                }
            ]
            can_arm = all(c["status"] == "PASS" for c in checks if c["required"])
            return {
                "executionMode": "TESTNET",
                "canArm": can_arm,
                "checks": checks
            }

        # Default PAPER mode
        persistence = self.persistence.readiness()
        persistence_required = self.persistence.mode.value == "REQUIRED"
        return {
            "executionMode": "PAPER",
            "canArm": (
                not self.kill_switch_active
                and (not persistence_required or persistence["durable"])
            ),
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
                },
                {
                    "id": "CHK-PERSISTENCE",
                    "name": "Persistence Outbox",
                    "required": persistence_required,
                    "status": (
                        "PASS"
                        if persistence["durable"]
                        else "FAIL"
                        if persistence_required
                        else "DEGRADED"
                    ),
                    "message": (
                        "Transactional outbox is connected and durable"
                        if persistence["durable"]
                        else "REQUIRED persistence is unavailable; Paper arming is blocked"
                        if persistence_required
                        else "OPTIONAL persistence is unavailable; Paper remains explicitly non-durable"
                    ),
                }
            ]
        }

    async def run_mainnet_read_only_preflight(self) -> dict:
        """Collect signed Mainnet evidence without changing worker lifecycle.

        This path deliberately creates a disposable adapter that is allowed to
        perform only the read/stream/reconciliation lifecycle.  It never binds
        the adapter to this worker, never acquires an execution lease, never
        calls an order endpoint, and always closes the private stream before
        returning.  A successful observation is evidence for a later release
        gate; it is not an ARM operation.
        """

        async with self._mainnet_preflight_lock:
            observed_at = utc_now()
            checks: List[dict[str, Any]] = []

            def add_check(
                check_id: str,
                name: str,
                passed: bool,
                message: str,
                *,
                required: bool = True,
            ) -> None:
                checks.append(
                    {
                        "id": check_id,
                        "name": name,
                        "required": required,
                        "status": "PASS" if passed else "FAIL",
                        "message": message,
                    }
                )

            before_signature = (
                self.execution_mode,
                self.engine_state,
                self.connection_state,
                self.market_data_healthy,
                self.private_stream_healthy,
                self.authenticated,
                self.reconciliation_status,
                self.kill_switch_active,
                self.pause_new_risk,
                self.recovery_only,
                tuple(self.symbols),
                id(self.execution_adapter),
                int(getattr(self.execution_adapter, "order_submission_attempts", 0) or 0),
                repr(self.active_configuration),
            )
            adapter: Optional[BinanceExecutionAdapter] = None
            order_submission_attempts = 0
            order_endpoint_attempts = 0
            credentials_configured = self._mainnet_configured()
            add_check(
                "CHK-PREFLIGHT-CREDENTIALS",
                "Mainnet Credentials",
                credentials_configured,
                "Secret-injected Mainnet credential pair is present"
                if credentials_configured
                else "Mainnet credentials are not injected into this revision",
            )

            connected = False
            market_fresh = False
            persistence_ready = False
            persistence_error = False
            durable_ledger = None
            durable_ledger_error = False
            try:
                try:
                    persistence = self.persistence.readiness()
                    has_pending = (
                        isinstance(persistence, Mapping)
                        and "pending_outbox" in persistence
                    )
                    has_failed = (
                        isinstance(persistence, Mapping)
                        and "failed_writes" in persistence
                    )
                    pending_outbox = persistence.get("pending_outbox") if has_pending else None
                    failed_writes = persistence.get("failed_writes") if has_failed else None

                    def _is_explicit_zero_counter(val: Any) -> bool:
                        if val is None or isinstance(val, bool):
                            return False
                        if isinstance(val, (int, float, Decimal)):
                            return val == 0
                        return False

                    persistence_ready = bool(
                        isinstance(persistence, Mapping)
                        and persistence.get("mode") == "REQUIRED"
                        and persistence.get("durable") is True
                        and has_pending
                        and _is_explicit_zero_counter(pending_outbox)
                        and has_failed
                        and _is_explicit_zero_counter(failed_writes)
                    )
                except Exception:
                    persistence_error = True
                add_check(
                    "CHK-PREFLIGHT-PERSISTENCE",
                    "Required SQL Persistence",
                    persistence_ready and not persistence_error,
                    "Required transactional outbox is durable"
                    if persistence_ready and not persistence_error
                    else "Required persistence is unavailable; preflight evidence is not durable",
                )
                ledger_factory = getattr(self.persistence, "create_execution_ledger", None)
                if credentials_configured and callable(ledger_factory):
                    try:
                        durable_ledger = await ledger_factory(
                            symbol="ETHUSDC",
                            venue=environment_label(BinanceEnvironment.MAINNET),
                        )
                    except Exception as exc:
                        durable_ledger_error = True
                        logger.error(
                            "Mainnet durable ledger snapshot failed: %s",
                            type(exc).__name__,
                        )
                    add_check(
                        "CHK-PREFLIGHT-DURABLE-LEDGER",
                        "Durable Mainnet Ledger Snapshot",
                        durable_ledger is not None and not durable_ledger_error,
                        "Cloud SQL Mainnet ledger scope was loaded before reconciliation"
                        if durable_ledger is not None and not durable_ledger_error
                        else "Cloud SQL Mainnet ledger scope could not be loaded safely",
                    )
                add_check(
                    "CHK-PREFLIGHT-KILL-SWITCH",
                    "Kill Switch",
                    not self.kill_switch_active,
                    "Kill switch is inactive"
                    if not self.kill_switch_active
                    else "Kill switch is active",
                )

                if credentials_configured and not durable_ledger_error:
                    adapter = BinanceExecutionAdapter(
                        api_key=os.getenv("BINANCE_MAINNET_API_KEY", ""),
                        api_secret=os.getenv("BINANCE_MAINNET_API_SECRET", ""),
                        env=BinanceEnvironment.MAINNET,
                        ledger=durable_ledger,
                        preflight_only=True,
                    )
                    connected = await adapter.connect()
                    try:
                        market_fresh = await adapter.refresh_market_data(["ETHUSDC"])
                    except Exception:
                        market_fresh = False

                    capabilities = adapter.capabilities
                    add_check(
                        "CHK-PREFLIGHT-CONNECTION",
                        "Mainnet Read-only Connection",
                        connected and adapter.connection_state == ConnectionState.READY,
                        "Fixed Mainnet adapter reached READY for observation"
                        if connected and adapter.connection_state == ConnectionState.READY
                        else "Mainnet read-only connection did not reach READY",
                    )
                    add_check(
                        "CHK-PREFLIGHT-AUTH",
                        "Signed Account Authentication",
                        bool(
                            capabilities.account_request_succeeded
                            and adapter.authenticated
                        ),
                        "Signed Mainnet account request succeeded"
                        if capabilities.account_request_succeeded and adapter.authenticated
                        else "Signed Mainnet account authentication is unverified",
                    )
                    add_check(
                        "CHK-PREFLIGHT-CAN-TRADE",
                        "Account Trade Permission",
                        bool(capabilities.trade_authorized),
                        "Binance account canTrade is true"
                        if capabilities.trade_authorized
                        else "Binance account canTrade is false or unverified",
                    )
                    add_check(
                        "CHK-PREFLIGHT-POSITION-MODE",
                        "Position Mode",
                        bool(capabilities.position_mode_known),
                        "Binance position mode was read successfully"
                        if capabilities.position_mode_known
                        else "Binance position mode is unknown",
                    )
                    rules_ready = adapter.is_symbol_ready_for_execution("ETHUSDC")
                    add_check(
                        "CHK-PREFLIGHT-RULES",
                        "ETHUSDC Exchange Rules",
                        rules_ready,
                        "Runtime exchangeInfo proves a TRADING USDC perpetual with complete filters"
                        if rules_ready
                        else "ETHUSDC exchange-derived contract or filters are incomplete",
                    )
                    reconciliation_ready = (
                        adapter.reconciliation.last_status == "IN_SYNC"
                    )
                    add_check(
                        "CHK-PREFLIGHT-RECONCILIATION",
                        "Account Reconciliation",
                        reconciliation_ready,
                        "Mainnet positions, open orders, fills, and account snapshot are in sync"
                        if reconciliation_ready
                        else "Mainnet exchange state is not reconciled with the disposable preflight ledger",
                    )
                    add_check(
                        "CHK-PREFLIGHT-PRIVATE-STREAM",
                        "Private Stream",
                        bool(adapter.private_stream_healthy),
                        "Private stream transport heartbeat was verified"
                        if adapter.private_stream_healthy
                        else "Private stream is unavailable or stale",
                    )
                    snapshot = adapter.account_snapshot
                    account_ready = self._is_mainnet_snapshot_risk_ready(
                        snapshot,
                        adapter,
                        require_execution_lease=False,
                    )
                    add_check(
                        "CHK-PREFLIGHT-ACCOUNT-RISK",
                        "USDC Account Risk Snapshot",
                        account_ready,
                        "USDC collateral, balance, leverage, exposure, liquidation, and fee/funding-inclusive daily PnL are within locked limits"
                        if account_ready
                        else "USDC collateral, mode, leverage, exposure, liquidation, or complete daily PnL evidence is unsafe or unavailable",
                    )
                    market_timestamp = adapter.last_market_event_at.get("ETHUSDC")
                    if market_timestamp is not None and market_timestamp.tzinfo is not None:
                        market_age = (utc_now() - market_timestamp).total_seconds()
                        market_fresh = bool(
                            market_fresh
                            and adapter.has_authoritative_market_sample("ETHUSDC")
                            and 0 <= market_age <= adapter._market_data_max_age()
                        )
                    else:
                        market_fresh = False
                    add_check(
                        "CHK-PREFLIGHT-MARKET",
                        "ETHUSDC Market Freshness",
                        market_fresh,
                        "Fresh Mainnet ETHUSDC book data was observed"
                        if market_fresh
                        else "Fresh Mainnet ETHUSDC market data is unavailable",
                    )
                else:
                    for check_id, name, message in (
                        (
                            "CHK-PREFLIGHT-CONNECTION",
                            "Mainnet Read-only Connection",
                            "Skipped because Mainnet credentials are not injected",
                        ),
                        (
                            "CHK-PREFLIGHT-AUTH",
                            "Signed Account Authentication",
                            "Skipped because Mainnet credentials are not injected",
                        ),
                        (
                            "CHK-PREFLIGHT-CAN-TRADE",
                            "Account Trade Permission",
                            "Skipped because Mainnet credentials are not injected",
                        ),
                        (
                            "CHK-PREFLIGHT-POSITION-MODE",
                            "Position Mode",
                            "Skipped because Mainnet credentials are not injected",
                        ),
                        (
                            "CHK-PREFLIGHT-RULES",
                            "ETHUSDC Exchange Rules",
                            "Skipped because Mainnet credentials are not injected",
                        ),
                        (
                            "CHK-PREFLIGHT-RECONCILIATION",
                            "Account Reconciliation",
                            "Skipped because Mainnet credentials are not injected",
                        ),
                        (
                            "CHK-PREFLIGHT-PRIVATE-STREAM",
                            "Private Stream",
                            "Skipped because Mainnet credentials are not injected",
                        ),
                        (
                            "CHK-PREFLIGHT-ACCOUNT-RISK",
                            "USDC Account Risk Snapshot",
                            "Skipped because Mainnet credentials are not injected",
                        ),
                        (
                            "CHK-PREFLIGHT-MARKET",
                            "ETHUSDC Market Freshness",
                            "Skipped because Mainnet credentials are not injected",
                        ),
                    ):
                        add_check(check_id, name, False, message)
            except Exception as exc:
                logger.error(
                    "Mainnet read-only preflight failed: %s",
                    type(exc).__name__,
                )
                add_check(
                    "CHK-PREFLIGHT-ERROR",
                    "Preflight Lifecycle",
                    False,
                    "Read-only preflight could not complete; see sanitized server logs",
                )
            finally:
                if adapter is not None:
                    order_submission_attempts = int(
                        getattr(adapter, "order_submission_attempts", 0) or 0
                    )
                    order_endpoint_attempts = int(
                        getattr(
                            getattr(adapter, "rest_client", None),
                            "order_endpoint_attempts",
                            0,
                        )
                        or 0
                    )
                    try:
                        await adapter.close()
                    except Exception as exc:
                        logger.error(
                            "Mainnet read-only preflight cleanup failed: %s",
                            type(exc).__name__,
                        )

            after_signature = (
                self.execution_mode,
                self.engine_state,
                self.connection_state,
                self.market_data_healthy,
                self.private_stream_healthy,
                self.authenticated,
                self.reconciliation_status,
                self.kill_switch_active,
                self.pause_new_risk,
                self.recovery_only,
                tuple(self.symbols),
                id(self.execution_adapter),
                int(getattr(self.execution_adapter, "order_submission_attempts", 0) or 0),
                repr(self.active_configuration),
            )
            state_unchanged = before_signature == after_signature
            add_check(
                "CHK-PREFLIGHT-WORKER-STATE",
                "Worker Lifecycle Unchanged",
                state_unchanged,
                "Worker remained in its prior lifecycle state"
                if state_unchanged
                else "Worker lifecycle changed during read-only preflight",
            )
            add_check(
                "CHK-PREFLIGHT-NO-ORDER-ENDPOINT",
                "No Order Endpoint",
                order_endpoint_attempts == 0,
                "No Binance order endpoint was called"
                if order_endpoint_attempts == 0
                else "A Binance order endpoint was called during read-only preflight",
            )
            add_check(
                "CHK-PREFLIGHT-NO-ORDER-SUBMISSION",
                "No Order Submission",
                order_submission_attempts == 0,
                "No order submission was attempted"
                if order_submission_attempts == 0
                else "An order submission was attempted during read-only preflight",
            )
            operational_checks = [
                check for check in checks if check["required"]
            ]
            preflight_passed = bool(
                operational_checks
                and all(check["status"] == "PASS" for check in operational_checks)
            )
            approval = self._env_flag("MAINNET_LIVE_APPROVED", False)
            logger.info(
                "monitor_event=mainnet_read_only_preflight preflight_passed=%s order_submission_attempts=%d order_endpoint_attempts=%d",
                preflight_passed,
                order_submission_attempts,
                order_endpoint_attempts,
            )
            return {
                "executionMode": "LIVE",
                "preflightOnly": True,
                "preflightPassed": preflight_passed,
                # A read-only observation can never arm this Worker, even if a
                # deployment happens to carry a stale approval flag.
                "canArm": False,
                "mainnetLiveApproved": approval,
                "engineState": self.engine_state.value,
                "orderSubmissionAttempts": order_submission_attempts,
                "order_submission_attempts": order_submission_attempts,
                "orderEndpointAttempts": order_endpoint_attempts,
                "checks": checks,
                "observedAt": observed_at.isoformat(),
            }

    async def set_pause_new_risk(self, active: bool) -> bool:
        """Toggle the deterministic risk pause without bypassing launch gates."""

        if not active and self.execution_mode == WorkerExecutionMode.LIVE:
            autonomous = (
                self._launch_session_value("policy") == MAINNET_LAUNCH_AUTONOMOUS
                and self._launch_session_value("state") == "AUTONOMOUS_ACTIVE"
            )
            if not autonomous:
                self.pause_new_risk = True
                self._refresh_engine_state()
                logger.warning(
                    "monitor_event=autonomous_resume_denied reason=continuation_approval_required"
                )
                return False
        self.pause_new_risk = bool(active)
        self._refresh_engine_state()
        return True

    async def set_recovery_only(self, active: bool):
        self.recovery_only = bool(active)
        self._refresh_engine_state()

    async def set_kill_switch(self, active: bool) -> dict:
        if not active:
            if self.kill_switch_active:
                adapter = self.execution_adapter
                if self.execution_mode == WorkerExecutionMode.PAPER and adapter is None:
                    self.kill_switch_active = False
                    self._refresh_engine_state()
                    return {"status": "CONFIRMED", "environment": "PAPER"}
                if self.execution_mode not in {
                    WorkerExecutionMode.TESTNET,
                    WorkerExecutionMode.LIVE,
                } or adapter is None:
                    return {
                        "status": "UNKNOWN",
                        "reason": f"Kill switch remains active until a verified {self._current_exchange_label()} restart/reconciliation.",
                    }
                try:
                    open_orders = await adapter.rest_client.request(
                        "GET", "/fapi/v1/openOrders", signed=True
                    )
                    if not isinstance(open_orders, list):
                        return {
                            "status": "UNKNOWN",
                            "reason": "Authoritative openOrders response is invalid.",
                        }
                    if open_orders:
                        return {
                            "status": "PARTIAL",
                            "remaining_orders": len(open_orders),
                            "reason": f"Kill switch remains active while {self._current_exchange_label()} open orders exist.",
                        }
                    reconciliation = await self.trigger_reconciliation()
                    if (
                        reconciliation != "IN_SYNC"
                        or not adapter.private_stream_healthy
                        or not adapter.authenticated
                    ):
                        return {
                            "status": "PARTIAL",
                            "reconciliation": reconciliation,
                            "reason": "Kill switch remains active until stream, authentication, and reconciliation are verified.",
                        }
                    self.kill_switch_active = False
                    self._refresh_engine_state()
                    return {"status": "CONFIRMED", "remaining_orders": 0}
                except BinanceAuthenticationError as exc:
                    adapter.invalidate_authentication()
                    logger.error("Kill switch release authentication failed: %s", exc)
                    return {
                        "status": "UNKNOWN",
                        "reason": f"{self._current_exchange_label()} authentication failed; kill switch remains active.",
                    }
                except Exception as exc:
                    logger.error("Kill switch release verification is unknown: %s", exc)
                    return {
                        "status": "UNKNOWN",
                        "reason": "Kill switch remains active because exchange release could not be verified.",
                    }
            self._refresh_engine_state()
            return {"status": "CONFIRMED"}

        # The local block is the first operation and survives every exchange failure.
        self.kill_switch_active = True
        # Releasing the kill switch must never silently resume autonomous
        # Mainnet risk. Keep new risk paused; an active autonomous launch is
        # additionally fenced below and will require fresh continuation auth.
        self.pause_new_risk = True
        self.engine_state = WorkerEngineState.EMERGENCY
        logger.error(
            "monitor_event=kill_switch_active environment=%s",
            self._current_exchange_label(),
        )
        await self._fence_autonomous_launch("kill_switch")
        adapter = self.execution_adapter
        if self.execution_mode == WorkerExecutionMode.PAPER and adapter is None:
            return {"status": "CONFIRMED", "environment": "PAPER"}
        if self.execution_mode not in {
            WorkerExecutionMode.TESTNET,
            WorkerExecutionMode.LIVE,
        }:
            return {
                "status": "UNKNOWN",
                "reason": "Mutable cancellation is restricted to the fixed Binance execution environment.",
            }
        if adapter is None:
            return {"status": "UNKNOWN", "reason": f"{self._current_exchange_label()} exchange adapter is unavailable."}
        adapter.bind_worker_authority(self)

        # A kill-switch transition invalidates every prior account/order
        # observation immediately.  Keep the local switch active even if the
        # exchange is unreachable; the subsequent read-only reconciliation is
        # best effort and can never clear the switch by itself.
        adapter.reconciliation.last_status = "UNKNOWN"
        await adapter.ledger.set_account_snapshot(None)

        cancellation = await adapter.cancel_all_open_orders(authority=self)
        cancellation_status = str(cancellation.get("status", "UNKNOWN")).upper()

        # Always attempt authoritative reconciliation after the cancellation
        # workflow, including PARTIAL/UNKNOWN outcomes.  This gives operators
        # the strongest available post-switch state without ever converting an
        # unverified cancellation into CONFIRMED.
        try:
            reconciliation = await self.trigger_reconciliation()
        except BinanceAuthenticationError as exc:
            adapter.invalidate_authentication()
            logger.error("Kill switch reconciliation authentication failed: %s", exc)
            reconciliation = "UNKNOWN"
        except Exception as exc:
            logger.error("Kill switch reconciliation is unknown: %s", exc)
            reconciliation = "UNKNOWN"

        if cancellation_status != "CONFIRMED":
            result = dict(cancellation)
            result["reconciliation"] = reconciliation
            return result
        if (
            reconciliation != "IN_SYNC"
            or not adapter.private_stream_healthy
            or not adapter.authenticated
        ):
            return {"status": "PARTIAL", "reconciliation": reconciliation}
        return {"status": "CONFIRMED", "remaining_orders": 0}
            
    async def trigger_reconciliation(self) -> str:
        if self.execution_adapter is not None:
            res = await self.execution_adapter.reconciliation.reconcile()
            self.reconciliation_status = res
            self._sync_adapter_state()
            if res == "IN_SYNC" and self._mainnet_launch_id:
                restore = getattr(self.persistence, "mark_mainnet_launch_reconciled", None)
                if callable(restore):
                    try:
                        await restore(self._mainnet_launch_id)
                        getter = getattr(self.persistence, "get_mainnet_launch_session", None)
                        if callable(getter):
                            self._set_mainnet_launch_session(
                                await getter(self._mainnet_launch_id)
                            )
                    except Exception as exc:
                        # A failed durable state transition must not clear a
                        # launch fence or claim autonomous authorization was
                        # restored.
                        logger.error(
                            "Launch reconciliation state update failed: %s",
                            type(exc).__name__,
                        )
            return res
            
        if self.execution_mode in {
            WorkerExecutionMode.TESTNET,
            WorkerExecutionMode.LIVE,
        }:
            self.authenticated = False
            self.private_stream_healthy = False
            self.reconciliation_status = "UNKNOWN"
            self.connection_state = "DISCONNECTED"
            return self.reconciliation_status
        else:
            self.reconciliation_status = "SIMULATED_SYNC"
            self.connection_state = "READY"
            # PAPER is a local simulation, not exchange authentication or a
            # private Binance stream. Never project simulated state as
            # Testnet evidence.
            self.private_stream_healthy = False
            self.authenticated = False
            return self.reconciliation_status

    def _validate_arm_request(self, req: ArmRequest) -> Optional[str]:
        if not req.instruments:
            return "ARM requires at least one instrument."
        if not any(req.strategies.model_dump().values()):
            return "ARM requires at least one enabled strategy."
        if req.executionMode == "TESTNET":
            supported = TestnetSafetyLimits.from_environment(BinanceEnvironment.TESTNET).allowed_symbols
        elif req.executionMode == "LIVE":
            supported = TestnetSafetyLimits.from_environment(BinanceEnvironment.MAINNET).allowed_symbols
        else:
            supported = {"BTCUSDT", "ETHUSDT", "ETHUSDC"}
        unsupported = sorted(set(req.instruments) - set(supported))
        if unsupported:
            return f"Unsupported instruments: {', '.join(unsupported)}"
        return None

    async def _reset_after_failed_exchange_arm(self) -> None:
        """Close a partially initialized adapter and clear failed ARM state."""
        # A failed LIVE arm may already have attached a Mainnet public stream.
        # Detach it before falling back to PAPER so stale exchange events cannot
        # continue feeding a disarmed runtime.
        await self._stop_public_market_stream()
        if self.execution_adapter is not None:
            try:
                await self.execution_adapter.close()
            except Exception as exc:
                logger.warning("Error closing failed exchange adapter: %s", exc)
            self.execution_adapter = None
        self.connection_state = "DISCONNECTED"
        self.execution_mode = WorkerExecutionMode.PAPER
        self.market_data_healthy = False
        self.private_stream_healthy = False
        self.authenticated = False
        self.reconciliation_status = "UNKNOWN"
        self.active_configuration = None
        self._set_mainnet_launch_session(None)
        self.pause_new_risk = False
        self.recovery_only = False
        self.risk_governor.hedge_mode = False
        self.risk_governor.max_leverage = Decimal("2.0")
        self.engine_state = WorkerEngineState.DISARMED

    async def _reset_after_failed_continuation(self) -> None:
        """Leave a LIVE worker disarmed after a failed continuation attempt."""

        logger.warning(
            "monitor_event=autonomous_continuation_failure launch_id=%s",
            self._mainnet_launch_id or "unknown",
        )
        await self._fence_autonomous_launch("failed_continuation")
        await self._stop_public_market_stream()
        if self.execution_adapter is not None:
            try:
                await self.execution_adapter.close()
            except Exception as exc:
                logger.warning(
                    "Error closing failed continuation adapter: %s",
                    type(exc).__name__,
                )
            self.execution_adapter = None
        self.execution_mode = WorkerExecutionMode.LIVE
        self.symbols = ["ETHUSDC"]
        self.connection_state = "DISCONNECTED"
        self.market_data_healthy = False
        self.private_stream_healthy = False
        self.authenticated = False
        self.reconciliation_status = "UNKNOWN"
        self.active_configuration = None
        self.pause_new_risk = False
        self.recovery_only = False
        self.risk_governor.hedge_mode = False
        self.risk_governor.max_leverage = Decimal("2.0")
        self.engine_state = WorkerEngineState.DISARMED

    async def _ensure_live_runtime_for_continuation(self) -> tuple[bool, str]:
        """Bootstrap a fresh Mainnet adapter after restart, without arming it.

        Cloud Run starts a new process without retaining the signed private
        stream, adapter, or execution lease.  Continuation therefore rebuilds
        those observations from fixed Mainnet configuration before the durable
        SQL transition can authorize risk-increasing decisions.
        """

        if not self._mainnet_configured():
            return False, "Mainnet credentials are missing from Secret Manager injection."
        if self.execution_mode != WorkerExecutionMode.LIVE:
            self.execution_mode = WorkerExecutionMode.LIVE
        self.symbols = ["ETHUSDC"]
        if not await self._restart_public_market_stream():
            await self._reset_after_failed_continuation()
            return False, "Runtime continuation preflight failed: public market data stream unavailable."

        self.engine_state = WorkerEngineState.ARMING
        exchange_environment = BinanceEnvironment.MAINNET
        api_key = os.getenv("BINANCE_MAINNET_API_KEY", "")
        api_secret = os.getenv("BINANCE_MAINNET_API_SECRET", "")
        self.risk_governor.max_leverage = TestnetSafetyLimits.from_environment(
            exchange_environment
        ).max_leverage
        try:
            if self.execution_adapter is not None:
                await self.execution_adapter.close()
                self.execution_adapter = None
            ledger_factory = getattr(self.persistence, "create_execution_ledger", None)
            if not callable(ledger_factory):
                raise RuntimeError("LIVE continuation requires a durable Mainnet ledger loader")
            durable_ledger = await ledger_factory(
                symbol="ETHUSDC",
                venue=environment_label(exchange_environment),
            )
            self.execution_adapter = BinanceExecutionAdapter(
                api_key=api_key,
                api_secret=api_secret,
                env=exchange_environment,
                ledger=durable_ledger,
            )
            if self.execution_adapter.ledger:
                self.execution_adapter.ledger.on_order_update = self.persistence.enqueue_order
                self.execution_adapter.ledger.on_fill_update = self.persistence.enqueue_fill
                self.execution_adapter.ledger.on_position_update = self.persistence.enqueue_position
            self.execution_adapter.before_order_submission = self._before_order_submission
            self.execution_adapter.on_order_submission_result = self._on_order_submission_result
            self.execution_adapter.bind_worker_authority(self)
            connected = await self.execution_adapter.connect()
            self._sync_adapter_state()
            if not connected or self.execution_adapter.connection_state != ConnectionState.READY:
                await self._reset_after_failed_continuation()
                return False, "Mainnet execution adapter did not reach READY."

            if getattr(self.execution_adapter, "execution_lease_required", False):
                raw_ttl = os.getenv("EXECUTION_LEASE_TTL_SECONDS", "10").strip()
                self._execution_lease_ttl_seconds = float(raw_ttl)
                if (
                    not math.isfinite(self._execution_lease_ttl_seconds)
                    or self._execution_lease_ttl_seconds <= 0
                ):
                    raise ValueError("invalid execution lease TTL")
                account_scope = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:24]
                scope_key = f"binance:{environment_label(exchange_environment)}:{account_scope}"
                lease = self.persistence.create_execution_lease(
                    scope_key,
                    owner_id=self._execution_lease_owner_id,
                    ttl_seconds=self._execution_lease_ttl_seconds,
                )
                self.execution_adapter.set_execution_lease(lease, required=True)
                if not await lease.acquire():
                    raise RuntimeError("another worker owns the account/environment execution lease")
                self._execution_lease_last_renewed_at = time.monotonic()

            self.risk_governor.hedge_mode = self.execution_adapter.capabilities.hedge_mode
            if not await self.execution_adapter.refresh_market_data(self.symbols):
                raise RuntimeError("fresh Mainnet market data is unavailable")
            self.last_market_event_at.update(self.execution_adapter.last_market_event_at)
            self.market_data_healthy = True
            preflight = self.get_preflight("LIVE")
            if not preflight.get("canArm"):
                failures = [
                    str(check.get("message", "unknown failure"))
                    for check in preflight.get("checks", [])
                    if check.get("status") == "FAIL"
                ]
                raise RuntimeError(
                    "Mainnet runtime preflight failed: " + "; ".join(failures[:8])
                )
            return True, ""
        except Exception as exc:
            logger.error(
                "Mainnet continuation runtime bootstrap failed: %s",
                type(exc).__name__,
            )
            await self._reset_after_failed_continuation()
            return False, "Mainnet continuation runtime is unavailable; execution remains disarmed."

    @staticmethod
    def _session_timestamp(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str) and value.strip():
            normalized = value.strip()
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                return None
            return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
        return None

    async def run_mainnet_continuation_readiness(
        self,
        *,
        require_active_runtime: bool = False,
        launch_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return sanitized evidence for continuation, never activate execution."""

        observed_at = utc_now()
        checks: list[dict[str, Any]] = []

        def add_check(check_id: str, name: str, passed: bool, message: str) -> None:
            checks.append(
                {
                    "id": check_id,
                    "name": name,
                    "required": True,
                    "status": "PASS" if passed else "FAIL",
                    "message": message,
                }
            )

        session: Optional[dict[str, Any]] = None
        try:
            getter = getattr(self.persistence, "get_mainnet_launch_session", None)
            if not callable(getter):
                raise RuntimeError("durable launch-session reader is unavailable")
            session = await getter(launch_id or self._mainnet_launch_id)
        except Exception as exc:
            logger.error("Continuation launch-session read failed: %s", type(exc).__name__)

        if session:
            self._set_mainnet_launch_session(session)

        policy = str(session.get("policy", "")) if session else ""
        state = str(session.get("state", "")) if session else ""
        submitted_orders = int(session.get("submitted_orders", 0) or 0) if session else 0
        reserved_orders = int(session.get("reserved_orders", 0) or 0) if session else 0
        expected_state = (
            policy == MAINNET_LAUNCH_STAGED and state == "PAUSED_NEW_RISK"
        ) or (
            policy == MAINNET_LAUNCH_AUTONOMOUS and state == "REAUTH_REQUIRED"
        )
        add_check(
            "CHK-CONTINUATION-SESSION",
            "Durable Paused Launch Session",
            bool(session and expected_state and submitted_orders >= 1 and reserved_orders >= submitted_orders),
            "Durable launch session contains the verified first-order pause"
            if session and expected_state and submitted_orders >= 1 and reserved_orders >= submitted_orders
            else "Launch session is missing, not paused, or has no durable first-order evidence",
        )
        lifecycle_safe = self.engine_state in {
            WorkerEngineState.PAUSED_NEW_RISK,
            WorkerEngineState.DISARMED,
        }
        add_check(
            "CHK-CONTINUATION-ENGINE",
            "Paused Worker Lifecycle",
            lifecycle_safe,
            "Worker is paused or disarmed before continuation"
            if lifecycle_safe
            else "Worker must be paused or disarmed before continuation",
        )
        add_check(
            "CHK-CONTINUATION-SYMBOL",
            "ETHUSDC Launch Scope",
            bool(session and str(session.get("symbol", "")).upper() == "ETHUSDC"),
            "Continuation is bounded to ETHUSDC"
            if session and str(session.get("symbol", "")).upper() == "ETHUSDC"
            else "Continuation scope is not ETHUSDC",
        )
        image_digest = os.getenv("WORKER_IMAGE_DIGEST", "").strip()
        add_check(
            "CHK-CONTINUATION-DIGEST",
            "Immutable Worker Digest",
            bool(
                session
                and re.fullmatch(r".+@sha256:[0-9a-fA-F]{64}", image_digest)
                and session.get("image_digest") == image_digest
            ),
            "Durable session is bound to this immutable Worker image"
            if session and session.get("image_digest") == image_digest
            else "Worker image digest is missing or differs from the launch session",
        )
        initial_approval = os.getenv("MAINNET_RELEASE_APPROVAL_ID", "").strip()
        approval_bound = bool(session and session.get("approval_id"))
        if initial_approval:
            approval_bound = approval_bound and session.get("approval_id") == initial_approval
        add_check(
            "CHK-CONTINUATION-INITIAL-APPROVAL",
            "Initial Release Approval Binding",
            approval_bound,
            "Continuation remains bound to the consumed initial release approval"
            if approval_bound
            else "Initial release approval binding is missing or mismatched",
        )
        add_check(
            "CHK-CONTINUATION-ENVIRONMENT",
            "LIVE Mainnet Environment",
            self.execution_mode == WorkerExecutionMode.LIVE
            and self._env_flag("MAINNET_LIVE_APPROVED", False),
            "Worker is configured for approved LIVE Mainnet continuation"
            if self.execution_mode == WorkerExecutionMode.LIVE
            and self._env_flag("MAINNET_LIVE_APPROVED", False)
            else "LIVE Mainnet approval is not active for this revision",
        )
        persistence = self.persistence.readiness()
        persistence_ready = (
            persistence.get("mode") == "REQUIRED"
            and persistence.get("durable") is True
        )
        add_check(
            "CHK-CONTINUATION-PERSISTENCE",
            "Required Durable Persistence",
            persistence_ready,
            "Required Cloud SQL persistence is durable"
            if persistence_ready
            else "Continuation requires durable REQUIRED persistence",
        )
        secret_versions = configured_secret_versions()
        secret_versions_ready = all(
            re.fullmatch(r"[1-9][0-9]*", value or "")
            for value in secret_versions.values()
        )
        add_check(
            "CHK-CONTINUATION-SECRET-VERSIONS",
            "Numeric Secret Manager Versions",
            secret_versions_ready,
            "Worker secret bindings expose only the approved numeric versions"
            if secret_versions_ready
            else "Worker secret version metadata is missing or not numeric",
        )

        try:
            preflight = await self.run_mainnet_read_only_preflight()
        except Exception as exc:
            logger.error("Continuation preflight failed: %s", type(exc).__name__)
            preflight = {
                "preflightPassed": False,
                "orderSubmissionAttempts": -1,
                "orderEndpointAttempts": -1,
                "checks": [],
                "observedAt": observed_at.isoformat(),
            }
        preflight_passed = bool(
            preflight.get("preflightPassed") is True
            and int(preflight.get("orderSubmissionAttempts", -1)) == 0
            and int(preflight.get("orderEndpointAttempts", -1)) == 0
        )
        add_check(
            "CHK-CONTINUATION-PREFLIGHT",
            "Fresh Read-only Mainnet Preflight",
            preflight_passed,
            "Read-only Mainnet preflight passed with zero order attempts"
            if preflight_passed
            else "Mainnet preflight failed, is stale, or reported an order attempt",
        )
        preflight_reconciliation = any(
            check.get("id") == "CHK-PREFLIGHT-RECONCILIATION"
            and check.get("status") == "PASS"
            for check in preflight.get("checks", [])
        )
        add_check(
            "CHK-CONTINUATION-RECONCILIATION",
            "First-order Reconciliation",
            preflight_reconciliation and self.reconciliation_status == "IN_SYNC",
            "Durable ledger and Mainnet account are IN_SYNC"
            if preflight_reconciliation and self.reconciliation_status == "IN_SYNC"
            else "First-order reconciliation is not verified as IN_SYNC",
        )

        if require_active_runtime:
            self._sync_adapter_state()
            active_runtime = bool(
                self.execution_adapter is not None
                and self.execution_adapter.env == BinanceEnvironment.MAINNET
                and self.execution_adapter.connection_state == ConnectionState.READY
                and self.authenticated
                and self.private_stream_healthy
                and self.reconciliation_status == "IN_SYNC"
                and self.is_market_data_fresh(["ETHUSDC"])
                and not self.kill_switch_active
            )
            add_check(
                "CHK-CONTINUATION-RUNTIME",
                "Fresh Private Runtime",
                active_runtime,
                "Mainnet adapter, private stream, market data, and kill switch are verified"
                if active_runtime
                else "Active Mainnet runtime is missing, stale, or unsafe",
            )

        continuation_ready = all(check["status"] == "PASS" for check in checks)
        return {
            "executionMode": "LIVE",
            "continuationOnly": True,
            "continuationReady": continuation_ready,
            "launchId": session.get("launch_id") if session else None,
            "launchPolicy": policy or None,
            "launchState": state or None,
            "mainnetLiveApproved": self._env_flag("MAINNET_LIVE_APPROVED", False),
            "engineState": self.engine_state.value,
            "workerImageDigest": os.getenv("WORKER_IMAGE_DIGEST", "").strip(),
            "workerRevision": configured_worker_revision(),
            "secretVersions": secret_versions,
            "submittedOrders": submitted_orders,
            "reservedOrders": reserved_orders,
            "persistenceDurable": persistence_ready,
            "preflightPassed": preflight.get("preflightPassed") is True,
            "preflightOrderSubmissionAttempts": int(preflight.get("orderSubmissionAttempts", -1)),
            "preflightOrderEndpointAttempts": int(preflight.get("orderEndpointAttempts", -1)),
            "orderSubmissionAttempts": int(preflight.get("orderSubmissionAttempts", -1)),
            "orderEndpointAttempts": int(preflight.get("orderEndpointAttempts", -1)),
            "preflight": preflight,
            "checks": checks,
            "observedAt": observed_at.isoformat(),
        }

    async def continue_autonomous(self, config: ContinuationRequest | dict) -> tuple[bool, str]:
        """Consume a continuation approval and activate autonomous Mainnet."""

        if self.kill_switch_active:
            return False, "Cannot continue: Kill switch is active"
        try:
            req = config if isinstance(config, ContinuationRequest) else ContinuationRequest.model_validate(config)
        except ValidationError as exc:
            return False, f"Invalid continuation request: {exc.errors()[0].get('msg', str(exc))}"
        if req.instruments != ["ETHUSDC"]:
            return False, "Autonomous continuation is bounded to exactly ETHUSDC"
        if not req.enforcePreflight:
            return False, "Autonomous continuation requires enforcePreflight=true"
        if not self._env_flag("MAINNET_LIVE_APPROVED", False):
            return False, "Mainnet remains disarmed until MAINNET_LIVE_APPROVED=true is set by the release gate."
        try:
            self.persistence.validate_execution_mode("LIVE")
            if not self.persistence.readiness().get("durable"):
                return False, "Required persistence is not ready; autonomous continuation is blocked."
        except Exception as exc:
            return False, f"Autonomous continuation persistence gate failed: {type(exc).__name__}"

        getter = getattr(self.persistence, "get_mainnet_launch_session", None)
        if not callable(getter):
            return False, "Durable launch-session reader is unavailable"
        try:
            session = await getter(req.launchId)
        except Exception as exc:
            return False, f"Durable launch-session read failed: {type(exc).__name__}"
        if not session:
            return False, "Continuation launch session was not found"
        self._set_mainnet_launch_session(session)
        policy = str(session.get("policy", ""))
        state = str(session.get("state", ""))
        if not (
            (policy == MAINNET_LAUNCH_STAGED and state == "PAUSED_NEW_RISK")
            or (policy == MAINNET_LAUNCH_AUTONOMOUS and state == "REAUTH_REQUIRED")
        ):
            return False, "Launch session is not awaiting a verified continuation"
        if int(session.get("submitted_orders", 0) or 0) < 1:
            return False, "Continuation requires durable first-order evidence"
        image_digest = os.getenv("WORKER_IMAGE_DIGEST", "").strip()
        if not re.fullmatch(r".+@sha256:[0-9a-fA-F]{64}", image_digest):
            return False, "Autonomous continuation requires an immutable WORKER_IMAGE_DIGEST"
        if session.get("image_digest") != image_digest:
            return False, "Continuation approval does not match the current Worker image"
        initial_approval = os.getenv("MAINNET_RELEASE_APPROVAL_ID", "").strip()
        if initial_approval and session.get("approval_id") != initial_approval:
            return False, "Continuation is not bound to the current initial release approval"
        if req.initialApprovalId and req.initialApprovalId != session.get("approval_id"):
            return False, "Continuation initial approval does not match the durable launch session"

        if not (
            self.execution_adapter is not None
            and self.execution_adapter.env == BinanceEnvironment.MAINNET
            and self.execution_adapter.connection_state == ConnectionState.READY
            and self.authenticated
            and self.private_stream_healthy
        ):
            prepared, message = await self._ensure_live_runtime_for_continuation()
            if not prepared:
                return False, message

        evidence = await self.run_mainnet_continuation_readiness(require_active_runtime=True)
        if not evidence.get("continuationReady"):
            await self._reset_after_failed_continuation()
            failures = [
                str(check.get("message", "unknown failure"))
                for check in evidence.get("checks", [])
                if check.get("status") == "FAIL"
            ]
            return False, "Autonomous continuation readiness failed: " + "; ".join(failures[:8])

        activator = getattr(self.persistence, "activate_mainnet_autonomous", None)
        if not callable(activator):
            await self._reset_after_failed_continuation()
            return False, "Durable autonomous continuation transition is unavailable"
        verified_at = self._session_timestamp(session.get("first_order_verified_at")) or utc_now()
        try:
            activated = await activator(
                launch_id=req.launchId,
                continuation_approval_id=req.continuationApprovalId,
                first_order_verified_at=verified_at,
                image_digest=image_digest,
            )
        except Exception as exc:
            logger.error("Autonomous continuation transaction failed: %s", type(exc).__name__)
            activated = None
        if not activated:
            await self._reset_after_failed_continuation()
            return False, "Autonomous continuation transaction was rejected; execution remains disarmed"

        self._set_mainnet_launch_session(activated)
        self.active_configuration = {
            **req.model_dump(),
            "launchPolicy": MAINNET_LAUNCH_AUTONOMOUS,
            "initialApprovalId": session.get("approval_id"),
        }
        self.pause_new_risk = False
        self.recovery_only = False
        self._refresh_engine_state()
        if self.engine_state != WorkerEngineState.ARMED:
            await self._reset_after_failed_continuation()
            return False, "Autonomous continuation did not reach the ARMED runtime state"
        logger.warning(
            "monitor_event=autonomous_continuation_activated launch_id=%s",
            req.launchId,
        )
        return True, ""

    async def arm(self, config: ArmRequest | dict):
        if self.kill_switch_active:
            return False, "Cannot arm: Kill switch is active"

        try:
            req = config if isinstance(config, ArmRequest) else ArmRequest.model_validate(config)
        except ValidationError as exc:
            return False, f"Invalid ARM request: {exc.errors()[0].get('msg', str(exc))}"

        mode = req.executionMode
        if mode == "TESTNET" and not self._testnet_configured():
            return False, "Configuration Preflight Failed: Testnet credentials or BINANCE_TESTNET=true missing."
        if mode == "LIVE":
            if not self._mainnet_configured():
                return False, "Configuration Preflight Failed: Mainnet credentials are missing from Secret Manager injection."
            if not self._env_flag("MAINNET_LIVE_APPROVED", False):
                return False, "Mainnet remains disarmed until MAINNET_LIVE_APPROVED=true is set by the release gate."
            if not req.enforcePreflight:
                return False, "LIVE ARM requires enforcePreflight=true and a fresh read-only Mainnet preflight."
            if req.launchPolicy != MAINNET_LAUNCH_STAGED:
                return False, "LIVE ARM requires the STAGED_FIRST_ORDER launch policy."
            release_approval_id = (
                (req.releaseApprovalId or os.getenv("MAINNET_RELEASE_APPROVAL_ID", "")).strip()
            )
            if not re.fullmatch(r"approval-[A-Za-z0-9-]{16,120}", release_approval_id):
                return False, "LIVE ARM requires a consumed release approval identifier."
        else:
            release_approval_id = ""

        validation_error = self._validate_arm_request(req)
        if validation_error:
            return False, validation_error
        try:
            self.persistence.validate_execution_mode(req.executionMode)
        except RuntimeError as exc:
            return False, str(exc)

        if (
            self.persistence.mode.value == "REQUIRED"
            and not self.persistence.readiness()["durable"]
        ):
            return False, (
                "Required persistence is not ready; execution is blocked "
                "until the transactional outbox is durable."
            )

        # This observation is intentionally performed before the Worker
        # changes mode, starts its persistent adapter, or acquires an
        # execution lease. It must pass without changing the lifecycle.
        if mode == "LIVE":
            try:
                preflight = await self.run_mainnet_read_only_preflight()
            except Exception as exc:
                return False, f"Mainnet read-only preflight failed: {type(exc).__name__}"
            if not (
                preflight.get("preflightPassed") is True
                and int(preflight.get("orderSubmissionAttempts", -1)) == 0
                and int(preflight.get("orderEndpointAttempts", -1)) == 0
            ):
                failures = [
                    str(check.get("message", "unknown failure"))
                    for check in preflight.get("checks", [])
                    if check.get("status") == "FAIL"
                ]
                return False, "LIVE read-only preflight failed: " + "; ".join(failures[:8])

        self.symbols = list(req.instruments)
        self.execution_mode = WorkerExecutionMode(mode)
        if mode in {"TESTNET", "LIVE"}:
            if not await self._restart_public_market_stream():
                await self._reset_after_failed_exchange_arm()
                return False, "Runtime preflight failed: public market data stream unavailable."
        else:
            await self._stop_public_market_stream()

        if mode in {"TESTNET", "LIVE"}:
            self.engine_state = WorkerEngineState.ARMING
            exchange_environment = (
                BinanceEnvironment.MAINNET
                if mode == "LIVE"
                else BinanceEnvironment.TESTNET
            )
            if mode == "LIVE":
                api_key = os.getenv("BINANCE_MAINNET_API_KEY", "")
                api_secret = os.getenv("BINANCE_MAINNET_API_SECRET", "")
                self.risk_governor.max_leverage = TestnetSafetyLimits.from_environment(
                    exchange_environment
                ).max_leverage
            else:
                api_key = os.getenv("BINANCE_TESTNET_API_KEY", "")
                api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "")
                self.risk_governor.max_leverage = TestnetSafetyLimits.from_environment(
                    exchange_environment
                ).max_leverage

            try:
                if (
                    self.execution_adapter is not None
                    and self.execution_adapter.env != exchange_environment
                ):
                    await self.execution_adapter.close()
                    self.execution_adapter = None
                # LIVE must always start from the scoped Cloud SQL ledger
                # loaded for BINANCE_MAINNET. Reusing a process-local or
                # Testnet ledger would make restart/reconciliation evidence
                # non-authoritative.
                if mode == "LIVE" and self.execution_adapter is not None:
                    await self.execution_adapter.close()
                    self.execution_adapter = None
                if self.execution_adapter is None:
                    durable_ledger = None
                    if mode == "LIVE":
                        ledger_factory = getattr(
                            self.persistence, "create_execution_ledger", None
                        )
                        if not callable(ledger_factory):
                            raise RuntimeError(
                                "LIVE execution requires a durable Mainnet ledger loader"
                            )
                        durable_ledger = await ledger_factory(
                            symbol="ETHUSDC",
                            venue=environment_label(exchange_environment),
                        )
                    self.execution_adapter = BinanceExecutionAdapter(
                        api_key=api_key,
                        api_secret=api_secret,
                        env=exchange_environment,
                        ledger=durable_ledger,
                    )

                # Bind persistence callbacks on every arm. This also repairs
                # an adapter that was retained while switching between repeated
                # arms in the same environment.
                if self.execution_adapter.ledger:
                    self.execution_adapter.ledger.on_order_update = self.persistence.enqueue_order
                    self.execution_adapter.ledger.on_fill_update = self.persistence.enqueue_fill
                    self.execution_adapter.ledger.on_position_update = self.persistence.enqueue_position
                # Risk-increasing exchange mutations must have a durable
                # transactional-outbox acknowledgement before REST POST.
                self.execution_adapter.before_order_submission = (
                    self._before_order_submission
                )
                self.execution_adapter.on_order_submission_result = (
                    self._on_order_submission_result
                )
                
                self.execution_adapter.bind_worker_authority(self)
                connected = await self.execution_adapter.connect()
                self._sync_adapter_state()
                if not connected or self.execution_adapter.connection_state != ConnectionState.READY:
                    failed_state = self.connection_state
                    await self._reset_after_failed_exchange_arm()
                    return False, f"Failed to initialize {mode} Execution Adapter. State: {failed_state}"
                if self.execution_adapter.execution_lease_required:
                    try:
                        raw_ttl = os.getenv("EXECUTION_LEASE_TTL_SECONDS", "10").strip()
                        self._execution_lease_ttl_seconds = float(raw_ttl)
                        if not math.isfinite(self._execution_lease_ttl_seconds) or self._execution_lease_ttl_seconds <= 0:
                            raise ValueError
                        account_scope = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:24]
                        scope_key = f"binance:{environment_label(exchange_environment)}:{account_scope}"
                        lease = self.persistence.create_execution_lease(
                            scope_key,
                            owner_id=self._execution_lease_owner_id,
                            ttl_seconds=self._execution_lease_ttl_seconds,
                        )
                        self.execution_adapter.set_execution_lease(lease, required=True)
                        if not await lease.acquire():
                            raise RuntimeError("another worker owns the account/environment execution lease")
                        self._execution_lease_last_renewed_at = time.monotonic()
                    except Exception as exc:
                        logger.error("Execution lease acquisition failed: %s", type(exc).__name__)
                        await self._reset_after_failed_exchange_arm()
                        return False, "Execution lease is unavailable; exchange execution remains disarmed."
                self.risk_governor.hedge_mode = self.execution_adapter.capabilities.hedge_mode
                if not await self.execution_adapter.refresh_market_data(self.symbols):
                    await self._reset_after_failed_exchange_arm()
                    return False, "Runtime preflight failed: fresh market data unavailable."
                self.last_market_event_at.update(self.execution_adapter.last_market_event_at)
                self.market_data_healthy = True
            except Exception as exc:
                logger.error("Error connecting %s Execution Adapter: %s", mode, exc)
                await self._reset_after_failed_exchange_arm()
                return False, f"Adapter connection failed: {exc}"

            # Runtime Preflight
            preflight = self.get_preflight(mode)
            if not preflight["canArm"]:
                failures = [c["message"] for c in preflight["checks"] if c["status"] == "FAIL"]
                await self._reset_after_failed_exchange_arm()
                return False, f"{mode} runtime preflight failed: {'; '.join(failures)}"

            if mode == "LIVE":
                image_digest = os.getenv("WORKER_IMAGE_DIGEST", "").strip()
                if not re.fullmatch(r".+@sha256:[0-9a-fA-F]{64}", image_digest):
                    await self._reset_after_failed_exchange_arm()
                    return False, "LIVE ARM requires the immutable WORKER_IMAGE_DIGEST release input."
                try:
                    session = await self.persistence.create_mainnet_launch_session(
                        approval_id=release_approval_id,
                        image_digest=image_digest,
                        symbol="ETHUSDC",
                    )
                except Exception as exc:
                    logger.error("Mainnet staged launch session unavailable: %s", type(exc).__name__)
                    await self._reset_after_failed_exchange_arm()
                    return False, "LIVE staged launch session is unavailable; execution remains disarmed."
                if (
                    str(session.get("state", "")) != "ACTIVE"
                    or int(session.get("reserved_orders", 0) or 0) != 0
                    or int(session.get("submitted_orders", 0) or 0) != 0
                ):
                    await self._reset_after_failed_exchange_arm()
                    return False, "LIVE staged launch session is already used or requires reconciliation."
                self._set_mainnet_launch_session(dict(session))

            self.engine_state = WorkerEngineState.ARMED
            self.active_configuration = req.model_dump()
            logger.info("Worker ARMED in %s mode", mode)
            return True, ""
        else:
            # Switching from Testnet back to Paper must tear down the previous
            # exchange adapter first. Otherwise stale signed/account/stream
            # state could be projected into a Paper runtime.
            if self.execution_adapter is not None:
                try:
                    await self.execution_adapter.close()
                except Exception as exc:
                    logger.warning(
                        "Error closing exchange adapter before Paper ARM: %s",
                        type(exc).__name__,
                    )
                self.execution_adapter = None
            self.connection_state = "DISCONNECTED"
            self.market_data_healthy = False
            self.private_stream_healthy = False
            self.authenticated = False
            self.reconciliation_status = "UNKNOWN"
            self.last_market_event_at.clear()
            self.risk_governor.hedge_mode = False
            self.engine_state = WorkerEngineState.ARMED
            self.active_configuration = req.model_dump()
            logger.info("Worker ARMED in PAPER mode")
            return True, ""

    async def disarm(self):
        # A manual DISARM is also an authorization boundary.  If autonomous
        # Mainnet was active, fence its durable launch session before tearing
        # down the local adapter so the same process cannot resume risk without
        # a fresh continuation approval.  The local DISARM remains fail-closed
        # even if the database is temporarily unavailable.
        await self._fence_autonomous_launch("disarm")

        if self.execution_adapter is not None:
            try:
                await self.execution_adapter.close()
            except Exception as e:
                logger.warning("Error closing execution adapter on disarm: %s", e)
            self.execution_adapter = None
        self.connection_state = "DISCONNECTED"
        self.market_data_healthy = False
        self.private_stream_healthy = False
        self.authenticated = False
        self.reconciliation_status = "DISCONNECTED"
        self.active_configuration = None
        self._mainnet_launch_id = None
        self.pause_new_risk = False
        self.recovery_only = False
        self.risk_governor.hedge_mode = False
        self.engine_state = (
            WorkerEngineState.EMERGENCY
            if self.kill_switch_active
            else WorkerEngineState.DISARMED
        )
        logger.info("Worker DISARMED")

    def _evaluate_execution_gate(self, decision) -> tuple[bool, str]:
        result = self.decision_execution_gate.check(decision)
        return result.allowed, result.reason

    async def _fail_closed_after_autonomous_execution_error(self, error: Exception) -> dict:
        """Stop autonomous mutation after an unexpected execution failure.

        A failed await can mean either a definitive rejection or an unknown
        exchange outcome. The worker therefore invalidates its local
        observations, activates the local kill switch, and performs the same
        best-effort authoritative cancellation/reconciliation workflow used by
        the operator kill-switch endpoint. The local block remains active
        regardless of whether the exchange can be reached.
        """

        self.pause_new_risk = True
        self.connection_state = ConnectionState.DEGRADED.value
        self.reconciliation_status = "UNKNOWN"
        adapter = self.execution_adapter
        if adapter is not None:
            adapter.state = ConnectionState.DEGRADED
            adapter.reconciliation.last_status = "UNKNOWN"
            try:
                await adapter.ledger.set_account_snapshot(None)
            except Exception as snapshot_error:
                logger.warning(
                    "Unable to invalidate account snapshot after autonomous failure: %s",
                    snapshot_error,
                )

        try:
            result = await self.set_kill_switch(True)
        except Exception as kill_switch_error:
            # set_kill_switch sets the local flag before any exchange call, but
            # preserve that invariant even if its own control path fails.
            self.kill_switch_active = True
            result = {
                "status": "UNKNOWN",
                "reason": f"Autonomous {self._current_exchange_label()} execution failed and kill-switch verification is unknown.",
            }
            logger.error(
                "Unable to complete autonomous-failure kill-switch workflow: %s",
                kill_switch_error,
            )

        self.kill_switch_active = True
        self._refresh_engine_state()
        logger.error(
            "Autonomous %s execution failed; local kill switch is ACTIVE: %s; result=%s",
            self._current_exchange_label(),
            error,
            result,
        )
        return result

    async def execute_manual_decision(self, decision):
        """Worker-owned manual Testnet path used by the controlled trial only."""
        allowed, reason = self._evaluate_execution_gate(decision)
        if not allowed or self.execution_adapter is None:
            raise RuntimeError(f"Decision execution gate blocked manual order: {reason}")
        adapter = self.execution_adapter
        adapter.bind_worker_authority(self)
        executed = await adapter.execute_decision(decision, authority=self)
        # The adapter deliberately contains most exchange errors so it can
        # classify definitive rejections versus ambiguity. A returned list is
        # not sufficient evidence of safety: a degraded adapter or unresolved
        # reconciliation must still enter the worker's kill-switch workflow.
        if (
            adapter.connection_state != ConnectionState.READY
            or getattr(adapter.reconciliation, "last_status", "UNKNOWN") != "IN_SYNC"
        ):
            await self._fail_closed_after_autonomous_execution_error(
                RuntimeError("Testnet execution did not finish READY and IN_SYNC")
            )
        return executed

    async def amend_testnet_order(
        self,
        symbol: str,
        client_order_id: str,
        new_price: Decimal,
        new_qty: Decimal,
        side: str,
    ):
        """Worker-owned wrapper for the verified Testnet LIMIT amendment path."""
        if self.execution_mode not in {
            WorkerExecutionMode.TESTNET,
            WorkerExecutionMode.LIVE,
        } or self.execution_adapter is None:
            return None
        self.execution_adapter.bind_worker_authority(self)
        return await self.execution_adapter.modify_order(
            symbol,
            client_order_id,
            new_price,
            new_qty,
            side,
            authority=self,
        )

    async def cancel_testnet_order(self, symbol: str, client_order_id: str) -> bool:
        """Worker-owned wrapper for the verified Testnet cancellation path."""
        if self.execution_mode not in {
            WorkerExecutionMode.TESTNET,
            WorkerExecutionMode.LIVE,
        } or self.execution_adapter is None:
            return False
        self.execution_adapter.bind_worker_authority(self)
        return await self.execution_adapter.cancel_order(
            symbol,
            client_order_id,
            authority=self,
        )

    async def emergency_flatten(self, symbol: Optional[str] = None):
        """Route an explicitly emergency, Testnet-only flatten through the worker."""
        if self.execution_mode not in {
            WorkerExecutionMode.TESTNET,
            WorkerExecutionMode.LIVE,
        } or self.execution_adapter is None:
            raise RuntimeError("Emergency flatten is available only for an active Testnet adapter")
        self.pause_new_risk = True
        self._refresh_engine_state()
        self.execution_adapter.bind_worker_authority(self)
        return await self.execution_adapter.emergency_flatten(symbol, authority=self)

    async def handle_market_event(self, event: MarketEvent):
        if not hasattr(self, "last_market_event_at"):
            self.last_market_event_at = {}
        event_timestamp = event.event_time
        if event_timestamp.tzinfo is None:
            logger.warning("Ignoring market event with a naive timestamp for %s", event.symbol)
            return
        symbol = str(event.symbol).upper()
        if symbol != event.symbol:
            event = event.model_copy(update={"symbol": symbol})
        if self.execution_mode in {
            WorkerExecutionMode.TESTNET,
            WorkerExecutionMode.LIVE,
        } and self.execution_adapter is not None:
            if not self.execution_adapter.record_market_event(event):
                logger.warning(
                    "Ignoring invalid %s market event for %s",
                    self._current_exchange_label(),
                    event.symbol,
                )
                return
        self.last_market_event_at[symbol] = event_timestamp
        if symbol in self._active_instruments():
            self.market_data_healthy = True
            
        pa_state = self.pa_engine.process_event(event)
        if not pa_state:
            return
            
        market_state = self.market_state_engine.classify(pa_state)

        enabled_strategies = self._enabled_strategies()
        grid_depth = (
            await self._observed_grid_depth(event.symbol)
            if "grid" in enabled_strategies
            else 0
        )
        grid_intent = (
            self.grid_engine.evaluate(pa_state, market_state, grid_depth=grid_depth)
            if "grid" in enabled_strategies
            else None
        )
        trend_intent = (
            self.trend_engine.evaluate(pa_state, market_state)
            if "trend" in enabled_strategies
            else None
        )
        shock_intent = (
            self.shock_engine.evaluate(pa_state, market_state)
            if "shock" in enabled_strategies
            else None
        )
        carry_intent = (
            self.carry_engine.evaluate(event, market_state)
            if "carry" in enabled_strategies
            else None
        )
        
        intents = [i for i in [grid_intent, trend_intent, shock_intent, carry_intent] if i]
        
        # Real or simulated RiskSnapshot
        if self.execution_mode in {
            WorkerExecutionMode.TESTNET,
            WorkerExecutionMode.LIVE,
        }:
            if self.execution_adapter is None:
                logger.error("%s risk evaluation has no execution adapter", self._current_exchange_label())
                self.connection_state = "DISCONNECTED"
                self.private_stream_healthy = False
                self.authenticated = False
                self.reconciliation_status = "UNKNOWN"
                self.pause_new_risk = True
                self._refresh_engine_state()
                return
            try:
                snapshot = await self.execution_adapter.ledger.get_account_snapshot()
                if (
                    snapshot is None
                    or not getattr(snapshot, "valid", False)
                    or getattr(snapshot, "exchange_environment", None)
                    != self._current_exchange_label()
                    or not self.is_account_snapshot_ready()
                ):
                    logger.error("No account snapshot available from execution adapter")
                    self.connection_state = "DEGRADED"
                    self.pause_new_risk = True
                    self._refresh_engine_state()
                    return # Block execution if no account truth
                    
                # Binance totalMarginBalance is the authoritative USDⓈ-M
                # equity field. Do not recompute it from wallet balance plus
                # PnL and risk double-counting account adjustments.
                equity = snapshot.margin_balance
                
                # Drawdown tracking
                if self.session_start_equity is None:
                    self.session_start_equity = equity
                if self.session_peak_equity is None or equity > self.session_peak_equity:
                    self.session_peak_equity = equity
                    
                if self.session_peak_equity > Decimal("0"):
                    drawdown_pct = ((self.session_peak_equity - equity) / self.session_peak_equity) * Decimal("100.0")
                else:
                    drawdown_pct = Decimal("0.0")

                risk_snapshot = RiskSnapshot(
                    portfolio_equity=equity,
                    unrealized_pnl=snapshot.unrealized_pnl,
                    realized_pnl_24h=(
                        snapshot.daily_realized_pnl
                        if snapshot.daily_loss_known
                        and snapshot.daily_realized_pnl is not None
                        else Decimal("0.0")
                    ),
                    margin_utilization_pct=snapshot.margin_utilization_pct,
                    effective_leverage=snapshot.effective_leverage,
                    current_drawdown_pct=max(Decimal("0.0"), drawdown_pct),
                    liquidation_distance_pct=snapshot.min_liquidation_distance_pct,
                    risk_state=self._derive_testnet_risk_state(
                        snapshot,
                        max(Decimal("0.0"), drawdown_pct),
                    ),
                    realized_pnl_24h_known=bool(snapshot.daily_loss_known),
                )
                self.persistence.enqueue_risk_snapshot(risk_snapshot)
                
                positions = await self.execution_adapter.ledger.get_positions()
                normalized_event_symbol = str(event.symbol).upper()
                symbol_positions = [
                    position
                    for position in positions
                    if str(position.symbol).upper() == normalized_event_symbol
                ]
                # positionAmt is signed.  Sum all legs so Hedge Mode cannot
                # accidentally use whichever LONG/SHORT row happens to appear
                # first.  The conservative gross check prevents a flat net
                # value from hiding simultaneous opposing exposure.
                current_position_qty = sum(
                    (position.quantity for position in symbol_positions),
                    Decimal("0.0"),
                )
                if self.execution_adapter.capabilities.hedge_mode:
                    gross_qty = sum(
                        (abs(position.quantity) for position in symbol_positions),
                        Decimal("0.0"),
                    )
                    if gross_qty != abs(current_position_qty):
                        logger.warning(
                            "Hedge Mode has opposing %s legs; blocking autonomous decision until explicitly reconciled",
                            normalized_event_symbol,
                        )
                        return
                
            except Exception as e:
                logger.error("Error extracting %s risk snapshot: %s", self._current_exchange_label(), e)
                self.connection_state = "DEGRADED"
                self.pause_new_risk = True
                self._refresh_engine_state()
                return # Block execution without fake fallback
        else:
            # Paper mode is an explicit simulation boundary.  Keep its
            # account fixture local to Paper and start flat; no simulated
            # position may be mistaken for exchange inventory or readiness.
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
        
        try:
            raw_target_exposure = self.meta_allocator.allocate(intents, event.symbol)
        except (TypeError, ValueError) as exc:
            # A malformed or conflicting strategy intent must stop this
            # decision before it can become a TargetExposure.  On Testnet,
            # keep the worker in pause-new-risk until an operator explicitly
            # clears the condition; reductions remain available at the gates.
            logger.error("Strategy intent pipeline rejected the event: %s", exc)
            if self.execution_mode in {
                WorkerExecutionMode.TESTNET,
                WorkerExecutionMode.LIVE,
            }:
                self.pause_new_risk = True
                self._refresh_engine_state()
            return
        
        target_exposure = self.recovery_engine.process(
            target=raw_target_exposure,
            risk=risk_snapshot,
            current_position_qty=current_position_qty
        )
        
        decision = self.risk_governor.evaluate(target_exposure, risk_snapshot, current_position_qty=current_position_qty)
        
        if decision.action != "NOOP":
            if self.engine_state in EXECUTABLE_ENGINE_STATES:
                if self.execution_mode in {
                    WorkerExecutionMode.TESTNET,
                    WorkerExecutionMode.LIVE,
                } and self.execution_adapter is not None:
                    launch_readiness = self.get_launch_readiness()
                    if self.execution_mode == WorkerExecutionMode.LIVE:
                        autonomous_enabled = self._env_flag("MAINNET_LIVE_APPROVED", False)
                        readiness_key = "mainnet_autonomous_ready"
                        environment_name = "MAINNET"
                    else:
                        autonomous_enabled = self._env_flag(
                            "AUTONOMOUS_TESTNET_EXECUTION", False
                        )
                        readiness_key = (
                            "testnet_autonomous_soak_ready"
                            if self._env_flag("AUTONOMOUS_TESTNET_SOAK_APPROVED", False)
                            else "testnet_autonomous_ready"
                        )
                        environment_name = "TESTNET"
                    if autonomous_enabled and launch_readiness[readiness_key]:
                        is_safe, reason = self._evaluate_execution_gate(decision)
                        if is_safe:
                            logger.info(
                                "[%s][AUTONOMOUS_EXEC] Executing decision %s for %s",
                                environment_name,
                                decision.decision_id,
                                decision.symbol,
                            )
                            try:
                                await self.execute_manual_decision(decision)
                            except Exception as exc:
                                await self._fail_closed_after_autonomous_execution_error(exc)
                        else:
                            logger.info(
                                "[%s][EXECUTION_BLOCKED] Decision %s blocked: %s",
                                environment_name,
                                decision.decision_id,
                                reason,
                            )
                    else:
                        logger.info(
                            "[%s][MONITOR_ONLY] Decision %s for %s "
                            "(deployment approval, current-build evidence, and "
                            "runtime readiness are all required)",
                            environment_name,
                            decision.decision_id,
                            decision.symbol,
                        )
                else:
                    logger.info(f"[{self.execution_mode.value}][SIMULATED] EXECUTION DECISION: {decision.symbol} | Action: {decision.action}")

        # Returning the worker-owned decision is an observability hook for the
        # supervised Testnet runner. Existing WebSocket callers ignore the
        # return value, while the runner can prove that the strategy/risk path
        # actually processed market events without creating a second authority.
        return decision

    async def start(self):
        logger.info("Initializing Blessing AI Trading Worker v0.2...")
        
        try:
            persistence_started = await self.persistence.start()
        except Exception as exc:
            # Keep the HTTP control/readiness surface alive so Cloud Run can
            # report a truthful 503 instead of turning a DB outage into an
            # apparently healthy replacement process. Risk-increasing gates
            # remain closed while REQUIRED persistence is unavailable.
            persistence_started = False
            logger.error("Required persistence startup failed (%s)", type(exc).__name__)
        if not persistence_started:
            logger.warning(
                "Persistence is unavailable; worker remains explicitly degraded and risk-increasing execution stays blocked until the configured mode is ready."
            )
        elif self.persistence.mode.value == "REQUIRED":
            # A new process must fence any previous autonomous session before
            # it can expose a LIVE runtime. Counters remain in SQL; only the
            # authorization state is moved to REAUTH_REQUIRED.
            try:
                await self.persistence.mark_mainnet_launches_reauth_required()
                session = await self.persistence.get_mainnet_launch_session()
                self._set_mainnet_launch_session(session)
            except Exception as exc:
                logger.error(
                    "Mainnet launch restart fencing failed: %s",
                    type(exc).__name__,
                )
                self._set_mainnet_launch_session(None)
        
        configured_mode = str(os.getenv("EXECUTION_MODE", "PAPER")).strip().upper()
        if configured_mode not in {"PAPER", "TESTNET", "LIVE"}:
            logger.error(
                "Unsupported EXECUTION_MODE=%s; keeping the worker in PAPER mode",
                configured_mode,
            )
            configured_mode = "PAPER"
        self.execution_mode = WorkerExecutionMode(configured_mode)
        self.symbols = await self._resolve_startup_symbols(configured_mode)
        if not self.symbols:
            self.market_data_healthy = False
            logger.error("No safe startup symbols resolved for EXECUTION_MODE=%s", configured_mode)
            self.start_heartbeat()
            self.scan_task = asyncio.create_task(self._periodic_scanner())
            return

        # PAPER is a local simulation boundary. Do not attach an exchange
        # market stream while reporting simulated provenance.
        if configured_mode == "PAPER":
            self.market_data_healthy = False
            self.start_heartbeat()
            self.scan_task = asyncio.create_task(self._periodic_scanner())
            return

        stream_environment = (
            BinanceEnvironment.MAINNET
            if configured_mode == "LIVE"
            else BinanceEnvironment.TESTNET
        )
        stream_venue = environment_label(stream_environment)
        logger.info(
            "Connecting to Binance %s public WS for: %s",
            stream_environment.value,
            self.symbols,
        )
        self.ws_client = BinancePublicWebSocket(
            symbols=self.symbols,
            base_ws_url=get_ws_url(stream_environment),
            event_callback=self.handle_market_event,
            venue=stream_venue,
        )
        if not await self.ws_client.start():
            logger.error("Binance %s public market stream could not be started", stream_environment.value)
        self.start_heartbeat()
        self.scan_task = asyncio.create_task(self._periodic_scanner())
        
        # We don't sleep forever here, we just start tasks.
        
    async def _heartbeat_loop(self):
        """Background task periodically updating heartbeat_at for Control Plane liveness monitoring."""
        while self.is_running:
            self.record_heartbeat()
            adapter = self.execution_adapter
            lease = getattr(adapter, "execution_lease", None) if adapter else None
            if lease is not None and time.monotonic() - self._execution_lease_last_renewed_at >= max(
                0.5, self._execution_lease_ttl_seconds / 3
            ):
                try:
                    if not await lease.renew():
                        adapter.state = ConnectionState.DEGRADED
                        adapter.reconciliation.last_status = "UNKNOWN"
                        logger.error("Execution lease renewal failed; worker is degraded")
                    self._execution_lease_last_renewed_at = time.monotonic()
                except Exception as exc:
                    adapter.state = ConnectionState.DEGRADED
                    adapter.reconciliation.last_status = "UNKNOWN"
                    logger.error("Execution lease renewal failed: %s", type(exc).__name__)
            try:
                await asyncio.sleep(self.heartbeat_interval_sec)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat loop error: %s", e)

    async def _periodic_scanner(self):
        while self.is_running:
            await asyncio.sleep(self.scanner.refresh_interval_sec)
            if self.execution_mode in {
                WorkerExecutionMode.TESTNET,
                WorkerExecutionMode.LIVE,
            }:
                # Exchange launch instruments are an explicit, bounded
                # configuration.  The research scanner must not expand them.
                continue
            try:
                new_symbols = await self.scanner.scan_active_symbols()
                if set(new_symbols) != set(self.symbols):
                    self.symbols = new_symbols
            except Exception as e:
                logger.error("Periodic scanner failed: %s", e)

    async def stop(self):
        logger.info("Gracefully stopping Trading Worker...")
        self.is_running = False
        self.stop_heartbeat()
        if self.scan_task and not self.scan_task.done():
            self.scan_task.cancel()
        if self.ws_client:
            try:
                await self.ws_client.stop()
            except RuntimeError:
                pass
        await self.persistence.stop()

def serve_api(app_instance):
    set_worker_engine(app_instance)
    try:
        port = int(os.getenv("PORT", "8080"))
    except ValueError:
        port = 8080
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
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
