import asyncio
import json
import logging
import math
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
from domain.models import MarketEvent, RiskSnapshot, utc_now

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
from apps.trading_worker.venues.binance.config import BinanceEnvironment, get_ws_url
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.gates import DecisionExecutionGate
from apps.trading_worker.venues.binance.models import (
    BinanceAuthenticationError,
    ConnectionState,
    TestnetSafetyLimits,
)
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

    @field_validator("instruments")
    @classmethod
    def normalize_instruments(cls, instruments: List[str]) -> List[str]:
        return [str(symbol).strip().upper() for symbol in instruments if str(symbol).strip()]


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

    # Readiness
    launch_readiness: Optional[LaunchReadiness] = None

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
                    normalized.setdefault("provenance", "BINANCE_LIVE")
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
async def pause_new_risk_endpoint(req: ToggleRequest):
    if not WORKER_ENGINE:
        raise HTTPException(status_code=503, detail="Worker not initialized")
    await WORKER_ENGINE.set_pause_new_risk(req.active)
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
        self.symbols = [str(symbol).upper() for symbol in (symbols or ["BTCUSDT"])]
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

    def _active_instruments(self) -> List[str]:
        if isinstance(self.active_configuration, dict):
            configured = self.active_configuration.get("instruments")
            if configured:
                return [str(symbol).upper() for symbol in configured]
        return [str(symbol).upper() for symbol in self.symbols]

    @staticmethod
    def _has_grid_lineage(value: object) -> bool:
        return any(
            str(intent_id).upper().startswith("GRID-")
            for intent_id in (value or [])
        )

    async def _observed_grid_depth(self, symbol: str) -> int:
        """Read grid depth from Worker-owned ledger lineage before expansion."""

        if self.execution_mode != WorkerExecutionMode.TESTNET:
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
        """Require complete exchange rules for every active Testnet symbol."""
        active_symbols = self._active_instruments()
        adapter = self.execution_adapter
        return bool(
            adapter
            and active_symbols
            and all(
                symbol in adapter.symbol_rules
                and adapter.symbol_rules[symbol].is_ready_for("LIMIT")
                and adapter.symbol_rules[symbol].is_ready_for("MARKET")
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
        if getattr(snapshot, "exchange_environment", None) != "BINANCE_TESTNET":
            return False
        timestamp = getattr(snapshot, "timestamp", None)
        if not isinstance(timestamp, datetime):
            return False
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
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
                timestamp = timestamp.replace(tzinfo=timezone.utc)
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
            self.execution_mode == WorkerExecutionMode.TESTNET
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
            provenance = "BINANCE_LIVE"
            exchange_env = "BINANCE_MAINNET"

        data_source = "BINANCE" if self.execution_mode != WorkerExecutionMode.PAPER else "SIMULATED"

        return WorkerRuntimeState(
            execution_authority="PYTHON_TRADING_WORKER",
            is_execution_authority=True,
            execution_mode=self.execution_mode,
            provenance=provenance,
            data_source=data_source,
            exchange_environment=exchange_env,
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
            heartbeat_at=self.heartbeat_at,
            health_indicators=health,
            config_version="v0.2.0-beta",
            active_configuration=self.active_configuration,
            updated_at=utc_now()
        )
        
    def get_capabilities(self) -> dict:
        testnet_configured = self._testnet_configured()
        self._sync_adapter_state()
        adapter_ready = (
            self.execution_adapter is not None
            and self.execution_adapter.connection_state == ConnectionState.READY
            and self._adapter_trade_authorized()
        )
        trade_authorized = self._adapter_trade_authorized()
        symbol_rules_loaded = self._symbol_rules_ready()
        account_snapshot_ready = self.is_account_snapshot_ready()
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
        return {
            "paper": True,
            "testnetConfigured": testnet_configured,
            "testnetAuthenticated": self.authenticated,
            "testnetTradeAuthorized": trade_authorized,
            "testnetPrivateStreamHealthy": self.private_stream_healthy,
            "testnetReconciliationInSync": self.reconciliation_status == "IN_SYNC",
            "testnetSymbolRulesLoaded": symbol_rules_loaded,
            "testnetAdapterReady": adapter_ready,
            "testnetAccountSnapshotReady": account_snapshot_ready,
            "testnetMarketDataFresh": market_data_fresh,
            "testnetExecutionReady": testnet_ready,
            "liveConfigured": False,
            "liveExecutionReady": False,
            "spotSupported": False,
            "usdmFuturesSupported": True,
            "hedgeModeSupported": bool(
                self.execution_adapter and self.execution_adapter.capabilities.hedge_mode
            )
        }

    def get_launch_readiness(self) -> dict:
        self._sync_adapter_state()
        testnet_configured = self._testnet_configured()
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
        market_data_fresh = self.is_market_data_fresh()

        readiness = LaunchReadiness(
            paper_ready=not self.kill_switch_active,
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
            small_live_ready=False
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
        
        return readiness.model_dump()

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
        self.pause_new_risk = bool(active)
        self._refresh_engine_state()

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
                if self.execution_mode != WorkerExecutionMode.TESTNET or adapter is None:
                    return {
                        "status": "UNKNOWN",
                        "reason": "Kill switch remains active until a verified Testnet restart/reconciliation.",
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
                            "reason": "Kill switch remains active while Testnet open orders exist.",
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
                        "reason": "Testnet authentication failed; kill switch remains active.",
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
        self.engine_state = WorkerEngineState.EMERGENCY
        adapter = self.execution_adapter
        if self.execution_mode == WorkerExecutionMode.PAPER and adapter is None:
            return {"status": "CONFIRMED", "environment": "PAPER"}
        if self.execution_mode != WorkerExecutionMode.TESTNET:
            return {
                "status": "UNKNOWN",
                "reason": "Mutable cancellation is restricted to Binance Testnet.",
            }
        if adapter is None:
            return {"status": "UNKNOWN", "reason": "Testnet exchange adapter is unavailable."}
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
            return res
            
        if self.execution_mode == WorkerExecutionMode.TESTNET:
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
            supported = TestnetSafetyLimits.from_environment().allowed_symbols
        else:
            supported = {"BTCUSDT", "ETHUSDT"}
        unsupported = sorted(set(req.instruments) - set(supported))
        if unsupported:
            return f"Unsupported instruments: {', '.join(unsupported)}"
        return None

    async def _reset_after_failed_testnet_arm(self) -> None:
        """Close a partially initialized adapter and clear failed ARM state."""
        if self.execution_adapter is not None:
            try:
                await self.execution_adapter.close()
            except Exception as exc:
                logger.warning("Error closing failed Testnet adapter: %s", exc)
            self.execution_adapter = None
        self.connection_state = "DISCONNECTED"
        self.execution_mode = WorkerExecutionMode.PAPER
        self.market_data_healthy = False
        self.private_stream_healthy = False
        self.authenticated = False
        self.reconciliation_status = "UNKNOWN"
        self.active_configuration = None
        self.pause_new_risk = False
        self.recovery_only = False
        self.risk_governor.hedge_mode = False
        self.engine_state = WorkerEngineState.DISARMED

    async def arm(self, config: ArmRequest | dict):
        if self.kill_switch_active:
            return False, "Cannot arm: Kill switch is active"

        try:
            req = config if isinstance(config, ArmRequest) else ArmRequest.model_validate(config)
        except ValidationError as exc:
            return False, f"Invalid ARM request: {exc.errors()[0].get('msg', str(exc))}"

        # Keep the live invariant unconditional, including for otherwise
        # incomplete requests.  No semantic validation or adapter creation may
        # ever turn a LIVE request into a partially accepted state.
        if req.executionMode == "LIVE":
            return False, "LIVE execution mode is permanently blocked in this sprint."

        validation_error = self._validate_arm_request(req)
        if validation_error:
            return False, validation_error

        mode = req.executionMode
        if mode == "TESTNET" and not self._testnet_configured():
            return False, "Configuration Preflight Failed: Testnet credentials or BINANCE_TESTNET=true missing."

        self.symbols = list(req.instruments)
        self.execution_mode = WorkerExecutionMode(mode)

        if mode == "TESTNET":
            self.engine_state = WorkerEngineState.ARMING
            api_key = os.getenv("BINANCE_TESTNET_API_KEY", "")
            api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "")

            try:
                if self.execution_adapter is None:
                    self.execution_adapter = BinanceExecutionAdapter(
                        api_key=api_key,
                        api_secret=api_secret,
                        env=BinanceEnvironment.TESTNET,
                    )
                self.execution_adapter.bind_worker_authority(self)
                connected = await self.execution_adapter.connect()
                self._sync_adapter_state()
                if not connected or self.execution_adapter.connection_state != ConnectionState.READY:
                    failed_state = self.connection_state
                    await self._reset_after_failed_testnet_arm()
                    return False, f"Failed to initialize Testnet Execution Adapter. State: {failed_state}"
                self.risk_governor.hedge_mode = self.execution_adapter.capabilities.hedge_mode
                if not await self.execution_adapter.refresh_market_data(self.symbols):
                    await self._reset_after_failed_testnet_arm()
                    return False, "Runtime preflight failed: fresh market data unavailable."
                self.last_market_event_at.update(self.execution_adapter.last_market_event_at)
                self.market_data_healthy = True
            except Exception as exc:
                logger.error("Error connecting Testnet Execution Adapter: %s", exc)
                await self._reset_after_failed_testnet_arm()
                return False, f"Adapter connection failed: {exc}"

            # Runtime Preflight
            preflight = self.get_preflight("TESTNET")
            if not preflight["canArm"]:
                failures = [c["message"] for c in preflight["checks"] if c["status"] == "FAIL"]
                await self._reset_after_failed_testnet_arm()
                return False, f"TESTNET runtime preflight failed: {'; '.join(failures)}"

            self.engine_state = WorkerEngineState.ARMED
            self.active_configuration = req.model_dump()
            logger.info("Worker ARMED in TESTNET mode")
            return True, ""
        else:
            # Switching from Testnet back to Paper must tear down the previous
            # exchange adapter first. Otherwise stale signed/account/stream
            # state could be projected into a Paper runtime.
            if self.execution_adapter is not None:
                try:
                    await self.execution_adapter.close()
                except Exception as exc:
                    logger.warning("Error closing Testnet adapter before Paper ARM: %s", exc)
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

    async def execute_manual_decision(self, decision):
        """Worker-owned manual Testnet path used by the controlled trial only."""
        allowed, reason = self._evaluate_execution_gate(decision)
        if not allowed or self.execution_adapter is None:
            raise RuntimeError(f"Decision execution gate blocked manual order: {reason}")
        self.execution_adapter.bind_worker_authority(self)
        return await self.execution_adapter.execute_decision(decision, authority=self)

    async def amend_testnet_order(
        self,
        symbol: str,
        client_order_id: str,
        new_price: Decimal,
        new_qty: Decimal,
        side: str,
    ):
        """Worker-owned wrapper for the verified Testnet LIMIT amendment path."""
        if self.execution_mode != WorkerExecutionMode.TESTNET or self.execution_adapter is None:
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
        if self.execution_mode != WorkerExecutionMode.TESTNET or self.execution_adapter is None:
            return False
        self.execution_adapter.bind_worker_authority(self)
        return await self.execution_adapter.cancel_order(
            symbol,
            client_order_id,
            authority=self,
        )

    async def emergency_flatten(self, symbol: Optional[str] = None):
        """Route an explicitly emergency, Testnet-only flatten through the worker."""
        if self.execution_mode != WorkerExecutionMode.TESTNET or self.execution_adapter is None:
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
            event_timestamp = event_timestamp.replace(tzinfo=timezone.utc)
        symbol = str(event.symbol).upper()
        if symbol != event.symbol:
            event = event.model_copy(update={"symbol": symbol})
        if self.execution_mode == WorkerExecutionMode.TESTNET and self.execution_adapter is not None:
            if not self.execution_adapter.record_market_event(event):
                logger.warning("Ignoring invalid Testnet market event for %s", event.symbol)
                return
        self.last_market_event_at[symbol] = event_timestamp
        if symbol in self._active_instruments():
            self.market_data_healthy = True
            
        pa_state = self.pa_engine.process_event(event)
        if not pa_state:
            return
            
        market_state = self.market_state_engine.classify(pa_state)
        
        grid_depth = await self._observed_grid_depth(event.symbol)
        grid_intent = self.grid_engine.evaluate(
            pa_state, market_state, grid_depth=grid_depth
        )
        trend_intent = self.trend_engine.evaluate(pa_state, market_state)
        shock_intent = self.shock_engine.evaluate(pa_state, market_state)
        carry_intent = self.carry_engine.evaluate(event, market_state)
        
        intents = [i for i in [grid_intent, trend_intent, shock_intent, carry_intent] if i]
        
        # Real or simulated RiskSnapshot
        if self.execution_mode == WorkerExecutionMode.TESTNET:
            if self.execution_adapter is None:
                logger.error("Testnet risk evaluation has no execution adapter")
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
                    or getattr(snapshot, "exchange_environment", None) != "BINANCE_TESTNET"
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
                    realized_pnl_24h=Decimal("0.0"),
                    margin_utilization_pct=snapshot.margin_utilization_pct,
                    effective_leverage=snapshot.effective_leverage,
                    current_drawdown_pct=max(Decimal("0.0"), drawdown_pct),
                    liquidation_distance_pct=snapshot.min_liquidation_distance_pct,
                    risk_state=self._derive_testnet_risk_state(
                        snapshot,
                        max(Decimal("0.0"), drawdown_pct),
                    ),
                )
                
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
                            "Hedge Mode has opposing BTCUSDT legs; blocking autonomous decision until explicitly reconciled"
                        )
                        return
                
            except Exception as e:
                logger.error("Error extracting Testnet risk snapshot: %s", e)
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
            if self.execution_mode == WorkerExecutionMode.TESTNET:
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
                if self.execution_mode == WorkerExecutionMode.TESTNET and self.execution_adapter is not None:
                    autonomous_enabled = self._env_flag("AUTONOMOUS_TESTNET_EXECUTION", False)
                    launch_readiness = self.get_launch_readiness()
                    readiness_key = (
                        "testnet_autonomous_soak_ready"
                        if self._env_flag("AUTONOMOUS_TESTNET_SOAK_APPROVED", False)
                        else "testnet_autonomous_ready"
                    )
                    if autonomous_enabled and launch_readiness[readiness_key]:
                        is_safe, reason = self._evaluate_execution_gate(decision)
                        if is_safe:
                            logger.info(f"[TESTNET][AUTONOMOUS_EXEC] Executing decision {decision.decision_id} for {decision.symbol}")
                            try:
                                await self.execute_manual_decision(decision)
                            except Exception as e:
                                logger.error(f"[TESTNET][AUTONOMOUS_EXEC] Execution failed: {e}")
                        else:
                            logger.info(f"[TESTNET][EXECUTION_BLOCKED] Decision {decision.decision_id} blocked: {reason}")
                    else:
                        logger.info(
                            "[TESTNET][MONITOR_ONLY] Decision %s for %s "
                            "(autonomous flag, launch approval, current-build evidence, "
                            "and runtime readiness are all required)",
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
        self.symbols = await self.scanner.scan_active_symbols()
        
        logger.info("Connecting to Binance WS for: %s", self.symbols)
        self.ws_client = BinancePublicWebSocket(
            symbols=self.symbols,
            base_ws_url=get_ws_url(BinanceEnvironment.TESTNET),
            event_callback=self.handle_market_event,
        )
        if not await self.ws_client.start():
            logger.error("Testnet public market stream could not be started")
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
            if self.execution_mode == WorkerExecutionMode.TESTNET:
                # Testnet launch instruments are an explicit, bounded
                # configuration.  The research scanner must not expand them.
                continue
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
        if self.ws_client:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.ws_client.stop())
            except RuntimeError:
                pass

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
