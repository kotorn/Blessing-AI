import asyncio
import hashlib
import hmac
import inspect
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
from typing import Any, Callable, Dict, List, Optional, Literal, Tuple, cast

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
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
from domain.models import Instrument, MarketEvent, MarketType, OrderSide, OrderType, RiskSnapshot, utc_now

from apps.trading_worker import mainnet_preflight
from apps.trading_worker.local_runtime import (
    clear_local_container_secrets,
    clear_local_mainnet_secrets,
    local_container_runtime,
    local_postgres_host,
    mainnet_secret_value,
    worker_identity_token_value,
)
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
    is_portfolio_margin_enabled,
)
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.gates import DecisionExecutionGate
from apps.trading_worker.venues.binance.local_pilot_readiness import local_live_pilot_readiness
from apps.trading_worker.venues.binance.local_pilot_verdict import (
    get_pilot_verdict_status,
    set_active_pilot_verdict,
    verify_pilot_readiness_verdict,
)
from apps.trading_worker.venues.binance.pilot_bracket import (
    DEFAULT_ENTRY_TARGET_NOTIONAL_USDC,
    DEFAULT_EXECUTION_RISK_BUFFER_USDC,
    apply_pilot_bracket_to_intent,
    plan_pilot_bracket,
)
from apps.trading_worker.venues.binance.mainnet_risk import LOCAL_LIVE_PILOT_POLICY
from apps.trading_worker.venues.binance.models import (
    BinanceAuthenticationError,
    ConnectionState,
    TestnetSafetyLimits,
)
from apps.trading_worker.persistence.manager import PersistenceManager
from apps.trading_worker.venues.binance.public_ws import BinancePublicWebSocket
from domain.wealth_metrics import (
    DeploymentStage,
    TradeRecord,
    WealthPerformanceMetrics,
    calculate_wealth_metrics,
    evaluate_promotion_gate,
)
from domain.trade_lineage import TradeLineage, OutcomeGrade
from domain.eight_d import EightDIncident, IncidentStatus, IncidentSeverity
from apps.learning_engine import (
    WealthEvaluator,
    PDCAEvaluator,
    WhyWhyAnalyzer,
    EightDManager,
    DynamicCapitalAllocator,
)

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
    launchPolicy: Literal["STAGED_FIRST_ORDER", "LIVE_RESEARCH_PILOT"] = "STAGED_FIRST_ORDER"
    pilotCampaignId: Optional[str] = None
    pilotReadinessVerdict: Optional[dict[str, Any]] = None

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


from apps.trading_worker.logging_config import configure_rotating_file_logger

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("blessing.worker")
configure_rotating_file_logger(logger_name="blessing")

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
LOCAL_SUPERVISOR_HEARTBEAT_TTL_SECONDS = 15.0
LOCAL_MAINNET_RISK_LIFECYCLE_METHODS = (
    "get_local_mainnet_risk_context",
    "verify_local_mainnet_protection",
)


def local_mainnet_risk_lifecycle_status(
    adapter: Any = None,
    worker: Any = None,
) -> tuple[bool, list[str]]:
    """Verify runtime lifecycle readiness without requiring prior order evidence."""

    unavailable = [
        name
        for name in LOCAL_MAINNET_RISK_LIFECYCLE_METHODS
        if not callable(getattr(adapter, name, None))
    ]
    if unavailable or adapter is None:
        return False, list(LOCAL_MAINNET_RISK_LIFECYCLE_METHODS)

    if worker is None:
        worker = getattr(adapter, "_worker_authority", None)

    missing: list[str] = []

    target_local = str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
    local_only = os.getenv("LOCAL_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}
    if not (target_local and local_only):
        missing.append("LOCAL_RUNTIME_TARGET_AND_LOCAL_ONLY")

    monitor_fn = getattr(worker, "_local_pilot_lifecycle_monitor_state", None)
    if callable(monitor_fn):
        try:
            m_state = monitor_fn()
            status = m_state.get("status") if isinstance(m_state, dict) else None
            if status in {"STALLED", "STALE", "DEGRADED"}:
                missing.append("pilot_monitor_healthy")
        except Exception:
            missing.append("pilot_monitor_healthy")

    market_fresh = False
    market_fresh_fn = getattr(worker, "is_market_data_fresh", None)
    if callable(market_fresh_fn):
        try:
            market_fresh = bool(market_fresh_fn())
        except Exception:
            market_fresh = False
    elif adapter is not None:
        last_event = getattr(adapter, "last_market_event_at", {}).get("ETHUSDC")
        market_fresh = bool(last_event is not None)
    if not market_fresh:
        missing.append("market_data_fresh")

    recon_status = "UNKNOWN"
    reconciliation = getattr(adapter, "reconciliation", None)
    if reconciliation is not None:
        recon_status = str(getattr(reconciliation, "last_status", "UNKNOWN")).upper()
    elif worker is not None:
        recon_status = str(getattr(worker, "reconciliation_status", "UNKNOWN")).upper()
    if recon_status != "IN_SYNC":
        missing.append("reconciliation_in_sync")

    stream_healthy = bool(
        getattr(adapter, "private_stream_healthy", False)
        or getattr(worker, "private_stream_healthy", False)
    )
    if not stream_healthy:
        missing.append("private_stream_healthy")

    recovery_only = bool(getattr(worker, "recovery_only", False))
    if recovery_only:
        missing.append("recovery_only_false")

    kill_switch_active = bool(getattr(worker, "kill_switch_active", False))
    if kill_switch_active:
        missing.append("kill_switch_inactive")

    if missing:
        return False, missing
    return True, []


def local_mainnet_preflight_lifecycle_status(
    result: Any,
    persistence: Any,
    adapter_factory: Any = BinanceExecutionAdapter,
) -> tuple[bool, list[str]]:
    """Evaluate lifecycle readiness from this read-only preflight, not stale runtime events."""
    missing = [
        name
        for name in LOCAL_MAINNET_RISK_LIFECYCLE_METHODS
        if not callable(getattr(adapter_factory, name, None))
    ]
    if not isinstance(result, dict):
        return False, ["read_only_preflight"]
    persistence_readiness = persistence.readiness() if callable(
        getattr(persistence, "readiness", None)
    ) else {}
    local_identity = isinstance(persistence_readiness, dict) and all(
        persistence_readiness.get(key) == expected
        for key, expected in {
            "mode": "REQUIRED",
            "durable": True,
            "runtime_target": "LOCAL",
            "database_provider": "POSTGRES_LOCAL",
            "database_host": local_postgres_host(),
            "database_port": 5433,
            "database_identity_verified": True,
            "schema_verified": True,
        }.items()
    )
    if not local_identity:
        missing.append("verified_local_postgres_identity")
    checks = {
        str(check.get("id")): check
        for check in result.get("checks", [])
        if isinstance(check, dict)
    }
    required_preflight_checks = (
        "CHK-PREFLIGHT-PERSISTENCE",
        "CHK-PREFLIGHT-DURABLE-LEDGER",
        "CHK-PREFLIGHT-KILL-SWITCH",
        "CHK-PREFLIGHT-CONNECTION",
        "CHK-PREFLIGHT-AUTH",
        "CHK-PREFLIGHT-CAN-TRADE",
        "CHK-PREFLIGHT-POSITION-MODE",
        "CHK-PREFLIGHT-RULES",
        "CHK-PREFLIGHT-RECONCILIATION",
        "CHK-PREFLIGHT-PRIVATE-STREAM",
        "CHK-PREFLIGHT-ACCOUNT-RISK",
        "CHK-PREFLIGHT-MARKET",
    )
    missing.extend(
        check_id
        for check_id in required_preflight_checks
        if checks.get(check_id, {}).get("status") != "PASS"
    )
    if result.get("orderSubmissionAttempts") != 0:
        missing.append("zero_order_submission_attempts")
    if result.get("orderEndpointAttempts") != 0:
        missing.append("zero_order_endpoint_attempts")
    unique_missing = list(dict.fromkeys(missing))
    return not unique_missing, unique_missing


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
    local_mainnet_risk_lifecycle_ready: bool = False
    local_mainnet_risk_lifecycle_missing: List[str] = Field(default_factory=list)
    local_pilot_execution_ready: bool = False
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
    # Non-secret Local supervisor identity is present even while DISARMED.
    # The Control Plane must bind a read-only preflight to this exact process.
    local_run_id: str = ""
    pilot_campaign_id: str = ""
    local_source_fingerprint: str = ""
    local_supervisor_instance_id: str = ""
    # This is deliberately separate from the general heartbeat: a fresh
    # process heartbeat does not prove the Local Pilot lifecycle monitor ran.
    pilot_lifecycle_monitor: Dict[str, Any] = Field(default_factory=dict)
    pilot_verdict_status: Optional[str] = None
    # Why pilot signals did or did not become orders (non-secret, truncated).
    pilot_attempt_diagnostics: Dict[str, Any] = Field(default_factory=dict)

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
_ACTIVE_UVICORN_SERVER: Optional[uvicorn.Server] = None
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
                await WORKER_ENGINE.enforce_local_supervisor_liveness()
            except Exception as e:
                logger.error("Local supervisor liveness enforcement failed: %s", type(e).__name__)
            try:
                WORKER_ENGINE.enforce_local_pilot_monitor_liveness()
            except Exception as e:
                logger.error("Local Pilot monitor watchdog failed: %s", type(e).__name__)
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
        pilot_verdict_status=get_pilot_verdict_status(),
        updated_at=utc_now()
    )

# FastAPI Control Plane API
app = FastAPI(title="Blessing AI Worker Control API", lifespan=lifespan)


def _env_enabled(name: str, environ: Optional[Mapping[str, str]] = None) -> bool:
    values = environ if environ is not None else os.environ
    return str(values.get(name, "")).strip().lower() in {"1", "true", "yes", "on"}


def worker_bind_host(environ: Optional[Mapping[str, str]] = None) -> str:
    """Keep a host-run local Worker private while preserving container/Cloud Run binds."""

    values = environ if environ is not None else os.environ
    if _env_enabled("LOCAL_ONLY", values):
        return "0.0.0.0" if local_container_runtime(values) else "127.0.0.1"
    configured = str(values.get("BIND_HOST", "")).strip()
    if configured:
        return configured
    return "0.0.0.0" if values.get("K_SERVICE") else "127.0.0.1"


def local_worker_identity_matches(expected_token: str, authorization: str) -> bool:
    expected = expected_token.strip()
    if not expected:
        return False
    return hmac.compare_digest(authorization, "Bearer " + expected)


@app.middleware("http")
async def require_local_worker_identity(request: Request, call_next: Any) -> Response:
    """Add a per-launch local service boundary; Cloud Run IAM remains authoritative in prod."""

    if not (
        _env_enabled("LOCAL_WORKER_AUTH_REQUIRED")
        or _env_enabled("LOCAL_ONLY")
    ):
        return await call_next(request)
    expected = worker_identity_token_value()
    authorization = request.headers.get("authorization", "")
    if not local_worker_identity_matches(expected, authorization):
        return JSONResponse(
            status_code=401,
            content={"detail": "Local control-plane identity is required"},
        )
    return await call_next(request)


@app.get("/health")
def health_check() -> Dict[str, Any]:
    return {"status": "ok", "timestamp": utc_now()}

@app.get("/ready")
def readiness_probe() -> Dict[str, Any]:
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

class SupervisorHeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pilotReadinessVerdict: Optional[dict[str, Any]] = None


@app.post("/supervisor/heartbeat")
def local_supervisor_heartbeat(payload: Optional[SupervisorHeartbeatRequest] = None):
    """Accept a per-launch heartbeat only from the authenticated Local parent."""
    if not (
        _env_enabled("LOCAL_ONLY")
        and str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
    ):
        raise HTTPException(status_code=404, detail="Local supervisor heartbeat is unavailable")
    if WORKER_ENGINE is None:
        raise HTTPException(status_code=503, detail="Worker is not initialized")
    if payload and "pilotReadinessVerdict" in payload.model_fields_set:
        set_active_pilot_verdict(payload.pilotReadinessVerdict)
    WORKER_ENGINE.record_local_supervisor_heartbeat()
    return {"status": "ok", "runtimeTarget": "LOCAL"}


class PilotVerdictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verdict: dict[str, Any]


@app.post("/local-pilot/verdict")
def set_pilot_verdict_endpoint(req: PilotVerdictRequest):
    token = worker_identity_token_value()
    ok, reason, details = verify_pilot_readiness_verdict(req.verdict, token)
    if not ok:
        raise HTTPException(status_code=400, detail=f"LOCAL_PILOT_VERDICT_REJECTED: {reason}")
    set_active_pilot_verdict(req.verdict)
    return {"status": "ok", "reason": reason, "details": details}

@app.get("/capabilities")
def get_capabilities():
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    return WORKER_ENGINE.get_capabilities()

@app.post("/arm")
async def arm(config: ArmRequest):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    if config.pilotReadinessVerdict is not None:
        set_active_pilot_verdict(config.pilotReadinessVerdict)
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

@app.get("/local-pilot/accounting")
async def local_pilot_accounting_endpoint():
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    return await WORKER_ENGINE.get_local_live_pilot_accounting()

@app.post("/continuation/readiness")
async def continuation_readiness_endpoint(launch_id: Optional[str] = None):
    """Verify first-order evidence without activating autonomous execution."""

    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    runner = getattr(WORKER_ENGINE, "run_mainnet_continuation_readiness", None)
    if not callable(runner):
        raise HTTPException(status_code=503, detail="Autonomous continuation is unavailable")
    return await cast(Any, runner(launch_id=launch_id))

@app.post("/continue")
async def continue_endpoint(config: ContinuationRequest):
    """Activate autonomous continuation only after a verified approval."""

    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    runner = getattr(WORKER_ENGINE, "continue_autonomous", None)
    if not callable(runner):
        raise HTTPException(status_code=503, detail="Autonomous continuation is unavailable")
    success, message = await cast(Any, runner(config))
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
    return await cast(Any, runner())

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

@app.get("/wealth/metrics")
def get_wealth_metrics_endpoint():
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    return WORKER_ENGINE.get_wealth_metrics()

@app.get("/incidents/8d")
def get_incidents_8d_endpoint(active_only: bool = False):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    return WORKER_ENGINE.get_eight_d_incidents(active_only=active_only)

class CloseIncidentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verification: str
    prevention: str
    lessons: str
    signoff_user_id: str = Field(min_length=1, max_length=128)

    @field_validator("verification", "prevention", "lessons", "signoff_user_id")
    @classmethod
    def require_nonblank_closure_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Closure evidence and authenticated sign-off are required")
        if len(normalized) > 4000:
            raise ValueError("Closure evidence exceeds the maximum length")
        return normalized

@app.post("/incidents/8d/{incident_id}/close")
def close_incident_endpoint(incident_id: str, req: CloseIncidentRequest):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    if len(incident_id) != 18 or not re.fullmatch(r"8D-[0-9]{8}-[0-9A-F]{6}", incident_id):
        raise HTTPException(status_code=400, detail="Invalid incident identifier")
    success = WORKER_ENGINE.close_eight_d_incident(
        incident_id, req.verification, req.prevention, req.lessons, req.signoff_user_id
    )
    if not success:
        raise HTTPException(status_code=404, detail="Incident not found or could not be closed")
    return {"status": "CLOSED", "incident_id": incident_id}

@app.get("/learning/lineages")
def get_lineages_endpoint(limit: int = 50):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    return WORKER_ENGINE.get_trade_lineages(limit=limit)

@app.get("/learning/pdca")
def get_pdca_endpoint():
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    return WORKER_ENGINE.get_pdca_status()

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
        self.wealth_evaluator = WealthEvaluator(initial_capital=Decimal("1000.0"))
        self.pdca_evaluator = PDCAEvaluator()
        self.eight_d_manager = EightDManager()
        self.capital_allocator = DynamicCapitalAllocator()
        
        self.ws_client = None
        self.execution_adapter: Optional[BinanceExecutionAdapter] = None
        
        # Runtime State
        self.start_time = utc_now()
        self.heartbeat_at = utc_now()
        self.heartbeat_interval_sec: float = 1.0
        self.heartbeat_task: Optional[asyncio.Task] = None
        self._local_supervisor_heartbeat_monotonic: Optional[float] = None
        self._local_supervisor_shutdown_started = False
        self._pilot_lifecycle_monitor_started_at: Optional[datetime] = None
        self._pilot_lifecycle_monitor_completed_at: Optional[datetime] = None
        self._pilot_lifecycle_monitor_last_success_at: Optional[datetime] = None
        self._pilot_lifecycle_monitor_last_error: Optional[str] = None
        self.execution_mode = WorkerExecutionMode.PAPER
        self.engine_state = WorkerEngineState.DISARMED
        self.connection_state = "DISCONNECTED"
        self.market_data_healthy = False
        self.private_stream_healthy = False
        self.authenticated = False
        self.reconciliation_status = "UNKNOWN"
        self.kill_switch_active = False
        self.pause_new_risk = False
        self._pilot_accounting_pause_active = False
        self.recovery_only = False
        self.active_configuration = None
        self.updated_at = utc_now()
        self.last_market_event_at: Dict[str, datetime] = {}
        # symbol -> (ledger_version, depth); invalidated by any ledger mutation.
        self._observed_grid_depth_cache: Dict[str, tuple] = {}
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
        self._last_risk_snapshot_enqueued_at: float = 0.0
        self._pilot_attempt_counts: Dict[str, int] = {}
        self._pilot_attempt_last: Dict[str, Optional[str]] = {
            "stage": None, "reason": None, "at": None, "signal_at": None,
        }

    _PILOT_ATTEMPT_STAGES = (
        "SIGNAL", "PREPLAN_FAILED", "EXECUTION_BLOCKED", "MONITOR_ONLY",
        "EXECUTING", "EXECUTION_ERROR",
    )

    def _note_pilot_attempt(self, stage: str, reason: Optional[str] = None) -> None:
        """Record where a strategy signal stopped, for the operator state view."""
        now = utc_now().isoformat()
        self._pilot_attempt_counts[stage] = self._pilot_attempt_counts.get(stage, 0) + 1
        if stage == "SIGNAL":
            self._pilot_attempt_last["signal_at"] = now
            return
        cleaned = "".join(
            ch if ch.isprintable() else " " for ch in str(reason or "")
        ).strip()[:200]
        self._pilot_attempt_last.update(stage=stage, reason=cleaned or None, at=now)

    def _pilot_attempt_diagnostics(self) -> Dict[str, Any]:
        counts = self._pilot_attempt_counts
        adapter_block = getattr(self.execution_adapter, "last_order_block", None)
        return {
            "signals": counts.get("SIGNAL", 0),
            "preplan_failed": counts.get("PREPLAN_FAILED", 0),
            "execution_blocked": counts.get("EXECUTION_BLOCKED", 0),
            "monitor_only": counts.get("MONITOR_ONLY", 0),
            "executing": counts.get("EXECUTING", 0),
            "execution_error": counts.get("EXECUTION_ERROR", 0),
            "last_stage": self._pilot_attempt_last["stage"],
            "last_reason": self._pilot_attempt_last["reason"],
            "last_at": self._pilot_attempt_last["at"],
            "last_signal_at": self._pilot_attempt_last["signal_at"],
            "adapter_last_block": dict(adapter_block) if isinstance(adapter_block, dict) else None,
        }

    def get_wealth_metrics(self) -> Dict[str, Any]:
        pm = self.wealth_evaluator.get_portfolio_metrics()
        gate = self.wealth_evaluator.check_promotion_readiness()
        insufficient_sample = pm.total_trades == 0 or any(
            reason.startswith("Insufficient trade sample size")
            for reason in gate.blocking_reasons
        )
        evidence_authoritative = False
        return {
            "evidence": {
                "status": "INSUFFICIENT_SAMPLE" if insufficient_sample else "PROCESS_LOCAL_UNVERIFIED",
                "source": "PROCESS_MEMORY",
                "sample_size": pm.total_trades,
                "authoritative": evidence_authoritative,
                "reason": (
                    "Closed-trade lineage is not yet connected to durable execution history; "
                    "worker memory is not authoritative performance evidence."
                ),
            },
            "portfolio": {
                "total_trades": pm.total_trades,
                "win_trades": pm.win_trades,
                "loss_trades": pm.loss_trades,
                "break_even_trades": pm.break_even_trades,
                "win_rate_pct": float(pm.win_rate_pct),
                "payoff_ratio": float(pm.payoff_ratio),
                "profit_factor": float(pm.profit_factor),
                "expectancy_usdt": float(pm.expectancy_usdt),
                "gross_profit": float(pm.gross_profit),
                "gross_loss": float(pm.gross_loss),
                "net_pnl": float(pm.net_pnl),
                "total_commission": float(pm.total_commission),
                "total_funding": float(pm.total_funding),
                "fee_drag_pct": float(pm.fee_drag_pct),
                "max_drawdown_pct": float(pm.max_drawdown_pct),
                "cagr_pct": float(pm.cagr_pct),
                "sharpe_ratio": float(pm.sharpe_ratio),
                "sortino_ratio": float(pm.sortino_ratio),
                "calmar_ratio": float(pm.calmar_ratio),
                "var_95_pct": float(pm.var_95_pct),
                "cvar_95_pct": float(pm.cvar_95_pct),
                "avg_slippage_bps": float(pm.avg_slippage_bps),
                "unknown_risk_violations": pm.unknown_risk_violations,
                "sustainable_growth_score": float(pm.sustainable_growth_score),
                "is_capital_safe": pm.is_capital_safe and evidence_authoritative,
                "capital_safety_status": (
                    "UNKNOWN" if not evidence_authoritative else "SAFE" if pm.is_capital_safe else "UNSAFE"
                ),
            },
            "promotion_gate": {
                "current_stage": gate.current_stage.value,
                "target_stage": gate.target_stage.value,
                "eligible": gate.eligible,
                "passed_criteria": gate.passed_criteria,
                "blocking_reasons": gate.blocking_reasons,
            },
            "strategies": {
                name: {
                    "total_trades": m.total_trades,
                    "win_rate_pct": float(m.win_rate_pct),
                    "net_pnl": float(m.net_pnl),
                    "sharpe_ratio": float(m.sharpe_ratio),
                    "max_drawdown_pct": float(m.max_drawdown_pct),
                }
                for name, m in self.wealth_evaluator.get_strategy_metrics().items()
            },
        }

    def get_eight_d_incidents(self, active_only: bool = False) -> List[Dict[str, Any]]:
        return [i.to_dict() for i in self.eight_d_manager.list_incidents(active_only=active_only)]

    def close_eight_d_incident(
        self,
        incident_id: str,
        verification: str,
        prevention: str,
        lessons: str,
        signoff_user_id: str,
    ) -> bool:
        return self.eight_d_manager.advance_and_close(
            incident_id=incident_id,
            verification_evidence=verification,
            systemic_prevention=prevention,
            closure_lessons=lessons,
            signoff_user_id=signoff_user_id,
        )

    def get_trade_lineages(self, limit: int = 50) -> List[Dict[str, Any]]:
        lineages = list(self.wealth_evaluator.lineages.values())
        return [lineage.to_dict() for lineage in lineages[-limit:]]

    def get_pdca_status(self) -> Dict[str, Any]:
        strategies = ["trend_breakout", "range_fade", "shock_momentum", "structural_grid"]
        results = {}
        lineages = list(self.wealth_evaluator.lineages.values())
        for strat in strategies:
            check = self.pdca_evaluator.evaluate_strategy(strat, lineages)
            results[strat] = {
                "sample_size": check.sample_size,
                "authoritative": check.authoritative,
                "plan_win_rate_pct": float(check.plan_win_rate_pct),
                "actual_win_rate_pct": float(check.actual_win_rate_pct),
                "win_rate_gap_pct": float(check.win_rate_gap_pct),
                "plan_edge_bps": float(check.plan_edge_bps),
                "actual_edge_bps": float(check.actual_edge_bps),
                "edge_decay_bps": float(check.edge_decay_bps),
                "plan_slippage_bps": float(check.plan_slippage_bps),
                "actual_slippage_bps": float(check.actual_slippage_bps),
                "drift_detected": check.drift_detected,
                "drift_severity": check.drift_severity,
                "evidence_status": check.evidence_status,
                "recommended_actions": check.recommended_actions,
                "triggers_8d": check.triggers_8d,
            }
        return results

    def record_closed_trade_lineage(self, lineage: TradeLineage) -> None:
        self.wealth_evaluator.record_lineage(lineage)
        if lineage.requires_8d:
            self.eight_d_manager.create_incident_from_lineage(lineage)

    def _bind_local_live_pilot_callbacks(self, local_mainnet_runtime: bool) -> bool:
        """Bind Pilot fill/mark/funding accounting hooks to the adapter.

        The hooks are gated on the launch session currently held by the worker.
        Callers must invoke this again once ARM has created or refreshed that
        session, because a fresh ARM has no session before it runs. Returns True
        when the hooks were bound for a LIVE_RESEARCH_PILOT session.
        """
        session = self._mainnet_launch_session
        pilot_runtime = bool(
            local_mainnet_runtime
            and isinstance(session, dict)
            and session.get("policy") == "LIVE_RESEARCH_PILOT"
        )
        adapter = self.execution_adapter
        if adapter is None:
            raise RuntimeError("no execution adapter available for Pilot accounting hooks")
        adapter.on_local_live_pilot_fill = (
            self._persist_local_live_pilot_fill if pilot_runtime else None
        )
        adapter.on_local_live_pilot_mark = (
            self._persist_local_live_pilot_mark if pilot_runtime else None
        )
        adapter.on_local_live_pilot_funding_reconcile = (
            self._reconcile_local_live_pilot_funding if pilot_runtime else None
        )
        return pilot_runtime

    def _set_mainnet_launch_session(self, session: Optional[dict[str, Any]]) -> None:
        """Project durable launch identity into the process-local API state."""
        previous_launch_id = getattr(self, "_mainnet_launch_id", None)
        previous_accounting_pause = bool(
            getattr(self, "_pilot_accounting_pause_active", False)
        )
        self._mainnet_launch_session = dict(session) if session else None
        self._pilot_accounting_pause_active = bool(
            session
            and (
                session.get("pilot_accounting_resume_eligible") is True
                or (
                    previous_accounting_pause
                    and str(session.get("launch_id") or "") == str(previous_launch_id or "")
                    and session.get("policy") == "LIVE_RESEARCH_PILOT"
                )
            )
        )
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

    def record_local_supervisor_heartbeat(self) -> None:
        """Record an authenticated heartbeat from the Local Control Plane."""
        self._local_supervisor_heartbeat_monotonic = time.monotonic()

    def local_supervisor_heartbeat_is_fresh(self) -> bool:
        if not (
            _env_enabled("LOCAL_ONLY")
            and str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
            and self.execution_mode == WorkerExecutionMode.LIVE
        ):
            return True
        last = self._local_supervisor_heartbeat_monotonic
        return bool(
            last is not None
            and 0 <= time.monotonic() - last <= LOCAL_SUPERVISOR_HEARTBEAT_TTL_SECONDS
        )

    async def enforce_local_supervisor_liveness(self) -> None:
        """Disarm and stop Local LIVE if its authenticated parent disappears."""
        if self.local_supervisor_heartbeat_is_fresh() or self._local_supervisor_shutdown_started:
            return
        if not (
            _env_enabled("LOCAL_ONLY")
            and str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
            and self.execution_mode == WorkerExecutionMode.LIVE
        ):
            return

        self._local_supervisor_shutdown_started = True
        logger.critical(
            "monitor_event=local_supervisor_heartbeat_lost action=disarm_and_exit"
        )
        # Prevent another risk-increasing decision while the adapter is being
        # closed and the durable launch state is fenced.
        self.pause_new_risk = True
        self.engine_state = WorkerEngineState.DISARMED
        os.environ["MAINNET_LIVE_APPROVED"] = "false"
        try:
            await self.disarm()
        except Exception as exc:
            logger.error("Local supervisor-loss disarm failed: %s", type(exc).__name__)
        finally:
            for name in (
                "BINANCE_MAINNET_API_KEY",
                "BINANCE_MAINNET_API_SECRET",
                "BINANCE_MAINNET_API_KEY_FILE",
                "BINANCE_MAINNET_API_SECRET_FILE",
                "WORKER_IDENTITY_TOKEN_FILE",
                "POSTGRES_PASSWORD_FILE",
                "MAINNET_RELEASE_APPROVAL_ID",
                "MAINNET_CONTINUATION_APPROVAL_ID",
                "LOCAL_SOURCE_FINGERPRINT",
            ):
                os.environ[name] = ""
            clear_local_container_secrets()
            os.environ["EXECUTION_MODE"] = "PAPER"
            self.execution_mode = WorkerExecutionMode.PAPER
            self.is_running = False
            server = _ACTIVE_UVICORN_SERVER
            if server is not None:
                server.should_exit = True

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
        """Return true only when the approved Worker secret source is available."""

        return bool(
            mainnet_secret_value("BINANCE_MAINNET_API_KEY").strip()
            and mainnet_secret_value("BINANCE_MAINNET_API_SECRET").strip()
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
        if launch_policy not in {MAINNET_LAUNCH_STAGED, MAINNET_LAUNCH_AUTONOMOUS, "LIVE_RESEARCH_PILOT"}:
            self.pause_new_risk = True
            self._refresh_engine_state()
            logger.error(
                "monitor_event=launch_session_violation reason=unknown_launch_policy"
            )
            return False
        try:
            reserved = await self.persistence.reserve_mainnet_risk_order(
                self._mainnet_launch_id,
                str(getattr(order, "client_order_id", "") or ""),
                str(getattr(order, "basket_id", "") or ""),
            )
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

    async def _persist_testnet_protection_update(self, record: Dict[str, Any]) -> bool:
        """Read back the durable Testnet bracket owner before each lifecycle step."""
        if self.execution_mode != WorkerExecutionMode.TESTNET:
            logger.error("Testnet protection persistence rejected outside Testnet mode")
            return False
        mode = str(getattr(self.persistence.mode, "value", self.persistence.mode)).upper()
        readiness = self.persistence.readiness()
        repository = getattr(self.persistence, "repository", None)
        protections = getattr(repository, "algo_protections", None)
        if (
            mode != "REQUIRED"
            or not self.persistence.is_connected
            or readiness.get("durable") is not True
            or protections is None
            or str(record.get("environment", "")).upper() != "TESTNET"
            or str(record.get("venue", "")).lower() != "binance_testnet"
        ):
            logger.error("Testnet protection persistence is unavailable or mis-scoped")
            return False
        try:
            stored = await protections.upsert_protection(record)
        except Exception as exc:
            logger.error(
                "Testnet protection persistence failed: %s", type(exc).__name__
            )
            return False
        return bool(
            stored
            and stored.get("environment") == "TESTNET"
            and stored.get("venue") == "binance_testnet"
            and stored.get("symbol") == str(record.get("symbol", "")).upper()
            and stored.get("entry_client_order_id") == record.get("entry_client_order_id")
            and stored.get("state") == record.get("state", "PENDING")
        )

    async def _claim_local_mainnet_entry_cancel(
        self, record: Dict[str, Any]
    ) -> bool:
        """Win the durable one-shot cancellation claim before an entry DELETE."""
        launch = getattr(self, "_mainnet_launch_session", None)
        if (
            self.execution_mode != WorkerExecutionMode.LIVE
            or os.getenv("LOCAL_ONLY", "").strip().lower() not in {"1", "true", "yes", "on"}
            or os.getenv("LOCAL_RUNTIME_TARGET", "").strip().upper() != "LOCAL"
            or not isinstance(launch, dict)
            or launch.get("policy") != "LIVE_RESEARCH_PILOT"
            or launch.get("runtime_target") != "LOCAL"
            or launch.get("launch_id") != str(self._mainnet_launch_id or "")
            or not str(launch.get("pilot_campaign_id") or "").strip()
            or str(record.get("environment", "")).upper() != "MAINNET"
            or str(record.get("venue", "")).lower() != "binance_mainnet"
            or str(record.get("symbol", "")).upper() != "ETHUSDC"
            or str(record.get("mainnet_launch_id", "")) != str(self._mainnet_launch_id or "")
            or not str(record.get("basket_id") or "").strip()
        ):
            return False
        mode = str(getattr(self.persistence.mode, "value", self.persistence.mode)).upper()
        readiness = self.persistence.readiness()
        repository = getattr(self.persistence, "repository", None)
        protections = getattr(repository, "algo_protections", None)
        claim = getattr(protections, "claim_mainnet_entry_cancel", None)
        if (
            mode != "REQUIRED" or not self.persistence.is_connected
            or readiness.get("durable") is not True or not callable(claim)
        ):
            return False
        try:
            stored = await cast(Any, claim(record))
        except Exception as exc:
            logger.error("Local Mainnet entry cancel claim failed: %s", type(exc).__name__)
            return False
        identity_fields = (
            "environment", "venue", "symbol", "mainnet_launch_id", "basket_id",
            "entry_client_order_id", "entry_side", "position_side",
            "requested_quantity", "management_mode",
        )
        return bool(
            isinstance(stored, dict)
            and all(stored.get(key) == record.get(key) for key in identity_fields)
            and "entry_cancel=ATTEMPTED_UNKNOWN" in str(stored.get("state_reason") or "")
        )

    async def _persist_local_mainnet_protection_update(
        self, record: Dict[str, Any]
    ) -> bool:
        """Read back the Local Mainnet Algo owner at every lifecycle boundary."""
        if (
            self.execution_mode != WorkerExecutionMode.LIVE
            or os.getenv("LOCAL_ONLY", "").strip().lower() not in {"1", "true", "yes", "on"}
            or os.getenv("LOCAL_RUNTIME_TARGET", "").strip().upper() != "LOCAL"
        ):
            logger.error("Local Mainnet protection persistence rejected outside Local LIVE")
            return False
        mode = str(getattr(self.persistence.mode, "value", self.persistence.mode)).upper()
        readiness = self.persistence.readiness()
        repository = getattr(self.persistence, "repository", None)
        protections = getattr(repository, "algo_protections", None)
        if (
            mode != "REQUIRED"
            or not self.persistence.is_connected
            or readiness.get("durable") is not True
            or protections is None
            or str(record.get("environment", "")).upper() != "MAINNET"
            or str(record.get("venue", "")).lower() != "binance_mainnet"
            or str(record.get("symbol", "")).upper() != "ETHUSDC"
        ):
            logger.error("Local Mainnet protection persistence is unavailable or mis-scoped")
            return False
        launch_id = str(self._mainnet_launch_id or "").strip()
        basket_id = str(record.get("basket_id") or "").strip()
        if not launch_id or not basket_id:
            logger.error("Local Mainnet protection owner has no durable launch/basket identity")
            return False
        is_unfilled_close = (
            str(record.get("state", "")).upper() == "CLOSED"
            and isinstance(record.get("unfilled_order_proof"), dict)
        )
        if is_unfilled_close and not callable(
            getattr(protections, "close_mainnet_unfilled_protection_with_proof", None)
        ):
            logger.error("Local Mainnet zero-fill closure proof persistence is unavailable")
            return False
        try:
            if not await self.persistence.bind_mainnet_launch_basket(launch_id, basket_id):
                logger.error("Local Mainnet launch could not bind the protection basket")
                return False
        except Exception as exc:
            logger.error("Local Mainnet launch/basket binding failed: %s", type(exc).__name__)
            return False
        record = dict(record)
        record["mainnet_launch_id"] = launch_id
        unfilled_proof = record.pop("unfilled_order_proof", None)
        try:
            if str(record.get("state", "")).upper() == "CLOSED" and isinstance(unfilled_proof, dict):
                stored = await protections.close_mainnet_unfilled_protection_with_proof(
                    str(record["symbol"]),
                    str(record["entry_client_order_id"]),
                    unfilled_proof,
                )
            else:
                stored = await protections.upsert_protection(record)
        except Exception as exc:
            logger.error(
                "Local Mainnet protection persistence failed: %s", type(exc).__name__
            )
            return False
        if not isinstance(stored, dict):
            return False
        identity_fields = (
            "environment",
            "venue",
            "symbol",
            "entry_client_order_id",
            "basket_id",
            "mainnet_launch_id",
            "entry_side",
            "position_side",
            "requested_quantity",
            "stop_client_algo_id",
            "take_profit_client_algo_id",
            "management_mode",
            "state",
            "state_reason",
            "filled_quantity",
            "entry_average_price",
            "stop_algo_id",
            "take_profit_algo_id",
            "first_fill_at",
            "protection_verified_at",
            "last_reconciled_at",
            "closed_at",
        )
        return all(stored.get(key) == record.get(key) for key in identity_fields) and (
            not is_unfilled_close
            or (
                isinstance(stored.get("closure_evidence"), dict)
                and stored["closure_evidence"].get("kind") == "UNFILLED_ENTRY_TERMINAL"
                and stored["closure_evidence"].get("client_order_id")
                == record.get("entry_client_order_id")
            )
        )

    def _merge_local_pilot_session_readback(self, result: Mapping[str, Any]) -> None:
        """Refresh monitor inputs without discarding the active approval binding."""
        session = getattr(self, "_mainnet_launch_session", None)
        if (
            isinstance(session, dict)
            and str(session.get("launch_id") or "") == str(result.get("launch_id") or "")
            and session.get("policy") == "LIVE_RESEARCH_PILOT"
        ):
            session.update(dict(result))

    async def _persist_local_live_pilot_fill(self, fill: Any) -> bool:
        """Persist realized fill PnL and its fee from the authenticated user stream.

        Commission is accepted only in the ETHUSDC quote asset. Other fee
        assets require an independently sourced conversion and therefore
        quarantine accounting instead of silently using a zero conversion.
        """
        session = self._mainnet_launch_session
        repository = getattr(self.persistence, "repository", None)
        append_event = getattr(repository, "append_local_live_pilot_event", None)
        was_paused_before_fill = bool(self.pause_new_risk)
        if (
            self.execution_mode != WorkerExecutionMode.LIVE
            or os.getenv("LOCAL_ONLY", "").strip().lower() not in {"1", "true", "yes", "on"}
            or os.getenv("LOCAL_RUNTIME_TARGET", "").strip().upper() != "LOCAL"
            or not isinstance(session, dict)
            or session.get("policy") != "LIVE_RESEARCH_PILOT"
            or not str(session.get("pilot_campaign_id") or "")
            or not str(session.get("launch_id") or "")
            or not self.persistence.is_connected
            or not callable(append_event)
        ):
            logger.error("Local Pilot fill accounting has no valid durable campaign binding")
            self.pause_new_risk = True
            self.engine_state = WorkerEngineState.DEGRADED
            return False

        if (
            str(getattr(fill, "symbol", "")).upper() != "ETHUSDC"
            or str(getattr(fill, "commission_asset", "")).upper() != "USDC"
        ):
            logger.error("Local Pilot fill fee currency is not directly denominated in USDC")
            self.pause_new_risk = True
            self.engine_state = WorkerEngineState.DEGRADED
            return False
        launch_id = str(session["launch_id"])
        campaign_id = str(session["pilot_campaign_id"])
        try:
            observed_at = datetime.fromtimestamp(
                float(fill.transaction_time) / 1000.0,
                tz=timezone.utc,
            )
            common = {
                "run_id": launch_id,
                "campaign_id": campaign_id,
                "launch_id": launch_id,
                "symbol": "ETHUSDC",
                "source": "BINANCE",
                "observed_at": observed_at,
            }
            # Fee first: if the process fails between the two idempotent
            # writes, replay can safely complete the missing realized event.
            append_event_fn = cast(Callable[..., Any], append_event)
            fee_result = await append_event_fn(
                **common,
                event_key=f"FEE:{fill.exchange_trade_id}",
                event_type="FEE",
                net_pnl_delta_usdc=-abs(Decimal(str(fill.commission))),
                payload={
                    "run_id": launch_id,
                    "exchange_event_id": str(fill.exchange_trade_id),
                    "exchange_order_id": str(fill.exchange_order_id),
                    "commission_usdc": str(abs(Decimal(str(fill.commission)))),
                    "asset": "USDC",
                },
            )
            fill_result = await append_event_fn(
                **common,
                event_key=f"FILL:{fill.exchange_trade_id}",
                event_type="FILL",
                net_pnl_delta_usdc=Decimal(str(fill.realized_pnl)),
                payload={
                    "run_id": launch_id,
                    "exchange_event_id": str(fill.exchange_trade_id),
                    "exchange_order_id": str(fill.exchange_order_id),
                    "client_order_id": str(fill.client_order_id),
                    "quantity": str(fill.quantity),
                    "price": str(fill.price),
                    "realized_pnl_usdc": str(fill.realized_pnl),
                    "side": str(getattr(fill.side, "value", fill.side)),
                    "position_side": str(getattr(fill.position_side, "value", fill.position_side)),
                },
            )
            if not isinstance(fee_result, dict) or not isinstance(fill_result, dict):
                raise RuntimeError("durable pilot event read-back failed")
            self._merge_local_pilot_session_readback(fee_result)
            self._merge_local_pilot_session_readback(fill_result)
            if "pilot_accounting_resume_eligible" in fill_result:
                self._pilot_accounting_pause_active = bool(
                    fill_result["pilot_accounting_resume_eligible"]
                    and (
                        self._pilot_accounting_pause_active
                        or not was_paused_before_fill
                    )
                )
            if (
                fee_result.get("pilot_drawdown_triggered") is True
                or fill_result.get("pilot_drawdown_triggered") is True
                or fill_result.get("pilot_status") == "CLOSE_ONLY"
                or fill_result.get("state") == "PAUSED_NEW_RISK"
            ):
                # A fill invalidates the marked unrealized component until a
                # fresh exchange position snapshot arrives. Keep the process
                # fence aligned with the durable recoverable pause.
                self.pause_new_risk = True
                self.engine_state = WorkerEngineState.PAUSED_NEW_RISK
            return True
        except Exception as exc:
            logger.error("Local Pilot fill accounting failed: %s", type(exc).__name__)
            self.pause_new_risk = True
            self.engine_state = WorkerEngineState.DEGRADED
            return False

    async def _persist_local_live_pilot_mark(
        self,
        symbol: str,
        event_id: str,
        unrealized_pnl_usdc: Decimal,
        observed_at_ms: object,
        *,
        snapshot_scope: str = "ETHUSDC_POSITION_ACCOUNT_UPDATE",
    ) -> bool:
        """Replace the Pilot's unrealized component from a Binance position event."""
        session = self._mainnet_launch_session
        repository = getattr(self.persistence, "repository", None)
        append_event = getattr(repository, "append_local_live_pilot_event", None)
        try:
            event_ms = int(str(observed_at_ms))
            pnl = Decimal(str(unrealized_pnl_usdc))
            if (
                str(symbol).upper() != "ETHUSDC"
                or event_ms <= 0
                or not pnl.is_finite()
                or not isinstance(session, dict)
                or session.get("policy") != "LIVE_RESEARCH_PILOT"
                or not self.persistence.is_connected
                or not callable(append_event)
            ):
                raise ValueError("position mark lacks a valid Pilot binding")
            observed_at = datetime.fromtimestamp(event_ms / 1000.0, tz=timezone.utc)
            mark_age = (utc_now() - observed_at).total_seconds()
            if mark_age < -2 or mark_age > 60:
                raise ValueError("position mark is stale or from the future")
            launch_id = str(session["launch_id"])
            append_event_fn = cast(Callable[..., Any], append_event)
            result = await append_event_fn(
                run_id=launch_id,
                campaign_id=str(session["pilot_campaign_id"]),
                launch_id=launch_id,
                symbol="ETHUSDC",
                event_key=f"MARK:{event_id}",
                event_type="MARK",
                source="BINANCE",
                observed_at=observed_at,
                net_pnl_delta_usdc=None,
                payload={
                    "run_id": launch_id,
                    "account_snapshot_id": str(event_id),
                    "position_side": "BOTH",
                    "unrealized_pnl_usdc": str(pnl),
                    "snapshot_scope": snapshot_scope,
                },
            )
            if not isinstance(result, dict):
                raise RuntimeError("position mark was not durably read back")
            self._merge_local_pilot_session_readback(result)
            if result.get("pilot_drawdown_triggered") is True:
                self.pause_new_risk = True
                self.engine_state = WorkerEngineState.PAUSED_NEW_RISK
                adapter = getattr(self, "execution_adapter", None)
                if adapter and hasattr(adapter, "check_and_enforce_pilot_protections"):
                    try:
                        await adapter.check_and_enforce_pilot_protections(authority=self)
                    except Exception as exc:
                        logger.error("Pilot drawdown protection enforcement error: %s", type(exc).__name__)
            elif (
                result.get("pilot_accounting_resumed") is True
                and getattr(self, "_pilot_accounting_pause_active", False)
                and not bool(getattr(self, "kill_switch_active", False))
                and str(getattr(self, "reconciliation_status", "IN_SYNC")).upper() == "IN_SYNC"
                and str(
                    getattr(
                        getattr(getattr(self, "execution_adapter", None), "reconciliation", None),
                        "last_status",
                        "UNKNOWN",
                    )
                ).upper() == "IN_SYNC"
            ):
                self._pilot_accounting_pause_active = False
                self.pause_new_risk = False
                refresh_state = getattr(self, "_refresh_engine_state", None)
                if callable(refresh_state):
                    refresh_state()
                else:
                    self.engine_state = WorkerEngineState.ARMED
            return True
        except Exception as exc:
            logger.error("Local Pilot position mark rejected: %s", type(exc).__name__)
            self.pause_new_risk = True
            self.engine_state = WorkerEngineState.DEGRADED
            return False

    async def get_local_live_pilot_accounting(self) -> dict[str, Any]:
        """Expose durable event-ledger accounting only with fresh reconciled evidence."""
        unknown = {
            "status": "UNKNOWN",
            "evidence_status": "UNVERIFIED",
            "net_pnl_usdc": "UNKNOWN",
            "peak_net_pnl_usdc": "UNKNOWN",
            "drawdown_usdc": "UNKNOWN",
            "realized_pnl_usdc": "UNKNOWN",
            "unrealized_pnl_usdc": "UNKNOWN",
            "fees_usdc": "UNKNOWN",
            "funding_usdc": "UNKNOWN",
            "slippage_usdc": "UNKNOWN",
            "mark_observed_at": None,
            "mark_age_seconds": None,
            "last_event_at": None,
            "reason": "PILOT_ACCOUNTING_EVIDENCE_UNAVAILABLE",
        }
        session = self._mainnet_launch_session
        adapter = self.execution_adapter
        if (
            self.execution_mode != WorkerExecutionMode.LIVE
            or not _env_enabled("LOCAL_ONLY")
            or str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() != "LOCAL"
            or not isinstance(session, dict)
            or session.get("policy") != "LIVE_RESEARCH_PILOT"
            or session.get("runtime_target") != "LOCAL"
            or not self.persistence.is_connected
            or not callable(getattr(self.persistence, "get_local_live_pilot_accounting", None))
            or adapter is None
            or adapter.env != BinanceEnvironment.MAINNET
        ):
            return unknown
        try:
            snapshot = await self.persistence.get_local_live_pilot_accounting(
                str(session["launch_id"])
            )
            if not isinstance(snapshot, dict):
                return unknown
            last_mark_at = snapshot.get("pilot_last_account_snapshot_at")
            if not isinstance(last_mark_at, datetime):
                return {**unknown, "status": "NOT_STARTED", "reason": "NO_AUTHORITATIVE_MARK"}
            if last_mark_at.tzinfo is None:
                last_mark_at = last_mark_at.replace(tzinfo=timezone.utc)
            mark_age = (utc_now() - last_mark_at).total_seconds()
            if mark_age < -2 or mark_age > 5:
                return {**unknown, "status": "STALE", "reason": "AUTHORITATIVE_MARK_STALE"}
            financial_id = snapshot.get("last_financial_event_id")
            mark_id = snapshot.get("last_mark_event_id")
            if financial_id is not None and (
                mark_id is None or int(financial_id) > int(mark_id)
            ):
                return {**unknown, "status": "PENDING_RECONCILIATION", "reason": "FINANCIAL_EVENT_AFTER_MARK"}
            if (
                self.reconciliation_status != "IN_SYNC"
                or adapter.reconciliation.last_status != "IN_SYNC"
                or not self.authenticated
                or not adapter.private_stream_healthy
            ):
                return {**unknown, "status": "RECONCILIATION_UNKNOWN", "reason": "EXCHANGE_STATE_NOT_IN_SYNC"}

            net = Decimal(str(snapshot["pilot_net_pnl_usdc"]))
            peak = Decimal(str(snapshot["pilot_peak_pnl_usdc"]))
            realized = Decimal(str(snapshot["realized_pnl_usdc"]))
            unrealized = Decimal(str(snapshot["unrealized_pnl_usdc"]))
            fees = Decimal(str(snapshot["fees_usdc"]))
            funding = Decimal(str(snapshot["funding_usdc"]))
            values = (net, peak, realized, unrealized, fees, funding)
            if any(not value.is_finite() for value in values):
                raise ValueError("accounting aggregate is not finite")
            # Independently verify the ledger identity before publishing values.
            if abs(net - (realized + unrealized - fees + funding)) > Decimal("0.00000001"):
                return {**unknown, "status": "MISMATCH", "reason": "PNL_COMPONENTS_DO_NOT_RECONCILE"}
            last_event_at_val = snapshot.get("last_event_at")
            return {
                "status": "VERIFIED",
                "evidence_status": "VERIFIED",
                "campaign_id": str(session["pilot_campaign_id"]),
                "launch_id": str(session["launch_id"]),
                "net_pnl_usdc": str(net),
                "peak_net_pnl_usdc": str(peak),
                "drawdown_usdc": str(max(Decimal("0"), peak - net)),
                "realized_pnl_usdc": str(realized),
                "unrealized_pnl_usdc": str(unrealized),
                "fees_usdc": str(fees),
                "funding_usdc": str(funding),
                "mark_observed_at": last_mark_at.isoformat(),
                "mark_age_seconds": max(0.0, mark_age),
                # No source-backed reference-price model is currently stored.
                "slippage_usdc": "UNKNOWN",
                "last_event_at": last_event_at_val.isoformat()
                if isinstance(last_event_at_val, datetime) else None,
                "reason": None,
            }
        except Exception as exc:
            logger.error("Local Pilot accounting readback failed: %s", type(exc).__name__)
            return unknown

    async def _reconcile_local_live_pilot_funding(
        self, trigger_symbol: object, trigger_time_ms: object
    ) -> bool:
        """Reconcile funding-triggered USDC income against durable exposure owners."""
        session = self._mainnet_launch_session
        adapter = self.execution_adapter
        persistence_repository = getattr(self.persistence, "repository", None)
        owner_repository = getattr(persistence_repository, "algo_protections", None)
        append_event = getattr(persistence_repository, "append_local_live_pilot_event", None)
        if (
            self.execution_mode != WorkerExecutionMode.LIVE
            or os.getenv("LOCAL_ONLY", "").strip().lower() not in {"1", "true", "yes", "on"}
            or os.getenv("LOCAL_RUNTIME_TARGET", "").strip().upper() != "LOCAL"
            or not isinstance(session, dict)
            or session.get("policy") != "LIVE_RESEARCH_PILOT"
            or not self.persistence.is_connected
            or not callable(getattr(owner_repository, "list_protections", None))
            or not callable(append_event)
            or adapter is None
            or getattr(adapter, "env", None) != BinanceEnvironment.MAINNET
        ):
            self.pause_new_risk = True
            self.engine_state = WorkerEngineState.DEGRADED
            return False
        try:
            trigger_ms = int(str(trigger_time_ms))
            if str(trigger_symbol or "").strip().upper() != "ETHUSDC":
                raise ValueError("funding trigger symbol is outside the approved pilot instrument")
            now = utc_now()
            now_ms = int(now.timestamp() * 1000)
            launch_started = self._session_timestamp(session.get("created_at"))
            if (
                trigger_ms <= 0
                or trigger_ms > now_ms + 2_000
                or launch_started is None
                or launch_started > now
                or (now - launch_started).total_seconds() > 90 * 24 * 60 * 60
            ):
                raise ValueError("Pilot funding history window is invalid or outside Binance retention")
            launch_id = str(session["launch_id"])
            campaign_id = str(session["pilot_campaign_id"])
            owners = await cast(Any, owner_repository).list_protections("binance_mainnet", "ETHUSDC")
            owners = [
                row for row in owners
                if isinstance(row, dict)
                and str(row.get("mainnet_launch_id") or "") == launch_id
                and Decimal(str(row.get("filled_quantity") or "0")) > 0
            ]
            if not owners:
                raise ValueError("Pilot has no durable filled position owner for funding attribution")

            reconciliation = getattr(adapter, "reconciliation", None)
            income_path = getattr(reconciliation, "_income_path", None)
            rest_client = getattr(adapter, "rest_client", None)
            if not isinstance(income_path, str) or not callable(getattr(rest_client, "request", None)):
                raise ValueError("signed Binance income-history route is unavailable")
            seen: dict[str, tuple[int, Decimal]] = {}
            attributable = 0
            max_pages = 10
            for page in range(1, max_pages + 1):
                rows = await cast(Any, rest_client).request(
                    "GET",
                    income_path,
                    signed=True,
                    params={
                        "symbol": "ETHUSDC",
                        "incomeType": "FUNDING_FEE",
                        "startTime": int(launch_started.timestamp() * 1000),
                        "endTime": now_ms,
                        "page": page,
                        "limit": 1000,
                    },
                )
                if not isinstance(rows, list):
                    raise ValueError("signed income-history response is not a list")
                for row in rows:
                    if not isinstance(row, dict):
                        raise ValueError("income-history row is invalid")
                    tran_id = str(row.get("tranId") or "")
                    income_time = int(str(row.get("time") or 0))
                    amount = Decimal(str(row.get("income")))
                    if (
                        str(row.get("incomeType") or "").upper() != "FUNDING_FEE"
                        or str(row.get("symbol") or "").upper() != "ETHUSDC"
                        or str(row.get("asset") or "").upper() != "USDC"
                        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", tran_id)
                        or income_time < int(launch_started.timestamp() * 1000)
                        or income_time > now_ms
                        or not amount.is_finite()
                    ):
                        raise ValueError("funding income row lacks a verified ETHUSDC/USDC identity")
                    prior = seen.get(tran_id)
                    identity = (income_time, amount)
                    if prior is not None and prior != identity:
                        raise ValueError("Binance funding tranId was returned with conflicting values")
                    if prior is not None:
                        continue
                    seen[tran_id] = identity
                    income_at = datetime.fromtimestamp(income_time / 1000.0, tz=timezone.utc)
                    matching_owners = []
                    earliest_fill: datetime | None = None
                    for owner in owners:
                        filled_at = self._session_timestamp(owner.get("first_fill_at"))
                        closed_at = self._session_timestamp(owner.get("closed_at"))
                        if filled_at is None:
                            raise ValueError("funding owner is missing its durable first-fill timestamp")
                        earliest_fill = min(earliest_fill, filled_at) if earliest_fill else filled_at
                        if filled_at <= income_at and (closed_at is None or income_at <= closed_at):
                            matching_owners.append(owner)
                    if not matching_owners and earliest_fill is not None and income_at < earliest_fill:
                        # Account income before this launch's first fill cannot
                        # belong to this campaign and is intentionally ignored.
                        continue
                    if len(matching_owners) != 1:
                        raise ValueError("funding income cannot be uniquely matched to one durable exposure owner")
                    owner = matching_owners[0]
                    append_event_fn = cast(Callable[..., Any], append_event)
                    saved = await append_event_fn(
                        run_id=launch_id,
                        campaign_id=campaign_id,
                        launch_id=launch_id,
                        symbol="ETHUSDC",
                        event_key=f"FUNDING:{tran_id}",
                        event_type="FUNDING",
                        source="BINANCE",
                        observed_at=income_at,
                        net_pnl_delta_usdc=amount,
                        payload={
                            "run_id": launch_id,
                            "exchange_event_id": tran_id,
                            "income_type": "FUNDING_FEE",
                            "asset": "USDC",
                            "income_usdc": str(amount),
                            "owner_entry_client_order_id": str(owner["entry_client_order_id"]),
                        },
                    )
                    if not isinstance(saved, dict):
                        raise RuntimeError("funding event write was not durably confirmed")
                    self._merge_local_pilot_session_readback(saved)
                    attributable += 1
                    if saved.get("pilot_drawdown_triggered") is True:
                        self.pause_new_risk = True
                        self.engine_state = WorkerEngineState.PAUSED_NEW_RISK
                if len(rows) < 1000:
                    break
            else:
                raise ValueError("income-history pagination exceeded its bounded page limit")
            if attributable == 0:
                raise ValueError("funding trigger has no attributable durable USDC income row")
            return True
        except Exception as exc:
            logger.error("Local Pilot funding accounting is unknown: %s", type(exc).__name__)
            self.pause_new_risk = True
            self.engine_state = WorkerEngineState.DEGRADED
            return False

    async def _on_account_snapshot_update(self, snapshot: Any) -> None:
        """Handle authoritative account snapshot update for pilot mark accounting."""
        if (
            self.execution_mode == WorkerExecutionMode.LIVE
            and isinstance(self._mainnet_launch_session, dict)
            and self._mainnet_launch_session.get("policy") == "LIVE_RESEARCH_PILOT"
            and getattr(snapshot, "valid", False)
            and getattr(snapshot, "unrealized_pnl", None) is not None
        ):
            ts = getattr(snapshot, "timestamp", utc_now())
            ts_ms = int(ts.timestamp() * 1000)
            await self._persist_local_live_pilot_mark(
                "ETHUSDC",
                f"{ts_ms}:ETHUSDC:REST_SNAPSHOT",
                Decimal(str(snapshot.unrealized_pnl)),
                ts_ms,
            )

    async def _persist_local_mainnet_close_verified(
        self, record: Dict[str, Any], proof: Dict[str, Any]
    ) -> bool:
        """Persist and read back proof for a verified Local Mainnet emergency close."""
        if (
            self.execution_mode != WorkerExecutionMode.LIVE
            or os.getenv("LOCAL_ONLY", "").strip().lower() not in {"1", "true", "yes", "on"}
            or os.getenv("LOCAL_RUNTIME_TARGET", "").strip().upper() != "LOCAL"
        ):
            logger.error("Local Mainnet close proof rejected outside Local LIVE")
            return False
        readiness = self.persistence.readiness()
        repository = getattr(getattr(self.persistence, "repository", None), "algo_protections", None)
        if (
            str(getattr(self.persistence.mode, "value", self.persistence.mode)).upper() != "REQUIRED"
            or not self.persistence.is_connected
            or readiness.get("durable") is not True
            or not callable(getattr(repository, "close_mainnet_protection_with_proof", None))
            or str(record.get("environment", "")).upper() != "MAINNET"
            or str(record.get("venue", "")).lower() != "binance_mainnet"
            or str(record.get("symbol", "")).upper() != "ETHUSDC"
            or str(record.get("mainnet_launch_id") or self._mainnet_launch_id or "")
            != str(self._mainnet_launch_id or "")
            or not str(record.get("basket_id") or "").strip()
            or str(proof.get("algo_id") or "") != "LOCAL_EMERGENCY_CLOSE"
        ):
            logger.error("Local Mainnet close proof persistence is unavailable or mis-scoped")
            return False
        try:
            stored = await cast(Any, repository).close_mainnet_protection_with_proof(
                str(record["symbol"]),
                str(record["entry_client_order_id"]),
                proof,
            )
        except Exception as exc:
            logger.error("Local Mainnet close proof write failed: %s", type(exc).__name__)
            return False
        evidence = stored.get("closure_evidence") if isinstance(stored, dict) else None
        return bool(
            isinstance(stored, dict)
            and str(stored.get("state", "")).upper() == "CLOSED"
            and str(stored.get("environment", "")).upper() == "MAINNET"
            and str(stored.get("venue", "")).lower() == "binance_mainnet"
            and str(stored.get("symbol", "")).upper() == "ETHUSDC"
            and str(stored.get("entry_client_order_id") or "")
            == str(record.get("entry_client_order_id") or "")
            and str(stored.get("mainnet_launch_id") or "") == str(self._mainnet_launch_id or "")
            and str(stored.get("basket_id") or "") == str(record.get("basket_id") or "")
            and isinstance(evidence, dict)
            and evidence.get("kind") == "LOCAL_EMERGENCY_CLOSE_VERIFIED"
            and evidence.get("client_order_id") == proof.get("client_order_id")
            and evidence.get("order_id") == str(proof.get("order_id"))
        )

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
            await self.persistence.release_mainnet_risk_order_reservation(
                self._mainnet_launch_id,
                str(getattr(order, "client_order_id", "") or ""),
            )
            return
        if normalized == "CONFIRMED":
            marked = await self.persistence.mark_mainnet_risk_order_submitted(
                self._mainnet_launch_id,
                str(getattr(order, "client_order_id", "") or ""),
            )
            if not marked:
                self.kill_switch_active = True
                self.connection_state = ConnectionState.DEGRADED.value
                self.reconciliation_status = "UNKNOWN"
                logger.error(
                    "monitor_event=launch_session_violation reason=submission_mark_failed"
                )
                logger.error("LIVE order was not durably marked; local kill switch is active")
                return
            launch_policy = self._launch_session_value("policy", MAINNET_LAUNCH_STAGED)
            if launch_policy == "LIVE_RESEARCH_PILOT":
                # Serialize a pilot entry lifecycle until its fill accounting
                # has been reconciled by a fresh position snapshot. Keep the
                # reason separate so an operator/reconciliation pause is never
                # accidentally cleared by that snapshot.
                accounting_pause_eligible = not self.pause_new_risk
                self.pause_new_risk = True
                if self._mainnet_launch_id:
                    try:
                        refreshed = await self.persistence.get_mainnet_launch_session(self._mainnet_launch_id)
                        if refreshed:
                            self._set_mainnet_launch_session(refreshed)
                    except Exception as exc:
                        logger.warning("Could not refresh pilot launch session after submission: %s", type(exc).__name__)
                self._pilot_accounting_pause_active = accounting_pause_eligible
                self._refresh_engine_state()
                logger.warning("monitor_event=pilot_order_confirmed pause_new_risk=true")
            elif launch_policy == MAINNET_LAUNCH_STAGED:
                self.pause_new_risk = True
                if self._mainnet_launch_id:
                    try:
                        refreshed = await self.persistence.get_mainnet_launch_session(self._mainnet_launch_id)
                        if refreshed:
                            self._set_mainnet_launch_session(refreshed)
                    except Exception as exc:
                        logger.warning("Could not refresh launch session after submission: %s", exc)
                self._refresh_engine_state()
                logger.warning(
                    "monitor_event=bounded_launch_order_confirmed pause_new_risk=true"
                )
                logger.warning("LIVE bounded launch risk-increasing order confirmed; new risk is paused")
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
    def _has_grid_lineage(value: object, client_order_id: str | None = None) -> bool:
        if any(
            str(intent_id).upper().startswith("GRID-")
            for intent_id in (value or [])
        ):
            return True
        if client_order_id:
            cid = str(client_order_id).upper()
            if cid.startswith("BAI-") or cid.startswith("B-") or cid.startswith("GRID-"):
                return True
        return False

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
        ledger_version = getattr(adapter.ledger, "version", None)
        cached = self._observed_grid_depth_cache.get(normalized_symbol)
        if cached is not None and ledger_version is not None and cached[0] == ledger_version:
            # The ledger only changes on order/fill/position mutations; market
            # events alone cannot move depth, so the scan can be skipped.
            return cached[1]
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
                and (
                    self._has_grid_lineage(order.source_intent_ids, getattr(order, "client_order_id", None))
                    or str(getattr(order, "strategy_id", "")).strip().lower() in {"grid", "structural grid"}
                )
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
                    or self._has_grid_lineage(fill.source_intent_ids, getattr(fill, "client_order_id", None))
                    or str(getattr(fill, "strategy_id", "")).strip().lower() in {"grid", "structural grid"}
                )
            }
            depth = self.grid_engine.observed_depth(
                position_qty=position_qty,
                open_grid_orders=open_grid_orders,
                filled_grid_orders=len(filled_grid_order_ids),
            )
            if ledger_version is not None:
                self._observed_grid_depth_cache[normalized_symbol] = (ledger_version, depth)
            return depth
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
        if not adapter or not active_symbols:
            return False
        if hasattr(adapter, "is_symbol_ready_for_execution"):
            return all(adapter.is_symbol_ready_for_execution(symbol) for symbol in active_symbols)
        symbol_rules = getattr(adapter, "symbol_rules", None)
        if not isinstance(symbol_rules, dict):
            return False
        return all(
            symbol in symbol_rules
            and hasattr(symbol_rules[symbol], "is_ready_for")
            and symbol_rules[symbol].is_ready_for("LIMIT")
            and symbol_rules[symbol].is_ready_for("MARKET")
            for symbol in active_symbols
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
                getattr(snapshot, field, None) is not None
                and Decimal(str(getattr(snapshot, field))) >= 0
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

    def has_open_quick_bracket(self, symbol: str = "ETHUSDC") -> bool:
        """Return True if a QUICK bracket or protected position is currently active."""
        adapter = getattr(self, "execution_adapter", None)
        if adapter is not None and hasattr(adapter, "has_open_quick_bracket"):
            return bool(adapter.has_open_quick_bracket(symbol))
        session = getattr(self, "_mainnet_launch_session", None)
        if isinstance(session, dict) and session.get("policy") == "LIVE_RESEARCH_PILOT":
            if session.get("state") == "ACTIVE" and int(session.get("submitted_orders", 0) or 0) > 0:
                current_exposure = session.get("pilot_current_total_exposure_usdc") or session.get("pilot_net_exposure_usdc")
                if current_exposure is not None:
                    try:
                        if abs(Decimal(str(current_exposure))) > Decimal("0"):
                            return True
                    except (InvalidOperation, TypeError, ValueError):
                        pass
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

        def fail(reason: str) -> bool:
            logger.warning("Mainnet snapshot risk check rejected: %s", reason)
            return False

        if snapshot is None or not getattr(snapshot, "valid", False):
            return fail("snapshot is None or not valid")
        if getattr(adapter, "env", None) != BinanceEnvironment.MAINNET:
            return fail(f"adapter env is not MAINNET: {getattr(adapter, 'env', None)}")
        if freshness_verified is None:
            freshness_check = getattr(adapter, "is_account_snapshot_fresh", None)
            freshness_verified = bool(callable(freshness_check) and freshness_check())
        if not freshness_verified:
            return fail("snapshot freshness_verified is false")

        lease = getattr(adapter, "execution_lease", None)
        if require_execution_lease and bool(getattr(adapter, "execution_lease_required", True)) and (
            lease is None or getattr(lease, "fencing_token", None) is None
        ):
            return fail("execution lease required but missing")
        if str(getattr(snapshot, "collateral_asset", "")).upper() != "USDC":
            return fail(f"collateral_asset is not USDC: {getattr(snapshot, 'collateral_asset', None)}")
        if str(getattr(snapshot, "risk_currency", "")).upper() != "USDC":
            return fail(f"risk_currency is not USDC: {getattr(snapshot, 'risk_currency', None)}")
        if str(getattr(snapshot, "daily_loss_asset", "")).upper() != "USDC":
            return fail(f"daily_loss_asset is not USDC: {getattr(snapshot, 'daily_loss_asset', None)}")
        if not bool(getattr(snapshot, "daily_loss_known", False)):
            return fail("daily_loss_known is false")
        if not bool(getattr(snapshot, "daily_pnl_includes_fees", False)):
            return fail("daily_pnl_includes_fees is false")
        if not bool(getattr(snapshot, "daily_pnl_includes_funding", False)):
            return fail("daily_pnl_includes_funding is false")
        if not bool(getattr(snapshot, "configured_leverage_known", False)):
            return fail("configured_leverage_known is false")
        if not bool(getattr(snapshot, "margin_mode_known", False)):
            return fail("margin_mode_known is false")
        if str(getattr(snapshot, "margin_mode", "")).upper() not in {
            "CROSS",
            "ISOLATED",
            "SINGLE_ASSET_CROSS",
        }:
            return fail(f"unsupported margin_mode: {getattr(snapshot, 'margin_mode', None)}")
        if str(getattr(snapshot, "liquidation_safety", "")).upper() != "KNOWN":
            return fail(f"liquidation_safety is not KNOWN: {getattr(snapshot, 'liquidation_safety', None)}")

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
        except (InvalidOperation, TypeError, ValueError) as exc:
            return fail(f"failed to parse decimal fields: {exc}")

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
            return fail(
                f"limits breached: collateral={collateral} wallet={wallet_balance} avail={available_balance} "
                f"eff_lev={effective_leverage} notional={total_position_notional}"
            )
        if (
            not configured_leverage.is_finite()
            or configured_leverage <= 0
            or configured_leverage > limits.max_leverage
        ):
            return fail(f"configured_leverage out of limits: {configured_leverage}")
        if not daily_pnl.is_finite() or not unrealized_pnl.is_finite():
            return fail(f"daily_pnl or unrealized_pnl not finite: daily={daily_pnl} unrealized={unrealized_pnl}")
        daily_loss = max(Decimal("0"), -(daily_pnl + unrealized_pnl))
        if not daily_loss.is_finite() or daily_loss >= limits.max_daily_loss:
            return fail(f"daily_loss out of limits: {daily_loss} >= {limits.max_daily_loss}")

        liquidation_distance = getattr(snapshot, "min_liquidation_distance_pct", None)
        if total_position_notional != 0:
            try:
                if liquidation_distance is None or not Decimal(str(liquidation_distance)).is_finite() or Decimal(str(liquidation_distance)) <= 0:
                    return fail(f"invalid liquidation_distance for active position: {liquidation_distance}")
            except (InvalidOperation, TypeError, ValueError) as exc:
                return fail(f"failed parsing liquidation_distance: {exc}")

        window_start = getattr(snapshot, "daily_loss_window_start", None)
        window_end = getattr(snapshot, "daily_loss_window_end", None)
        if not isinstance(window_start, datetime) or not isinstance(window_end, datetime):
            return fail(f"window_start/end not datetime: {type(window_start)} {type(window_end)}")
        if window_start.tzinfo is None or window_end.tzinfo is None:
            return fail("window_start/end missing tzinfo")
        start_utc = window_start.astimezone(timezone.utc)
        end_utc = window_end.astimezone(timezone.utc)
        now_utc = utc_now()
        in_window = (
            start_utc.hour == 0
            and start_utc.minute == 0
            and start_utc.second == 0
            and start_utc.microsecond == 0
            and end_utc >= start_utc
            and end_utc <= start_utc + timedelta(days=1)
            and start_utc <= now_utc < end_utc
        )
        if not in_window:
            return fail(f"daily loss window invalid: start={start_utc} end={end_utc} now={now_utc}")
        return True

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
        adapter = self.execution_adapter
        if adapter is None:
            return
        reconciliation = getattr(adapter, "reconciliation", None)
        if getattr(reconciliation, "authentication_failed", False) and hasattr(adapter, "invalidate_authentication"):
            adapter.invalidate_authentication()
        adapter_state = getattr(adapter, "connection_state", None)
        self.connection_state = getattr(adapter_state, "value", str(adapter_state))
        self.authenticated = bool(getattr(adapter, "authenticated", False))
        stream_health = getattr(adapter, "private_stream_healthy", None)
        if stream_health is None:
            stream = getattr(adapter, "user_stream", None)
            stream_health = bool(stream and getattr(stream, "is_connected", False))
        self.private_stream_healthy = bool(stream_health)
        reconcil = getattr(adapter, "reconciliation", None)
        self.reconciliation_status = getattr(reconcil, "last_status", "UNKNOWN") if reconcil else "UNKNOWN"
        self._refresh_engine_state()

    def _adapter_trade_authorized(self) -> bool:
        adapter = self.execution_adapter
        return bool(
            adapter
            and getattr(getattr(adapter, "capabilities", None), "trade_authorized", False)
        )

    def _local_pilot_lifecycle_monitor_state(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        launch_session = self._mainnet_launch_session
        campaign_binding = str(os.getenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "")).strip()
        pilot_bound = (
            isinstance(launch_session, dict)
            and launch_session.get("policy") == "LIVE_RESEARCH_PILOT"
        ) or bool(campaign_binding)
        if not (
            self.execution_mode == WorkerExecutionMode.LIVE
            and pilot_bound
        ):
            return {"status": "NOT_APPLICABLE"}
        observed_at = now or utc_now()
        started = self._pilot_lifecycle_monitor_started_at
        completed = self._pilot_lifecycle_monitor_completed_at
        last_success = self._pilot_lifecycle_monitor_last_success_at
        status = "NOT_RUN"
        if started is not None and (completed is None or started > completed):
            status = "STALLED" if (observed_at - started).total_seconds() > 10 else "RUNNING"
        elif self._pilot_lifecycle_monitor_last_error:
            status = "DEGRADED"
        elif last_success is not None:
            age = (observed_at - last_success).total_seconds()
            status = "HEALTHY" if 0 <= age <= 15 else "STALE"
        return {
            "status": status,
            "last_started_at": started.isoformat() if started else None,
            "last_completed_at": completed.isoformat() if completed else None,
            "last_success_at": last_success.isoformat() if last_success else None,
            "last_error": self._pilot_lifecycle_monitor_last_error,
        }

    def _degrade_after_pilot_monitor_failure(self, adapter: Any) -> None:
        """Stop risk increases when a monitor cycle cannot prove its work."""
        self.pause_new_risk = True
        self.engine_state = WorkerEngineState.DEGRADED
        try:
            adapter.state = ConnectionState.DEGRADED
            reconciliation = getattr(adapter, "reconciliation", None)
            if reconciliation is not None:
                reconciliation.last_status = "UNKNOWN"
        except Exception:
            # The separate monitor status remains degraded even when the
            # adapter cannot expose a mutable connection state.
            pass

    def local_pilot_monitor_allows_new_risk(self) -> bool:
        """Require a recent successful monitor; allow only a bounded active cycle."""
        monitor = self._local_pilot_lifecycle_monitor_state()
        if monitor.get("status") == "HEALTHY":
            return True
        if monitor.get("status") != "RUNNING":
            return False
        try:
            last_success = datetime.fromisoformat(str(monitor["last_success_at"]))
            started = datetime.fromisoformat(str(monitor["last_started_at"]))
            now = utc_now()
            success_age = (now - last_success).total_seconds()
            run_age = (now - started).total_seconds()
            return 0 <= success_age <= 15 and 0 <= run_age <= 10
        except (KeyError, TypeError, ValueError):
            return False

    def enforce_local_pilot_monitor_liveness(self) -> None:
        """Independent heartbeat-loop watchdog blocks risk if pilot monitoring stalls."""
        monitor = self._local_pilot_lifecycle_monitor_state()
        if monitor.get("status") in {"STALLED", "STALE", "DEGRADED"}:
            self._degrade_after_pilot_monitor_failure(self.execution_adapter)

    def _refresh_engine_state(self) -> None:
        """Derive the single operational state from canonical control flags."""
        if self.kill_switch_active:
            self.engine_state = WorkerEngineState.EMERGENCY
        elif self.active_configuration is None:
            self.engine_state = WorkerEngineState.DISARMED
        elif (
            self.execution_mode in {
                WorkerExecutionMode.TESTNET,
                WorkerExecutionMode.LIVE,
            }
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
            self.engine_state = WorkerEngineState.ARMED

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
            local_run_id=os.getenv("LOCAL_RUN_ID", "").strip() if _env_enabled("LOCAL_ONLY") else "",
            pilot_campaign_id=os.getenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "").strip() if _env_enabled("LOCAL_ONLY") else "",
            local_source_fingerprint=os.getenv("LOCAL_SOURCE_FINGERPRINT", "").strip() if _env_enabled("LOCAL_ONLY") else "",
            local_supervisor_instance_id=os.getenv("LOCAL_SUPERVISOR_INSTANCE_ID", "").strip() if _env_enabled("LOCAL_ONLY") else "",
            pilot_lifecycle_monitor=self._local_pilot_lifecycle_monitor_state(),
            pilot_verdict_status=get_pilot_verdict_status(),
            pilot_attempt_diagnostics=self._pilot_attempt_diagnostics(),
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
        local_risk_lifecycle_ready, local_risk_lifecycle_missing = (
            local_mainnet_risk_lifecycle_status(self.execution_adapter, worker=self)
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
            local_mainnet_risk_lifecycle_ready=local_risk_lifecycle_ready,
            local_mainnet_risk_lifecycle_missing=local_risk_lifecycle_missing,
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
            and (
                str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() != "LOCAL"
                or (
                    self._env_flag("LOCAL_ONLY", False)
                    and readiness.local_mainnet_risk_lifecycle_ready
                )
            )
            and self.local_supervisor_heartbeat_is_fresh()
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

        session = launch_session if isinstance(launch_session, dict) else {}
        pilot_policy = session.get("policy") == "LIVE_RESEARCH_PILOT"
        pilot_expires_at = session.get("pilot_campaign_expires_at") or session.get("expires_at")
        pilot_expired = False
        if pilot_policy:
            if isinstance(pilot_expires_at, str):
                try:
                    pilot_expires_dt = datetime.fromisoformat(pilot_expires_at)
                    if pilot_expires_dt.tzinfo is None:
                        pilot_expires_dt = pilot_expires_dt.replace(tzinfo=timezone.utc)
                    pilot_expired = pilot_expires_dt <= utc_now()
                except Exception:
                    pilot_expired = True
            elif isinstance(pilot_expires_at, datetime):
                dt = (
                    pilot_expires_at
                    if pilot_expires_at.tzinfo is not None
                    else pilot_expires_at.replace(tzinfo=timezone.utc)
                )
                pilot_expired = dt <= utc_now()
            else:
                pilot_expired = True

        readiness.local_pilot_execution_ready = bool(
            self.execution_mode == WorkerExecutionMode.LIVE
            and readiness.mainnet_preflight_ready
            and pilot_policy
            and session.get("state") == "ACTIVE"
            and session.get("pilot_status", "ACTIVE") == "ACTIVE"
            and not session.get("pilot_drawdown_triggered", False)
            and not pilot_expired
            and self.engine_state == WorkerEngineState.ARMED
            and not self.pause_new_risk
            and not getattr(self, "_pilot_accounting_pause_active", False)
            and not self.kill_switch_active
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
                    "message": "Required durable PostgreSQL transactional outbox is ready"
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
            if str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL":
                risk_lifecycle_ready, missing_methods = local_mainnet_risk_lifecycle_status(
                    self.execution_adapter, worker=self
                )
                checks.append(
                    {
                        "id": "CHK-LOCAL-MAINNET-RISK-LIFECYCLE",
                        "name": "Local Basket Risk and Protection Lifecycle",
                        "required": True,
                        "status": "PASS" if risk_lifecycle_ready else "FAIL",
                        "message": (
                            "Fresh Local PostgreSQL risk and signed stop/target evidence are verified"
                            if risk_lifecycle_ready
                            else "Local Mainnet remains blocked; lifecycle check failed: "
                            + ", ".join(missing_methods)
                        ),
                    }
                )
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
        # The observation internals were extracted (behavior-preserving) to
        # apps.trading_worker.mainnet_preflight.  Everything they used to read
        # from ``self`` is handed over explicitly via the context; the adapter
        # class is resolved from this module's namespace at call time so that
        # monkeypatching apps.trading_worker.main.BinanceExecutionAdapter keeps
        # intercepting disposable preflight adapter creation.
        result = await mainnet_preflight.run_mainnet_read_only_preflight(
            mainnet_preflight.MainnetPreflightContext(
                worker=self,
                adapter_factory=BinanceExecutionAdapter,
                is_mainnet_configured=self._mainnet_configured,
                is_snapshot_risk_ready=self._is_mainnet_snapshot_risk_ready,
                env_flag=self._env_flag,
            )
        )
        if (
            isinstance(result, dict)
            and str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
        ):
            lifecycle_ready, blockers = local_mainnet_preflight_lifecycle_status(
                result, self.persistence, BinanceExecutionAdapter
            )
            lifecycle_check = next(
                (
                    check
                    for check in result.get("checks", [])
                    if isinstance(check, dict)
                    and check.get("id") == "CHK-PREFLIGHT-LOCAL-RISK-LIFECYCLE"
                ),
                None,
            )
            if lifecycle_check is not None:
                lifecycle_check["status"] = "PASS" if lifecycle_ready else "FAIL"
                lifecycle_check["message"] = (
                    "This read-only preflight verified Local PostgreSQL, launch-history reconciliation, and the stop/target verifier before any order"
                    if lifecycle_ready
                    else "Read-only Local Mainnet lifecycle preflight is incomplete: "
                    + ", ".join(blockers)
                )
            required_checks = [
                check
                for check in result.get("checks", [])
                if isinstance(check, dict) and check.get("required")
            ]
            result["preflightPassed"] = bool(
                required_checks
                and all(check.get("status") == "PASS" for check in required_checks)
            )
            result["canArm"] = False
        return result

    async def set_pause_new_risk(self, active: bool) -> bool:
        """Toggle the deterministic risk pause without bypassing launch gates."""

        if active:
            self._pilot_accounting_pause_active = False

        if not active and self.execution_mode == WorkerExecutionMode.LIVE:
            autonomous = (
                self._launch_session_value("policy") == MAINNET_LAUNCH_AUTONOMOUS
                and self._launch_session_value("state") == "AUTONOMOUS_ACTIVE"
            )
            staged_pending = (
                self._launch_session_value("policy") == MAINNET_LAUNCH_STAGED
                and self._launch_session_value("state") == "ACTIVE"
                and int(self._launch_session_value("submitted_orders", 0) or 0) == 0
            )
            if not autonomous and not staged_pending:
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
                    open_orders_path = getattr(adapter, "_open_orders_path", "/fapi/v1/openOrders")
                    open_orders = await adapter.rest_client.request(
                        "GET", open_orders_path, signed=True
                    )
                    if not isinstance(open_orders, list):
                        return {
                            "status": "UNKNOWN",
                            "reason": "Authoritative openOrders response is invalid.",
                        }
                    open_algos_path = getattr(adapter, "_open_algo_orders_path", None)
                    open_algos = []
                    if open_algos_path:
                        try:
                            algos_resp = await adapter.rest_client.request(
                                "GET", open_algos_path, signed=True, params={"algoType": "CONDITIONAL"}
                            )
                            if isinstance(algos_resp, list):
                                open_algos = algos_resp
                            elif isinstance(algos_resp, dict) and isinstance(algos_resp.get("orders"), list):
                                open_algos = algos_resp["orders"]
                        except Exception as exc:
                            logger.warning("Failed to check open algo orders on kill-switch release: %s", exc)
                            return {
                                "status": "UNKNOWN",
                                "reason": "Failed to verify open algo orders on exchange.",
                            }
                    total_remaining = len(open_orders) + len(open_algos)
                    if total_remaining > 0:
                        return {
                            "status": "PARTIAL",
                            "remaining_orders": total_remaining,
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
                session = self._mainnet_launch_session
                is_pilot = isinstance(session, dict) and session.get("policy") == "LIVE_RESEARCH_PILOT"
                if is_pilot:
                    resume = getattr(self.persistence, "resume_mainnet_pilot_campaign", None)
                    if callable(resume):
                        try:
                            resumed = await resume(self._mainnet_launch_id)
                            if resumed:
                                getter = getattr(self.persistence, "get_mainnet_launch_session", None)
                                if callable(getter):
                                    self._set_mainnet_launch_session(
                                        await getter(self._mainnet_launch_id)
                                    )
                                logger.info("Local Live Pilot campaign resumed successfully after reconciliation")
                        except Exception as exc:
                            logger.error(
                                "Pilot campaign restart resume failed: %s",
                                type(exc).__name__,
                            )
                else:
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
            limits_symbols = TestnetSafetyLimits.from_environment(BinanceEnvironment.TESTNET).allowed_symbols
            supported = limits_symbols | {
                "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "SUIUSDT", "NEARUSDT",
                "LINKUSDT", "ETHUSDC", "BTCUSDC",
            }
        elif req.executionMode == "LIVE":
            limits_symbols = TestnetSafetyLimits.from_environment(BinanceEnvironment.MAINNET).allowed_symbols
            supported = limits_symbols | {
                "ETHUSDC", "BTCUSDC", "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
            }
        else:
            supported = {
                "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
                "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "SUIUSDT", "NEARUSDT",
                "LINKUSDT", "ETHUSDC", "BTCUSDC",
            }
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
        api_key = "".join(mainnet_secret_value("BINANCE_MAINNET_API_KEY").split())
        api_secret = "".join(mainnet_secret_value("BINANCE_MAINNET_API_SECRET").split())
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
                portfolio_margin=is_portfolio_margin_enabled(),
            )
            if self.execution_adapter.ledger:
                self.execution_adapter.ledger.on_order_update = self.persistence.enqueue_order
                self.execution_adapter.ledger.on_fill_update = self.persistence.enqueue_fill
                self.execution_adapter.ledger.on_position_update = self.persistence.enqueue_position
                self.execution_adapter.ledger.on_account_snapshot_update = self._on_account_snapshot_update
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
        first_order_client_order_id = str(
            session.get("first_order_client_order_id", "") or ""
        ).strip() if session else ""
        pending_order_client_order_id = str(
            session.get("pending_order_client_order_id", "") or ""
        ).strip() if session else ""
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
        first_order_identity_ready = bool(
            re.fullmatch(r"[A-Za-z0-9_-]{1,64}", first_order_client_order_id)
            and not pending_order_client_order_id
        )
        add_check(
            "CHK-CONTINUATION-ORDER-IDENTITY",
            "Durable First-Order Client Identity",
            first_order_identity_ready,
            "The first risk-increasing order is bound to a durable client order ID and no ambiguous order remains"
            if first_order_identity_ready
            else "First-order client ID is missing or an exchange order remains ambiguous",
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
            "Immutable Cloud Image or Local Runtime Fingerprint",
            bool(
                session
                and (
                    (
                        str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
                        and session.get("runtime_target") == "LOCAL"
                        and session.get("image_digest") is None
                        and re.fullmatch(
                            r"[0-9a-fA-F]{64}",
                            str(os.getenv("LOCAL_SOURCE_FINGERPRINT", "")).strip(),
                        )
                        and session.get("runtime_fingerprint")
                        == os.getenv("LOCAL_SOURCE_FINGERPRINT", "").strip().lower()
                    )
                    or (
                        str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() != "LOCAL"
                        and session.get("runtime_target", "CLOUD_RUN") == "CLOUD_RUN"
                        and re.fullmatch(r".+@sha256:[0-9a-fA-F]{64}", image_digest)
                        and session.get("image_digest") == image_digest
                    )
                )
            ),
            "Durable session is bound to this exact runtime target"
            if session
            else "Runtime fingerprint or immutable Cloud image does not match the launch session",
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
        local_runtime = str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
        local_persistence_identity = (
            persistence.get("runtime_target") == "LOCAL"
            and persistence.get("database_provider") == "POSTGRES_LOCAL"
            and persistence.get("database_host") == local_postgres_host()
            and persistence.get("database_port") == 5433
            and persistence.get("database_identity_verified") is True
            and self._env_flag("LOCAL_ONLY", False)
        )
        persistence_ready = (
            persistence.get("mode") == "REQUIRED"
            and persistence.get("durable") is True
            and (not local_runtime or local_persistence_identity)
        )
        add_check(
            "CHK-CONTINUATION-PERSISTENCE",
            "Required Durable Persistence",
            persistence_ready,
            "Required durable PostgreSQL persistence is ready"
            if persistence_ready
            else "Continuation requires durable REQUIRED persistence",
        )
        if local_runtime:
            add_check(
                "CHK-CONTINUATION-LOCAL-IDENTITY",
                "Local Runtime and PostgreSQL Identity",
                local_persistence_identity,
                "Local-only runtime is bound to loopback PostgreSQL"
                if local_persistence_identity
                else "Local Mainnet continuation requires LOCAL_ONLY and the approved Local PostgreSQL route on port 5433",
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
        if self.execution_adapter is not None:
            try:
                self.reconciliation_status = await self.execution_adapter.reconciliation.reconcile()
            except Exception as exc:
                logger.error("Execution adapter reconciliation failed: %s", exc)
                self.reconciliation_status = "UNKNOWN"
            self._sync_adapter_state()
        reconciliation_synced = bool(
            preflight_reconciliation and self.reconciliation_status == "IN_SYNC"
        )
        add_check(
            "CHK-CONTINUATION-RECONCILIATION",
            "First-order Reconciliation",
            reconciliation_synced,
            "Durable ledger and Mainnet account are IN_SYNC"
            if reconciliation_synced
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
            "firstOrderClientOrderId": first_order_client_order_id or None,
            "pendingOrderClientOrderId": pending_order_client_order_id or None,
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
        if str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL":
            return False, "Legacy autonomous continuation is unavailable on Local; use the active campaign workflow."
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
        local_runtime = str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
        runtime_target = "LOCAL" if local_runtime else "CLOUD_RUN"
        runtime_fingerprint = os.getenv("LOCAL_SOURCE_FINGERPRINT", "").strip().lower() if local_runtime else None
        image_digest = os.getenv("WORKER_IMAGE_DIGEST", "").strip() if not local_runtime else None
        if local_runtime:
            if (
                not re.fullmatch(r"[0-9a-f]{64}", runtime_fingerprint or "")
                or session.get("runtime_target") != "LOCAL"
                or session.get("image_digest") is not None
                or session.get("runtime_fingerprint") != runtime_fingerprint
            ):
                return False, "Continuation approval does not match the current Local runtime fingerprint"
        elif (
            not re.fullmatch(r".+@sha256:[0-9a-fA-F]{64}", image_digest or "")
            or session.get("runtime_target", "CLOUD_RUN") != "CLOUD_RUN"
            or session.get("image_digest") != image_digest
        ):
            return False, "Continuation approval does not match the current immutable Worker image"
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
                runtime_target=runtime_target,
                runtime_fingerprint=runtime_fingerprint,
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
            if req.launchPolicy not in {MAINNET_LAUNCH_STAGED, "LIVE_RESEARCH_PILOT"}:
                return False, "LIVE ARM launch policy is unsupported."
            release_approval_id = (
                (req.releaseApprovalId or os.getenv("MAINNET_RELEASE_APPROVAL_ID", "")).strip()
            )
            local_runtime = str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
            bound_pilot_campaign = str(os.getenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "")).strip()
            if local_runtime and not bound_pilot_campaign:
                return False, "Local Mainnet LIVE requires a server-bound Research Pilot campaign."
            if bound_pilot_campaign and req.launchPolicy != "LIVE_RESEARCH_PILOT":
                return False, "A pilot-bound Worker cannot use a non-pilot LIVE launch policy."
            if req.launchPolicy == "LIVE_RESEARCH_PILOT":
                pilot_readiness = local_live_pilot_readiness()
                if pilot_readiness["can_start"] is not True:
                    return False, "LIVE_RESEARCH_PILOT_RUNTIME_NOT_READY: " + ",".join(pilot_readiness["blockers"])
                if not local_runtime or os.getenv("LOCAL_ONLY", "").strip().lower() not in {"1", "true", "yes", "on"}:
                    return False, "Live Research Pilot is restricted to LOCAL_ONLY Local runtime."
                if not re.fullmatch(r"pilot-[A-Za-z0-9][A-Za-z0-9_-]{7,126}", req.pilotCampaignId or ""):
                    return False, "LIVE_RESEARCH_PILOT requires a server-bound campaign id."
                if os.getenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "").strip() != req.pilotCampaignId:
                    return False, "Live Research Pilot campaign does not match the supervisor binding."
                if req.strategies.model_dump(exclude_unset=False) != {
                    key: key == os.getenv("LOCAL_LIVE_PILOT_STRATEGY_ID", "")
                    for key in ("grid", "trend", "shock", "carry")
                } or sum(req.strategies.model_dump().values()) != 1:
                    return False, "Live Research Pilot must enable only its approved strategy."
            approval_pattern = (
                r"local-approval-[0-9a-f-]{36}"
                if local_runtime
                else r"approval-[A-Za-z0-9-]{16,120}"
            )
            if not re.fullmatch(approval_pattern, release_approval_id, flags=re.IGNORECASE):
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
                api_key = "".join(mainnet_secret_value("BINANCE_MAINNET_API_KEY").split())
                api_secret = "".join(mainnet_secret_value("BINANCE_MAINNET_API_SECRET").split())
                self.risk_governor.max_leverage = TestnetSafetyLimits.from_environment(
                    exchange_environment
                ).max_leverage
            else:
                api_key = "".join(str(os.getenv("BINANCE_TESTNET_API_KEY", "")).split())
                api_secret = "".join(str(os.getenv("BINANCE_TESTNET_API_SECRET", "")).split())
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
                # LIVE must always start from the scoped durable PostgreSQL
                # ledger loaded for BINANCE_MAINNET. Reusing a process-local
                # or Testnet ledger would make restart/reconciliation evidence
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
                        portfolio_margin=is_portfolio_margin_enabled(),
                    )

                # Bind persistence callbacks on every arm. This also repairs
                # an adapter that was retained while switching between repeated
                # arms in the same environment.
                if self.execution_adapter.ledger:
                    self.execution_adapter.ledger.on_order_update = self.persistence.enqueue_order
                    self.execution_adapter.ledger.on_fill_update = self.persistence.enqueue_fill
                    self.execution_adapter.ledger.on_position_update = self.persistence.enqueue_position
                    self.execution_adapter.ledger.on_account_snapshot_update = self._on_account_snapshot_update
                # Risk-increasing exchange mutations must have a durable
                # transactional-outbox acknowledgement before REST POST.
                self.execution_adapter.before_order_submission = (
                    self._before_order_submission
                )
                self.execution_adapter.on_order_submission_result = (
                    self._on_order_submission_result
                )
                self.execution_adapter.on_testnet_protection_update = (
                    self._persist_testnet_protection_update
                    if mode == "TESTNET"
                    else None
                )
                local_mainnet_runtime = bool(
                    mode == "LIVE"
                    and exchange_environment == BinanceEnvironment.MAINNET
                    and os.getenv("LOCAL_ONLY", "").strip().lower()
                    in {"1", "true", "yes", "on"}
                    and os.getenv("LOCAL_RUNTIME_TARGET", "").strip().upper() == "LOCAL"
                )
                self.execution_adapter.on_local_mainnet_protection_update = (
                    self._persist_local_mainnet_protection_update
                    if local_mainnet_runtime
                    else None
                )
                self.execution_adapter.on_local_mainnet_entry_cancel_claim = (
                    self._claim_local_mainnet_entry_cancel
                    if local_mainnet_runtime
                    else None
                )
                self.execution_adapter.on_local_mainnet_close_verified = (
                    self._persist_local_mainnet_close_verified
                    if local_mainnet_runtime
                    else None
                )
                # Provisional binding: a fresh ARM has no launch session yet, so
                # this only clears stale hooks. The hooks are bound for real after
                # the launch session is created below.
                self._bind_local_live_pilot_callbacks(local_mainnet_runtime)
                if mode == "TESTNET":
                    self.execution_adapter.require_testnet_protection = True
                    repository = getattr(self.persistence, "repository", None)
                    self.execution_adapter.reconciliation.algo_protection_repository = (
                        getattr(repository, "algo_protections", None)
                    )
                    self.execution_adapter.reconciliation.require_testnet_algo_ownership = True
                    history_repository = getattr(repository, "binance_history", None)
                    register_anchor = getattr(history_repository, "register_testnet_anchor", None)
                    if not callable(register_anchor):
                        await self._reset_after_failed_exchange_arm()
                        return False, "Testnet execution requires durable read-only history anchoring."
                    testnet_run_id = f"testnet-readonly-{uuid.uuid4().hex}"
                    try:
                        await register_anchor(
                            run_id=testnet_run_id,
                            symbol="ETHUSDC",
                            anchor_at=datetime.now(timezone.utc),
                        )
                    except Exception as exc:
                        logger.error(
                            "Testnet durable history anchor failed: %s", type(exc).__name__
                        )
                        await self._reset_after_failed_exchange_arm()
                        return False, "Testnet history anchor is unavailable; execution remains disarmed."
                    self.execution_adapter.reconciliation.testnet_history_run_id = testnet_run_id
                
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
                local_runtime = str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
                runtime_target = "LOCAL" if local_runtime else "CLOUD_RUN"
                runtime_fingerprint = os.getenv("LOCAL_SOURCE_FINGERPRINT", "").strip().lower() if local_runtime else None
                image_digest = os.getenv("WORKER_IMAGE_DIGEST", "").strip() if not local_runtime else None
                if local_runtime:
                    if not re.fullmatch(r"[0-9a-f]{64}", runtime_fingerprint or ""):
                        await self._reset_after_failed_exchange_arm()
                        return False, "Local LIVE ARM requires the approved source fingerprint."
                elif not re.fullmatch(r".+@sha256:[0-9a-fA-F]{64}", image_digest or ""):
                    await self._reset_after_failed_exchange_arm()
                    return False, "LIVE ARM requires the immutable WORKER_IMAGE_DIGEST release input."
                try:
                    pilot_binding = None
                    launch_policy = "STAGED_FIRST_ORDER"
                    max_order_count: int | None = 1
                    if req.launchPolicy == "LIVE_RESEARCH_PILOT":
                        launch_policy = "LIVE_RESEARCH_PILOT"
                        max_order_count = 1
                        pilot_binding = {
                            "campaign_id": req.pilotCampaignId,
                            "git_sha": os.getenv("LOCAL_LIVE_PILOT_GIT_SHA", "").strip().lower(),
                            "source_hash": os.getenv("LOCAL_LIVE_PILOT_SOURCE_HASH", "").strip().lower(),
                            "dependency_hash": os.getenv("LOCAL_LIVE_PILOT_DEPENDENCY_HASH", "").strip().lower(),
                            "migration_hash": os.getenv("LOCAL_LIVE_PILOT_MIGRATION_HASH", "").strip().lower(),
                            "strategy_hash": os.getenv("LOCAL_LIVE_PILOT_STRATEGY_HASH", "").strip().lower(),
                            "secret_project_id": os.getenv("LOCAL_LIVE_PILOT_SECRET_PROJECT_ID", "").strip(),
                            "api_key_version": os.getenv("BINANCE_MAINNET_API_KEY_VERSION", "").strip(),
                            "api_secret_version": os.getenv("BINANCE_MAINNET_API_SECRET_VERSION", "").strip(),
                            "management_mode": os.getenv("LOCAL_LIVE_PILOT_MANAGEMENT_MODE", "").strip(),
                            "expires_at": os.getenv("LOCAL_LIVE_PILOT_EXPIRES_AT", "").strip(),
                            "risk_policy_hash": os.getenv("LOCAL_LIVE_PILOT_RISK_POLICY_HASH", "").strip().lower(),
                        }
                    session = await self.persistence.create_mainnet_launch_session(
                        approval_id=release_approval_id,
                        image_digest=image_digest,
                        symbol="ETHUSDC",
                        runtime_target=runtime_target,
                        runtime_fingerprint=runtime_fingerprint,
                        policy=launch_policy,
                        max_risk_increasing_orders=max_order_count,
                        pilot_binding=pilot_binding,
                    )
                except Exception as exc:
                    error_msg = str(exc)
                    logger.error("Mainnet launch session unavailable: %s (%s)", type(exc).__name__, exc)
                    await self._reset_after_failed_exchange_arm()
                    if "submitted order pending review" in error_msg:
                        return False, "LIVE staged launch failed: existing session has a submitted order pending review."
                    return False, "LIVE staged launch session is unavailable; execution remains disarmed."
                if (
                    str(session.get("state", "")) != "ACTIVE"
                    or int(session.get("reserved_orders", 0) or 0) != 0
                    or int(session.get("submitted_orders", 0) or 0) != 0
                ):
                    await self._reset_after_failed_exchange_arm()
                    return False, "LIVE staged launch session is already used or requires reconciliation."
                self._set_mainnet_launch_session(dict(session))
                # Bind the Pilot accounting hooks against the session just created.
                # Fail closed if they cannot be bound for a Pilot session.
                try:
                    pilot_hooks_bound = self._bind_local_live_pilot_callbacks(
                        local_mainnet_runtime
                    )
                except Exception as exc:
                    logger.error("Local Pilot accounting hook binding failed: %s", type(exc).__name__)
                    await self._reset_after_failed_exchange_arm()
                    return False, "LIVE staged launch accounting binding failed; execution remains disarmed."
                if str(session.get("policy", "")) == "LIVE_RESEARCH_PILOT" and not pilot_hooks_bound:
                    await self._reset_after_failed_exchange_arm()
                    return False, "LIVE Pilot accounting hooks are not bound; execution remains disarmed."

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
        self.pause_new_risk = True
        self.engine_state = WorkerEngineState.DISARMED
        local_live = (
            self.execution_mode == WorkerExecutionMode.LIVE
            and _env_enabled("LOCAL_ONLY")
            and str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
        )
        if local_live:
            os.environ["MAINNET_LIVE_APPROVED"] = "false"
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
        if local_live:
            for name in (
                "BINANCE_MAINNET_API_KEY",
                "BINANCE_MAINNET_API_SECRET",
                "BINANCE_MAINNET_API_KEY_FILE",
                "BINANCE_MAINNET_API_SECRET_FILE",
                "MAINNET_RELEASE_APPROVAL_ID",
                "MAINNET_CONTINUATION_APPROVAL_ID",
                "LOCAL_SOURCE_FINGERPRINT",
            ):
                os.environ[name] = ""
            clear_local_mainnet_secrets()
        logger.info("Worker DISARMED")

    # The risk context needs the account snapshot <= 5s old across the whole
    # entry chain (clamp, gate #1, final fence: several seconds), while the
    # general worker tolerance is 30s. Refresh just before the chain starts.
    PILOT_ENTRY_SNAPSHOT_MAX_AGE_SEC = 1.5

    async def _ensure_fresh_pilot_account_snapshot(self) -> bool:
        adapter = self.execution_adapter
        if adapter is None:
            return False
        snapshot = getattr(adapter, "account_snapshot", None)
        timestamp = getattr(snapshot, "timestamp", None)
        if isinstance(timestamp, datetime) and timestamp.tzinfo is not None:
            age = (utc_now() - timestamp).total_seconds()
            if 0 <= age <= self.PILOT_ENTRY_SNAPSHOT_MAX_AGE_SEC:
                return True
        reconciler = getattr(getattr(adapter, "reconciliation", None), "reconcile", None)
        if not callable(reconciler):
            return False
        try:
            result = await reconciler()
        except Exception as exc:
            logger.warning("Pilot entry snapshot refresh failed: %s", type(exc).__name__)
            return False
        if result != "IN_SYNC":
            return False
        self.reconciliation_status = "IN_SYNC"
        return True

    def _pilot_basket_id(self) -> Optional[str]:
        """One stable basket per launch for every pilot bracket.

        The launch session binds its basket once and later reservations and the
        order risk context accept only that basket, so a random basket per
        decision strands the campaign after the first aborted attempt.
        """
        session = self._mainnet_launch_session
        if not isinstance(session, dict):
            return None
        bound = str(session.get("basket_id") or "").strip()
        if bound:
            return bound
        launch_id = str(session.get("launch_id") or "").strip()
        if not launch_id:
            return None
        digest = hashlib.sha256(f"pilot-basket:{launch_id}".encode("utf-8")).hexdigest()
        return f"pilot-{digest[:16]}"

    async def _clamp_order_notional_if_needed(
        self,
        decision,
        reference_price: Optional[Decimal] = None,
    ):
        """Clamp risk-increasing orders to safely satisfy venue single-order notional caps."""
        pilot_session = getattr(self, "_mainnet_launch_session", None)
        is_local_pilot_decision = (
            self.execution_mode == WorkerExecutionMode.LIVE
            and str(getattr(decision, "symbol", "")).upper() == "ETHUSDC"
            and (
                bool(os.getenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "").strip())
                or (
                    isinstance(pilot_session, dict)
                    and pilot_session.get("policy") == "LIVE_RESEARCH_PILOT"
                )
            )
        )
        if is_local_pilot_decision and any(
            not getattr(order, "reduce_only", False) and order.side != OrderSide.BUY
            for order in getattr(decision, "orders", [])
        ):
            raise ValueError("Local live pilot accepts BUY entries only")
        if self.execution_adapter is None:
            return decision
        limits = getattr(self.execution_adapter, "safety_limits", None)
        rules = (
            getattr(self.execution_adapter, "symbol_rules", {}).get(decision.symbol)
            if self.execution_adapter
            else None
        )
        price = reference_price
        if limits and rules and ((price and price > 0) or hasattr(self.execution_adapter, "get_fresh_market_price")):
            max_order_notional = getattr(limits, "max_single_order_notional", None)
            if max_order_notional and max_order_notional > 0:
                new_orders = []
                clamped_any = False
                for order in getattr(decision, "orders", []):
                    if not getattr(order, "reduce_only", False):
                        is_live_pilot = (
                            self.execution_mode == WorkerExecutionMode.LIVE
                            and str(getattr(decision, "symbol", "")).upper() == "ETHUSDC"
                            and (
                                bool(os.getenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "").strip())
                                or (
                                    isinstance(self._mainnet_launch_session, dict)
                                    and self._mainnet_launch_session.get("policy") == "LIVE_RESEARCH_PILOT"
                                )
                            )
                        )
                        if is_live_pilot and (
                            order.stop_loss_price is None
                            or order.take_profit_price is None
                            or getattr(order, "management_mode", None) != "QUICK"
                        ):
                            try:
                                side_str = "BUY" if order.side == OrderSide.BUY else "SELL"
                                side_price = None
                                if hasattr(self.execution_adapter, "get_fresh_market_price"):
                                    price_getter = self.execution_adapter.get_fresh_market_price
                                    if callable(price_getter):
                                        res = price_getter(decision.symbol, side_str)
                                        if inspect.iscoroutine(res):
                                            res = await res
                                        if res is not None and res > 0:
                                            side_price = res
                                if side_price is None:
                                    cached = (
                                        getattr(self.execution_adapter, "last_market_ask", {}).get(decision.symbol)
                                        if side_str == "BUY"
                                        else getattr(self.execution_adapter, "last_market_bid", {}).get(decision.symbol)
                                    )
                                    if cached is not None and cached > 0:
                                        side_price = cached
                                if side_price is None and price is not None and price > 0:
                                    side_price = price
                                if side_price is None or side_price <= 0:
                                    raise ValueError(
                                        f"Fresh side market price unavailable for {decision.symbol} {side_str}"
                                    )

                                # 1. Provisional bracket plan at side_price with default costs
                                provisional_plan = plan_pilot_bracket(
                                    rules=rules,
                                    entry_price=side_price,
                                    side=order.side,
                                    notional_cap_usdc=Decimal(str(LOCAL_LIVE_PILOT_POLICY.get("order_notional_usdc", "50"))),
                                    entry_target_notional_usdc=Decimal(str(LOCAL_LIVE_PILOT_POLICY.get(
                                        "entry_target_notional_usdc", DEFAULT_ENTRY_TARGET_NOTIONAL_USDC
                                    ))),
                                    execution_risk_buffer_usdc=Decimal(str(LOCAL_LIVE_PILOT_POLICY.get(
                                        "execution_risk_buffer_usdc", DEFAULT_EXECUTION_RISK_BUFFER_USDC
                                    ))),
                                )
                                provisional_order = apply_pilot_bracket_to_intent(
                                    order, provisional_plan, basket_id=self._pilot_basket_id()
                                )

                                # 2. Cost-aware pre-planning if adapter provides cost evidence
                                cost_provider = getattr(
                                    self.execution_adapter, "get_local_mainnet_cost_evidence", None
                                )
                                if callable(cost_provider):
                                    context = {
                                        "runtime_target": "LOCAL",
                                        "validated_quantity": provisional_plan.quantity,
                                        "validated_entry_price": side_price,
                                    }
                                    risk_ctx_provider = getattr(
                                        self.execution_adapter, "get_local_mainnet_risk_context", None
                                    )
                                    if callable(risk_ctx_provider):
                                        try:
                                            ctx_res = risk_ctx_provider(provisional_order, context)
                                            if inspect.iscoroutine(ctx_res):
                                                ctx_res = await ctx_res
                                            if isinstance(ctx_res, dict):
                                                # Keep validated_quantity/entry_price: the cost
                                                # provider reads them from this same dict.
                                                context = {**context, **ctx_res}
                                        except Exception as ctx_err:
                                            logger.warning(
                                                "Risk context resolution before cost evidence failed: %s", ctx_err
                                            )

                                    cost_evidence = cost_provider(provisional_order, context)
                                    if inspect.iscoroutine(cost_evidence):
                                        cost_evidence = await cost_evidence

                                    if not isinstance(cost_evidence, dict):
                                        raise ValueError(
                                            "Exchange-derived cost evidence unavailable for pilot bracket pre-planning"
                                        )

                                    actual_fees = Decimal(str(cost_evidence.get("fees_upper_bound_usdc", "0")))
                                    actual_funding = Decimal(str(cost_evidence.get("funding_upper_bound_usdc", "0")))
                                    actual_slippage = Decimal(str(cost_evidence.get("slippage_upper_bound_usdc", "0")))
                                    campaign_headroom = Decimal(str(LOCAL_LIVE_PILOT_POLICY.get(
                                        "campaign_drawdown_usdc", "5"
                                    )))
                                    context_headrooms = [
                                        Decimal(str(context[name]))
                                        for name in ("basket_headroom_usdc", "daily_loss_headroom_usdc")
                                        if context.get(name) is not None
                                    ]
                                    if context_headrooms:
                                        campaign_headroom = min(campaign_headroom, *context_headrooms)

                                    re_planned = plan_pilot_bracket(
                                        rules=rules,
                                        entry_price=side_price,
                                        side=order.side,
                                        notional_cap_usdc=Decimal(str(LOCAL_LIVE_PILOT_POLICY.get("order_notional_usdc", "50"))),
                                        entry_target_notional_usdc=Decimal(str(LOCAL_LIVE_PILOT_POLICY.get(
                                            "entry_target_notional_usdc", DEFAULT_ENTRY_TARGET_NOTIONAL_USDC
                                        ))),
                                        execution_risk_buffer_usdc=Decimal(str(LOCAL_LIVE_PILOT_POLICY.get(
                                            "execution_risk_buffer_usdc", DEFAULT_EXECUTION_RISK_BUFFER_USDC
                                        ))),
                                        campaign_drawdown_headroom_usdc=campaign_headroom,
                                        estimated_fees_usdc=actual_fees,
                                        estimated_funding_usdc=actual_funding,
                                        estimated_slippage_usdc=actual_slippage,
                                    )

                                    # Enforce quantity parity
                                    if re_planned.quantity != provisional_plan.quantity:
                                        raise ValueError(
                                            f"Pilot bracket pre-planning quantity parity mismatch: "
                                            f"provisional {provisional_plan.quantity} != re-planned {re_planned.quantity}"
                                        )

                                    bracket_plan = re_planned
                                else:
                                    bracket_plan = provisional_plan

                                order = apply_pilot_bracket_to_intent(
                                    order, bracket_plan, basket_id=self._pilot_basket_id()
                                )
                                new_orders.append(order)
                                clamped_any = True
                                continue
                            except Exception as bracket_err:
                                logger.error(
                                    "Unable to derive pilot bracket for order %s: %s",
                                    order.client_order_id,
                                    bracket_err,
                                )
                                raise RuntimeError(
                                    f"Pilot bracket derivation failed for {order.client_order_id}: {bracket_err}"
                                ) from bracket_err
                        est_price = price
                        if est_price is None or est_price <= 0:
                            side_str = "BUY" if order.side == OrderSide.BUY else "SELL"
                            if hasattr(self.execution_adapter, "get_fresh_market_price"):
                                price_getter = self.execution_adapter.get_fresh_market_price
                                if callable(price_getter):
                                    res = price_getter(decision.symbol, side_str)
                                    if inspect.iscoroutine(res):
                                        res = await res
                                    if res is not None and res > 0:
                                        est_price = res
                        if est_price and est_price > 0:
                            est_notional = order.quantity * est_price
                            order_type_val = getattr(order.order_type, "value", order.order_type)
                            min_notional = rules.min_notional_for(order_type_val)
                            if est_notional > max_order_notional:
                                # Target 90% of max notional, capped at 45 USDC for the 50 USDC pilot limit
                                target_notional = min(max_order_notional * Decimal("0.90"), Decimal("45.0"))
                                target_qty = target_notional / est_price
                                is_market = order_type_val == OrderType.MARKET.value
                                clamped_qty = rules.normalize_quantity(target_qty, is_market=is_market)
                                min_qty = rules.market_min_qty if is_market and rules.market_min_qty else rules.min_qty
                                if (
                                    clamped_qty >= min_qty
                                    and (clamped_qty * est_price) >= min_notional
                                    and (clamped_qty * est_price) <= max_order_notional
                                ):
                                    logger.info(
                                        "Clamping order %s quantity from %s to %s to satisfy single-order cap %s",
                                        order.client_order_id,
                                        order.quantity,
                                        clamped_qty,
                                        max_order_notional,
                                    )
                                    new_orders.append(order.model_copy(update={"quantity": clamped_qty}))
                                    clamped_any = True
                                    continue
                            elif min_notional > 0 and est_notional < min_notional:
                                target_notional = min(
                                    min_notional * Decimal("1.25"),
                                    max_order_notional * Decimal("0.90"),
                                    Decimal("45.0"),
                                )
                                target_qty = target_notional / est_price
                                is_market = order_type_val == OrderType.MARKET.value
                                clamped_qty = rules.normalize_quantity(target_qty, is_market=is_market)
                                min_qty = rules.market_min_qty if is_market and rules.market_min_qty else rules.min_qty
                                if (
                                    clamped_qty >= min_qty
                                    and (clamped_qty * est_price) >= min_notional
                                    and (clamped_qty * est_price) <= max_order_notional
                                ):
                                    logger.info(
                                        "Bumping order %s quantity from %s to %s to satisfy exchange min_notional %s (capped at %s)",
                                        order.client_order_id,
                                        order.quantity,
                                        clamped_qty,
                                        min_notional,
                                        max_order_notional,
                                    )
                                    new_orders.append(order.model_copy(update={"quantity": clamped_qty}))
                                    clamped_any = True
                                    continue
                    new_orders.append(order)
                if clamped_any:
                    new_delta = sum(
                        (
                            o.quantity if o.side == OrderSide.BUY else -o.quantity
                            for o in new_orders
                        ),
                        Decimal("0.0"),
                    )
                    return decision.model_copy(
                        update={
                            "orders": new_orders,
                            "net_exposure_delta": new_delta,
                        }
                    )
        return decision

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
        if (
            self.execution_mode == WorkerExecutionMode.TESTNET
            and str(getattr(decision.risk_class, "value", decision.risk_class)).upper()
            in {"NEW_RISK", "INCREASE_RISK"}
        ):
            # Normal strategy events must use the same guarded lifecycle as
            # the explicit Testnet runner; no risk-increasing bypass remains.
            return await self.execute_protected_testnet_decision(decision)
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

    async def execute_protected_testnet_decision(self, decision):
        """Worker-owned Testnet market entry with mandatory post-fill protection."""
        if self.execution_mode != WorkerExecutionMode.TESTNET or self.execution_adapter is None:
            raise RuntimeError("Protected entry execution is available only in an armed Testnet worker")
        repository = getattr(self.persistence, "repository", None)
        protection_store = getattr(repository, "algo_protections", None)
        if protection_store is not None:
            try:
                active_protections = await protection_store.list_active_protections(
                    venue="binance_testnet", symbol="ETHUSDC"
                )
            except Exception as exc:
                await self._fail_closed_after_autonomous_execution_error(
                    RuntimeError(
                        f"Testnet protection ownership could not be read: {type(exc).__name__}"
                    )
                )
                return []
            if active_protections:
                await self._fail_closed_after_autonomous_execution_error(
                    RuntimeError("An unresolved Testnet protection chain already exists")
                )
                return []
        allowed, reason = self._evaluate_execution_gate(decision)
        if not allowed:
            raise RuntimeError(f"Protected Testnet execution gate blocked entry: {reason}")
        adapter = self.execution_adapter
        adapter.bind_worker_authority(self)
        executed = await adapter.execute_protected_testnet_decision(
            decision, authority=self,
        )
        if (
            adapter.connection_state != ConnectionState.READY
            or getattr(adapter.reconciliation, "last_status", "UNKNOWN") != "IN_SYNC"
            or adapter.last_testnet_protection.get("status") != "PROTECTED"
        ):
            await self._fail_closed_after_autonomous_execution_error(
                RuntimeError("Protected Testnet entry did not finish protected, READY and IN_SYNC")
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

    async def close_protected_ethusdc_testnet_trial(self, entry_client_order_id: str) -> Dict[str, Any]:
        """Close one owned Testnet trial and read back the exchange and durable owner."""
        adapter = self.execution_adapter
        if (self.execution_mode != WorkerExecutionMode.TESTNET or adapter is None
                or getattr(adapter, "env", None) != BinanceEnvironment.TESTNET):
            raise RuntimeError("Protected Testnet trial close requires an armed Testnet Worker")
        repository = getattr(self.persistence, "repository", None)
        protections = getattr(repository, "algo_protections", None)
        if protections is None or not entry_client_order_id:
            raise RuntimeError("Protected Testnet trial owner is unavailable")
        owners = await protections.list_active_protections(
            venue="binance_testnet", symbol="ETHUSDC"
        )
        if len(owners) != 1 or owners[0].get("entry_client_order_id") != entry_client_order_id:
            raise RuntimeError("Protected Testnet trial owner is ambiguous")
        owner = dict(owners[0])
        if owner.get("state") != "PROTECTED" or not all(
            owner.get(key) for key in (
                "stop_client_algo_id", "take_profit_client_algo_id",
                "stop_algo_id", "take_profit_algo_id",
            )
        ):
            raise RuntimeError("Protected Testnet trial bracket is unverified")
        close_id = adapter.testnet_trial_close_client_order_id(entry_client_order_id)
        self.pause_new_risk = True
        self._refresh_engine_state()
        claimed_reason = f"protected_ethusdc_testnet_trial_close:{close_id}:CLAIMED"
        claimed = await protections.claim_testnet_protection_close(
            venue="binance_testnet",
            symbol="ETHUSDC",
            entry_client_order_id=entry_client_order_id,
            entry_side=owner["entry_side"],
            position_side=owner["position_side"],
            filled_quantity=Decimal(str(owner["filled_quantity"])),
            stop_algo_id=str(owner["stop_algo_id"]),
            take_profit_algo_id=str(owner["take_profit_algo_id"]),
            state_reason=claimed_reason,
        )
        if not isinstance(claimed, dict):
            raise RuntimeError("Protected Testnet trial close was already claimed or changed")
        owner = dict(claimed)

        # The repository changes CLAIMED -> SUBMITTING with compare-and-swap
        # immediately before the sole POST. An ambiguous result is never retried.
        close_orders = await adapter.close_owned_testnet_trial(owner, authority=self)
        if adapter.last_emergency_result.get("status") != "CONFIRMED" or len(close_orders) != 1:
            raise RuntimeError("Protected Testnet trial close outcome is unknown")
        protection_at_close = adapter.last_emergency_result.get("protection_at_close")
        if (not isinstance(protection_at_close, dict)
                or protection_at_close.get("status") != "PROTECTED"
                or not isinstance(protection_at_close.get("stop"), dict)
                or not isinstance(protection_at_close.get("target"), dict)
                or protection_at_close["stop"].get("client_algo_id") != owner["stop_client_algo_id"]
                or protection_at_close["stop"].get("status") != "NEW"
                or not (
                    (protection_at_close["stop"].get("close_position") is True and protection_at_close["stop"].get("reduce_only") is False)
                    or (protection_at_close["stop"].get("close_position") is False and protection_at_close["stop"].get("reduce_only") is True)
                )
                or protection_at_close["target"].get("client_algo_id") != owner["take_profit_client_algo_id"]
                or protection_at_close["target"].get("status") != "NEW"
                or not (
                    (protection_at_close["target"].get("close_position") is True and protection_at_close["target"].get("reduce_only") is False)
                    or (protection_at_close["target"].get("close_position") is False and protection_at_close["target"].get("reduce_only") is True)
                )):
            raise RuntimeError("Protected Testnet close lacks pre-submission stop/target read-back")
        close_id = str(close_orders[0].client_order_id or "")
        close_exchange_order_id = str(close_orders[0].exchange_order_id or "")
        close_readback = await adapter.query_order("ETHUSDC", close_id)
        close_side = "SELL" if str(owner["entry_side"]).upper() == "BUY" else "BUY"
        expected_quantity = Decimal(str(owner["filled_quantity"]))
        if (not close_id or not close_exchange_order_id or not isinstance(close_readback, dict)
                or str(close_readback.get("orderId", "")) != close_exchange_order_id
                or str(close_readback.get("clientOrderId", "")) != close_id
                or str(close_readback.get("symbol", "")).upper() != "ETHUSDC"
                or str(close_readback.get("side", "")).upper() != close_side
                or str(close_readback.get("positionSide", "")).upper() != "BOTH"
                or str(close_readback.get("type", "")).upper() != "MARKET"
                or str(close_readback.get("reduceOnly", "")).lower() != "true"
                or Decimal(str(close_readback.get("origQty", "0"))) != expected_quantity
                or str(close_readback.get("status", "")).upper() != "FILLED"
                or Decimal(str(close_readback.get("executedQty", "0"))) != expected_quantity):
            raise RuntimeError("Protected Testnet trial close order was not verified")
        if not await adapter._cancel_local_mainnet_owned_algos(owner):
            raise RuntimeError("Protected Testnet trial Algo cancellation is unknown")

        positions = await adapter.rest_client.request(
            "GET", adapter._position_risk_path, signed=True
        )
        open_orders = await adapter.rest_client.request(
            "GET", "/fapi/v1/openOrders", signed=True, params={"symbol": "ETHUSDC"}
        )
        open_algos = await adapter.rest_client.request(
            "GET", adapter._open_algo_orders_path, signed=True,
            params={"symbol": "ETHUSDC", "algoType": "CONDITIONAL"},
        )
        if not isinstance(positions, list) or not isinstance(open_orders, list) or not isinstance(open_algos, list):
            raise RuntimeError("Protected Testnet trial final exchange read-back is invalid")
        if any(
            isinstance(row, dict) and row.get("symbol") == "ETHUSDC"
            and Decimal(str(row.get("positionAmt", "0"))) != 0
            for row in positions
        ) or open_orders or open_algos:
            raise RuntimeError("Protected Testnet trial left exchange risk open")
        if await adapter.reconciliation.reconcile() != "IN_SYNC" or adapter.reconciliation.last_diffs:
            raise RuntimeError("Protected Testnet trial final reconciliation is unknown")
        owner.update(state="CLOSED", state_reason="protected_ethusdc_testnet_trial_verified")
        if not await self._persist_testnet_protection_update(owner):
            raise RuntimeError("Protected Testnet trial closure was not durable")
        return {
            "close_status": "VERIFIED",
            "close_client_order_id": close_id,
            "close_order_type": "MARKET",
            "close_order_reduce_only": True,
            "filled_quantity": str(owner["filled_quantity"]),
            "protection_at_close": protection_at_close,
            "position_after": [], "open_orders_after": [], "open_algo_after": [],
            "reconciliation_status": "IN_SYNC", "diff_count": 0,
        }

    async def claim_testnet_trial_close_submission(
        self, owner: Dict[str, Any], client_order_id: str,
    ) -> Dict[str, Any] | None:
        """Consume the durable one-use close submission marker using CAS."""
        if (self.execution_mode != WorkerExecutionMode.TESTNET
                or self.execution_adapter is None
                or getattr(self.execution_adapter, "env", None) != BinanceEnvironment.TESTNET
                or not isinstance(owner, dict) or owner.get("state") != "CLOSE_PENDING"):
            return None
        expected = f"protected_ethusdc_testnet_trial_close:{client_order_id}:CLAIMED"
        if owner.get("state_reason") != expected:
            return None
        repository = getattr(getattr(self.persistence, "repository", None), "algo_protections", None)
        marker = getattr(repository, "mark_testnet_protection_close_submitting", None)
        if not callable(marker):
            return None
        submitting_reason = f"protected_ethusdc_testnet_trial_close:{client_order_id}:SUBMITTING"
        try:
            stored = await marker(
                venue="binance_testnet", symbol="ETHUSDC",
                entry_client_order_id=str(owner.get("entry_client_order_id") or ""),
                claimed_reason=expected, submitting_reason=submitting_reason,
            )
        except Exception as exc:
            logger.error("Testnet close attempt fence failed: %s", type(exc).__name__)
            return None
        if (not isinstance(stored, dict) or stored.get("state") != "CLOSE_PENDING"
                or stored.get("state_reason") != submitting_reason
                or stored.get("entry_client_order_id") != owner.get("entry_client_order_id")):
            return None
        return dict(stored)

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
        pilot_session = getattr(self, "_mainnet_launch_session", None)
        is_local_pilot = (
            self.execution_mode == WorkerExecutionMode.LIVE
            and (
                (isinstance(pilot_session, dict) and pilot_session.get("policy") == "LIVE_RESEARCH_PILOT")
                or bool(os.getenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "").strip())
            )
        )
        pilot_market_recorded = False
        if is_local_pilot:
            now = utc_now()
            age_sec = (now - event_timestamp.astimezone(timezone.utc)).total_seconds()
            adapter = self.execution_adapter
            age_limit = 3.0
            if adapter is not None and callable(getattr(adapter, "_market_data_max_age", None)):
                try:
                    age_limit = float(adapter._market_data_max_age())
                except (TypeError, ValueError):
                    age_limit = 0.0
            if not math.isfinite(age_limit) or age_limit <= 0 or age_sec < -0.5 or age_sec > age_limit:
                logger.warning("Ignoring stale or future Local Pilot market sample for %s", symbol)
                return
            source = "mark" if event.mark_price is not None else "book"
            try:
                bid = Decimal(str(event.best_bid))
                ask = Decimal(str(event.best_ask))
                mark = Decimal(str(event.mark_price if event.mark_price is not None else event.last_price))
            except (InvalidOperation, TypeError, ValueError):
                return
            if not mark.is_finite() or mark <= 0:
                return
            if source == "book" and (
                not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask <= bid
            ):
                logger.warning("Ignoring invalid Local Pilot bookTicker quote for %s", symbol)
                return
            event_id = str(event.event_id or "").strip()
            suffix = re.search(r"(\d+)$", event_id)
            sequence = int(suffix.group(1)) if suffix else -1
            sample_order = (event_timestamp.astimezone(timezone.utc), sequence, event_id)
            if not event_id:
                return
            previous = getattr(self, "_local_pilot_market_samples", {}).get((symbol, source))
            if previous is not None and sample_order <= previous:
                logger.warning("Ignoring duplicate or out-of-order Local Pilot %s sample for %s", source, symbol)
                return
            samples = getattr(self, "_local_pilot_market_samples", None)
            if samples is None:
                samples = self._local_pilot_market_samples = {}
            samples[(symbol, source)] = sample_order
            if adapter is None or not callable(getattr(adapter, "record_market_event", None)):
                return
            if not adapter.record_market_event(event):
                return
            pilot_market_recorded = True
            if source == "mark":
                # Keep mark/reference freshness separate. It is never a grid
                # or price-action signal input for the real-money pilot.
                return
            midpoint = (bid + ask) / Decimal("2")
            event = event.model_copy(update={"last_price": midpoint})
        if self.execution_mode in {
            WorkerExecutionMode.TESTNET,
            WorkerExecutionMode.LIVE,
        } and self.execution_adapter is not None:
            if not pilot_market_recorded and not self.execution_adapter.record_market_event(event):
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
        if not self.active_configuration:
            return

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

        if (
            self.execution_mode == WorkerExecutionMode.LIVE
            and getattr(self, "engine_state", None) == WorkerEngineState.ARMED
        ):
            now_mono = time.monotonic()
            if now_mono - getattr(self, "_last_live_eval_log_at", 0.0) >= 10.0:
                self._last_live_eval_log_at = now_mono
                grid_delta = grid_intent.desired_delta_qty if grid_intent else None
                logger.info(
                    "[MAINNET_EVAL] symbol=%s price=%s depth=%s reclaim=%s grid_delta=%s",
                    event.symbol,
                    event.last_price,
                    grid_depth,
                    getattr(pa_state, "is_reclaiming", None),
                    grid_delta,
                )
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
                    reconciler = getattr(getattr(self.execution_adapter, "reconciliation", None), "reconcile", None)
                    if callable(reconciler):
                        try:
                            sync_result = await reconciler()
                            if sync_result == "IN_SYNC":
                                self.reconciliation_status = "IN_SYNC"
                                snapshot = await self.execution_adapter.ledger.get_account_snapshot()
                        except Exception as exc:
                            logger.warning("Auto-reconcile on missing snapshot failed: %s", exc)

                if (
                    snapshot is None
                    or not getattr(snapshot, "valid", False)
                    or getattr(snapshot, "exchange_environment", None)
                    != self._current_exchange_label()
                    or not self.is_account_snapshot_ready()
                ):
                    logger.error("No account snapshot available from execution adapter")
                    self.connection_state = "DEGRADED"
                    # A staged launch still requires an authoritative account
                    # snapshot before the first risk-increasing decision.  The
                    # staged order limit is an order-count guard, not a
                    # substitute for account truth.
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
                now_monotonic = time.monotonic()
                if now_monotonic - self._last_risk_snapshot_enqueued_at >= 10.0:
                    self.persistence.enqueue_risk_snapshot(risk_snapshot)
                    self._last_risk_snapshot_enqueued_at = now_monotonic
                
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
            self._note_pilot_attempt("SIGNAL")
            if self.engine_state in EXECUTABLE_ENGINE_STATES:
                if self.execution_mode in {
                    WorkerExecutionMode.TESTNET,
                    WorkerExecutionMode.LIVE,
                } and self.execution_adapter is not None:
                    launch_readiness = self.get_launch_readiness()
                    if self.execution_mode == WorkerExecutionMode.LIVE:
                        autonomous_enabled = self._env_flag("MAINNET_LIVE_APPROVED", False)
                        policy = str(self._launch_session_value("policy", MAINNET_LAUNCH_STAGED))
                        if policy == MAINNET_LAUNCH_STAGED:
                            live_ready = bool(
                                launch_readiness.get("mainnet_preflight_ready")
                                and launch_readiness.get("mainnet_launch_state") == "ACTIVE"
                                and self.engine_state == WorkerEngineState.ARMED
                                and not self.pause_new_risk
                            )
                        elif policy == "LIVE_RESEARCH_PILOT":
                            live_ready = bool(launch_readiness.get("local_pilot_execution_ready"))
                        else:
                            live_ready = False
                        environment_name = "MAINNET"
                        is_ready = autonomous_enabled and live_ready
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
                        is_ready = autonomous_enabled and bool(launch_readiness.get(readiness_key, False))
                    if is_ready:
                        if self.execution_mode == WorkerExecutionMode.LIVE and policy == "LIVE_RESEARCH_PILOT":
                            session_gate = getattr(self, "_pilot_session_gate", None)
                            if not callable(session_gate):
                                self._note_pilot_attempt("EXECUTION_BLOCKED", "Durable pilot session timer gate is unavailable")
                                return decision
                            session_allowed, session_reason = await session_gate()
                            if not session_allowed:
                                self._note_pilot_attempt("EXECUTION_BLOCKED", session_reason)
                                return decision
                        if (
                            self.execution_mode == WorkerExecutionMode.LIVE
                            and policy == "LIVE_RESEARCH_PILOT"
                            and not await self._ensure_fresh_pilot_account_snapshot()
                        ):
                            self._note_pilot_attempt(
                                "EXECUTION_BLOCKED",
                                "Account snapshot could not be refreshed and reconciled before entry",
                            )
                            return decision
                        try:
                            decision = await self._clamp_order_notional_if_needed(decision, event.last_price)
                        except Exception as clamp_err:
                            logger.error(
                                "[%s][PREPLAN_FAILED] Order clamping or bracket pre-planning failed for %s: %s",
                                environment_name,
                                decision.decision_id,
                                clamp_err,
                            )
                            self._note_pilot_attempt("PREPLAN_FAILED", str(clamp_err))
                            return
                        is_safe, reason = self._evaluate_execution_gate(decision)
                        if is_safe:
                            logger.info(
                                "[%s][AUTONOMOUS_EXEC] Executing decision %s for %s",
                                environment_name,
                                decision.decision_id,
                                decision.symbol,
                            )
                            self._note_pilot_attempt("EXECUTING")
                            try:
                                await self.execute_manual_decision(decision)
                            except Exception as exc:
                                self._note_pilot_attempt(
                                    "EXECUTION_ERROR", f"{type(exc).__name__}: {exc}"
                                )
                                await self._fail_closed_after_autonomous_execution_error(exc)
                        else:
                            logger.info(
                                "[%s][EXECUTION_BLOCKED] Decision %s blocked: %s",
                                environment_name,
                                decision.decision_id,
                                reason,
                            )
                            self._note_pilot_attempt("EXECUTION_BLOCKED", reason)
                    else:
                        logger.info(
                            "[%s][MONITOR_ONLY] Decision %s for %s "
                            "(deployment approval, current-build evidence, and "
                            "runtime readiness are all required)",
                            environment_name,
                            decision.decision_id,
                            decision.symbol,
                        )
                        self._note_pilot_attempt(
                            "MONITOR_ONLY",
                            "Execution readiness not met (approval, evidence or runtime readiness)",
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
            if (
                adapter is not None
                and self.execution_mode == WorkerExecutionMode.LIVE
                and isinstance(self._mainnet_launch_session, dict)
                and self._mainnet_launch_session.get("policy") == "LIVE_RESEARCH_PILOT"
                and hasattr(adapter, "check_and_enforce_pilot_protections")
            ):
                monitor_started_at = utc_now()
                self._pilot_lifecycle_monitor_started_at = monitor_started_at
                try:
                    actions = await adapter.check_and_enforce_pilot_protections(authority=self)
                    self._pilot_lifecycle_monitor_completed_at = utc_now()
                    failure_count = actions.get("failed_action_count") if isinstance(actions, dict) else None
                    unmatched_count = actions.get("unmatched_owner_count") if isinstance(actions, dict) else None
                    valid_monitor_counts = all(
                        isinstance(value, int) and not isinstance(value, bool) and value >= 0
                        for value in (failure_count, unmatched_count)
                    )
                    if not valid_monitor_counts:
                        self._pilot_lifecycle_monitor_last_error = "MONITOR_RESULT_INVALID"
                        self._degrade_after_pilot_monitor_failure(adapter)
                    elif failure_count or unmatched_count:
                        self._pilot_lifecycle_monitor_last_error = "MONITOR_ACTION_UNVERIFIED"
                        self._degrade_after_pilot_monitor_failure(adapter)
                    else:
                        self._pilot_lifecycle_monitor_last_error = None
                        self._pilot_lifecycle_monitor_last_success_at = self._pilot_lifecycle_monitor_completed_at
                except Exception as exc:
                    self._pilot_lifecycle_monitor_completed_at = utc_now()
                    self._pilot_lifecycle_monitor_last_error = type(exc).__name__
                    self._degrade_after_pilot_monitor_failure(adapter)
                    logger.error("Pilot lifecycle monitor error: %s", type(exc).__name__)
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
        local_live = (
            self.execution_mode == WorkerExecutionMode.LIVE
            and _env_enabled("LOCAL_ONLY")
            and str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
        )
        if self.execution_mode == WorkerExecutionMode.LIVE:
            self.pause_new_risk = True
            self.engine_state = WorkerEngineState.DISARMED
            if local_live:
                os.environ["MAINNET_LIVE_APPROVED"] = "false"
            try:
                await self.disarm()
            except Exception as exc:
                logger.error("Worker stop disarm failed: %s", type(exc).__name__)
            finally:
                if local_live:
                    for name in (
                        "BINANCE_MAINNET_API_KEY",
                        "BINANCE_MAINNET_API_SECRET",
                        "BINANCE_MAINNET_API_KEY_FILE",
                        "BINANCE_MAINNET_API_SECRET_FILE",
                        "MAINNET_RELEASE_APPROVAL_ID",
                        "MAINNET_CONTINUATION_APPROVAL_ID",
                        "LOCAL_SOURCE_FINGERPRINT",
                    ):
                        os.environ[name] = ""
                    clear_local_mainnet_secrets()
                    os.environ["EXECUTION_MODE"] = "PAPER"
                    self.execution_mode = WorkerExecutionMode.PAPER
                    self.pause_new_risk = True
        if self.scan_task and not self.scan_task.done():
            self.scan_task.cancel()
        if self.ws_client:
            try:
                await self.ws_client.stop()
            except RuntimeError:
                pass
        await self.persistence.stop()

async def serve_api(app_instance):
    global _ACTIVE_UVICORN_SERVER
    set_worker_engine(app_instance)
    try:
        port = int(os.getenv("PORT", "8080"))
    except ValueError:
        port = 8080
    config = uvicorn.Config(app, host=worker_bind_host(), port=port, log_level="info")
    server = uvicorn.Server(config)
    _ACTIVE_UVICORN_SERVER = server
    try:
        await server.serve()
    finally:
        if _ACTIVE_UVICORN_SERVER is server:
            _ACTIVE_UVICORN_SERVER = None

async def main():
    app_instance = TradingWorkerApp()
    local_runtime = (
        _env_enabled("LOCAL_ONLY")
        and str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
    )
    try:
        await app_instance.start()
        await serve_api(app_instance)
    finally:
        if local_runtime:
            # A normal shutdown must be just as fail-closed as heartbeat loss.
            os.environ["MAINNET_LIVE_APPROVED"] = "false"
            if app_instance.execution_mode == WorkerExecutionMode.LIVE:
                app_instance.pause_new_risk = True
                app_instance.engine_state = WorkerEngineState.DISARMED
                try:
                    await app_instance.disarm()
                except Exception as exc:
                    logger.error("Local shutdown disarm failed: %s", type(exc).__name__)
            for name in (
                "BINANCE_MAINNET_API_KEY",
                "BINANCE_MAINNET_API_SECRET",
                "BINANCE_MAINNET_API_KEY_FILE",
                "BINANCE_MAINNET_API_SECRET_FILE",
                "WORKER_IDENTITY_TOKEN_FILE",
                "POSTGRES_PASSWORD_FILE",
                "MAINNET_RELEASE_APPROVAL_ID",
                "MAINNET_CONTINUATION_APPROVAL_ID",
                "LOCAL_SOURCE_FINGERPRINT",
            ):
                os.environ[name] = ""
            clear_local_container_secrets()
            os.environ["EXECUTION_MODE"] = "PAPER"
            app_instance.execution_mode = WorkerExecutionMode.PAPER
        try:
            await app_instance.stop()
        except Exception as exc:
            logger.error("Worker shutdown cleanup failed: %s", type(exc).__name__)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
