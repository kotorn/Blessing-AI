"""Native, environment-aware Binance USDⓈ-M execution adapter.

The adapter can speak to Testnet or Mainnet, but the Trading Worker remains the
only mutable authority and Mainnet still requires the deployment launch gate.
"""

import asyncio
import hashlib
import logging
import math
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, cast
from uuid import uuid4

from domain.enums import (
    EconomicRiskClass,
    MarketType,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionSide,
    TimeInForce,
)
from domain.models import (
    ExchangeFill,
    ExecutionDecision,
    ExecutionOrder,
    MarketEvent,
    OrderIntent,
    utc_now,
)
from apps.trading_worker.execution_lease import (
    ExecutionLease,
    LeaseLostError,
    execution_lease_required,
)

from .capabilities import BinanceCapabilities
from .config import BinanceEnvironment, environment_label
from .gates import OrderExecutionGate
from .local_pilot_readiness import local_live_pilot_readiness
from .mainnet_risk import (
    LOCAL_LIVE_PILOT_POLICY,
    LOCAL_LIVE_PILOT_POLICY_SHA256,
    MAINNET_RISK_POLICY,
    MAINNET_RISK_POLICY_SHA256,
    derive_local_mainnet_snapshot_risk,
)
from .protection import ProtectionIntent, ProtectionResult, verify_protection
from .ledger import ExecutionLedger, InMemoryLedger
from .models import (
    BinanceAuthenticationError,
    BinanceDefinitiveRejection,
    BinanceRateLimitError,
    BinanceTimestampError,
    BinanceTransportAmbiguity,
    ConnectionState,
    TestnetSafetyLimits,
)
from .reconciliation import BinanceReconciliation, ReconciliationDiff
from .rest_client import BinanceAPIError, BinanceRestClient
from .symbol_rules import SymbolTradingRules
from .user_stream import BinanceUserStream

logger = logging.getLogger("blessing.venues.binance.execution")
_LOCAL_PILOT_POSITION_MAX_RTT_SECONDS = 2.5


def _local_pilot_position_mark_time_ms(
    request_started_at_ms: int,
    request_started_monotonic: float,
    response_received_monotonic: float,
) -> int:
    """Return a conservative request-window start or reject stale evidence."""
    duration = response_received_monotonic - request_started_monotonic
    if (
        isinstance(request_started_at_ms, bool)
        or request_started_at_ms <= 0
        or not math.isfinite(duration)
        or not 0 <= duration <= _LOCAL_PILOT_POSITION_MAX_RTT_SECONDS
    ):
        raise ValueError("signed position-risk request exceeded freshness budget")
    return request_started_at_ms


def _exchange_bool(value: object) -> bool:
    """Parse exchange booleans without treating a false string as truthy."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


class BinanceExecutionAdapter:
    """Blessing AI's sole mutable exchange adapter for a fixed Binance route."""

    testnet_environment = BinanceEnvironment.TESTNET

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        env: BinanceEnvironment = BinanceEnvironment.TESTNET,
        ledger: Optional[ExecutionLedger] = None,
        preflight_only: bool = False,
        portfolio_margin: Optional[bool] = None,
    ) -> None:
        if not isinstance(env, BinanceEnvironment):
            raise ValueError("Binance execution requires TESTNET or MAINNET")
        if env == BinanceEnvironment.MAINNET and os.getenv(
            "MAINNET_LIVE_APPROVED", ""
        ).strip().lower() not in {"1", "true", "yes", "on"} and not preflight_only:
            raise ValueError("Mainnet adapter construction requires MAINNET_LIVE_APPROVED=true")

        self.env = env
        self.preflight_only = bool(preflight_only)
        # Fail closed for every Testnet adapter, not only when called through
        # the current Worker wrapper. Isolated unit tests can explicitly opt
        # out when testing generic adapter mechanics without a bracket.
        self.require_testnet_protection = env == BinanceEnvironment.TESTNET
        self.api_key = "".join(str(api_key or "").split())
        self.api_secret = "".join(str(api_secret or "").split())
        self.ledger = ledger or InMemoryLedger()
        self.safety_limits = TestnetSafetyLimits.from_environment(env)
        self.rest_client = BinanceRestClient(
            self.api_key,
            self.api_secret,
            env,
            read_only=self.preflight_only,
            portfolio_margin=portfolio_margin,
        )
        self.capabilities = BinanceCapabilities()
        self.user_stream = BinanceUserStream(
            self.rest_client,
            env,
            on_disconnect=self._on_user_stream_disconnect,
            on_reconnected=self._on_user_stream_reconnected,
            on_authentication_failed=self.invalidate_authentication,
        )
        self.reconciliation = BinanceReconciliation(self.rest_client, self.ledger)
        self.state = ConnectionState.DISCONNECTED
        self.order_gate = OrderExecutionGate(self)
        self.last_market_event_at: Dict[str, datetime] = {}
        self.last_market_price: Dict[str, Decimal] = {}
        # PERCENT_PRICE is evaluated against Binance's mark price. Keep this
        # separate from last_market_price because book-ticker samples use a
        # bid/ask midpoint for executable-price estimation.
        self.last_market_reference_price: Dict[str, Decimal] = {}
        self.last_market_reference_at: Dict[str, datetime] = {}
        self.last_market_bid: Dict[str, Decimal] = {}
        self.last_market_ask: Dict[str, Decimal] = {}
        self.last_market_bid_qty: Dict[str, Decimal] = {}
        self.last_market_ask_qty: Dict[str, Decimal] = {}
        # Executable bid/ask clock; mark-price frames must not refresh it.
        self.last_market_book_at: Dict[str, datetime] = {}
        # Last order-level block, surfaced in the worker state for operators.
        self.last_order_block: Optional[Dict[str, str]] = None
        self.last_market_event_source: Dict[str, str] = {}
        self.last_market_event_venue: Dict[str, str] = {}
        self.last_market_event_market_type: Dict[str, str] = {}
        self.last_order_event_at: Dict[str, datetime] = {}
        self.last_emergency_result: Dict[str, Any] = {"status": "UNKNOWN"}
        self.last_testnet_protection: Dict[str, Any] = {"status": "NOT_RUN"}
        self.last_local_mainnet_protection: Dict[str, Any] = {"status": "NOT_RUN"}
        self._last_local_mainnet_risk_evidence: Optional[Dict[str, Any]] = None
        self._last_local_mainnet_protection_evidence: Optional[Dict[str, Any]] = None
        self._local_mainnet_entry_deadlines: Dict[str, float] = {}
        self.order_submission_attempts = 0
        # The adapter is intentionally not an independent execution authority.
        # A Worker instance binds itself immediately before using the internal
        # submit path; direct adapter calls remain blocked.
        self._worker_authority: Optional[object] = None
        self._mutation_lock = asyncio.Lock()
        self.execution_lease: Optional[ExecutionLease] = None
        # TradingWorkerApp installs the durable outbox barrier.  Keeping the
        # callback on the adapter makes the ordering explicit at the only REST
        # mutation boundary and leaves the adapter testable without a database.
        self.before_order_submission: Optional[
            Callable[[ExecutionOrder], Awaitable[bool]]
        ] = None
        # The Worker uses this callback to persist the staged first-order
        # transition. A result other than CONFIRMED is kept in reconciliation
        # quarantine and never retried blindly.
        self.on_order_submission_result: Optional[
            Callable[[ExecutionOrder, str], Awaitable[None]]
        ] = None
        # Testnet risk-increasing entry requires a separate durable bracket
        # ownership row before the exchange entry can be submitted.
        self.on_testnet_protection_update: Optional[
            Callable[[Dict[str, Any]], Awaitable[bool]]
        ] = None
        self.on_local_mainnet_protection_update: Optional[
            Callable[[Dict[str, Any]], Awaitable[bool]]
        ] = None
        self.on_local_mainnet_entry_cancel_claim: Optional[
            Callable[[Dict[str, Any]], Awaitable[bool]]
        ] = None
        self.on_local_mainnet_close_verified: Optional[
            Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[bool]]
        ] = None
        # The Local Pilot consumes private-stream fills into its durable PnL
        # ledger. A failed write is a reconciliation failure, never a warning
        # that permits more risk.
        self.on_local_live_pilot_fill: Optional[
            Callable[[ExchangeFill], Awaitable[bool]]
        ] = None
        self.on_local_live_pilot_mark: Optional[
            Callable[[str, str, Decimal, object], Awaitable[bool]]
        ] = None
        self.on_local_live_pilot_funding_reconcile: Optional[
            Callable[[object, object], Awaitable[bool]]
        ] = None
        # Cloud Run and Mainnet always require a distributed lease. Local
        # Testnet tests can opt into the same requirement with an env flag.
        self.execution_lease_required = bool(
            env == BinanceEnvironment.MAINNET or execution_lease_required()
        )

    @property
    def connection_state(self) -> ConnectionState:
        """Canonical adapter connection state consumed by the worker."""
        return self.state

    @asynccontextmanager
    async def _mutation_scope(self) -> AsyncIterator[None]:
        """Serialize mutations, allowing only the owning task to enter again."""
        task = asyncio.current_task()
        if getattr(self, "_mutation_owner_task", None) is task:
            yield
            return
        if not hasattr(self, "_mutation_lock"):
            self._mutation_lock = asyncio.Lock()
        async with self._mutation_lock:
            self._mutation_owner_task: Optional[asyncio.Task[Any]] = task
            try:
                yield
            finally:
                self._mutation_owner_task = None

    @property
    def environment_label(self) -> str:
        """Canonical exchange provenance label for this adapter instance."""

        return environment_label(self.env)

    @property
    def mutation_lock(self) -> asyncio.Lock:
        """Serialize all mutable Binance REST operations with the kill switch."""
        return self._mutation_lock

    @property
    def portfolio_margin(self) -> bool:
        return getattr(self.rest_client, "portfolio_margin", False)

    @property
    def _order_path(self) -> str:
        return "/papi/v1/um/order" if self.portfolio_margin else "/fapi/v1/order"

    @property
    def _open_orders_path(self) -> str:
        return "/papi/v1/um/openOrders" if self.portfolio_margin else "/fapi/v1/openOrders"

    @property
    def _position_risk_path(self) -> str:
        return "/papi/v1/um/positionRisk" if self.portfolio_margin else "/fapi/v2/positionRisk"

    @property
    def _algo_order_path(self) -> str:
        return "/papi/v1/um/algo/order" if self.portfolio_margin else "/fapi/v1/algoOrder"

    @property
    def _algo_order_query_path(self) -> str:
        return "/papi/v1/um/algo/algoOrder" if self.portfolio_margin else "/fapi/v1/algoOrder"

    @property
    def _open_algo_orders_path(self) -> str:
        return "/papi/v1/um/algo/openAlgoOrders" if self.portfolio_margin else "/fapi/v1/openAlgoOrders"

    @property
    def authenticated(self) -> bool:
        """Authentication is true only after signed account capability discovery."""
        return bool(
            self.env in {BinanceEnvironment.TESTNET, BinanceEnvironment.MAINNET}
            and getattr(self.capabilities, "account_request_succeeded", False)
            and getattr(self.capabilities, "authenticated", False)
            and not getattr(self.reconciliation, "authentication_failed", False)
        )

    @property
    def symbol_rules(self) -> Dict[str, SymbolTradingRules]:
        """Canonical symbol-rule API for worker readiness and order validation."""
        return self.capabilities.symbol_rules

    def is_symbol_ready_for_execution(self, symbol: str) -> bool:
        """Validate a selected symbol against live exchange metadata.

        Mainnet is intentionally limited to the USDⓈ-M ETHUSDC perpetual. The
        filters themselves remain entirely exchange-derived in
        ``SymbolTradingRules``; this method only validates contract identity.
        """

        normalized = str(symbol).strip().upper()
        rules = self.symbol_rules.get(normalized)
        if rules is None or not rules.is_ready_for("LIMIT") or not rules.is_ready_for("MARKET"):
            return False
        if self.env == BinanceEnvironment.MAINNET and not rules.is_usdc_perpetual():
            return False
        return True

    @property
    def account_snapshot(self) -> Any:
        return getattr(self.ledger, "account_snapshot", None)

    @property
    def private_stream_healthy(self) -> bool:
        """Return stream connectivity plus the stream's own freshness verdict."""
        stream = self.user_stream
        health_checker = getattr(stream, "is_healthy", None)
        if callable(health_checker):
            return bool(health_checker())
        return bool(stream and getattr(stream, "is_connected", False))

    def is_account_snapshot_fresh(self) -> bool:
        snapshot = self.account_snapshot
        if snapshot is None or not getattr(snapshot, "valid", False):
            return False
        if getattr(snapshot, "exchange_environment", None) != environment_label(self.env):
            return False
        timestamp = getattr(snapshot, "timestamp", None)
        if not isinstance(timestamp, datetime):
            return False
        if timestamp.tzinfo is None:
            return False
        age = (utc_now() - timestamp).total_seconds()
        try:
            max_age = float(os.getenv("ACCOUNT_SNAPSHOT_MAX_AGE_SEC", "30"))
        except ValueError:
            max_age = 30.0
        if not math.isfinite(max_age) or max_age <= 0 or age < 0 or age > max_age:
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

    def has_open_quick_bracket(self, symbol: str = "ETHUSDC") -> bool:
        """Return True if a QUICK bracket or protected position is currently active."""
        normalized_symbol = str(symbol).upper()
        last_protection = getattr(self, "last_local_mainnet_protection", {})
        if isinstance(last_protection, dict) and last_protection.get("status") == "PROTECTED":
            return True
        ledger = getattr(self, "ledger", None)
        positions = getattr(ledger, "positions", []) if ledger is not None else []
        for pos in positions:
            pos_symbol = str(getattr(pos, "symbol", "")).upper()
            if pos_symbol == normalized_symbol:
                qty = getattr(pos, "quantity", Decimal("0"))
                try:
                    if abs(Decimal(str(qty))) > Decimal("0"):
                        return True
                except (InvalidOperation, TypeError, ValueError):
                    pass
        authority = getattr(self, "_worker_authority", None)
        session = getattr(authority, "_mainnet_launch_session", None)
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

    @staticmethod
    def _enum_value(value: Any) -> Any:
        return getattr(value, "value", value)

    @staticmethod
    def _decimal_value(value: Any, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
        if value is None or isinstance(value, bool):
            raise ValueError("required decimal is missing")
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
        if not parsed.is_finite() or (positive and parsed <= 0) or (nonnegative and parsed < 0):
            raise ValueError("required decimal is invalid")
        return parsed

    @staticmethod
    def _local_order_fingerprint(order: Any) -> tuple[str, ...]:
        return (
            str(getattr(order, "client_order_id", "")),
            str(getattr(order, "basket_id", "") or ""),
            str(getattr(order, "exchange_order_id", "") or ""),
            str(getattr(order, "symbol", "")).upper(),
            str(getattr(getattr(order, "side", None), "value", getattr(order, "side", ""))).upper(),
            str(getattr(order, "order_type", "")).upper(),
            str(getattr(order, "quantity", "")),
            str(getattr(order, "price", "")),
            str(getattr(order, "status", "")).upper(),
            str(getattr(getattr(order, "position_side", None), "value", getattr(order, "position_side", ""))).upper(),
        )

    @staticmethod
    def _local_fill_fingerprint(fill: Any) -> tuple[str, ...]:
        return (
            str(getattr(fill, "exchange_trade_id", "")),
            str(getattr(fill, "exchange_order_id", "")),
            str(getattr(fill, "client_order_id", "")),
            str(getattr(fill, "symbol", "")).upper(),
            str(getattr(getattr(fill, "side", None), "value", getattr(fill, "side", ""))).upper(),
            str(getattr(getattr(fill, "position_side", None), "value", getattr(fill, "position_side", ""))).upper(),
            str(getattr(fill, "quantity", "")),
            str(getattr(fill, "price", "")),
            str(getattr(fill, "commission", "")),
            str(getattr(fill, "commission_asset", "")).upper(),
        )

    @staticmethod
    def _local_position_fingerprint(position: Any) -> tuple[str, ...]:
        return (
            str(getattr(position, "symbol", "")).upper(),
            str(getattr(getattr(position, "position_side", None), "value", getattr(position, "position_side", ""))).upper(),
            str(getattr(position, "quantity", "")),
            str(getattr(position, "entry_price", "")),
            str(getattr(position, "mark_price", "")),
            str(getattr(position, "liquidation_price", "")),
            str(getattr(position, "unrealized_pnl", "")),
            str(getattr(position, "leverage", "")),
            str(getattr(position, "margin_type", "")).upper(),
        )

    async def _local_mainnet_durable_ledger_matches(self, worker: Any) -> bool:
        """Require the in-process ledger to equal a fresh PostgreSQL read-back."""
        ledger = self.ledger
        if (
            getattr(ledger, "durable_snapshot", False) is not True
            or str(getattr(ledger, "symbol", "")).upper() != MAINNET_RISK_POLICY.symbol
            or str(getattr(ledger, "venue", "")).upper() != environment_label(BinanceEnvironment.MAINNET)
        ):
            return False
        persistence = getattr(worker, "persistence", None)
        reload_ledger: Any = getattr(persistence, "create_execution_ledger", None)
        if not callable(reload_ledger):
            return False
        durable = await cast(Any, reload_ledger(
            symbol=MAINNET_RISK_POLICY.symbol,
            venue=environment_label(BinanceEnvironment.MAINNET),
        ))
        if (
            getattr(durable, "durable_snapshot", False) is not True
            or str(getattr(durable, "symbol", "")).upper() != MAINNET_RISK_POLICY.symbol
            or str(getattr(durable, "venue", "")).upper() != environment_label(BinanceEnvironment.MAINNET)
            or not await ledger.is_initialized()
            or not await durable.is_initialized()
        ):
            return False
        local_orders = await ledger.get_all_orders()
        durable_orders = await durable.get_all_orders()
        local_fills = await ledger.get_fills()
        durable_fills = await durable.get_fills()
        local_positions = await ledger.get_positions()
        durable_positions = await durable.get_positions()
        return (
            sorted(map(self._local_order_fingerprint, local_orders))
            == sorted(map(self._local_order_fingerprint, durable_orders))
            and sorted(map(self._local_fill_fingerprint, local_fills))
            == sorted(map(self._local_fill_fingerprint, durable_fills))
            and sorted(map(self._local_position_fingerprint, local_positions))
            == sorted(map(self._local_position_fingerprint, durable_positions))
        )

    async def _local_mainnet_runtime_evidence(
        self,
        *,
        require_unused_staged_launch: bool,
        expected_order_client_id: str | None = None,
        expected_basket_id: str | None = None,
    ) -> Optional[Dict[str, Any]]:
        """Collect real Local runtime, PostgreSQL, lease, and signed-snapshot proof."""
        worker = self._worker_authority
        engine_state = str(
            self._enum_value(getattr(worker, "engine_state", ""))
        ).upper()
        pause_new_risk = bool(getattr(worker, "pause_new_risk", True))
        lifecycle_state_ready = (
            engine_state == "ARMED" and not pause_new_risk
            if require_unused_staged_launch
            else engine_state == "PAUSED_NEW_RISK" and pause_new_risk
        )
        if (
            self.env != BinanceEnvironment.MAINNET
            or self.preflight_only
            or worker is None
            or os.getenv("LOCAL_ONLY", "").strip().lower() not in {"1", "true", "yes", "on"}
            or os.getenv("LOCAL_RUNTIME_TARGET", "").strip().upper() != "LOCAL"
            or os.getenv("MAINNET_LIVE_APPROVED", "").strip().lower() not in {"1", "true", "yes", "on"}
            or str(self._enum_value(getattr(worker, "execution_mode", ""))).upper() != "LIVE"
            or getattr(worker, "active_configuration", None) is None
            or not lifecycle_state_ready
            or bool(getattr(worker, "kill_switch_active", True))
            or bool(getattr(worker, "recovery_only", True))
            or self.state != ConnectionState.READY
            or not self.authenticated
            or not bool(getattr(self.capabilities, "trade_authorized", False))
            or not self.private_stream_healthy
            or str(getattr(self.reconciliation, "last_status", "UNKNOWN")).upper() != "IN_SYNC"
        ):
            return None

        persistence = getattr(worker, "persistence", None)
        readiness_method = getattr(persistence, "readiness", None)
        launch_loader: Any = getattr(persistence, "get_mainnet_launch_session", None)
        if not callable(readiness_method) or not callable(launch_loader):
            return None
        readiness = readiness_method()
        if not isinstance(readiness, dict) or any(
            readiness.get(key) != expected
            for key, expected in {
                "mode": "REQUIRED",
                "durable": True,
                "runtime_target": "LOCAL",
                "database_provider": "POSTGRES_LOCAL",
                "database_host": "127.0.0.1",
                "database_port": 5433,
                "database_identity_verified": True,
                "schema_verified": True,
                "pending_outbox": 0,
                "queue_size": 0,
                "failed_writes": 0,
                "dropped_writes": 0,
                "unflushed_writes": 0,
            }.items()
        ):
            return None

        fingerprint = str(os.getenv("LOCAL_SOURCE_FINGERPRINT", "")).strip().lower()
        if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
            return None
        launch_id = str(getattr(worker, "_mainnet_launch_id", "") or "").strip()
        if not launch_id:
            return None
        session = await cast(Any, launch_loader(launch_id))
        if not isinstance(session, dict) or (
            str(session.get("launch_id", "")) != launch_id
            or str(session.get("symbol", "")).upper() != MAINNET_RISK_POLICY.symbol
            or str(session.get("runtime_target", "")).upper() != "LOCAL"
            or session.get("runtime_fingerprint") != fingerprint
            or session.get("image_digest") is not None
            or not str(session.get("approval_id", "")).strip()
        ):
            return None
        if require_unused_staged_launch:
            pilot_session = session.get("policy") == "LIVE_RESEARCH_PILOT"
            if pilot_session:
                expires_at = session.get("pilot_campaign_expires_at")
                if not isinstance(expires_at, datetime) or expires_at.tzinfo is None or expires_at <= utc_now():
                    return None
                if (
                    session.get("state") != "ACTIVE"
                    or session.get("pilot_status") != "ACTIVE"
                    or session.get("pilot_drawdown_triggered") is not False
                ):
                    return None
                # Accounting must be refreshed from Binance and durably read
                # back after the first submitted order; otherwise fail closed.
                if int(session.get("submitted_orders", 0) or 0) > 0:
                    account_checked_at = session.get("pilot_last_account_snapshot_at")
                    if not isinstance(account_checked_at, datetime) or account_checked_at.tzinfo is None:
                        return None
                    if (utc_now() - account_checked_at).total_seconds() > 5:
                        return None
            elif (
                session.get("policy") != "STAGED_FIRST_ORDER"
                or session.get("state") != "ACTIVE"
                or session.get("submitted_orders") != 0
                or session.get("first_order_client_order_id") not in (None, "")
            ):
                return None
            pristine = (
                session.get("reserved_orders") == session.get("submitted_orders")
                and session.get("pending_order_client_order_id") in (None, "")
                and session.get("basket_id") in (None, "", expected_basket_id)
            ) if pilot_session else (
                session.get("reserved_orders") == 0
                and session.get("pending_order_client_order_id") in (None, "")
                and session.get("basket_id") in (None, "", expected_basket_id)
            )
            reserved_for_current_order = bool(
                expected_order_client_id
                and expected_basket_id
                and session.get("reserved_orders") == 1
                and str(session.get("pending_order_client_order_id") or "")
                == expected_order_client_id
                and str(session.get("basket_id") or "") == expected_basket_id
            )
            if not pristine and not reserved_for_current_order:
                return None

        lease = self.execution_lease
        if lease is None or getattr(lease, "fencing_token", None) is None:
            return None
        await lease.assert_valid()

        snapshot = self.account_snapshot
        if not self.is_account_snapshot_fresh():
            return None
        snapshot_risk = derive_local_mainnet_snapshot_risk(
            snapshot, now=utc_now(), max_age_seconds=Decimal("5")
        )
        market_checker = getattr(worker, "is_market_data_fresh", None)
        if not callable(market_checker) or not market_checker([MAINNET_RISK_POLICY.symbol]):
            return None
        sample_at = self.last_market_event_at.get(MAINNET_RISK_POLICY.symbol)
        if not isinstance(sample_at, datetime) or sample_at.tzinfo is None:
            return None
        sample_age = (utc_now() - sample_at).total_seconds()
        if sample_age < 0 or sample_age > self._market_data_max_age():
            return None
        if not self.has_authoritative_market_sample(MAINNET_RISK_POLICY.symbol):
            return None

        if not await self._local_mainnet_durable_ledger_matches(worker):
            return None
        repository = getattr(persistence, "repository", None)
        protections = getattr(repository, "algo_protections", None)
        if protections is None:
            return None
        return {
            "worker": worker,
            "persistence": persistence,
            "repository": repository,
            "protections": protections,
            "readiness": readiness,
            "session": session,
            "launch_id": launch_id,
            "fingerprint": fingerprint,
            "snapshot": snapshot,
            "snapshot_risk": snapshot_risk,
        }

    def has_fresh_local_mainnet_lifecycle_evidence(self) -> bool:
        """Readiness requires fresh successful risk and protection read-backs."""
        worker = self._worker_authority
        market_fresh = getattr(worker, "is_market_data_fresh", None)
        if (
            self.env != BinanceEnvironment.MAINNET
            or self.preflight_only
            or self.state != ConnectionState.READY
            or not self.authenticated
            or not bool(getattr(self.capabilities, "trade_authorized", False))
            or not self.private_stream_healthy
            or str(getattr(self.reconciliation, "last_status", "UNKNOWN")).upper()
            != "IN_SYNC"
            or worker is None
            or str(self._enum_value(getattr(worker, "execution_mode", ""))).upper()
            != "LIVE"
            or bool(getattr(worker, "kill_switch_active", True))
            or bool(getattr(worker, "recovery_only", True))
            or not callable(market_fresh)
            or not market_fresh([MAINNET_RISK_POLICY.symbol])
            or not self.is_account_snapshot_fresh()
        ):
            return False
        risk = self._last_local_mainnet_risk_evidence
        protection = self._last_local_mainnet_protection_evidence
        if not isinstance(risk, dict) or not isinstance(protection, dict):
            return False
        if any(
            risk.get(key) != protection.get(key)
            for key in ("launch_id", "client_order_id", "basket_id", "policy_sha256")
        ):
            return False
        now = time.monotonic()
        return all(
            isinstance(record.get("observed_monotonic"), (int, float))
            and 0 <= now - record["observed_monotonic"] <= 5
            for record in (risk, protection)
        )

    async def get_local_mainnet_risk_context(
        self, intent: OrderIntent, execution_context: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Build risk inputs only from durable Local state and fresh signed data.

        This provider is intentionally read-only. It admits only a new staged
        launch with a reconciled flat account; basket identity or existing
        exposure that cannot be proven durable is rejected rather than guessed.
        """
        try:
            expected_client_order_id = str(intent.client_order_id or "").strip()
            expected_basket_id = str(intent.basket_id or "").strip()
            runtime = await self._local_mainnet_runtime_evidence(
                require_unused_staged_launch=True,
                expected_order_client_id=expected_client_order_id,
                expected_basket_id=expected_basket_id,
            )
            if runtime is None:
                return None
            if (
                str(intent.symbol).upper() != MAINNET_RISK_POLICY.symbol
                or not str(intent.client_order_id or "").strip()
                or not str(intent.basket_id or "").strip()
            ):
                return None
            quantity = self._decimal_value(execution_context.get("validated_quantity"), positive=True)
            entry_price = self._decimal_value(execution_context.get("validated_entry_price"), positive=True)
            if quantity != self._decimal_value(intent.quantity, positive=True):
                return None
            if execution_context.get("runtime_target") != "LOCAL":
                return None
            side = str(self._enum_value(intent.side)).upper()
            if side not in {"BUY", "SELL"}:
                return None
            bid = self._decimal_value(self.last_market_bid.get(MAINNET_RISK_POLICY.symbol), positive=True)
            ask = self._decimal_value(self.last_market_ask.get(MAINNET_RISK_POLICY.symbol), positive=True)
            if ask < bid:
                return None
            if str(self._enum_value(intent.order_type)).upper() == "MARKET":
                executable_quote = ask if side == "BUY" else bid
                if entry_price != executable_quote:
                    return None

            ledger_positions = await self.ledger.get_positions()
            ledger_open_orders = await self.ledger.get_open_orders()
            active_owners = await runtime["protections"].list_active_protections(
                "binance_mainnet", MAINNET_RISK_POLICY.symbol
            )
            expected_owner = await runtime["protections"].get_protection(
                "binance_mainnet",
                MAINNET_RISK_POLICY.symbol,
                expected_client_order_id,
            )
            if expected_owner is not None and (
                expected_owner.get("basket_id") != expected_basket_id
                or expected_owner.get("state") != "PENDING"
                or Decimal(str(expected_owner.get("filled_quantity", "-1"))) != 0
                or str(expected_owner.get("entry_side", "")).upper()
                != str(self._enum_value(intent.side)).upper()
                or str(expected_owner.get("position_side", "")).upper()
                != str(self._enum_value(intent.position_side)).upper()
                or Decimal(str(expected_owner.get("requested_quantity", "-1"))) != quantity
                or Decimal(str(expected_owner.get("stop_trigger_price", "-1")))
                != self._decimal_value(intent.stop_loss_price, positive=True)
                or Decimal(str(expected_owner.get("take_profit_trigger_price", "-1")))
                != self._decimal_value(intent.take_profit_price, positive=True)
            ):
                return None
            pending_local_orders = [
                order for order in ledger_open_orders
                if str(getattr(order, "client_order_id", ""))
                != expected_client_order_id
            ]
            current_local_orders = [
                order for order in ledger_open_orders
                if str(getattr(order, "client_order_id", ""))
                == expected_client_order_id
            ]
            if pending_local_orders or len(current_local_orders) > 1 or (
                current_local_orders
                and (
                    str(getattr(current_local_orders[0], "status", "")).upper() != "PENDING"
                    or str(getattr(current_local_orders[0], "exchange_order_id", "") or "")
                    or str(getattr(current_local_orders[0], "basket_id", "") or "")
                    != expected_basket_id
                )
            ):
                return None
            other_owners = [
                owner for owner in active_owners
                if str(owner.get("entry_client_order_id", "")) != expected_client_order_id
            ]
            current_active_owners = [
                owner for owner in active_owners
                if str(owner.get("entry_client_order_id", "")) == expected_client_order_id
            ]
            if other_owners or len(current_active_owners) > 1 or (
                current_active_owners and expected_owner is None
            ):
                return None
            if (
                any(Decimal(str(position.quantity)) != 0 for position in ledger_positions)
                or runtime["snapshot_risk"].current_gross_exposure_usdc != 0
            ):
                return None

            intent_cost_estimates: dict[str, Decimal] = {}
            for source in (
                "estimated_fees_usdc",
                "estimated_funding_usdc",
                "estimated_slippage_usdc",
            ):
                value = self._decimal_value(getattr(intent, source, None))
                if value < 0:
                    return None
                intent_cost_estimates[source] = value
            basket_id = expected_basket_id
            if runtime["session"].get("basket_id") not in (None, "", basket_id):
                return None
            observed_at = utc_now()
            result = {
                "observed_at": observed_at,
                "runtime_target": "LOCAL",
                "database_provider": "POSTGRES_LOCAL",
                "database_identity_verified": True,
                "persistence_durable": True,
                "lease_held": True,
                "kill_switch_active": False,
                "market_data_fresh": True,
                "account_snapshot_fresh": True,
                "reconciliation_status": "IN_SYNC",
                "basket_id": basket_id,
                "run_id": runtime["launch_id"],
                "risk_policy_version": MAINNET_RISK_POLICY.version,
                "risk_policy_sha256": MAINNET_RISK_POLICY_SHA256,
                "source_fingerprint": runtime["fingerprint"],
                "entry_price": entry_price,
                "basket_headroom_usdc": min(
                    MAINNET_RISK_POLICY.basket_budget_usdc,
                    MAINNET_RISK_POLICY.basket_drawdown_usdc,
                ),
                "daily_loss_headroom_usdc": runtime["snapshot_risk"].daily_loss_headroom_usdc,
                "current_gross_exposure_usdc": runtime["snapshot_risk"].current_gross_exposure_usdc,
                "current_basket_exposure_usdc": Decimal("0"),
                "collateral_usdc": runtime["snapshot_risk"].collateral_usdc,
                "available_balance_usdc": self._decimal_value(
                    getattr(runtime["snapshot"], "available_balance", None), positive=True
                ),
                "configured_leverage": runtime["snapshot_risk"].configured_leverage,
                "effective_leverage": runtime["snapshot_risk"].effective_leverage,
                "active_exposure_chains": 0,
                "same_active_basket": False,
                "is_first_risk_increasing_order": True,
                # These values are strategy/caller hints only. A signed book
                # spread is not an execution-depth bound, nor does it prove
                # commission tier or funding exposure. The gate deliberately
                # requires a separate exchange-derived cost provider and will
                # not promote these values to verified costs.
                "intent_cost_estimates_usdc": intent_cost_estimates,
                "cost_evidence_status": "NOT_AVAILABLE",
                "cost_estimate_source": "CALLER_UNVERIFIED",
            }
            session = runtime["session"]
            if session.get("policy") == "LIVE_RESEARCH_PILOT":
                if (
                    session.get("pilot_status") != "ACTIVE"
                    or session.get("pilot_risk_policy_hash") != LOCAL_LIVE_PILOT_POLICY_SHA256
                    or session.get("pilot_strategy_hash") is None
                ):
                    return None
                current_net = self._decimal_value(session.get("pilot_net_pnl_usdc"))
                peak_net = self._decimal_value(session.get("pilot_peak_pnl_usdc"))
                snapshot_checked_at = session.get("pilot_last_account_snapshot_at")
                if int(session.get("submitted_orders", 0) or 0) > 0 and (
                    not isinstance(snapshot_checked_at, datetime)
                    or snapshot_checked_at.tzinfo is None
                    or (utc_now() - snapshot_checked_at).total_seconds() > 5
                ):
                    return None
                if int(session.get("submitted_orders", 0) or 0) > 0:
                    accounting_reader: Any = getattr(
                        self._worker_authority, "get_local_live_pilot_accounting", None
                    )
                    if not callable(accounting_reader):
                        return None
                    accounting = await cast(Any, accounting_reader())
                    if (
                        not isinstance(accounting, Mapping)
                        or accounting.get("status") != "VERIFIED"
                        or accounting.get("campaign_id") != session.get("pilot_campaign_id")
                        or accounting.get("launch_id") != runtime["launch_id"]
                    ):
                        return None
                    current_net = self._decimal_value(accounting.get("net_pnl_usdc"))
                    peak_net = self._decimal_value(accounting.get("peak_net_pnl_usdc"))
                result["basket_headroom_usdc"] = Decimal("2")
                result["daily_loss_headroom_usdc"] = min(
                    Decimal("5"), runtime["snapshot_risk"].daily_loss_headroom_usdc
                )
                result["live_research_pilot"] = {
                    "approval_verified": True,
                    "approval_role": "trading_admin",
                    "binding_verified": True,
                    "runtime_target": "LOCAL",
                    "symbol": "ETHUSDC",
                    "status": session["pilot_status"],
                    "management_mode": session.get("pilot_management_mode"),
                    "source_hash": session.get("pilot_source_hash"),
                    "dependency_hash": session.get("pilot_dependency_hash"),
                    "migration_hash": session.get("pilot_migration_hash"),
                    "strategy_hash": session.get("pilot_strategy_hash"),
                    "risk_policy_hash": session.get("pilot_risk_policy_hash"),
                    "campaign_expires_at": session.get("pilot_campaign_expires_at"),
                    "limits": {
                        "max_position_notional_usdc": Decimal("50"),
                        "max_order_notional_usdc": Decimal("50"),
                        "max_total_exposure_usdc": Decimal("50"),
                        "max_position_stop_risk_usdc": Decimal("2"),
                        "campaign_drawdown_usdc": Decimal("5"),
                        "max_leverage": Decimal("10"),
                    },
                    "drawdown_triggered": session.get("pilot_drawdown_triggered"),
                    "current_position_notional_usdc": Decimal("0"),
                    "current_total_exposure_usdc": runtime["snapshot_risk"].current_gross_exposure_usdc,
                    "current_net_pnl_usdc": current_net,
                    "peak_net_pnl_usdc": peak_net,
                    "configured_leverage": runtime["snapshot_risk"].configured_leverage,
                    "effective_leverage": runtime["snapshot_risk"].effective_leverage,
                    "active_exposure_chains": 0,
                    "active_position_count": 0,
                }
            self._last_local_mainnet_risk_evidence = {
                "launch_id": runtime["launch_id"],
                "client_order_id": str(intent.client_order_id),
                "basket_id": basket_id,
                "policy_sha256": MAINNET_RISK_POLICY_SHA256,
                "observed_monotonic": time.monotonic(),
            }
            self._last_local_mainnet_protection_evidence = None
            return result
        except Exception as exc:
            logger.warning("Local Mainnet risk context rejected: %s", type(exc).__name__)
            return None

    async def get_local_mainnet_cost_evidence(
        self, intent: OrderIntent, context: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Estimate QUICK round-trip costs from fresh Binance read-only data.

        Depth-based slippage is not a guarantee against future book movement;
        it is a conservative snapshot estimate. Missing/insufficient evidence
        blocks the order rather than substituting zero.
        """
        if (
            self.env != BinanceEnvironment.MAINNET
            or self.preflight_only
            or str(getattr(intent, "symbol", "")).upper() != "ETHUSDC"
            or str(getattr(intent, "management_mode", "")).upper() != "QUICK"
            or context.get("runtime_target") != "LOCAL"
        ):
            return None
        try:
            quantity = self._decimal_value(context.get("validated_quantity"), positive=True)
            entry_price = self._decimal_value(context.get("validated_entry_price"), positive=True)
            side = str(self._enum_value(intent.side)).upper()
            if side not in {"BUY", "SELL"} or quantity * entry_price > Decimal("50"):
                return None
            async def fetch_observed(
                key: str, route: str, *, signed: bool = False, params: dict[str, Any] | None = None
            ) -> tuple[Any, dict[str, Any]]:
                started_monotonic = time.monotonic()
                started_at = utc_now()
                response = await self.rest_client.request(
                    "GET", route, signed=signed, params=params or {}
                )
                completed_monotonic = time.monotonic()
                completed_at = utc_now()
                return response, {
                    "evidence_key": key,
                    "route": route,
                    "method": "GET",
                    "signed": signed,
                    "params": params or {},
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "started_monotonic": started_monotonic,
                    "duration_ms": max(0.0, (completed_monotonic - started_monotonic) * 1000),
                    "completed_monotonic": completed_monotonic,
                    # The REST response does not consistently expose an
                    # authoritative source timestamp for these cost inputs.
                    "source_timestamp": None,
                }

            commission_path = (
                "/papi/v1/um/commissionRate"
                if self.portfolio_margin
                else "/fapi/v1/commissionRate"
            )
            leverage_bracket_path = (
                "/papi/v1/um/leverageBracket"
                if self.portfolio_margin
                else "/fapi/v1/leverageBracket"
            )
            observed = await asyncio.gather(
                fetch_observed("commission", commission_path, signed=True,
                               params={"symbol": "ETHUSDC"}),
                fetch_observed("depth", "/fapi/v1/depth",
                               params={"symbol": "ETHUSDC", "limit": 1000}),
                fetch_observed("funding", "/fapi/v1/fundingRate",
                               params={"symbol": "ETHUSDC", "limit": 3}),
                fetch_observed("funding_info", "/fapi/v1/fundingInfo"),
                fetch_observed("leverage_brackets", leverage_bracket_path, signed=True,
                               params={"symbol": "ETHUSDC"}),
            )
            (commission, commission_observation), (depth, depth_observation), \
                (funding, funding_observation), (funding_info, funding_info_observation), \
                (leverage_brackets, leverage_observation) = observed
            observations = {
                "commission": commission_observation,
                "depth": depth_observation,
                "funding": funding_observation,
                "funding_info": funding_info_observation,
                "leverage_brackets": leverage_observation,
            }
            observed_at = max(item["completed_at"] for item in observations.values())
            now_monotonic = time.monotonic()
            if any(
                item["duration_ms"] < 0
                or now_monotonic - item["completed_monotonic"] < 0
                or now_monotonic - item["completed_monotonic"] > 5
                for item in observations.values()
            ):
                return None
            if not isinstance(commission, dict) or str(commission.get("symbol", "")).upper() != "ETHUSDC":
                return None
            taker_rate = self._decimal_value(commission.get("takerCommissionRate"), nonnegative=True)
            fee_bound = quantity * entry_price * taker_rate * Decimal("2")
            if not isinstance(depth, dict) or not isinstance(depth.get("bids"), list) or not isinstance(depth.get("asks"), list):
                return None
            bids, asks = depth["bids"], depth["asks"]
            if not bids or not asks:
                return None
            best_bid = self._decimal_value(bids[0][0], positive=True)
            best_ask = self._decimal_value(asks[0][0], positive=True)
            if best_ask < best_bid:
                return None

            def adverse_depth_cost(levels: list[Any], buy: bool, reference: Decimal) -> Decimal:
                remaining = quantity
                total = Decimal("0")
                for level in levels:
                    if not isinstance(level, (list, tuple)) or len(level) < 2:
                        raise ValueError("malformed depth level")
                    price = self._decimal_value(level[0], positive=True)
                    available = self._decimal_value(level[1], positive=True)
                    filled = min(remaining, available)
                    adverse = max(Decimal("0"), price - reference) if buy else max(Decimal("0"), reference - price)
                    total += filled * adverse
                    remaining -= filled
                    if remaining <= 0:
                        return total
                raise ValueError("depth does not cover the pilot quantity")

            # Entry and exit consume opposing sides. For either direction this
            # bounds the snapshot's adverse walk across both legs.
            midpoint = (best_bid + best_ask) / Decimal("2")
            slippage_bound = adverse_depth_cost(asks, True, midpoint) + adverse_depth_cost(bids, False, midpoint)

            if isinstance(funding, dict):
                funding_rows = [funding]
            elif isinstance(funding, list):
                funding_rows = funding
            else:
                return None
            observed_rates = [
                abs(self._decimal_value(row.get("fundingRate")))
                for row in funding_rows
                if isinstance(row, dict) and str(row.get("symbol", "")).upper() == "ETHUSDC"
            ]
            if not observed_rates:
                return None
            if isinstance(funding_info, dict):
                info_rows = [funding_info]
            elif isinstance(funding_info, list):
                info_rows = funding_info
            else:
                return None
            symbol_info = [row for row in info_rows if isinstance(row, dict)
                           and str(row.get("symbol", "")).upper() == "ETHUSDC"]
            if len(symbol_info) > 1:
                return None
            if not isinstance(leverage_brackets, list):
                return None
            bracket_records = [record for record in leverage_brackets
                               if isinstance(record, dict) and str(record.get("symbol", "")).upper() == "ETHUSDC"]
            if len(bracket_records) != 1 or not isinstance(bracket_records[0].get("brackets"), list):
                return None
            notional = quantity * entry_price
            matching_brackets = []
            for bracket in bracket_records[0]["brackets"]:
                if not isinstance(bracket, dict):
                    continue
                floor = self._decimal_value(bracket.get("notionalFloor"), nonnegative=True)
                cap = self._decimal_value(bracket.get("notionalCap"), positive=True)
                if floor <= notional <= cap:
                    matching_brackets.append(bracket)
            if len(matching_brackets) != 1:
                return None
            maintenance_ratio = self._decimal_value(matching_brackets[0].get("maintMarginRatio"), positive=True)
            bracket_funding_cap = maintenance_ratio * Decimal("0.75")
            if symbol_info:
                interval = self._decimal_value(symbol_info[0].get("fundingIntervalHours"), positive=True)
                rate_cap = abs(self._decimal_value(symbol_info[0].get("adjustedFundingRateCap"), nonnegative=True))
                rate_floor = abs(self._decimal_value(symbol_info[0].get("adjustedFundingRateFloor"), nonnegative=True))
                funding_cap = max(rate_cap, rate_floor)
            else:
                # Binance documents the default 8h interval and derives the
                # default cap/floor from 0.75 x maintenance margin ratio.
                # Unknown fundingInfo uses the shortest permitted horizon as
                # a conservative upper bound.
                interval = Decimal("1")
                funding_cap = bracket_funding_cap
            if interval > Decimal("24") or funding_cap <= 0:
                return None
            horizon_seconds = int(LOCAL_LIVE_PILOT_POLICY["quick_max_hold_seconds"])
            periods = math.ceil(
                Decimal(horizon_seconds) / (interval * Decimal("3600"))
            ) + 1
            funding_bound = notional * funding_cap * periods
            return {
                "source": (
                    "BINANCE_PAPI_COMMISSION_FUNDING_DEPTH"
                    if self.portfolio_margin
                    else "BINANCE_FAPI_COMMISSION_FUNDING_DEPTH"
                ),
                "symbol": "ETHUSDC",
                "client_order_id": str(intent.client_order_id),
                "quantity": quantity,
                "side": side,
                "observed_at": observed_at,
                "fees_upper_bound_usdc": fee_bound,
                "funding_upper_bound_usdc": funding_bound,
                "slippage_upper_bound_usdc": slippage_bound,
                "funding_interval_hours": interval,
                "funding_events_assumed": periods,
                "cost_horizon_seconds": horizon_seconds,
                "cost_horizon_source": "LOCAL_LIVE_PILOT_POLICY",
                "runtime_target": "LOCAL",
                "venue": "BINANCE_MAINNET",
                "funding_rate_cap_source": "BINANCE_FUNDING_INFO" if symbol_info else "BINANCE_LEVERAGE_BRACKET_MMR",
                "funding_rate_observed_abs_max": max(observed_rates),
                "request_observations": observations,
                "depth_levels_requested": 1000,
                "depth_bid_levels_received": len(bids),
                "depth_ask_levels_received": len(asks),
            }
        except Exception as exc:
            logger.warning("Binance cost evidence unavailable: %s", type(exc).__name__)
            return None

    def invalidate_authentication(self) -> None:
        """Drop authentication immediately after any signed-request auth failure."""
        self.capabilities.authenticated = False
        self.capabilities.account_request_succeeded = False
        self.capabilities.trade_authorized = False
        self.state = ConnectionState.DEGRADED

    def bind_worker_authority(self, worker: object) -> None:
        """Bind the owning Worker object for the sole mutable execution path."""
        if self._worker_authority is not None and self._worker_authority is not worker:
            raise RuntimeError("Worker authority cannot be rebound")
        self._worker_authority = worker

    def _worker_authorized(self, authority: Any) -> bool:
        return self._worker_authority is not None and authority is self._worker_authority

    async def _notify_order_submission_result(
        self, order: ExecutionOrder, outcome: str
    ) -> None:
        callback = self.on_order_submission_result
        if callback is None:
            return
        try:
            await callback(order, outcome)
        except Exception as exc:
            # A callback failure cannot turn an exchange mutation into a
            # verified success. Keep the adapter degraded and require the
            # Worker to reconcile before any further risk increase.
            self.state = ConnectionState.DEGRADED
            self.reconciliation.last_status = "UNKNOWN"
            logger.error(
                "monitor_event=staged_session_violation order_submission_outcome_persist_failed symbol=%s error_class=%s",
                order.symbol,
                type(exc).__name__,
            )
            logger.error(
                "Order submission outcome persistence failed for %s: %s",
                order.client_order_id,
                type(exc).__name__,
            )

    def set_execution_lease(
        self, lease: Optional[ExecutionLease], *, required: Optional[bool] = None
    ) -> None:
        """Attach the account/environment-scoped lease owned by the Worker."""

        self.execution_lease = lease
        if required is not None:
            self.execution_lease_required = bool(required)

    async def _assert_execution_lease(self, risk_class: EconomicRiskClass) -> None:
        """Fence the worker immediately before any request that can add risk."""

        risk = (
            risk_class
            if isinstance(risk_class, EconomicRiskClass)
            else EconomicRiskClass(str(risk_class))
        )
        if risk not in {
            EconomicRiskClass.NEW_RISK,
            EconomicRiskClass.INCREASE_RISK,
        }:
            return
        lease = self.execution_lease
        if lease is None:
            if self.execution_lease_required:
                raise LeaseLostError("Distributed execution lease is required before submission")
        else:
            await lease.assert_valid()

        # This synchronous heartbeat check runs after any asynchronous lease
        # validation so it is the last authority check before the caller hands
        # the order to the REST client.
        if (
            self.env == BinanceEnvironment.MAINNET
            and str(os.getenv("LOCAL_ONLY", "")).strip().lower()
            in {"1", "true", "yes", "on"}
            and str(os.getenv("LOCAL_RUNTIME_TARGET", "")).strip().upper() == "LOCAL"
        ):
            authority = self._worker_authority
            heartbeat_check = getattr(
                authority, "local_supervisor_heartbeat_is_fresh", None
            )
            if not callable(heartbeat_check) or not heartbeat_check():
                raise LeaseLostError(
                    "Local supervisor heartbeat is missing or stale before Mainnet submission"
                )

    async def _final_risk_increase_fence(
        self,
        decision: ExecutionDecision,
        intent: OrderIntent,
        expected_prepared: Any,
        *,
        client_order_id: str,
        reserved_open_orders: int,
        reserved_notional: Decimal,
        allow_emergency_fallback: bool,
    ) -> None:
        """Recheck Worker authority after REST throttling and before network send."""

        if (
            self._is_local_live_pilot_bound()
            and local_live_pilot_readiness()["can_start"] is not True
        ):
            raise LeaseLostError(
                "Local Live Research Pilot capability gate is not ready before submission"
            )
        if self._is_local_live_pilot_bound():
            monitor_check = getattr(
                self._worker_authority, "local_pilot_monitor_allows_new_risk", None
            )
            if not callable(monitor_check) or monitor_check() is not True:
                raise LeaseLostError(
                    "Local Pilot lifecycle monitor is stale, stalled, or degraded before submission"
                )

        gate: Any = None
        if self.env == BinanceEnvironment.MAINNET:
            gate = getattr(self._worker_authority, "_evaluate_execution_gate", None)
            if not callable(gate):
                raise LeaseLostError(
                    "Mainnet Worker decision gate is unavailable before submission"
                )
            allowed, reason = cast(Tuple[bool, str], gate(decision))
            if not allowed:
                raise LeaseLostError(
                    f"Mainnet Worker decision gate closed before submission: {reason}"
                )

        final_gate = await self.order_gate.check(
            intent,
            decision.risk_class,
            reserved_open_orders=reserved_open_orders,
            reserved_notional=reserved_notional,
            exclude_client_order_id=client_order_id,
            allow_emergency_fallback=allow_emergency_fallback,
        )
        if not final_gate.allowed or final_gate.prepared is None:
            raise LeaseLostError(
                f"Final order risk gate closed before submission: {final_gate.reason}"
            )
        if final_gate.prepared != expected_prepared:
            raise LeaseLostError(
                "Final order risk inputs changed after preparation; a fresh decision is required"
            )

        # The order-level gate may await fresh market/protection data. Recheck
        # the lease and all synchronous Worker freshness flags after it, with
        # no further await before returning to the HTTP send boundary.
        await self._assert_execution_lease(decision.risk_class)
        if gate is not None:
            allowed, reason = cast(Tuple[bool, str], gate(decision))
            if not allowed:
                raise LeaseLostError(
                    f"Mainnet Worker decision gate closed before submission: {reason}"
                )
        if self._is_local_live_pilot_bound():
            monitor_check = getattr(
                self._worker_authority, "local_pilot_monitor_allows_new_risk", None
            )
            if not callable(monitor_check) or monitor_check() is not True:
                raise LeaseLostError(
                    "Local Pilot lifecycle monitor became stale, stalled, or degraded before submission"
                )

    @staticmethod
    def _exchange_event_time(payload: Dict[str, Any]) -> Optional[datetime]:
        raw_timestamp = payload.get("E")
        if raw_timestamp in (None, ""):
            raw_timestamp = payload.get("time")
        if raw_timestamp in (None, ""):
            return None
        try:
            parsed = float(raw_timestamp)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed) or parsed <= 0:
            return None
        # Binance REST/WS timestamps are milliseconds since Unix epoch.
        try:
            return datetime.fromtimestamp(parsed / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    def _record_rest_market_sample(
        self,
        symbol: str,
        *,
        price: Decimal,
        payload: Dict[str, Any],
        reference_price: Optional[Decimal] = None,
        bid: Optional[Decimal] = None,
        ask: Optional[Decimal] = None,
        bid_qty: Optional[Decimal] = None,
        ask_qty: Optional[Decimal] = None,
    ) -> bool:
        if not price.is_finite() or price <= 0:
            return False
        normalized_symbol = symbol.upper()
        timestamp = self._exchange_event_time(payload)
        if timestamp is None:
            return False
        self.last_market_event_at[normalized_symbol] = timestamp
        self.last_market_price[normalized_symbol] = price
        label = environment_label(self.env)
        self.last_market_event_source[normalized_symbol] = f"{label}_REST"
        self.last_market_event_venue[normalized_symbol] = label
        self.last_market_event_market_type[normalized_symbol] = MarketType.USDM_FUTURES.value
        if (
            reference_price is not None
            and reference_price.is_finite()
            and reference_price > 0
        ):
            self.last_market_reference_price[normalized_symbol] = reference_price
            self.last_market_reference_at[normalized_symbol] = timestamp
        if bid is not None and ask is not None:
            self.last_market_bid[normalized_symbol] = bid
            self.last_market_ask[normalized_symbol] = ask
            self.last_market_book_at[normalized_symbol] = timestamp
            if bid_qty is not None and bid_qty.is_finite() and bid_qty > 0:
                self.last_market_bid_qty[normalized_symbol] = bid_qty
            if ask_qty is not None and ask_qty.is_finite() and ask_qty > 0:
                self.last_market_ask_qty[normalized_symbol] = ask_qty
        return True

    def has_authoritative_market_sample(self, symbol: str) -> bool:
        normalized_symbol = str(symbol).upper()
        return (
            self.last_market_event_source.get(normalized_symbol)
            in {
                f"{environment_label(self.env)}_WS",
                f"{environment_label(self.env)}_REST",
            }
            and self.last_market_event_venue.get(normalized_symbol)
            == environment_label(self.env)
            and self.last_market_event_market_type.get(normalized_symbol)
            == MarketType.USDM_FUTURES.value
        )

    def get_market_reference_price(self, symbol: str) -> Optional[Decimal]:
        """Return a fresh Binance mark price for percent-price filters."""

        normalized_symbol = str(symbol).upper()
        price = self.last_market_reference_price.get(normalized_symbol)
        timestamp = self.last_market_reference_at.get(normalized_symbol)
        if (
            price is None
            or timestamp is None
            or not price.is_finite()
            or price <= 0
        ):
            return None
        if timestamp.tzinfo is None:
            return None
        age = (utc_now() - timestamp).total_seconds()
        if age < 0 or age > self._market_data_max_age():
            return None
        return price

    def record_market_event(self, event: MarketEvent) -> bool:
        """Record a real market sample for per-symbol freshness checks."""
        market_type = getattr(event.market_type, "value", event.market_type)
        venue = str(event.venue).upper()
        expected_venue = environment_label(self.env)
        if market_type != MarketType.USDM_FUTURES.value or venue != expected_venue:
            logger.warning(
                "Ignoring market event outside Binance %s USDⓈ-M: venue=%s market_type=%s",
                self.env.value,
                event.venue,
                market_type,
            )
            return False
        raw_price = event.mark_price or event.last_price
        try:
            price = Decimal(str(raw_price))
        except (InvalidOperation, ValueError):
            return False
        if not price.is_finite() or price <= 0:
            return False
        timestamp = event.event_time
        if timestamp.tzinfo is None:
            return False
        symbol = str(event.symbol).upper()
        self.last_market_event_at[symbol] = timestamp
        self.last_market_price[symbol] = price
        self.last_market_event_source[symbol] = f"{expected_venue}_WS"
        self.last_market_event_venue[symbol] = expected_venue
        self.last_market_event_market_type[symbol] = MarketType.USDM_FUTURES.value
        if event.mark_price is not None:
            try:
                mark_price = Decimal(str(event.mark_price))
            except (InvalidOperation, TypeError, ValueError):
                mark_price = None
            if mark_price is not None and mark_price.is_finite() and mark_price > 0:
                self.last_market_reference_price[symbol] = mark_price
                self.last_market_reference_at[symbol] = timestamp
        # markPrice frames have no order book; the public stream parser mirrors
        # the mark into best_bid/best_ask. Only book events may set the
        # executable quote, otherwise it alternates between ask and mark.
        if event.mark_price is not None:
            return True
        try:
            bid = Decimal(str(event.best_bid))
            ask = Decimal(str(event.best_ask))
            if bid.is_finite() and ask.is_finite() and bid > 0 and ask >= bid:
                self.last_market_bid[symbol] = bid
                self.last_market_ask[symbol] = ask
                self.last_market_book_at[symbol] = timestamp
        except (InvalidOperation, TypeError, ValueError):
            pass
        return True

    def _market_data_max_age(self) -> float:
        raw = os.getenv("MAX_MARKET_DATA_AGE_SEC", "3.0")
        try:
            value = float(raw)
        except ValueError:
            return 3.0
        return value if value > 0 and value != float("inf") and value != float("-inf") else 3.0

    async def get_fresh_market_price(
        self, symbol: str, side: Optional[str] = None
    ) -> Optional[Decimal]:
        """Return a fresh executable route price; never synthesize one.

        ``side`` is optional for compatibility with mark-price consumers.  A
        MARKET order passes BUY/SELL and therefore uses the executable ask/bid
        rather than a mid/mark estimate.
        """
        return await self._get_fresh_market_price(symbol, side)

    async def _get_fresh_market_price(
        self, symbol: str, side: Optional[str] = None
    ) -> Optional[Decimal]:
        normalized_symbol = symbol.upper()
        normalized_side = str(side or "").upper()
        cached = (
            self.last_market_ask.get(normalized_symbol)
            if normalized_side == OrderSide.BUY.value
            else self.last_market_bid.get(normalized_symbol)
            if normalized_side == OrderSide.SELL.value
            else self.last_market_reference_price.get(normalized_symbol)
        )
        # Executable bid/ask samples and the mark/reference sample have
        # independent freshness clocks. A fresh book ticker must never make
        # an older mark price look fresh for PERCENT_PRICE validation.
        # Every production writer of last_market_bid/ask also sets
        # last_market_book_at; the event clock is only a fallback for state
        # seeded without a book timestamp.
        event_at = (
            self.last_market_book_at.get(normalized_symbol)
            or self.last_market_event_at.get(normalized_symbol)
            if normalized_side in {OrderSide.BUY.value, OrderSide.SELL.value}
            else self.last_market_reference_at.get(normalized_symbol)
        )
        if event_at and cached and (
            self.has_authoritative_market_sample(normalized_symbol)
            or normalized_side not in {OrderSide.BUY.value, OrderSide.SELL.value}
        ):
            if event_at.tzinfo is None:
                return None
            age = (utc_now() - event_at).total_seconds()
            if 0 <= age <= self._market_data_max_age():
                return cached

        try:
            if normalized_side in {OrderSide.BUY.value, OrderSide.SELL.value}:
                quote = await self.get_best_bid_ask(normalized_symbol)
                if quote is None:
                    return None
                return quote[1] if normalized_side == OrderSide.BUY.value else quote[0]
            payload = await self.rest_client.request(
                "GET", "/fapi/v1/premiumIndex", params={"symbol": normalized_symbol}
            )
            if not isinstance(payload, dict):
                return None
            if str(payload.get("symbol", "")).upper() != normalized_symbol:
                return None
            price = Decimal(str(payload.get("markPrice")))
            if not self._record_rest_market_sample(
                normalized_symbol,
                price=price,
                payload=payload,
                reference_price=price,
            ):
                return None
            return price
        except BinanceAuthenticationError:
            self.invalidate_authentication()
            self.reconciliation.last_status = "UNKNOWN"
            return None
        except BinanceRateLimitError:
            self.state = ConnectionState.DEGRADED
            self.reconciliation.last_status = "UNKNOWN"
            return None
        except (BinanceTransportAmbiguity, BinanceTimestampError):
            self.state = ConnectionState.DEGRADED
            self.reconciliation.last_status = "UNKNOWN"
            return None
        except Exception as exc:
            self.state = ConnectionState.DEGRADED
            self.reconciliation.last_status = "UNKNOWN"
            logger.warning("Fresh market price unavailable for %s: %s", normalized_symbol, exc)
            return None

    async def get_best_bid_ask(self, symbol: str) -> Optional[Tuple[Decimal, Decimal]]:
        """Fetch a current route book quote for final order validation."""
        normalized_symbol = symbol.upper()
        try:
            payload = await self.rest_client.request(
                "GET", "/fapi/v1/ticker/bookTicker", params={"symbol": normalized_symbol}
            )
            if not isinstance(payload, dict):
                return None
            if str(payload.get("symbol", "")).upper() != normalized_symbol:
                return None
            bid = Decimal(str(payload.get("bidPrice")))
            ask = Decimal(str(payload.get("askPrice")))
            if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
                return None
            try:
                bid_qty = Decimal(str(payload.get("bidQty")))
                ask_qty = Decimal(str(payload.get("askQty")))
            except (InvalidOperation, TypeError, ValueError):
                return None
            if (
                not bid_qty.is_finite()
                or not ask_qty.is_finite()
                or bid_qty <= 0
                or ask_qty <= 0
            ):
                return None
            if not self._record_rest_market_sample(
                normalized_symbol,
                price=(bid + ask) / Decimal("2"),
                payload=payload,
                bid=bid,
                ask=ask,
                bid_qty=bid_qty,
                ask_qty=ask_qty,
            ):
                return None
            return bid, ask
        except BinanceAuthenticationError:
            self.invalidate_authentication()
            self.reconciliation.last_status = "UNKNOWN"
            return None
        except BinanceRateLimitError:
            self.state = ConnectionState.DEGRADED
            self.reconciliation.last_status = "UNKNOWN"
            return None
        except (BinanceTransportAmbiguity, BinanceTimestampError):
            self.state = ConnectionState.DEGRADED
            self.reconciliation.last_status = "UNKNOWN"
            return None
        except Exception as exc:
            self.state = ConnectionState.DEGRADED
            self.reconciliation.last_status = "UNKNOWN"
            logger.warning("Book quote unavailable for %s: %s", normalized_symbol, exc)
            return None

    async def refresh_market_data(self, symbols: List[str]) -> bool:
        results = [await self.get_best_bid_ask(symbol) for symbol in symbols]
        return bool(results) and all(result is not None for result in results)

    async def _on_user_stream_disconnect(self) -> None:
        logger.warning("[%s] User stream disconnected. Adapter transitioning to DEGRADED.", self.env)
        self.state = ConnectionState.DEGRADED
        try:
            await self.reconciliation.reconcile()
            if getattr(self.reconciliation, "authentication_failed", False):
                self.invalidate_authentication()
        except Exception as exc:
            logger.error("Reconciliation after user-stream disconnect failed: %s", exc)

    async def _on_user_stream_reconnected(self) -> None:
        logger.info("[%s] User stream reconnected. Initiating reconciliation.", self.env)
        self.state = ConnectionState.SYNCING
        sync_result = await self.reconciliation.reconcile()
        if getattr(self.reconciliation, "authentication_failed", False):
            self.invalidate_authentication()
        if sync_result == "IN_SYNC" and self.private_stream_healthy and self.authenticated:
            self.state = ConnectionState.READY
            logger.info("[%s] Reconnected and IN_SYNC. State transitioned to READY.", self.env)
        else:
            self.state = ConnectionState.DEGRADED
            logger.warning(
                "[%s] Reconnection verification failed: sync=%s ws=%s auth=%s",
                self.env,
                sync_result,
                self.private_stream_healthy,
                self.authenticated,
            )

    async def connect(self) -> bool:
        self.state = ConnectionState.CONNECTING
        try:
            await self.rest_client.init_session()
            self.state = ConnectionState.AUTHENTICATING
            if not await self.capabilities.discover(self.rest_client):
                self.state = ConnectionState.DEGRADED
                return False

            self.state = ConnectionState.STREAM_STARTING
            if not await self.user_stream.start(self._on_ws_event):
                self.state = ConnectionState.DEGRADED
                return False

            self.state = ConnectionState.SYNCING
            sync_result = await self.reconciliation.reconcile()
            if getattr(self.reconciliation, "authentication_failed", False):
                self.invalidate_authentication()
            if sync_result == "IN_SYNC" and self.private_stream_healthy and self.authenticated:
                self.state = ConnectionState.READY
                return True
            self.state = ConnectionState.DEGRADED
            return False
        except Exception as exc:
            self.invalidate_authentication()
            logger.error("Binance %s adapter connection failed: %s", self.env.value, exc)
            return False

    async def arm(self) -> bool:
        if self.preflight_only:
            logger.warning("Read-only preflight adapter cannot transition to ARMED")
            return False
        return await self.connect()

    async def _on_ws_event(self, event: Any) -> None:
        event_type = event.get("e") if isinstance(event, dict) else None
        if event_type == "ORDER_TRADE_UPDATE":
            order_info = event.get("o", {})
            symbol = str(order_info.get("s", "")).upper()
            client_order_id = str(order_info.get("c", ""))
            if not symbol or not client_order_id:
                return
            self.last_order_event_at[client_order_id] = utc_now()
            status = str(order_info.get("X", "UNKNOWN"))
            existing_order = await self.ledger.get_order_by_client_id(client_order_id)
            if existing_order is None:
                # A private event without a local intent/decision lineage may
                # be a manual order or a different worker instance. Never
                # adopt it as if this worker authorized it; reconciliation
                # must remain non-IN_SYNC until the operator resolves it.
                logger.error(
                    "Quarantining unowned %s order event %s for %s",
                    self.environment_label,
                    client_order_id,
                    symbol,
                )
                await self.ledger.set_account_snapshot(None)
                self.reconciliation.last_diffs = [
                    ReconciliationDiff(
                        code="EXCHANGE_ORDER_EVENT_UNKNOWN_LOCALLY",
                        symbol=symbol,
                        local_value=client_order_id,
                        exchange_value=str(order_info.get("i") or "UNKNOWN"),
                    )
                ]
                self.reconciliation.last_status = "UNKNOWN"
                return

            if existing_order:
                existing_order.status = status
                if order_info.get("i") is not None:
                    existing_order.exchange_order_id = str(order_info["i"])
                await self.ledger.upsert_order(existing_order)

            # Every order lifecycle update can change open-order count,
            # exposure, balances, or fills.  Invalidate all prior readiness
            # evidence before the next risk-increasing decision.
            await self.ledger.set_account_snapshot(None)
            self.reconciliation.last_status = "UNKNOWN"

            if order_info.get("x") != "TRADE":
                return
            try:
                required_fill_fields = ("t", "i", "l", "L", "n", "N", "rp", "m", "T")
                missing_fill_fields = [
                    field
                    for field in required_fill_fields
                    if order_info.get(field) in (None, "")
                ]
                if missing_fill_fields:
                    raise ValueError(
                        "Trade update is missing required fields: "
                        + ", ".join(missing_fill_fields)
                    )
                side = OrderSide(str(order_info.get("S")))
                position_side = PositionSide(str(order_info.get("ps", "BOTH")))
                quantity = Decimal(str(order_info["l"]))
                price = Decimal(str(order_info["L"]))
                commission = Decimal(str(order_info["n"]))
                realized_pnl = Decimal(str(order_info["rp"]))
                if any(
                    not value.is_finite()
                    for value in (quantity, price, commission, realized_pnl)
                ):
                    raise ValueError("Trade update contains non-finite economics")
                if quantity <= 0 or price <= 0 or commission < 0:
                    raise ValueError("Trade update contains unusable economics")
                event_time = event.get("E") or order_info.get("T")
                if event_time in (None, ""):
                    raise ValueError("Trade update has no event or transaction timestamp")
                fill = ExchangeFill(
                    exchange_trade_id=str(order_info["t"]),
                    exchange_order_id=str(order_info["i"]),
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=side,
                    position_side=position_side,
                    quantity=quantity,
                    price=price,
                    commission=commission,
                    commission_asset=str(order_info["N"]),
                    realized_pnl=realized_pnl,
                    maker=_exchange_bool(order_info.get("m", False)),
                    event_time=event_time,
                    transaction_time=order_info["T"],
                    source=self.environment_label,
                    strategy_id=(existing_order.strategy_id if existing_order else "portfolio"),
                    decision_id=(existing_order.decision_id if existing_order else None),
                    target_exposure_id=(
                        existing_order.target_exposure_id if existing_order else None
                    ),
                    source_intent_ids=(
                        list(existing_order.source_intent_ids) if existing_order else []
                    ),
                )
                await self.ledger.append_fill(fill)
                if (
                    self.env == BinanceEnvironment.MAINNET
                    and self._is_local_mainnet_runtime()
                    and self.on_local_live_pilot_fill is not None
                ):
                    try:
                        persisted = await self.on_local_live_pilot_fill(fill)
                    except Exception as exc:
                        persisted = False
                        logger.error(
                            "Local Pilot fill accounting callback failed: %s",
                            type(exc).__name__,
                        )
                    if persisted is not True:
                        await self.ledger.set_account_snapshot(None)
                        self.reconciliation.last_status = "UNKNOWN"
                        self.reconciliation.last_diffs = [
                            ReconciliationDiff(
                                code="LOCAL_PILOT_FILL_ACCOUNTING_UNAVAILABLE",
                                symbol=symbol,
                                local_value=fill.exchange_trade_id,
                                exchange_value="durable_pnl_write_not_confirmed",
                            )
                        ]
                        logger.error(
                            "Local Pilot fill accounting is unconfirmed; new risk remains blocked"
                        )
            except (InvalidOperation, KeyError, ValueError, TypeError) as exc:
                logger.error("Invalid %s fill event ignored: %s", self.environment_label, exc)
        elif event_type == "ACCOUNT_UPDATE":
            update_data = event.get("a", {})
            if not isinstance(update_data, dict):
                await self.ledger.set_account_snapshot(None)
                self.reconciliation.last_diffs = [
                    ReconciliationDiff(
                        code="ACCOUNT_UPDATE_INVALID",
                        exchange_value="account_update_data_not_object",
                    )
                ]
                self.reconciliation.last_status = "UNKNOWN"
                return

            account_update_diffs: List[ReconciliationDiff] = []

            def mark_account_update_unknown(diff: ReconciliationDiff) -> None:
                account_update_diffs.append(diff)
                self.reconciliation.last_diffs = account_update_diffs
                self.reconciliation.last_status = "UNKNOWN"
                authority = getattr(self, "_worker_authority", None)
                if authority is not None:
                    authority.reconciliation_status = "UNKNOWN"

            raw_positions_val = update_data.get("P", [])
            raw_positions: list[Any] = raw_positions_val if isinstance(raw_positions_val, list) else []
            if not isinstance(raw_positions_val, list):
                mark_account_update_unknown(
                    ReconciliationDiff(
                        code="ACCOUNT_POSITION_UPDATE_INVALID",
                        exchange_value="positions_not_list",
                    )
                )

            for position in raw_positions:
                if not isinstance(position, dict):
                    mark_account_update_unknown(
                        ReconciliationDiff(
                            code="ACCOUNT_POSITION_UPDATE_INVALID",
                            exchange_value="position_not_object",
                        )
                    )
                    continue
                symbol = str(position.get("s", "")).upper()
                raw_position_side = position.get("ps")
                position_side = str(raw_position_side or "").upper()
                if not symbol or not position_side or position.get("pa") in (None, ""):
                    mark_account_update_unknown(
                        ReconciliationDiff(
                            code="ACCOUNT_POSITION_UPDATE_INCOMPLETE",
                            symbol=symbol or None,
                            exchange_value="symbol_positionSide_positionAmt_required",
                        )
                    )
                    continue
                # ACCOUNT_UPDATE is a delta.  Binance does not include every
                # position-risk field (notably mark/liquidation price and
                # leverage) in every event.  Preserve the last authoritative
                # value instead of replacing it with a fabricated zero/None.
                existing_position = next(
                    (
                        item
                        for item in await self.ledger.get_positions()
                        if str(item.symbol).upper() == symbol
                        and item.position_side.value == position_side
                    ),
                    None,
                )
                merged_position = {
                    "symbol": symbol,
                    "positionSide": position_side,
                    "positionAmt": position.get("pa"),
                    "entryPrice": position.get("ep"),
                    "unRealizedProfit": position.get("up"),
                    "marginType": position.get("mt"),
                    "eventTime": event.get("E"),
                    "source": self.environment_label,
                }
                if existing_position is not None:
                    for raw_name, attribute in (
                        ("entryPrice", "entry_price"),
                        ("unRealizedProfit", "unrealized_pnl"),
                        ("marginType", "margin_type"),
                        ("markPrice", "mark_price"),
                        ("liquidationPrice", "liquidation_price"),
                        ("leverage", "leverage"),
                    ):
                        if merged_position.get(raw_name) in (None, ""):
                            previous_value = getattr(existing_position, attribute, None)
                            if previous_value is not None:
                                merged_position[raw_name] = str(previous_value)
                try:
                    await self.ledger.upsert_position(merged_position)
                except (InvalidOperation, TypeError, ValueError) as exc:
                    # A new active delta without a complete authoritative
                    # position-risk record is not an exposure of zero and is
                    # not safe to promote into the ledger. REST reconciliation
                    # must obtain the complete positionRisk row first.
                    mark_account_update_unknown(
                        ReconciliationDiff(
                            code="ACCOUNT_POSITION_UPDATE_INCOMPLETE",
                            symbol=symbol,
                            exchange_value=str(exc),
                        )
                    )
            raw_balances_val = update_data.get("B", [])
            raw_balances: list[Any] = raw_balances_val if isinstance(raw_balances_val, list) else []
            if not isinstance(raw_balances_val, list):
                mark_account_update_unknown(
                    ReconciliationDiff(
                        code="ACCOUNT_BALANCE_UPDATE_INVALID",
                        exchange_value="balances_not_list",
                    )
                )

            for balance in raw_balances:
                if not isinstance(balance, dict):
                    mark_account_update_unknown(
                        ReconciliationDiff(
                            code="ACCOUNT_BALANCE_UPDATE_INVALID",
                            exchange_value="balance_not_object",
                        )
                    )
                    continue
                missing_balance_fields = [
                    field
                    for field in ("a", "wb", "cw")
                    if balance.get(field) in (None, "")
                ]
                if missing_balance_fields:
                    mark_account_update_unknown(
                        ReconciliationDiff(
                            code="ACCOUNT_BALANCE_UPDATE_INCOMPLETE",
                            exchange_value="missing=" + ",".join(missing_balance_fields),
                        )
                    )
                    continue
                try:
                    wallet_balance = Decimal(str(balance["wb"]))
                    cross_wallet_balance = Decimal(str(balance["cw"]))
                except (InvalidOperation, TypeError, ValueError):
                    mark_account_update_unknown(
                        ReconciliationDiff(
                            code="ACCOUNT_BALANCE_UPDATE_INVALID",
                            symbol=str(balance.get("a") or "").upper() or None,
                            exchange_value="wallet_or_cross_wallet_not_decimal",
                        )
                    )
                    continue
                if not wallet_balance.is_finite() or not cross_wallet_balance.is_finite():
                    mark_account_update_unknown(
                        ReconciliationDiff(
                            code="ACCOUNT_BALANCE_UPDATE_INVALID",
                            symbol=str(balance.get("a") or "").upper() or None,
                            exchange_value="wallet_or_cross_wallet_not_finite",
                        )
                    )
            if (
                self.env == BinanceEnvironment.MAINNET
                and self._is_local_mainnet_runtime()
                and self.on_local_live_pilot_funding_reconcile is not None
                and str(update_data.get("m") or "").strip().upper() == "FUNDING_FEE"
            ):
                try:
                    funding_reconciled = await self.on_local_live_pilot_funding_reconcile(
                        update_data.get("S"),
                        event.get("E")
                    )
                except Exception as exc:
                    logger.error(
                        "Local Pilot funding reconciliation failed: %s",
                        type(exc).__name__,
                    )
                    funding_reconciled = False
                if funding_reconciled is not True:
                    mark_account_update_unknown(
                        ReconciliationDiff(
                            code="LOCAL_PILOT_FUNDING_RECONCILIATION_UNKNOWN",
                            symbol="ETHUSDC",
                            exchange_value="signed_income_history_not_reconciled",
                        )
                    )
            # B contains per-asset deltas, not the USDⓈ-M aggregate totals used
            # by ExchangeAccountSnapshot. Do not project one asset (for example
            # USDT) into wallet/margin totals. The next signed REST reconcile
            # must provide the authoritative aggregate account snapshot.
            # A delta event is not a complete account snapshot and cannot
            # prove reconciliation.  Force the next authoritative REST
            # snapshot/reconcile before any risk-increasing order.
            await self.ledger.set_account_snapshot(None)
            # Invalidate reconciliation before invoking accounting callbacks:
            # a fresh PnL mark must not clear an accounting pause based on the
            # previous snapshot while this delta is still being reconciled.
            self.reconciliation.last_status = "UNKNOWN"
            authority = getattr(self, "_worker_authority", None)
            if authority is not None:
                authority.reconciliation_status = "UNKNOWN"
            if (
                self.env == BinanceEnvironment.MAINNET
                and self._is_local_mainnet_runtime()
                and self.on_local_live_pilot_mark is not None
            ):
                pilot_positions = [
                    row for row in raw_positions
                    if isinstance(row, dict)
                    and str(row.get("s") or "").upper() == "ETHUSDC"
                    and str(row.get("ps") or "").upper() == "BOTH"
                ]
                if len(pilot_positions) > 1:
                    mark_persisted = False
                elif not pilot_positions:
                    mark_persisted = True
                else:
                    position_update = pilot_positions[0]
                    try:
                        event_time = event.get("E")
                        unrealized = Decimal(str(position_update.get("up")))
                        if event_time in (None, "") or not unrealized.is_finite():
                            raise ValueError("private position mark is incomplete")
                        mark_persisted = await self.on_local_live_pilot_mark(
                            "ETHUSDC",
                            f"{event_time}:ETHUSDC:BOTH",
                            unrealized,
                            event_time,
                        )
                    except Exception as exc:
                        logger.error(
                            "Local Pilot position mark persistence failed: %s",
                            type(exc).__name__,
                        )
                        mark_persisted = False
                if mark_persisted is not True:
                    account_update_diffs.append(
                        ReconciliationDiff(
                            code="LOCAL_PILOT_MARK_ACCOUNTING_UNAVAILABLE",
                            symbol="ETHUSDC",
                            exchange_value="durable_position_mark_not_confirmed",
                        )
                    )
            self.reconciliation.last_diffs = account_update_diffs
            self.reconciliation.last_status = "UNKNOWN"

    def _generate_client_order_id(
        self, context_id: str, symbol: str, order_index: int = 0, attempt: int = 1
    ) -> str:
        raw_str = f"{context_id}-{symbol}"
        hash_str = hashlib.sha256(raw_str.encode()).hexdigest()[:12]
        return f"BAI-{hash_str}-{order_index}-{attempt}"

    @staticmethod
    def _order_from_response(
        intent: OrderIntent,
        response: Dict[str, Any],
        prepared: Any,
        client_order_id: str,
        decision: Optional[ExecutionDecision] = None,
        allow_terminal_status: bool = False,
    ) -> ExecutionOrder:
        if not isinstance(response, dict):
            raise BinanceTransportAmbiguity("Binance order response is not an object")
        order_id = response.get("orderId")
        status = response.get("status")
        if order_id in (None, "") or status in (None, ""):
            raise BinanceTransportAmbiguity("Binance order response did not contain orderId/status")
        expected_symbol = str(prepared.symbol).upper()
        response_symbol = response.get("symbol")
        if response_symbol in (None, "") or str(response_symbol).upper() != expected_symbol:
            raise BinanceTransportAmbiguity(
                "Binance order response symbol does not match the submitted intent"
            )

        response_client_id = response.get("clientOrderId")
        if response_client_id not in (None, "") and str(response_client_id) != client_order_id:
            raise BinanceTransportAmbiguity(
                "Binance order response clientOrderId does not match the submitted intent"
            )

        response_side = response.get("side")
        expected_side = getattr(intent.side, "value", intent.side)
        if response_side not in (None, "") and str(response_side).upper() != str(expected_side).upper():
            raise BinanceTransportAmbiguity(
                "Binance order response side does not match the submitted intent"
            )
        response_position_side = response.get("positionSide")
        expected_position_side = getattr(intent.position_side, "value", intent.position_side)
        if (
            response_position_side not in (None, "")
            and str(response_position_side).upper() != str(expected_position_side).upper()
        ):
            raise BinanceTransportAmbiguity(
                "Binance order response positionSide does not match the submitted intent"
            )

        raw_quantity = response.get("origQty")
        if raw_quantity in (None, ""):
            raise BinanceTransportAmbiguity("Binance order response did not contain origQty")
        try:
            response_quantity = Decimal(str(raw_quantity))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise BinanceTransportAmbiguity("Binance order response origQty is invalid") from exc
        if (
            not response_quantity.is_finite()
            or response_quantity <= 0
            or response_quantity != prepared.quantity
        ):
            raise BinanceTransportAmbiguity(
                "Binance order response quantity does not match the normalized intent"
            )

        normalized_status = str(status).upper()
        if normalized_status not in {
            "NEW",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCELED",
            "CANCELLED",
            "EXPIRED",
            "REJECTED",
        }:
            raise BinanceTransportAmbiguity(
                f"Binance order response contains an unknown status: {status}"
            )
        if not allow_terminal_status and normalized_status in {
            "CANCELED",
            "CANCELLED",
            "EXPIRED",
            "REJECTED",
        }:
            raise BinanceDefinitiveRejection(
                -2010,
                f"Binance returned terminal order status {normalized_status}",
            )

        parsed_price: Optional[Decimal] = None
        for candidate_key in ("price", "avgPrice"):
            raw_val = response.get(candidate_key)
            if raw_val not in (None, "", "0", 0):
                try:
                    p = Decimal(str(raw_val))
                    if p.is_finite() and p > 0:
                        parsed_price = p
                        break
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise BinanceTransportAmbiguity("Binance order response price is invalid") from exc

        if parsed_price is not None:
            price = parsed_price
            if prepared.price is not None and price != prepared.price:
                raise BinanceTransportAmbiguity(
                    "Binance order response price does not match the normalized intent"
                )
        else:
            price = prepared.estimated_price
        if not price.is_finite() or price <= 0:
            raise BinanceTransportAmbiguity("Binance order response has no usable execution price")

        if "reduceOnly" in response and _exchange_bool(response.get("reduceOnly")) != bool(intent.reduce_only):
            raise BinanceTransportAmbiguity(
                "Binance order response reduceOnly does not match the submitted intent"
            )
        return ExecutionOrder(
            symbol=prepared.symbol,
            side=intent.side,
            quantity=prepared.quantity,
            price=price,
            order_type=prepared.order_type,
            client_order_id=str(response.get("clientOrderId") or client_order_id),
            status=normalized_status,
            exchange_order_id=str(order_id),
            timestamp=utc_now(),
            market_type=intent.market_type,
            position_side=intent.position_side,
            reduce_only=intent.reduce_only,
            time_in_force=intent.time_in_force,
            strategy_id=intent.strategy_id,
            decision_id=decision.decision_id if decision else None,
            target_exposure_id=decision.target_exposure_id if decision else None,
            source_intent_ids=list(
                decision.source_intent_ids if decision else intent.source_intent_ids
            ),
            risk_class=(decision.risk_class if decision else EconomicRiskClass.NOOP),
        )

    async def _resolve_ambiguous_order(
        self,
        intent: OrderIntent,
        prepared: Any,
        client_order_id: str,
        decision: Optional[ExecutionDecision] = None,
        *,
        defer_reconciliation: bool = False,
        pending_order: Optional[ExecutionOrder] = None,
    ) -> Optional[ExecutionOrder]:
        self.state = ConnectionState.RECONCILING
        recovered_order: Optional[ExecutionOrder] = None
        order_status_known = False
        fill_recovery_verified = True
        executed_quantity = Decimal("0")
        for attempt, delay in enumerate((0.0, 0.1, 0.25)):
            if delay:
                await asyncio.sleep(delay)
            try:
                status_response = await self.rest_client.request(
                    "GET",
                    self._order_path,
                    signed=True,
                    params={"symbol": prepared.symbol, "origClientOrderId": client_order_id},
                )
                recovered_order = self._order_from_response(
                    intent,
                    status_response,
                    prepared,
                    client_order_id,
                    decision,
                    allow_terminal_status=True,
                )
                await self.ledger.upsert_order(recovered_order)
                order_status_known = True
                try:
                    executed_quantity = Decimal(str(status_response.get("executedQty", "0")))
                except (InvalidOperation, TypeError, ValueError):
                    executed_quantity = Decimal("NaN")
                if not executed_quantity.is_finite() or executed_quantity < 0:
                    fill_recovery_verified = False
                    self.reconciliation.last_status = "UNKNOWN"
                    logger.error(
                        "Ambiguous order %s has invalid executed quantity",
                        client_order_id,
                    )
                    break
                if (
                    recovered_order is not None
                    and (
                        str(recovered_order.status).upper() in {"FILLED", "PARTIALLY_FILLED"}
                        or executed_quantity > 0
                    )
                ):
                    try:
                        await self.reconciliation._recover_order_fills(
                            recovered_order, status_response
                        )
                    except BinanceAuthenticationError:
                        self.invalidate_authentication()
                        if not (defer_reconciliation and order_status_known):
                            return None
                        fill_recovery_verified = False
                    except Exception as exc:
                        # A filled exchange order without canonical userTrades
                        # is not an execution success. Keep the adapter
                        # degraded even if a later position snapshot is flat.
                        fill_recovery_verified = False
                        self.reconciliation.last_status = "UNKNOWN"
                        logger.error(
                            "Ambiguous filled order %s has unverified fills: %s",
                            client_order_id,
                            exc,
                        )
                break
            except BinanceAuthenticationError:
                self.invalidate_authentication()
                if defer_reconciliation:
                    fill_recovery_verified = False
                    break
                return None
            except BinanceDefinitiveRejection as exc:
                if exc.code == -2013 and attempt < 2:
                    # A just-accepted order may not be visible to the query
                    # endpoint immediately.  Keep it quarantined and poll.
                    continue
                if exc.code == -2013:
                    logger.info("Ambiguous order %s confirmed absent on exchange", client_order_id)
                    order_status_known = True
                else:
                    logger.error("Ambiguous order status was rejected unexpectedly: %s", exc)
                break
            except Exception as exc:
                # Do not infer absence from a transport exception whose text
                # happens to contain "does not exist".
                logger.error("Ambiguous order status remains unknown: %s", exc)
                break

        deferred_order_known = bool(
            defer_reconciliation
            and recovered_order is not None
            and order_status_known
        )
        if defer_reconciliation:
            sync_result = "DEFERRED"
        else:
            try:
                sync_result = await self.reconciliation.reconcile()
            except Exception as exc:
                logger.error("Authoritative reconciliation after ambiguity failed: %s", exc)
                sync_result = "UNKNOWN"
        if deferred_order_known:
            self.state = (
                ConnectionState.RECONCILING
                if (
                    fill_recovery_verified
                    and self.private_stream_healthy
                    and self.authenticated
                )
                else ConnectionState.DEGRADED
            )
        elif (
            sync_result == "IN_SYNC"
            and self.private_stream_healthy
            and self.authenticated
            and order_status_known
            and fill_recovery_verified
        ):
            self.state = ConnectionState.READY
        else:
            self.state = ConnectionState.DEGRADED
        # The recovered record remains in the ledger for reconciliation, but
        # it is not reported as an executable success while the authoritative
        # post-mutation checks are degraded.
        if (
            defer_reconciliation
            and recovered_order is None
            and pending_order is not None
        ):
            # The POST may have filled even though every client-ID status read
            # failed. Return the durable pending identity to the protected
            # Testnet path so it attempts one ID-bound cancel and the
            # reduce-only emergency fallback. It must never be reported as a
            # confirmed order or trigger another entry submission.
            self.state = ConnectionState.DEGRADED
            return pending_order
        if self.state != ConnectionState.READY and not deferred_order_known:
            return None
        if recovered_order is None:
            # Even confirmed absence is not permission to submit another order
            # automatically after an ambiguous POST. Keep the adapter fenced
            # until an operator-driven reconciliation/re-approval.
            self.state = ConnectionState.DEGRADED
            return None
        terminal_without_execution = (
            str(recovered_order.status).upper()
            in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
            and executed_quantity == 0
        )
        if terminal_without_execution:
            self.state = ConnectionState.DEGRADED
            if defer_reconciliation:
                await self._close_testnet_protection_owner(
                    intent, recovered_order, "entry_terminal_without_fill"
                )
            return None
        return recovered_order

    async def _post_mutation_reconcile(
        self, order: ExecutionOrder, response: Dict[str, Any]
    ) -> bool:
        """Verify REST acknowledgement before exposing it as execution success."""
        self.reconciliation.last_status = "UNKNOWN"
        try:
            if str(order.status).upper() in {"FILLED", "PARTIALLY_FILLED"}:
                await self.reconciliation._recover_order_fills(order, response)
            sync_result = await self.reconciliation.reconcile()
        except BinanceAuthenticationError:
            self.invalidate_authentication()
            return False
        except Exception as exc:
            logger.error(
                "Post-mutation %s reconciliation failed for %s: %s",
                self.environment_label,
                order.client_order_id,
                exc,
            )
            self.reconciliation.last_status = "UNKNOWN"
            self.state = ConnectionState.DEGRADED
            return False

        verified = bool(
            sync_result == "IN_SYNC"
            and self.private_stream_healthy
            and self.authenticated
        )
        self.state = ConnectionState.READY if verified else ConnectionState.DEGRADED
        return verified

    async def execute_decision(
        self,
        decision: ExecutionDecision,
        *,
        authority: Optional[object] = None,
    ) -> List[ExecutionOrder]:
        return await self._execute_authorized_decision(
            decision, authority=authority, protected_testnet=False,
        )

    async def execute_protected_testnet_decision(
        self, decision: ExecutionDecision, *, authority: Optional[object] = None
    ) -> List[ExecutionOrder]:
        """Submit one market entry and verify post-fill Algo protection on Testnet."""
        self.last_testnet_protection = {"status": "BLOCKED", "reason": "preflight_not_passed"}
        if self.env != BinanceEnvironment.TESTNET or len(decision.orders) != 1:
            logger.error("Protected entry lifecycle is restricted to one Testnet order")
            return []
        intent = decision.orders[0]
        if (
            str(getattr(decision.risk_class, "value", decision.risk_class)).upper()
            not in {"NEW_RISK", "INCREASE_RISK"}
            or str(intent.symbol or "").strip().upper() != "ETHUSDC"
            or str(getattr(intent.order_type, "value", intent.order_type)).upper() != "MARKET"
            or intent.stop_loss_price is None
            or intent.take_profit_price is None
            or not str(intent.client_order_id or "").strip()
            or bool(getattr(intent, "reduce_only", False))
        ):
            logger.error("Protected Testnet entry requires one ETHUSDC market order, stable ID, stop and target")
            self.last_testnet_protection = {"status": "BLOCKED", "reason": "invalid_protected_entry"}
            return []
        if not callable(self.on_testnet_protection_update):
            logger.error("Protected Testnet entry requires durable PostgreSQL protection ownership")
            self.last_testnet_protection = {"status": "BLOCKED", "reason": "durable_protection_store_unavailable"}
            return []
        self.last_testnet_protection = {
            "status": "PENDING",
            "entry_client_order_id": intent.client_order_id,
        }
        return await self._execute_authorized_decision(
            decision, authority=authority, protected_testnet=True,
        )

    async def _execute_authorized_decision(
        self, decision: ExecutionDecision, *, authority: Optional[object],
        protected_testnet: bool,
    ) -> List[ExecutionOrder]:
        """Reject direct adapter mutation; only the bound Worker may submit."""
        if self.preflight_only:
            logger.error("Blocked order submission from a read-only preflight adapter")
            return []
        if not self._worker_authorized(authority):
            logger.error(
                "Blocked direct Binance adapter mutation for decision %s; use TradingWorkerApp",
                getattr(decision, "decision_id", "UNKNOWN"),
            )
            return []
        risk_class = str(
            getattr(decision.risk_class, "value", decision.risk_class)
        ).upper()
        if (
            self.env == BinanceEnvironment.MAINNET
            and self._is_local_live_pilot_bound()
            and risk_class in {"NEW_RISK", "INCREASE_RISK"}
            and local_live_pilot_readiness()["can_start"] is not True
        ):
            logger.error(
                "Blocked Local Live Research Pilot risk increase: runtime capability gate is not ready"
            )
            return []
        if (
            self.require_testnet_protection
            and self.env == BinanceEnvironment.TESTNET
            and risk_class in {"NEW_RISK", "INCREASE_RISK"}
            and not protected_testnet
        ):
            logger.error(
                "Blocked Testnet risk increase outside the durable post-fill protection lifecycle"
            )
            return []
        gate: Any = getattr(authority, "_evaluate_execution_gate", None)
        if not callable(gate):
            logger.error("Blocked Binance mutation because Worker gate is unavailable")
            return []
        allowed, reason = cast(Tuple[bool, str], gate(decision))
        if not allowed:
            logger.warning("Worker decision gate blocked adapter mutation: %s", reason)
            return []
        async with self._mutation_scope():
            # The first gate check may have happened while another mutation was
            # in flight. Re-evaluate after acquiring the single-flight lock so
            # a kill switch or degraded state cannot release a queued order.
            allowed, reason = cast(Tuple[bool, str], gate(decision))
            if not allowed:
                logger.warning(
                    "Worker decision gate blocked queued adapter mutation: %s", reason
                )
                return []
            return await self._execute_decision(
                decision, authority=authority,
                enforce_testnet_protection=protected_testnet,
            )

    async def _execute_decision(
        self,
        decision: ExecutionDecision,
        *,
        allow_emergency_fallback: bool = False,
        authority: Optional[object] = None,
        enforce_testnet_protection: bool = False,
        before_mutation: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> List[ExecutionOrder]:
        if self.preflight_only:
            logger.error("Blocked internal order submission from a read-only preflight adapter")
            return []
        if not self._worker_authorized(authority):
            logger.error(
                "Blocked internal Binance mutation outside the bound Trading Worker"
            )
            return []
        risk_class = str(
            getattr(decision.risk_class, "value", decision.risk_class)
        ).upper()
        if (
            self.env == BinanceEnvironment.MAINNET
            and self._is_local_live_pilot_bound()
            and risk_class in {"NEW_RISK", "INCREASE_RISK"}
            and local_live_pilot_readiness()["can_start"] is not True
        ):
            logger.error(
                "Blocked Local Live Research Pilot risk increase at the adapter send path"
            )
            return []
        if (
            self.require_testnet_protection
            and self.env == BinanceEnvironment.TESTNET
            and risk_class in {"NEW_RISK", "INCREASE_RISK"}
            and not enforce_testnet_protection
        ):
            logger.error(
                "Blocked internal Testnet risk increase outside the protected entry lifecycle"
            )
            return []
        if (
            not allow_emergency_fallback
            and (self.state != ConnectionState.READY or decision.action == "NOOP")
        ):
            return []
        if decision.action == "NOOP":
            return []
        if enforce_testnet_protection and (
            self.env != BinanceEnvironment.TESTNET
            or decision.risk_class not in {
                EconomicRiskClass.NEW_RISK, EconomicRiskClass.INCREASE_RISK,
            }
            or len(decision.orders) != 1
        ):
            logger.error("Protected entry lifecycle request failed its Testnet scope")
            return []

        enforce_local_mainnet_protection = bool(
            self.env == BinanceEnvironment.MAINNET
            and self._is_local_mainnet_runtime()
            and risk_class in {"NEW_RISK", "INCREASE_RISK"}
        )
        if enforce_local_mainnet_protection and len(decision.orders) != 1:
            logger.error("Local Mainnet protection lifecycle accepts one entry order at a time")
            return []

        executed_orders: List[ExecutionOrder] = []
        reserved_open_orders = 0
        reserved_notional = Decimal("0")
        for index, intent in enumerate(decision.orders):
            if not allow_emergency_fallback and getattr(
                authority, "kill_switch_active", False
            ):
                self.state = ConnectionState.DEGRADED
                logger.warning("Kill switch blocked remaining %s order mutations", environment_label(self.env))
                return executed_orders
            # The identity checked by the risk gate must be the identity sent
            # to Binance and recorded by the durable outbox. In particular,
            # Mainnet cannot validate a bracket against the strategy's
            # transient intent ID and then submit a different client ID.
            client_order_id = intent.client_order_id
            if self.env == BinanceEnvironment.MAINNET:
                client_order_id = self._generate_client_order_id(
                    str(decision.decision_id), str(intent.symbol).upper(), order_index=index
                )
                intent = intent.model_copy(update={"client_order_id": client_order_id})
            gate_result = await self.order_gate.check(
                intent,
                decision.risk_class,
                reserved_open_orders=reserved_open_orders,
                reserved_notional=reserved_notional,
                allow_emergency_fallback=allow_emergency_fallback,
            )
            if not gate_result.allowed or gate_result.prepared is None:
                logger.warning("Order blocked by final gate: %s", gate_result.reason)
                self.last_order_block = {
                    "stage": "ORDER_GATE",
                    "reason": " ".join(str(gate_result.reason).split())[:200],
                }
                continue
            prepared = gate_result.prepared
            if enforce_testnet_protection:
                try:
                    stop_trigger = Decimal(str(intent.stop_loss_price))
                    target_trigger = Decimal(str(intent.take_profit_price))
                    is_long = str(getattr(intent.side, "value", intent.side)).upper() == "BUY"
                    rules = self.symbol_rules.get(prepared.symbol)
                    triggers_valid = (
                        stop_trigger < prepared.estimated_price < target_trigger
                        if is_long
                        else target_trigger < prepared.estimated_price < stop_trigger
                    )
                    triggers_valid = bool(
                        triggers_valid and rules is not None
                        and rules.normalize_price(stop_trigger) == stop_trigger
                        and rules.normalize_price(target_trigger) == target_trigger
                    )
                except (InvalidOperation, TypeError, ValueError):
                    triggers_valid = False
                if not triggers_valid:
                    logger.error("Protected Testnet entry has invalid stop/target triggers")
                    return executed_orders
            if not client_order_id:
                client_order_id = self._generate_client_order_id(
                    str(decision.decision_id), prepared.symbol, order_index=index
                )
            # Mainnet retries/restarts must reuse the same exchange identity
            # for one worker decision. The risk governor's human-readable
            # intent id contains a runtime sequence, so it is not sufficient
            # as the exchange idempotency key on its own.
            params: Dict[str, Any] = {
                "symbol": prepared.symbol,
                "side": intent.side.value,
                "type": prepared.order_type,
                "quantity": str(prepared.quantity),
                "newClientOrderId": client_order_id,
            }
            if prepared.price is not None:
                params["price"] = str(prepared.price)
                time_in_force = getattr(intent.time_in_force, "value", intent.time_in_force)
                params["timeInForce"] = (
                    "GTX"
                    if intent.post_only or str(time_in_force).upper() == TimeInForce.POST_ONLY.value
                    else str(time_in_force).upper()
                )
            if self.capabilities.hedge_mode:
                params["positionSide"] = intent.position_side.value
            if intent.reduce_only and not self.capabilities.hedge_mode:
                params["reduceOnly"] = "true"

            planned_order: Optional[ExecutionOrder] = None
            local_mainnet_owner_record: Optional[Dict[str, Any]] = None
            submission_attempted = False
            try:
                planned_order = ExecutionOrder(
                    symbol=prepared.symbol,
                    side=intent.side,
                    quantity=prepared.quantity,
                    price=prepared.price or prepared.estimated_price,
                    order_type=prepared.order_type,
                    client_order_id=client_order_id,
                    basket_id=(
                        str(intent.basket_id).strip()
                        if enforce_local_mainnet_protection and intent.basket_id
                        else None
                    ),
                    status="PENDING",
                    timestamp=utc_now(),
                    market_type=getattr(intent, "market_type", MarketType.USDM_FUTURES),
                    position_side=getattr(intent, "position_side", PositionSide.BOTH),
                    reduce_only=bool(getattr(intent, "reduce_only", False)),
                    time_in_force=getattr(intent, "time_in_force", TimeInForce.GTC),
                    strategy_id=str(getattr(intent, "strategy_id", "portfolio")),
                    decision_id=getattr(decision, "decision_id", None),
                    target_exposure_id=getattr(decision, "target_exposure_id", None),
                    source_intent_ids=list(
                        getattr(decision, "source_intent_ids", None)
                        or getattr(intent, "source_intent_ids", None)
                        or []
                    ),
                    risk_class=decision.risk_class,
                )

                if enforce_local_mainnet_protection:
                    if (
                        not callable(self.on_local_mainnet_protection_update)
                        or not callable(self.on_local_mainnet_close_verified)
                    ):
                        logger.error(
                            "Local Mainnet entry blocked: durable protection or close-proof writer is unavailable"
                        )
                        self.state = ConnectionState.DEGRADED
                        continue
                    local_mainnet_owner_record = self._local_mainnet_protection_record(
                        intent, planned_order
                    )
                    if not await self._persist_local_mainnet_protection(
                        local_mainnet_owner_record
                    ):
                        logger.error(
                            "Local Mainnet entry blocked before submission: protection owner was not read back"
                        )
                        self.state = ConnectionState.DEGRADED
                        continue

                if enforce_testnet_protection:
                    protection_writer = self.on_testnet_protection_update
                    if not callable(protection_writer):
                        logger.error("Testnet entry blocked: durable protection writer is unavailable")
                        self.state = ConnectionState.DEGRADED
                        continue
                    try:
                        durable_protection = await protection_writer(
                            self._testnet_protection_record(intent, planned_order)
                        )
                    except Exception as exc:
                        logger.error(
                            "Testnet entry ownership write failed: %s", type(exc).__name__
                        )
                        durable_protection = False
                    if not durable_protection:
                        logger.error("Testnet entry blocked before exchange submission: protection ownership was not read back")
                        self.state = ConnectionState.DEGRADED
                        continue

                durable_barrier = self.before_order_submission
                is_risk_increasing = decision.risk_class in {
                    EconomicRiskClass.NEW_RISK,
                    EconomicRiskClass.INCREASE_RISK,
                }
                if durable_barrier is None:
                    if self.env == BinanceEnvironment.MAINNET and is_risk_increasing:
                        raise LeaseLostError(
                            "Mainnet risk-increasing order requires a durable outbox barrier"
                        )
                else:
                    durable = await durable_barrier(planned_order)
                    if not durable and is_risk_increasing:
                        logger.error(
                            "Risk-increasing order %s blocked because the durable outbox was not acknowledged",
                            client_order_id,
                        )
                        self.state = ConnectionState.DEGRADED
                        if enforce_testnet_protection:
                            await self._close_testnet_protection_owner(
                                intent, planned_order,
                                "durable_outbox_unavailable_before_submit",
                            )
                        if enforce_local_mainnet_protection:
                            await self._close_local_mainnet_pending_owner(
                                intent, planned_order,
                                "durable_outbox_unavailable_before_submit",
                            )
                        continue
                    if not durable:
                        logger.warning(
                            "Durable outbox unavailable for risk-reducing order %s; emergency path remains allowed",
                            client_order_id,
                        )

                # Keep the pending observation in the local ledger as well. If
                # the response is ambiguous, reconciliation has a lineage record
                # even when the exchange private stream races the REST response.
                await self.ledger.upsert_order(planned_order)
                # Keep this directly adjacent to the mutating request.  A
                # lease checked at arm time or at the first decision gate may
                # have been fenced while this order was being prepared.

                async def before_order_send() -> None:
                    nonlocal submission_attempted
                    if (
                        is_risk_increasing
                        and self.env == BinanceEnvironment.MAINNET
                    ):
                        await self._final_risk_increase_fence(
                            decision,
                            intent,
                            prepared,
                            client_order_id=client_order_id,
                            reserved_open_orders=reserved_open_orders,
                            reserved_notional=reserved_notional,
                            allow_emergency_fallback=allow_emergency_fallback,
                        )
                    else:
                        await self._assert_execution_lease(decision.risk_class)
                    if enforce_testnet_protection:
                        await self._assert_testnet_entry_position_flat(intent)
                    if before_mutation is not None:
                        # An explicitly fenced mutation must revalidate a live
                        # lease even when its risk class is EMERGENCY; the
                        # ordinary close-only path may otherwise bypass it.
                        await self._assert_execution_lease(EconomicRiskClass.NEW_RISK)
                        await before_mutation()
                    # The REST client invokes this after throttle waits and
                    # immediately before its HTTP request. A pre-send fence
                    # failure therefore remains definitively not-submitted.
                    if enforce_local_mainnet_protection:
                        # Sending precedes any possible first fill. Keep this
                        # conservative bound across delayed/ambiguous responses.
                        self._local_mainnet_entry_deadlines[client_order_id] = time.monotonic() + 5.0
                    self.order_submission_attempts += 1
                    submission_attempted = True

                response = await self.rest_client.request(
                    "POST",
                    self._order_path,
                    signed=True,
                    params=params,
                    before_mutation=before_order_send,
                )
                if not isinstance(response, dict):
                    raise BinanceTransportAmbiguity("Binance order response is invalid")
                order = self._order_from_response(
                    intent, response, prepared, client_order_id, decision,
                    allow_terminal_status=(
                        enforce_testnet_protection or enforce_local_mainnet_protection
                    ),
                )
                await self.ledger.upsert_order(order)
                if enforce_local_mainnet_protection and not await self._protect_local_mainnet_entry(
                    intent, order, response, authority=authority,
                ):
                    await self._notify_order_submission_result(order, "AMBIGUOUS")
                    self.state = ConnectionState.DEGRADED
                    return executed_orders
                if enforce_testnet_protection and not await self._protect_testnet_entry(
                    intent, order, response, authority=authority,
                ):
                    await self._notify_order_submission_result(order, "AMBIGUOUS")
                    self.state = ConnectionState.DEGRADED
                    return executed_orders
                if not await self._post_mutation_reconcile(order, response):
                    await self._notify_order_submission_result(order, "AMBIGUOUS")
                    logger.error(
                        "monitor_event=ambiguous_order environment=%s symbol=%s",
                        environment_label(self.env),
                        order.symbol,
                    )
                    logger.error(
                        "%s order %s acknowledged but not fully verified; keeping execution degraded",
                        environment_label(self.env),
                        client_order_id,
                    )
                    continue
                await self._notify_order_submission_result(order, "CONFIRMED")
                executed_orders.append(order)
                if order.status in ("NEW", "PARTIALLY_FILLED"):
                    reserved_open_orders += 1
                    reserved_notional += prepared.notional
            except BinanceAuthenticationError as exc:
                self.invalidate_authentication()
                if planned_order is not None:
                    await self._notify_order_submission_result(
                        planned_order, "AMBIGUOUS" if submission_attempted else "REJECTED"
                    )
                logger.error("%s authentication failed while submitting %s: %s", environment_label(self.env), client_order_id, exc)
            except (BinanceRateLimitError, BinanceTimestampError) as exc:
                if planned_order is not None:
                    await self._notify_order_submission_result(
                        planned_order, "AMBIGUOUS" if submission_attempted else "REJECTED"
                    )
                logger.error("%s mutable request was not submitted: %s", environment_label(self.env), exc)
                self.state = ConnectionState.DEGRADED
            except BinanceDefinitiveRejection as exc:
                logger.warning("%s order rejected definitively: %s", environment_label(self.env), exc)
                rejected_order = ExecutionOrder(
                    symbol=prepared.symbol,
                    side=intent.side,
                    quantity=prepared.quantity,
                    price=prepared.price or prepared.estimated_price,
                    order_type=prepared.order_type,
                    client_order_id=client_order_id,
                    status="REJECTED",
                    timestamp=utc_now(),
                    strategy_id=intent.strategy_id,
                    decision_id=decision.decision_id,
                    target_exposure_id=decision.target_exposure_id,
                    source_intent_ids=list(
                        decision.source_intent_ids or intent.source_intent_ids
                    ),
                    risk_class=decision.risk_class,
                )
                await self.ledger.upsert_order(rejected_order)
                await self._notify_order_submission_result(rejected_order, "REJECTED")
                if (
                    enforce_local_mainnet_protection
                    and planned_order is not None
                    and local_mainnet_owner_record is not None
                ):
                    await self._close_local_mainnet_pending_owner(
                        intent, planned_order, "entry_rejected_definitively"
                    )
                if enforce_testnet_protection and not submission_attempted:
                    await self._close_testnet_protection_owner(
                        intent, planned_order, "entry_rejected_definitively"
                    )
            except LeaseLostError as exc:
                if planned_order is not None:
                    await self._notify_order_submission_result(planned_order, "REJECTED")
                    if (
                        enforce_local_mainnet_protection
                        and local_mainnet_owner_record is not None
                    ):
                        if submission_attempted:
                            unknown = dict(local_mainnet_owner_record)
                            unknown.update(
                                state="UNKNOWN",
                                state_reason="entry_submission_fenced_after_attempt",
                            )
                            await self._persist_local_mainnet_protection(unknown)
                        else:
                            await self._close_local_mainnet_pending_owner(
                                intent, planned_order, "lease_lost_before_entry_submit"
                            )
                    if enforce_testnet_protection and not submission_attempted:
                        await self._close_testnet_protection_owner(
                            intent, planned_order, "lease_lost_before_entry_submit"
                        )
                self.state = ConnectionState.DEGRADED
                self.last_order_block = {
                    "stage": "FINAL_FENCE",
                    "reason": " ".join(str(exc).split())[:200],
                }
                logger.error("Order submission fenced before request: %s", exc)
            except (BinanceTransportAmbiguity, BinanceAPIError) as exc:
                logger.error("%s order response is ambiguous: %s", environment_label(self.env), exc)
                logger.error(
                    "monitor_event=ambiguous_order environment=%s symbol=%s",
                    environment_label(self.env),
                    prepared.symbol,
                )
                if planned_order is not None:
                    await self._notify_order_submission_result(planned_order, "AMBIGUOUS")
                recovered = await self._resolve_ambiguous_order(
                    intent, prepared, client_order_id, decision,
                    defer_reconciliation=(
                        enforce_testnet_protection or enforce_local_mainnet_protection
                    ),
                    pending_order=planned_order,
                )
                if recovered is not None:
                    if enforce_local_mainnet_protection:
                        recovered_status: Dict[str, Any] = {}
                        try:
                            candidate = await self.query_order(
                                prepared.symbol, client_order_id
                            )
                            if isinstance(candidate, dict):
                                recovered_status = candidate
                        except Exception as status_exc:
                            logger.error(
                                "Recovered Local Mainnet entry status is unresolved: %s",
                                type(status_exc).__name__,
                            )
                        if not await self._protect_local_mainnet_entry(
                            intent, recovered, recovered_status, authority=authority,
                        ):
                            await self._notify_order_submission_result(
                                recovered, "AMBIGUOUS"
                            )
                            self.state = ConnectionState.DEGRADED
                            return executed_orders
                        if not await self._post_mutation_reconcile(
                            recovered, recovered_status,
                        ):
                            await self._notify_order_submission_result(
                                recovered, "AMBIGUOUS"
                            )
                            return executed_orders
                    if enforce_testnet_protection:
                        # Reconciliation is deferred until durable Algo owners
                        # move from PENDING to PROTECTED. Resolve the exact
                        # order again by its stable client ID, then run the
                        # same post-fill bracket/flatten lifecycle as a normal
                        # successful POST response.
                        recovered_status: Dict[str, Any] = {}
                        try:
                            candidate = await self.query_order(
                                prepared.symbol, client_order_id
                            )
                            if isinstance(candidate, dict):
                                validated = self._order_from_response(
                                    intent, candidate, prepared, client_order_id,
                                    decision, allow_terminal_status=True,
                                )
                                if (
                                    validated.exchange_order_id
                                    == recovered.exchange_order_id
                                    and validated.client_order_id == recovered.client_order_id
                                ):
                                    recovered_status = candidate
                        except Exception as status_exc:
                            logger.error(
                                "Recovered Testnet entry status is not authoritative: %s",
                                type(status_exc).__name__,
                            )
                        if not await self._protect_testnet_entry(
                            intent, recovered, recovered_status, authority=authority,
                        ):
                            await self._notify_order_submission_result(
                                recovered, "AMBIGUOUS"
                            )
                            self.state = ConnectionState.DEGRADED
                            return executed_orders
                        if not await self._post_mutation_reconcile(
                            recovered, recovered_status,
                        ):
                            await self._notify_order_submission_result(
                                recovered, "AMBIGUOUS"
                            )
                            return executed_orders
                    await self._notify_order_submission_result(recovered, "CONFIRMED")
                    executed_orders.append(recovered)
                elif (
                    enforce_local_mainnet_protection
                    and local_mainnet_owner_record is not None
                ):
                    unknown = dict(local_mainnet_owner_record)
                    unknown.update(
                        state="UNKNOWN",
                        state_reason="entry_submission_or_fill_state_unknown",
                    )
                    await self._persist_local_mainnet_protection(unknown)
                    await self._degrade_local_mainnet_protection(self._worker_authority)
            except Exception as exc:
                if planned_order is not None:
                    if (
                        enforce_local_mainnet_protection
                        and local_mainnet_owner_record is not None
                    ):
                        if submission_attempted:
                            unknown = dict(local_mainnet_owner_record)
                            unknown.update(
                                state="UNKNOWN",
                                state_reason="entry_submission_or_fill_state_unknown",
                            )
                            await self._persist_local_mainnet_protection(unknown)
                            await self._degrade_local_mainnet_protection(
                                self._worker_authority
                            )
                        else:
                            await self._close_local_mainnet_pending_owner(
                                intent, planned_order, "entry_failed_before_submit"
                            )
                    if enforce_testnet_protection and not submission_attempted:
                        await self._close_testnet_protection_owner(
                            intent, planned_order, "entry_failed_before_submit"
                        )
                    await self._notify_order_submission_result(
                        planned_order, "AMBIGUOUS" if submission_attempted else "REJECTED"
                    )
                logger.error("Unexpected %s order execution failure: %s", environment_label(self.env), exc)
                self.state = ConnectionState.DEGRADED

        return executed_orders

    async def query_order(self, symbol: str, client_order_id: str) -> Optional[Dict[str, Any]]:
        try:
            return await self.rest_client.request(
                "GET",
                self._order_path,
                signed=True,
                params={"symbol": symbol.upper(), "origClientOrderId": client_order_id},
            )
        except BinanceDefinitiveRejection as exc:
            if exc.code == -2013:
                return None
            raise

    async def read_back_algo_protection(
        self, intent: ProtectionIntent, *, entry_client_order_id: str
    ) -> ProtectionResult:
        """Read authoritative fill, position and Algo snapshots without placing orders.

        This is evidence collection, not an order-entry gate. A missing or
        contradictory source remains AMBIGUOUS; callers must never equate a
        successful REST response with exchange-confirmed protection.
        """
        if not entry_client_order_id or intent.symbol != "ETHUSDC":
            return ProtectionResult(False, "AMBIGUOUS", ("invalid_entry_identity",))
        try:
            fills = await self.ledger.get_fills()
            entry_fills = [
                fill for fill in fills
                if fill.client_order_id == entry_client_order_id
                and fill.symbol == intent.symbol
                and str(getattr(fill.side, "value", fill.side)).upper() == intent.entry_side
                and str(getattr(fill.position_side, "value", fill.position_side)).upper()
                == intent.position_side
            ]
            filled_qty = sum(
                (Decimal(str(fill.quantity)) for fill in entry_fills),
                Decimal("0"),
            )
            average_entry_price = (
                sum((Decimal(str(fill.quantity)) * Decimal(str(fill.price)) for fill in entry_fills), Decimal("0"))
                / filled_qty
                if filled_qty > 0 else None
            )
            entry_order = await self.ledger.get_order_by_client_id(entry_client_order_id)
            if entry_order is None:
                raise ValueError("entry order is missing from durable ledger")
            entry_terminal = str(entry_order.status).upper() in {
                "FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED",
            }
            positions = await self.rest_client.request(
                "GET", self._position_risk_path, signed=True,
            )
            position_observed_ms = int(time.time() * 1000)
            if not isinstance(positions, list):
                raise ValueError("position response is not a list")
            matching = [
                row for row in positions
                if isinstance(row, dict)
                and row.get("symbol") == intent.symbol
                and row.get("positionSide", "BOTH") == intent.position_side
            ]
            if len(matching) != 1:
                raise ValueError("position identity is not unique")
            position_qty = Decimal(str(matching[0]["positionAmt"]))
            stop_query = await self.rest_client.request(
                "GET", self._algo_order_query_path, signed=True,
                params={"symbol": intent.symbol, "algoId": intent.stop_algo_id},
            )
            take_query = await self.rest_client.request(
                "GET", self._algo_order_query_path, signed=True,
                params={"symbol": intent.symbol, "algoId": intent.take_profit_algo_id},
            )
            query_observed_ms = int(time.time() * 1000)
            open_algos = await self.rest_client.request(
                "GET", self._open_algo_orders_path, signed=True,
                params={"symbol": intent.symbol, "algoType": "CONDITIONAL"},
            )
            open_observed_ms = int(time.time() * 1000)
            return verify_protection(
                intent, position_qty=position_qty, filled_qty=filled_qty,
                stop_query=stop_query, take_profit_query=take_query,
                open_algos=open_algos, average_entry_price=average_entry_price,
                entry_terminal=entry_terminal, now_ms=int(time.time() * 1000),
                query_observed_ms=query_observed_ms,
                open_observed_ms=open_observed_ms,
                position_observed_ms=position_observed_ms,
            )
        except Exception as exc:
            logger.error("Algo protection read-back unavailable: %s", type(exc).__name__)
            return ProtectionResult(False, "AMBIGUOUS", ("exchange_read_back_failed",))

    async def verify_local_mainnet_protection(
        self, intent: OrderIntent, verification_context: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Verify the durable Mainnet owner against fills and signed Algo snapshots.

        This performs GET-only read-back. A planned bracket, a PENDING owner,
        or merely having this method present is never reported as protected.
        """
        entry_client_order_id = str(getattr(intent, "client_order_id", "") or "").strip()
        if (
            not entry_client_order_id
            or str(getattr(intent, "symbol", "")).upper() != MAINNET_RISK_POLICY.symbol
            or not str(getattr(intent, "basket_id", "") or "").strip()
        ):
            return None
        try:
            runtime = await self._local_mainnet_runtime_evidence(
                require_unused_staged_launch=False
            )
            if runtime is None:
                return None
            session = runtime["session"]
            if session.get("policy") == "LIVE_RESEARCH_PILOT":
                if (
                    session.get("state") != "ACTIVE"
                    or session.get("pilot_status") != "ACTIVE"
                    or str(session.get("first_order_client_order_id", "")) != entry_client_order_id
                ):
                    return None
            elif (
                session.get("policy") != "STAGED_FIRST_ORDER"
                or session.get("state") != "PAUSED_NEW_RISK"
                or str(session.get("first_order_client_order_id", "")) != entry_client_order_id
            ):
                return None
            risk_evidence = self._last_local_mainnet_risk_evidence
            basket_id = str(getattr(intent, "basket_id", "") or "").strip()
            if (
                not isinstance(risk_evidence, dict)
                or risk_evidence.get("launch_id") != runtime["launch_id"]
                or risk_evidence.get("client_order_id") != entry_client_order_id
                or risk_evidence.get("basket_id") != basket_id
                or risk_evidence.get("policy_sha256") != MAINNET_RISK_POLICY_SHA256
                or not isinstance(risk_evidence.get("observed_monotonic"), (int, float))
                or not 0 <= time.monotonic() - risk_evidence["observed_monotonic"] <= 5
            ):
                return None
            owners = runtime["protections"]
            owner = await owners.get_protection(
                "binance_mainnet", MAINNET_RISK_POLICY.symbol, entry_client_order_id
            )
            entry_order = await self.ledger.get_order_by_client_id(entry_client_order_id)
            if owner is None:
                # Before entry, absence is expected and simply blocks the
                # pre-entry gate. Once an entry exists, missing ownership is
                # an unresolved live exposure and must degrade the runtime.
                if entry_order is not None:
                    await self._degrade_local_mainnet_protection(runtime["worker"])
                return None
            if not isinstance(owner, dict) or entry_order is None:
                await self._degrade_local_mainnet_protection(runtime["worker"])
                return None
            required_owner = {
                "environment": "MAINNET",
                "venue": "binance_mainnet",
                "symbol": MAINNET_RISK_POLICY.symbol,
                "entry_client_order_id": entry_client_order_id,
                "basket_id": basket_id,
                "mainnet_launch_id": runtime["launch_id"],
                "state": "PROTECTED",
            }
            if any(owner.get(key) != value for key, value in required_owner.items()):
                await self._degrade_local_mainnet_protection(runtime["worker"])
                return None
            side = str(self._enum_value(intent.side)).upper()
            position_side = str(self._enum_value(intent.position_side)).upper()
            if (
                str(self._enum_value(entry_order.side)).upper() != side
                or str(self._enum_value(entry_order.position_side)).upper() != position_side
                or str(entry_order.symbol).upper() != MAINNET_RISK_POLICY.symbol
                or str(getattr(entry_order, "basket_id", "") or "") != basket_id
                or Decimal(str(owner.get("requested_quantity")))
                != self._decimal_value(intent.quantity, positive=True)
                or str(owner.get("entry_side", "")).upper() != side
                or str(owner.get("position_side", "")).upper() != position_side
                or Decimal(str(owner.get("stop_trigger_price")))
                != self._decimal_value(intent.stop_loss_price, positive=True)
                or Decimal(str(owner.get("take_profit_trigger_price")))
                != self._decimal_value(intent.take_profit_price, positive=True)
            ):
                await self._degrade_local_mainnet_protection(runtime["worker"])
                return None

            fills = [
                fill
                for fill in await self.ledger.get_fills()
                if fill.client_order_id == entry_client_order_id
                and str(fill.symbol).upper() == MAINNET_RISK_POLICY.symbol
            ]
            if not fills:
                return None
            filled_quantity = sum(
                (self._decimal_value(fill.quantity, positive=True) for fill in fills),
                Decimal("0"),
            )
            weighted_entry = sum(
                (
                    self._decimal_value(fill.quantity, positive=True)
                    * self._decimal_value(fill.price, positive=True)
                    for fill in fills
                ),
                Decimal("0"),
            ) / filled_quantity
            if (
                Decimal(str(owner.get("filled_quantity"))) != filled_quantity
                or Decimal(str(owner.get("entry_average_price"))) != weighted_entry
                or str(entry_order.status).upper()
                not in {"FILLED", "CANCELED", "CANCELLED", "EXPIRED"}
            ):
                await self._degrade_local_mainnet_protection(runtime["worker"])
                return None

            stop_id = owner.get("stop_algo_id")
            target_id = owner.get("take_profit_algo_id")
            if (
                stop_id in (None, "")
                or target_id in (None, "")
                or owner.get("protection_verified_at") is None
            ):
                await self._degrade_local_mainnet_protection(runtime["worker"])
                return None
            stop_client_id = str(owner.get("stop_client_algo_id", "")).strip()
            target_client_id = str(owner.get("take_profit_client_algo_id", "")).strip()
            bracket = ProtectionIntent(
                symbol=MAINNET_RISK_POLICY.symbol,
                entry_side=side,
                position_side=position_side,
                entry_qty=self._decimal_value(owner.get("filled_quantity"), positive=True),
                stop_trigger=self._decimal_value(owner.get("stop_trigger_price"), positive=True),
                take_profit_trigger=self._decimal_value(
                    owner.get("take_profit_trigger_price"), positive=True
                ),
                stop_algo_id=int(stop_id),
                stop_client_algo_id=stop_client_id,
                take_profit_algo_id=int(target_id),
                take_profit_client_algo_id=target_client_id,
            )
            result = await self.read_back_algo_protection(
                bracket, entry_client_order_id=entry_client_order_id
            )
            if not result.protected or not isinstance(result.evidence, dict):
                if filled_quantity > 0:
                    await self._degrade_local_mainnet_protection(runtime["worker"])
                return None

            entry_price = self._decimal_value(
                verification_context.get("validated_entry_price"), positive=True
            )
            quantity = self._decimal_value(
                verification_context.get("validated_quantity"), positive=True
            )
            requested_quantity = self._decimal_value(
                owner.get("requested_quantity"), positive=True
            )
            if (
                entry_price != weighted_entry
                or quantity != requested_quantity
                or filled_quantity > requested_quantity
            ):
                await self._degrade_local_mainnet_protection(runtime["worker"])
                return None
            evidence = dict(result.evidence)
            now = utc_now()
            output = {
                "verified": True,
                "verified_at": now,
                "runtime_target": "LOCAL",
                "run_id": runtime["launch_id"],
                "risk_policy_sha256": MAINNET_RISK_POLICY_SHA256,
                "basket_id": basket_id,
                "bracket_id": basket_id,
                "entry_client_order_id": entry_client_order_id,
                "symbol": MAINNET_RISK_POLICY.symbol,
                "entry_price": weighted_entry,
                "entry_quantity": filled_quantity,
                "entry_notional_usdc": weighted_entry * filled_quantity,
                **evidence,
            }
            self._last_local_mainnet_protection_evidence = {
                "launch_id": runtime["launch_id"],
                "client_order_id": entry_client_order_id,
                "basket_id": basket_id,
                "policy_sha256": MAINNET_RISK_POLICY_SHA256,
                "observed_monotonic": time.monotonic(),
            }
            return output
        except Exception as exc:
            logger.warning("Local Mainnet protection verification failed: %s", type(exc).__name__)
            if self.reconciliation.last_status == "IN_SYNC":
                # A malformed/missing owner or contradictory signed read-back
                # is not retryable evidence.  Keep new risk fenced until a
                # separate reconciliation establishes the account state.
                await self._degrade_local_mainnet_protection(self._worker_authority)
            return None

    async def _degrade_local_mainnet_protection(self, worker: Any) -> None:
        self.state = ConnectionState.DEGRADED
        self.reconciliation.last_status = "UNKNOWN"
        if worker is not None:
            try:
                worker.pause_new_risk = True
                refresh = getattr(worker, "_refresh_engine_state", None)
                if callable(refresh):
                    refresh()
            except Exception:
                pass

    async def _query_algo_order(self, *, symbol: str, client_algo_id: str) -> Optional[Dict[str, Any]]:
        try:
            result = await self.rest_client.request(
                "GET", self._algo_order_query_path, signed=True,
                params={"symbol": symbol, "clientAlgoId": client_algo_id},
            )
        except BinanceDefinitiveRejection as exc:
            if exc.code == -2013:
                return None
            raise
        return result if isinstance(result, dict) else None

    @staticmethod
    def _fill_event_time_ms(fill: Any) -> Optional[int]:
        value = getattr(fill, "event_time", None)
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return None
            return int(value.timestamp() * 1000)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @classmethod
    def _extract_fill_event_times(cls, fills: Iterable[Any]) -> List[int]:
        times: List[int] = []
        for fill in fills:
            t = cls._fill_event_time_ms(fill)
            if t is not None:
                times.append(t)
        return times

    @staticmethod
    def _testnet_protection_client_ids(entry_client_order_id: str) -> tuple[str, str]:
        digest = hashlib.sha256(entry_client_order_id.encode("utf-8")).hexdigest()[:16]
        return f"BAI-SL-{digest}", f"BAI-TP-{digest}"

    @classmethod
    def _testnet_protection_record(
        cls,
        intent: OrderIntent,
        order: ExecutionOrder,
        **updates: Any,
    ) -> Dict[str, Any]:
        stop_client_id, target_client_id = cls._testnet_protection_client_ids(
            order.client_order_id
        )
        record: Dict[str, Any] = {
            "environment": "TESTNET",
            "venue": "binance_testnet",
            "symbol": order.symbol.upper(),
            "entry_client_order_id": order.client_order_id,
            "basket_id": str(intent.basket_id or "").strip(),
            "entry_side": str(getattr(intent.side, "value", intent.side)).upper(),
            "position_side": str(
                getattr(intent.position_side, "value", intent.position_side)
            ).upper(),
            "requested_quantity": order.quantity,
            "filled_quantity": Decimal("0"),
            "entry_average_price": None,
            "stop_trigger_price": Decimal(str(intent.stop_loss_price)),
            "take_profit_trigger_price": Decimal(str(intent.take_profit_price)),
            "stop_client_algo_id": stop_client_id,
            "take_profit_client_algo_id": target_client_id,
            "state": "PENDING",
        }
        record.update(updates)
        return record

    @staticmethod
    def _is_local_mainnet_runtime() -> bool:
        return bool(
            os.getenv("LOCAL_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}
            and os.getenv("LOCAL_RUNTIME_TARGET", "").strip().upper() == "LOCAL"
        )

    def _is_local_live_pilot_bound(self) -> bool:
        worker = self._worker_authority
        session = getattr(worker, "_mainnet_launch_session", None)
        return bool(
            str(os.getenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "")).strip()
            or (isinstance(session, dict) and session.get("policy") == "LIVE_RESEARCH_PILOT")
        )

    def _local_mainnet_protection_record(
        self, intent: OrderIntent, order: ExecutionOrder, **updates: Any
    ) -> Dict[str, Any]:
        stop_client_id, target_client_id = self._testnet_protection_client_ids(
            order.client_order_id
        )
        worker = self._worker_authority
        record: Dict[str, Any] = {
            "environment": "MAINNET",
            "venue": "binance_mainnet",
            "symbol": order.symbol.upper(),
            "entry_client_order_id": order.client_order_id,
            "basket_id": str(order.basket_id or getattr(intent, "basket_id", "") or "").strip(),
            "mainnet_launch_id": str(getattr(worker, "_mainnet_launch_id", "") or "").strip() or None,
            "entry_side": str(getattr(intent.side, "value", intent.side)).upper(),
            "position_side": str(
                getattr(intent.position_side, "value", intent.position_side)
            ).upper(),
            "requested_quantity": order.quantity,
            "filled_quantity": Decimal("0"),
            "entry_average_price": None,
            "stop_trigger_price": Decimal(str(intent.stop_loss_price)),
            "take_profit_trigger_price": Decimal(str(intent.take_profit_price)),
            "stop_client_algo_id": stop_client_id,
            "take_profit_client_algo_id": target_client_id,
            "management_mode": getattr(intent, "management_mode", None),
            "state": "PENDING",
        }
        record.update(updates)
        return record

    @staticmethod
    def _local_close_decision_id(entry_client_order_id: str) -> str:
        digest = hashlib.sha256(entry_client_order_id.encode("utf-8")).hexdigest()[:24]
        return f"LOCAL-PROTECTION-CLOSE-{digest}"

    @staticmethod
    def _local_close_id_from_reason(reason: Any) -> Optional[str]:
        prefix = "local_close_client_order_id="
        for part in str(reason or "").split(";"):
            if part.startswith(prefix):
                candidate = part[len(prefix):].strip()
                return candidate or None
        return None

    @staticmethod
    def _local_close_submission_state(reason: Any) -> Optional[str]:
        prefix = "close_submission="
        for part in str(reason or "").split(";"):
            if part.startswith(prefix):
                value = part[len(prefix):].strip().upper()
                return value or None
        return None

    @staticmethod
    def _local_recovery_reason(record: Mapping[str, Any], **updates: Any) -> str:
        """Keep mutation-attempt markers ahead of diagnostic text in the 256-char field."""
        parts = {}
        for part in str(record.get("state_reason") or "").split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                parts[key] = value
        if not parts and set(updates) == {"cause"}:
            return str(updates["cause"])[:256]
        parts.update({key: str(value) for key, value in updates.items()})
        priority = ("local_close_client_order_id", "close_submission", "algo_cancel", "entry_cancel")
        ordered = [key for key in priority if key in parts]
        ordered.extend(key for key in parts if key not in priority)
        return ";".join(f"{key}={parts[key]}" for key in ordered)[:256]

    async def _claim_local_mainnet_close_submission(
        self, protections: Any, record: Dict[str, Any], *,
        close_client_order_id: str,
    ) -> Optional[Mapping[str, Any]]:
        """Only a True fenced attempt acknowledgement permits close mutation."""
        reserve = getattr(protections, "claim_local_emergency_close", None)
        mark_attempted = getattr(protections, "mark_local_emergency_close_attempted", None)
        loader = getattr(protections, "get_protection", None)
        if not (callable(reserve) and callable(mark_attempted) and callable(loader)):
            return None
        claimant_id = getattr(self, "_local_close_claimant_id", None)
        if claimant_id is None:
            claimant_id = self._local_close_claimant_id = uuid4().hex
        reservation = await cast(Any, reserve(
            record["symbol"], record["entry_client_order_id"], close_client_order_id,
            claimant_id=claimant_id,
        ))
        if (
            not isinstance(reservation, Mapping)
            or reservation.get("claimed") is not True
            or reservation.get("close_client_order_id") != close_client_order_id
            or type(reservation.get("fencing_token")) is not int
            or reservation["fencing_token"] < 1
        ):
            return None
        if await cast(Any, mark_attempted(
            record["symbol"], record["entry_client_order_id"], close_client_order_id,
            claimant_id=claimant_id, fencing_token=reservation["fencing_token"],
        )) is not True:
            return None
        stored = await cast(Any, loader("binance_mainnet", record["symbol"], record["entry_client_order_id"]))
        if not isinstance(stored, Mapping) or any(
            stored.get(key) != record.get(key) for key in (
                "environment", "venue", "symbol", "entry_client_order_id", "basket_id",
                "mainnet_launch_id", "filled_quantity", "entry_side", "position_side",
            )
        ) or stored.get("state") != "CLOSE_PENDING" or (
            self._local_close_id_from_reason(stored.get("state_reason")) != close_client_order_id
            or self._local_close_submission_state(stored.get("state_reason")) != "ATTEMPTED"
        ):
            return None
        # The fenced claim is now permanently consumed. Acknowledge ambiguity
        # markers before cancellation; failure leaves the whole flow query-only.
        if not await self._persist_local_mainnet_protection(record):
            return None
        return record

    async def _owned_algos_are_absent(self, record: Dict[str, Any]) -> bool:
        """Read back owned protections without repeating an ambiguous cancel."""
        symbol = str(record.get("symbol") or "").upper()
        client_ids = {
            str(record.get("stop_client_algo_id") or "").strip(),
            str(record.get("take_profit_client_algo_id") or "").strip(),
        }
        if not symbol or "" in client_ids or len(client_ids) != 2:
            return False
        try:
            open_algos = await self.rest_client.request(
                "GET",
                self._open_algo_orders_path,
                signed=True,
                params={"symbol": symbol, "algoType": "CONDITIONAL"},
            )
        except Exception:
            return False
        return bool(
            isinstance(open_algos, list)
            and not any(
                isinstance(row, dict)
                and str(row.get("clientAlgoId") or "") in client_ids
                for row in open_algos
            )
        )

    async def _persist_local_mainnet_protection(
        self, record: Dict[str, Any]
    ) -> bool:
        writer = self.on_local_mainnet_protection_update
        if not self._is_local_mainnet_runtime() or not callable(writer):
            return False
        try:
            return bool(await writer(record))
        except Exception as exc:
            logger.error(
                "Local Mainnet protection owner write failed: %s", type(exc).__name__
            )
            return False

    async def _close_local_mainnet_pending_owner(
        self, intent: OrderIntent, order: ExecutionOrder, reason: str
    ) -> bool:
        """Close owner only when entry submission is known not to have occurred."""
        record = self._local_mainnet_protection_record(intent, order)
        record.update(state="CLOSED", state_reason=reason[:256], closed_at=utc_now())
        stored = await self._persist_local_mainnet_protection(record)
        if not stored:
            await self._degrade_local_mainnet_protection(self._worker_authority)
        return stored

    async def _submit_local_mainnet_protection_algo(
        self,
        *,
        symbol: str,
        side: str,
        position_side: str,
        order_type: str,
        trigger_price: Decimal,
        quantity: Decimal,
        client_algo_id: str,
        authority: Any,
        deadline: float,
    ) -> Optional[Dict[str, Any]]:
        """Place one fill-sized reduce-only Algo and resolve ambiguity by ID only."""
        if position_side != "BOTH" or quantity <= 0 or not quantity.is_finite():
            return None

        async def assert_authority() -> None:
            if (
                not self._worker_authorized(authority)
                or bool(getattr(authority, "kill_switch_active", False))
            ):
                raise LeaseLostError("Worker authority closed during Local Mainnet protection")
            await self._assert_execution_lease(EconomicRiskClass.INCREASE_RISK)

        params: Dict[str, Any] = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "positionSide": position_side,
            "type": order_type,
            "quantity": str(quantity),
            "reduceOnly": "true",
            "closePosition": "false",
            "triggerPrice": str(trigger_price),
            "workingType": "MARK_PRICE",
            "clientAlgoId": client_algo_id,
        }
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            response = await asyncio.wait_for(
                self.rest_client.request(
                    "POST",
                    self._algo_order_path,
                    signed=True,
                    params=params,
                    before_mutation=assert_authority,
                ),
                timeout=remaining,
            )
        except (BinanceTransportAmbiguity, asyncio.TimeoutError):
            # This request is never retried. A single query by the durable
            # clientAlgoId is the only permitted ambiguity resolution.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                response = await asyncio.wait_for(
                    self._query_algo_order(
                        symbol=symbol, client_algo_id=client_algo_id
                    ),
                    timeout=remaining,
                )
            except Exception:
                return None
        except Exception as exc:
            logger.error(
                "Local Mainnet protection submission failed: %s", type(exc).__name__
            )
            return None
        if not isinstance(response, dict):
            return None
        algo_id_raw = response.get("algoId")
        if algo_id_raw is None:
            return None
        try:
            algo_id = int(algo_id_raw)
        except (TypeError, ValueError):
            return None
        if algo_id <= 0 or str(response.get("clientAlgoId") or "") != client_algo_id:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            confirmed = await asyncio.wait_for(
                self.rest_client.request(
                    "GET",
                    self._algo_order_query_path,
                    signed=True,
                    params={"symbol": symbol, "algoId": algo_id},
                ),
                timeout=remaining,
            )
        except Exception:
            return None
        return confirmed if isinstance(confirmed, dict) else None

    async def _cancel_local_mainnet_owned_algos(
        self, record: Dict[str, Any]
    ) -> bool:
        """Cancel owned still-open brackets once and prove they left openAlgoOrders."""
        return await self.reconciliation._cancel_local_mainnet_owned_algos(record)

    async def _local_mainnet_close_order_filled(
        self, intent: OrderIntent, close_client_order_id: str
    ) -> bool:
        """Read-only check that the reduce-only emergency close is fully FILLED."""
        try:
            response = await self.query_order(str(intent.symbol), close_client_order_id)
            if not isinstance(response, dict):
                return False
            close_side = (
                "SELL"
                if str(getattr(intent.side, "value", intent.side)).upper() == "BUY"
                else "BUY"
            )
            executed = Decimal(str(response.get("executedQty")))
            original = Decimal(str(response.get("origQty")))
            return bool(
                str(response.get("clientOrderId") or "") == close_client_order_id
                and str(response.get("symbol") or "").upper() == str(intent.symbol).upper()
                and str(response.get("side") or "").upper() == close_side
                and str(response.get("positionSide") or "BOTH").upper() == "BOTH"
                and _exchange_bool(response.get("reduceOnly")) is True
                and str(response.get("status") or "").upper() == "FILLED"
                and executed.is_finite()
                and executed > 0
                and executed == original
            )
        except Exception:
            return False

    async def _local_mainnet_position_is_flat(self, intent: OrderIntent) -> bool:
        """Read signed position risk and verify that the net position is flat (0)."""
        try:
            positions = await self.rest_client.request(
                "GET", self._position_risk_path, signed=True
            )
            if not isinstance(positions, list):
                return False
            matching = [
                row
                for row in positions
                if isinstance(row, dict)
                and str(row.get("symbol") or "").upper() == str(intent.symbol).upper()
                and str(row.get("positionSide") or "BOTH").upper() == "BOTH"
            ]
            if len(matching) != 1:
                return False
            amount = Decimal(str(matching[0].get("positionAmt")))
            return amount.is_finite() and amount == 0
        except Exception:
            return False

    async def _verify_local_mainnet_close(
        self,
        intent: OrderIntent,
        record: Dict[str, Any],
        close_client_order_id: str,
        *,
        authority: Any,
    ) -> bool:
        """Read back close order, userTrades, flat position and Algo cleanup."""
        try:
            response = await self.query_order(
                str(intent.symbol), close_client_order_id
            )
            if not isinstance(response, dict):
                return False
            close_side = (
                "SELL"
                if str(getattr(intent.side, "value", intent.side)).upper() == "BUY"
                else "BUY"
            )
            if (
                str(response.get("clientOrderId") or "") != close_client_order_id
                or str(response.get("symbol") or "").upper() != str(intent.symbol).upper()
                or str(response.get("side") or "").upper() != close_side
                or str(response.get("positionSide") or "BOTH").upper() != "BOTH"
                or _exchange_bool(response.get("reduceOnly")) is not True
                or str(response.get("status") or "").upper() != "FILLED"
            ):
                return False
            executed = Decimal(str(response.get("executedQty")))
            original = Decimal(str(response.get("origQty")))
            if not executed.is_finite() or executed <= 0 or executed != original:
                return False
            close_order = await self.ledger.get_order_by_client_id(
                close_client_order_id
            )
            exchange_order_id = str(response.get("orderId") or "").strip()
            if (
                close_order is None
                or not exchange_order_id.isdigit()
                or int(exchange_order_id) <= 0
                or str(getattr(close_order, "exchange_order_id", "") or "")
                != exchange_order_id
            ):
                return False
            await self.reconciliation._recover_order_fills(close_order, response)
            fills = [
                fill
                for fill in await self.ledger.get_fills()
                if str(getattr(fill, "client_order_id", "")) == close_client_order_id
                and str(getattr(fill, "symbol", "")).upper() == str(intent.symbol).upper()
            ]
            fill_qty = sum(
                (Decimal(str(fill.quantity)) for fill in fills), Decimal("0")
            )
            if (
                not fills
                or fill_qty != executed
                or any(
                    str(getattr(fill.side, "value", fill.side)).upper() != close_side
                    or str(getattr(fill, "exchange_order_id", "") or "")
                    != exchange_order_id
                    for fill in fills
                )
            ):
                return False
            positions = await self.rest_client.request(
                "GET", self._position_risk_path, signed=True
            )
            if not isinstance(positions, list):
                return False
            matching = [
                row for row in positions
                if isinstance(row, dict)
                and str(row.get("symbol") or "").upper() == str(intent.symbol).upper()
                and str(row.get("positionSide") or "BOTH").upper() == "BOTH"
            ]
            if len(matching) != 1 or Decimal(str(matching[0].get("positionAmt"))) != 0:
                return False
            open_orders = await self.rest_client.request(
                "GET", self._open_orders_path, signed=True,
                params={"symbol": str(intent.symbol).upper()},
            )
            if not isinstance(open_orders, list) or any(
                isinstance(row, dict)
                and str(row.get("symbol") or "").upper() == str(intent.symbol).upper()
                for row in open_orders
            ):
                return False
            open_algos = await self.rest_client.request(
                "GET", self._open_algo_orders_path, signed=True,
                params={"symbol": str(intent.symbol).upper(), "algoType": "CONDITIONAL"},
            )
            if not isinstance(open_algos, list) or any(
                isinstance(row, dict)
                and str(row.get("symbol") or "").upper() == str(intent.symbol).upper()
                for row in open_algos
            ):
                return False
            # Verification never mutates: cancellation has already been claimed
            # before the close POST, and ambiguity must remain query-only.
            if not await self._owned_algos_are_absent(record):
                return False
            if (
                Decimal(str(record.get("filled_quantity") or "0")) <= 0
                or record.get("entry_average_price") is None
            ):
                # A close may be exchange-confirmed, but entry ownership is
                # incomplete without durable entry fills, so do not claim the
                # basket lifecycle is fully CLOSED.
                unresolved = dict(record)
                unresolved.update(
                    state="UNKNOWN",
                    state_reason=self._local_recovery_reason(
                        record, local_close_client_order_id=close_client_order_id,
                        cause="close_confirmed_entry_fill_unknown",
                    ),
                    last_reconciled_at=utc_now(),
                )
                await self._persist_local_mainnet_protection(unresolved)
                return False
            updated = dict(record)
            proof = {
                "algo_id": "LOCAL_EMERGENCY_CLOSE",
                "order_id": exchange_order_id,
                "client_order_id": close_client_order_id,
                "order_status": "FILLED",
                "executed_quantity": str(executed),
                "trade_quantity": str(fill_qty),
                "position_quantity": "0",
                "open_child_order_ids": [],
                "open_owner_algo_ids": [],
                "verified_at": utc_now(),
            }
            close_writer = self.on_local_mainnet_close_verified
            if not callable(close_writer) or not await close_writer(updated, proof):
                return False
            self.last_local_mainnet_protection = {
                "status": "CLOSED_AFTER_PROTECTION_FAILURE",
                "entry_client_order_id": str(intent.client_order_id),
                "close_client_order_id": close_client_order_id,
            }
            return True
        except Exception as exc:
            logger.error(
                "Local Mainnet close read-back is unresolved: %s", type(exc).__name__
            )
            return False

    async def _local_mainnet_close_only_once(
        self,
        intent: OrderIntent,
        order: ExecutionOrder,
        record: Dict[str, Any],
        *,
        reason: str,
        authority: Any,
    ) -> bool:
        async with self._mutation_scope():
            return await self._local_mainnet_close_only_locked(
                intent, order, record, reason=reason, authority=authority
            )

    async def _local_mainnet_close_only_locked(
        self, intent: OrderIntent, order: ExecutionOrder, record: Dict[str, Any],
        *, reason: str, authority: Any,
    ) -> bool:
        """Reserve a durable client ID, submit at most once, then verify read-only."""
        worker = self._worker_authority
        if (
            self.env != BinanceEnvironment.MAINNET
            or not self._is_local_mainnet_runtime()
            or not self._worker_authorized(authority)
            or str(getattr(intent.position_side, "value", intent.position_side)).upper() != "BOTH"
        ):
            await self._degrade_local_mainnet_protection(worker)
            return False
        protections = getattr(
            getattr(getattr(authority, "persistence", None), "repository", None),
            "algo_protections", None,
        )
        loader: Any = getattr(protections, "get_protection", None)
        if callable(loader):
            try:
                durable = await cast(Any, loader("binance_mainnet", str(intent.symbol).upper(), order.client_order_id))
            except Exception:
                durable = None
            if not isinstance(durable, Mapping):
                await self._degrade_local_mainnet_protection(worker)
                return False
            record = dict(durable)
        decision_id = self._local_close_decision_id(order.client_order_id)
        deterministic_id = self._generate_client_order_id(
            decision_id, str(intent.symbol).upper(), order_index=0
        )
        existing_id = self._local_close_id_from_reason(record.get("state_reason"))
        new_reservation = existing_id is None
        if existing_id is not None and existing_id != deterministic_id:
            await self._degrade_local_mainnet_protection(worker)
            return False
        close_client_order_id = existing_id or deterministic_id
        prior_submission_state = self._local_close_submission_state(
            record.get("state_reason")
        )
        unclaimed_record = dict(record)
        reserved = dict(record)
        reserved.update(
            state="CLOSE_PENDING",
            state_reason=self._local_recovery_reason(
                record, local_close_client_order_id=close_client_order_id,
                close_submission="RESERVED" if new_reservation else prior_submission_state or "UNKNOWN",
                cause=reason[:48],
            ),
            last_reconciled_at=utc_now(),
        )
        # A fresh reservation remains local until the atomic submission claim.
        # Never overwrite a durable ATTEMPTED marker with a stale RESERVED row.
        # Legacy and ATTEMPTED states remain query-only.
        record = reserved
        if not new_reservation and prior_submission_state != "RESERVED":
            verified = await self._verify_local_mainnet_close(
                intent, record, close_client_order_id, authority=authority
            )
            if not verified:
                degraded = dict(record)
                degraded.update(
                    state="UNKNOWN",
                    state_reason=self._local_recovery_reason(record, outcome="UNKNOWN"),
                    last_reconciled_at=utc_now(),
                )
                await self._persist_local_mainnet_protection(degraded)
                await self._degrade_local_mainnet_protection(worker)
            return verified
        if not new_reservation:
            try:
                prior_exchange_order = await self.query_order(
                    str(intent.symbol), close_client_order_id
                )
            except Exception:
                prior_exchange_order = {"outcome": "UNKNOWN"}
            if prior_exchange_order is not None:
                unknown = dict(record)
                unknown.update(
                    state="UNKNOWN",
                    state_reason=self._local_recovery_reason(record, close_submission="UNKNOWN"),
                    last_reconciled_at=utc_now(),
                )
                await self._persist_local_mainnet_protection(unknown)
                await self._degrade_local_mainnet_protection(worker)
                return False
        else:
            # Deterministic client IDs are queried before any mutating call,
            # even on the first local invocation, so a stale/legacy exchange
            # order cannot be mistaken for a fresh close attempt.
            try:
                prior_exchange_order = await self.query_order(
                    str(intent.symbol), close_client_order_id
                )
            except Exception:
                prior_exchange_order = {"outcome": "UNKNOWN"}
            if prior_exchange_order is not None:
                unknown = dict(record)
                unknown.update(
                    state="UNKNOWN",
                    state_reason=self._local_recovery_reason(record, close_submission="UNKNOWN"),
                    last_reconciled_at=utc_now(),
                )
                await self._persist_local_mainnet_protection(unknown)
                await self._degrade_local_mainnet_protection(worker)
                return False

        entry_readback: Optional[Mapping[str, Any]] = None
        close_owner_filled: Optional[Decimal] = None
        try:
            recorded_filled = Decimal(str(record.get("filled_quantity")))
            if not recorded_filled.is_finite() or recorded_filled < 0:
                raise ValueError("durable entry quantity is invalid")
            if recorded_filled > 0:
                close_owner_filled = recorded_filled
            else:
                observed_entry = await self.query_order(
                    str(intent.symbol), str(intent.client_order_id)
                )
                requested = Decimal(str(record.get("requested_quantity")))
                executed = (
                    Decimal(str(observed_entry.get("executedQty")))
                    if isinstance(observed_entry, Mapping)
                    else Decimal("NaN")
                )
                original = (
                    Decimal(str(observed_entry.get("origQty")))
                    if isinstance(observed_entry, Mapping)
                    else Decimal("NaN")
                )
                exchange_id = (
                    str(observed_entry.get("orderId") or "").strip()
                    if isinstance(observed_entry, Mapping)
                    else ""
                )
                expected_side = str(record.get("entry_side") or "").upper()
                observed_side = (
                    str(observed_entry.get("side") or "").upper()
                    if isinstance(observed_entry, Mapping)
                    else ""
                )
                observed_status = (
                    str(observed_entry.get("status") or "").upper()
                    if isinstance(observed_entry, Mapping)
                    else ""
                )
                known_exchange_id = str(
                    record.get("exchange_order_id")
                    or getattr(order, "exchange_order_id", "")
                    or ""
                ).strip()
                if (
                    not isinstance(observed_entry, Mapping)
                    or str(observed_entry.get("clientOrderId") or "")
                    != str(intent.client_order_id)
                    or str(observed_entry.get("symbol") or "").upper()
                    != str(intent.symbol).upper()
                    or observed_side != expected_side
                    or str(observed_entry.get("positionSide") or "BOTH").upper()
                    != "BOTH"
                    or observed_status
                    not in {"FILLED", "CANCELED", "CANCELLED", "EXPIRED"}
                    or not exchange_id.isdigit()
                    or int(exchange_id) <= 0
                    or not known_exchange_id.isdigit()
                    or known_exchange_id != exchange_id
                    or str(record.get("entry_client_order_id") or "")
                    != str(intent.client_order_id)
                    or str(order.client_order_id) != str(intent.client_order_id)
                    or not requested.is_finite()
                    or requested <= 0
                    or not original.is_finite()
                    or original != requested
                    or not executed.is_finite()
                    or executed <= 0
                    or executed > requested
                ):
                    raise ValueError(
                        "terminal signed entry order read-back is incomplete or mismatched"
                    )
                entry_readback = observed_entry
                close_owner_filled = executed
        except Exception as exc:
            logger.error(
                "Emergency close lacks verifiable entry execution size: %s",
                type(exc).__name__,
            )
            unknown = dict(record)
            unknown.update(
                state="UNKNOWN",
                state_reason=self._local_recovery_reason(unclaimed_record, cause="entry_execution_readback_unknown"),
                last_reconciled_at=utc_now(),
            )
            await self._persist_local_mainnet_protection(unknown)
            await self._degrade_local_mainnet_protection(worker)
            return False

        try:
            positions = await self.rest_client.request(
                "GET", self._position_risk_path, signed=True
            )
            if not isinstance(positions, list):
                raise ValueError("signed position snapshot is invalid")
            matching = [
                row for row in positions
                if isinstance(row, dict)
                and str(row.get("symbol") or "").upper() == str(intent.symbol).upper()
                and str(row.get("positionSide") or "BOTH").upper() == "BOTH"
            ]
            if len(matching) != 1:
                raise ValueError("signed close position identity is not unique")
            amount = Decimal(str(matching[0].get("positionAmt")))
            if not amount.is_finite() or amount == 0:
                raise ValueError("signed close position is flat or invalid")
            try:
                recorded_filled = Decimal(str(record.get("filled_quantity")))
                owner_filled = close_owner_filled
                owner_side = str(record.get("entry_side") or "").upper()
                owner_position_side = str(record.get("position_side") or "").upper()
                intent_side = str(getattr(intent.side, "value", intent.side)).upper()
                if owner_filled is None:
                    raise ValueError("entry execution quantity is unknown")
                expected_position = (
                    owner_filled if owner_side == "BUY" else -owner_filled
                )
            except (InvalidOperation, TypeError, ValueError):
                recorded_filled = Decimal("NaN")
                owner_filled = Decimal("NaN")
                owner_side = owner_position_side = intent_side = ""
                expected_position = Decimal("NaN")
            # Never flatten an account-level position just because it shares
            # this symbol. The live net position must be exactly the signed
            # quantity owned by this durable entry before any close POST.
            if (
                not owner_filled.is_finite()
                or owner_filled <= 0
                or owner_side not in {"BUY", "SELL"}
                or owner_side != intent_side
                or owner_position_side != "BOTH"
                or str(getattr(intent.position_side, "value", intent.position_side)).upper()
                != owner_position_side
                or (entry_readback is not None and recorded_filled != 0)
                or (entry_readback is None and owner_filled != recorded_filled)
                or expected_position != amount
            ):
                unknown = dict(record)
                unknown.update(
                    state="UNKNOWN",
                    state_reason=self._local_recovery_reason(unclaimed_record, cause="emergency_close_owner_position_mismatch"),
                    last_reconciled_at=utc_now(),
                )
                await self._persist_local_mainnet_protection(unknown)
                await self._degrade_local_mainnet_protection(worker)
                return False
            await self.ledger.replace_positions(positions, mark_initialized=False)
            close_intent = OrderIntent(
                client_order_id=close_client_order_id,
                symbol=str(intent.symbol).upper(),
                market_type=MarketType.USDM_FUTURES,
                side=OrderSide.SELL if amount > 0 else OrderSide.BUY,
                position_side=PositionSide.BOTH,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
                quantity=abs(amount),
                basket_id=str(record.get("basket_id") or "").strip() or None,
                reduce_only=True,
                strategy_id="local_mainnet_protection_fallback",
            )
            close_decision = ExecutionDecision(
                decision_id=decision_id,
                symbol=str(intent.symbol).upper(),
                action="CLOSE_POSITION",
                risk_class=EconomicRiskClass.EMERGENCY,
                orders=[close_intent],
                target_exposure_id=order.target_exposure_id,
                source_intent_ids=[order.client_order_id],
            )
            attempt_record = dict(record)
            close_state_reason = self._local_recovery_reason(
                record, local_close_client_order_id=close_client_order_id,
                close_submission="ATTEMPTED", algo_cancel="ATTEMPTED_UNKNOWN",
            )
            if entry_readback is not None:
                close_state_reason += (
                    f";entry_order_id={entry_readback.get('orderId')};"
                    f"entry_executed_qty={owner_filled};"
                )
            attempt_record.update(
                state="CLOSE_PENDING",
                state_reason=close_state_reason[:256],
                last_reconciled_at=utc_now(),
            )
            # Reservation alone cannot authorize POST. A losing or unavailable
            # fenced attempt acknowledgement is entirely read-only.
            try:
                claimed = await self._claim_local_mainnet_close_submission(
                    protections, attempt_record,
                    close_client_order_id=close_client_order_id,
                )
            except Exception:
                # The CAS may have committed. Never replace its reason with
                # our pre-claim snapshot, even when its acknowledgement is lost.
                await self._degrade_local_mainnet_protection(worker)
                return await self._verify_local_mainnet_close(
                    intent, record, close_client_order_id, authority=authority
                )
            if not isinstance(claimed, Mapping) or any(
                claimed.get(key) != attempt_record.get(key)
                for key in ("entry_client_order_id", "symbol", "filled_quantity", "entry_side", "position_side", "state_reason")
            ) or claimed.get("state") != "CLOSE_PENDING":
                await self._degrade_local_mainnet_protection(worker)
                return await self._verify_local_mainnet_close(
                    intent, record, close_client_order_id, authority=authority
                )
            record = dict(claimed)
            # Send the reduce-only close first. The exchange stop/target stay
            # live until the close is proven FILLED, so a failed close never
            # leaves the position unprotected (the one-shot claim forbids a
            # retry). _execute_decision re-derives the same stable client ID
            # from the decision ID, passes the durable outbox barrier, and
            # performs one risk-reducing POST. It never retries after ambiguity.
            try:
                await self._execute_decision(
                    close_decision,
                    allow_emergency_fallback=True,
                    authority=authority,
                )
            finally:
                close_filled = await self._local_mainnet_close_order_filled(
                    intent, close_client_order_id
                )
                position_is_flat = (
                    await self._local_mainnet_position_is_flat(intent)
                    if close_filled
                    else False
                )
                # Once the close has filled and the position reads flat,
                # the position is verified flat. The owned stop/target are
                # reduce-only, so a trigger racing this window is reduced to zero
                # by the exchange and cannot reduce twice or open the opposite
                # side; cancelling them is only cleanup.
                # On any failure or ambiguity they are left in place (fail closed).
                if close_filled and position_is_flat:
                    cancellation_confirmed = await self._cancel_local_mainnet_owned_algos(record)
                    if cancellation_confirmed:
                        record["state_reason"] = self._local_recovery_reason(record, algo_cancel="CONFIRMED")
                        # Failure to acknowledge cleanup must preserve the durable
                        # ATTEMPTED_UNKNOWN marker; it cannot permit another DELETE.
                        if not await self._persist_local_mainnet_protection(record):
                            record = dict(claimed)
        except Exception as exc:
            logger.error(
                "Local Mainnet close-only request is unresolved: %s", type(exc).__name__
            )

        verified = await self._verify_local_mainnet_close(
            intent, record, close_client_order_id, authority=authority
        )
        if not verified:
            unknown = dict(record)
            unknown.update(
                state="UNKNOWN",
                state_reason=self._local_recovery_reason(record, outcome="UNKNOWN"),
                last_reconciled_at=utc_now(),
            )
            await self._persist_local_mainnet_protection(unknown)
            await self._degrade_local_mainnet_protection(worker)
        return verified

    def _reconstruct_intent_and_order_from_record(
        self, record: Mapping[str, Any]
    ) -> Tuple[OrderIntent, ExecutionOrder]:
        """Reconstruct the minimal OrderIntent and ExecutionOrder needed for exit execution."""
        symbol = str(record.get("symbol") or "ETHUSDC").upper()
        client_order_id = str(record.get("entry_client_order_id"))
        qty = Decimal(str(record.get("filled_quantity") or record.get("order_quantity") or "0.1"))
        side_str = str(record.get("entry_side") or record.get("side") or "BUY").upper()
        side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL
        pos_side_str = str(record.get("position_side") or "BOTH").upper()
        pos_side = PositionSide(pos_side_str) if pos_side_str in {"BOTH", "LONG", "SHORT"} else PositionSide.BOTH
        intent = OrderIntent(
            client_order_id=client_order_id,
            symbol=symbol,
            basket_id=str(record.get("basket_id") or f"basket-{client_order_id}"),
            market_type=MarketType.USDM_FUTURES,
            side=side,
            position_side=pos_side,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.GTC,
            quantity=qty,
            management_mode=str(record.get("management_mode") or "QUICK"),
        )
        order = ExecutionOrder(
            client_order_id=client_order_id,
            symbol=symbol,
            exchange_order_id=str(record.get("exchange_order_id") or ""),
            side=side,
            position_side=pos_side,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.GTC,
            quantity=qty,
            price=Decimal(str(record.get("entry_average_price") or record.get("entry_price") or "0")),
            status=OrderStatus.FILLED if Decimal(str(record.get("filled_quantity") or 0)) > 0 else OrderStatus.NEW,
        )
        return intent, order

    def evaluate_pilot_drawdown(
        self, session: Mapping[str, Any], current_net_pnl: Decimal | str | float
    ) -> bool:
        """Check whether campaign drawdown has breached the 5 USDC limit."""
        if not isinstance(session, Mapping) or session.get("policy") != "LIVE_RESEARCH_PILOT":
            return False
        if session.get("pilot_drawdown_triggered") is True:
            return True
        peak = Decimal(str(session.get("pilot_peak_pnl_usdc") or "0"))
        current = Decimal(str(current_net_pnl))
        limit = Decimal(str(session.get("pilot_max_drawdown_usdc") or "5"))
        return (peak - current) >= limit

    async def _recover_unprotected_local_pilot_owner(
        self, owner: Mapping[str, Any], *, authority: Any, launch_id: str
    ) -> bool:
        """Fence new risk, then make one owner-scoped close attempt for known exposure."""
        if not isinstance(owner, Mapping):
            return False
        try:
            filled = self._decimal_value(owner.get("filled_quantity"), nonnegative=True)
            requested = self._decimal_value(owner.get("requested_quantity"), positive=True)
        except (InvalidOperation, TypeError, ValueError):
            return False
        if (
            str(owner.get("mainnet_launch_id") or "") != launch_id
            or str(owner.get("symbol") or "").upper() != MAINNET_RISK_POLICY.symbol
            or str(owner.get("position_side") or "").upper() != "BOTH"
            or str(owner.get("management_mode") or "").upper() != "QUICK"
            or filled > requested
        ):
            return False

        persistence = getattr(authority, "persistence", None)
        close_only: Any = getattr(persistence, "enter_local_live_pilot_close_only", None)
        if not callable(close_only):
            return False
        transitioned = await cast(Any, close_only(
            launch_id, reason="PILOT_RECONCILIATION_UNKNOWN"
        ))
        if (
            not isinstance(transitioned, Mapping)
            or str(transitioned.get("pilot_status") or "").upper()
            not in {"CLOSE_ONLY", "EXPIRED", "REVOKED", "COMPLETED"}
            or str(transitioned.get("state") or "").upper()
            not in {"PAUSED_NEW_RISK", "REAUTH_REQUIRED", "RECONCILIATION_REQUIRED"}
        ):
            return False
        setattr(authority, "_mainnet_launch_session", dict(transitioned))

        if str(owner.get("state") or "").upper() == "PENDING" or filled == 0:
            # A zero-fill PENDING owner may still have a live exchange entry.
            # Fence the campaign first, then cancel only this exact client ID
            # and read the terminal order/fills back before any close decision.
            updated = await self._cancel_and_read_back_pilot_entry(
                owner, authority=authority
            )
            if not isinstance(updated, Mapping):
                # Canonical userTrades may lag even when Binance has a
                # terminal exact-ID entry order and a matching signed
                # position. Keep the durable owner/accounting untouched, but
                # allow the existing deterministic, fenced close-only path to
                # independently re-prove that exact position before POST.
                # The close verifier intentionally leaves the owner UNKNOWN
                # until canonical entry fills and price are durable.
                try:
                    entry = await self.query_order(
                        str(owner.get("symbol") or "").upper(),
                        str(owner.get("entry_client_order_id") or ""),
                    )
                    executed = self._decimal_value(
                        entry.get("executedQty"), positive=True
                    ) if isinstance(entry, Mapping) else Decimal("NaN")
                    original = self._decimal_value(
                        entry.get("origQty"), positive=True
                    ) if isinstance(entry, Mapping) else Decimal("NaN")
                    exchange_id = str(entry.get("orderId") or "").strip() if isinstance(entry, Mapping) else ""
                    expected_side = str(owner.get("entry_side") or "").upper()
                    known_exchange_id = str(owner.get("exchange_order_id") or "").strip()
                    terminal = str(entry.get("status") or "").upper() if isinstance(entry, Mapping) else ""
                    if (
                        not isinstance(entry, Mapping)
                        or str(entry.get("clientOrderId") or "") != str(owner.get("entry_client_order_id") or "")
                        or str(entry.get("symbol") or "").upper() != MAINNET_RISK_POLICY.symbol
                        or expected_side not in {"BUY", "SELL"}
                        or str(entry.get("side") or "").upper() != expected_side
                        or str(entry.get("positionSide") or "BOTH").upper() != "BOTH"
                        or terminal not in {"FILLED", "CANCELED", "CANCELLED", "EXPIRED"}
                        or not exchange_id.isdigit() or int(exchange_id) <= 0
                        or not known_exchange_id.isdigit() or known_exchange_id != exchange_id
                        or not requested.is_finite() or original != requested
                        or not executed.is_finite() or executed <= 0 or executed > requested
                    ):
                        return False
                    positions = await self.rest_client.request(
                        "GET", self._position_risk_path, signed=True
                    )
                    if not isinstance(positions, list):
                        return False
                    matching = [
                        row for row in positions
                        if isinstance(row, Mapping)
                        and str(row.get("symbol") or "").upper() == MAINNET_RISK_POLICY.symbol
                        and str(row.get("positionSide") or "BOTH").upper() == "BOTH"
                    ]
                    if len(matching) != 1:
                        return False
                    signed_amount = self._decimal_value(
                        matching[0].get("positionAmt"), nonnegative=False
                    )
                    expected_amount = executed if expected_side == "BUY" else -executed
                    if signed_amount != expected_amount:
                        return False
                except Exception:  # noqa: BLE001 - any signed-read failure must block the close handoff
                    return False
                recovered_intent = OrderIntent(
                    client_order_id=str(owner.get("entry_client_order_id")),
                    symbol=MAINNET_RISK_POLICY.symbol,
                    basket_id=str(owner.get("basket_id") or ""),
                    market_type=MarketType.USDM_FUTURES,
                    side=OrderSide.BUY if expected_side == "BUY" else OrderSide.SELL,
                    position_side=PositionSide.BOTH,
                    order_type=OrderType.MARKET,
                    time_in_force=TimeInForce.GTC,
                    quantity=executed,
                    management_mode="QUICK",
                )
                recovered_order = ExecutionOrder(
                    client_order_id=str(owner.get("entry_client_order_id")),
                    symbol=MAINNET_RISK_POLICY.symbol,
                    exchange_order_id=exchange_id,
                    side=recovered_intent.side,
                    position_side=PositionSide.BOTH,
                    order_type=OrderType.MARKET,
                    time_in_force=TimeInForce.GTC,
                    quantity=executed,
                    price=Decimal(0),
                    status=OrderStatus.FILLED,
                )
                return await self._local_mainnet_close_only_once(
                    recovered_intent,
                    recovered_order,
                    dict(owner),
                    reason="PILOT_ZERO_FILL_CANONICAL_TRADES_PENDING",
                    authority=authority,
                )
            owner = updated
            try:
                filled = self._decimal_value(
                    owner.get("filled_quantity"), nonnegative=True
                )
            except (InvalidOperation, TypeError, ValueError):
                return False
            if str(owner.get("state") or "").upper() == "CLOSED" and filled == 0:
                return True
            if filled > requested:
                return False

        if filled <= 0:
            return False
        try:
            average = self._decimal_value(owner.get("entry_average_price"), positive=True)
            side = str(owner.get("entry_side") or owner.get("side") or "").upper()
            stop = self._decimal_value(owner.get("stop_trigger_price"), positive=True)
            target = self._decimal_value(
                owner.get("take_profit_trigger_price"), positive=True
            )
        except (InvalidOperation, TypeError, ValueError):
            return False
        if (
            side not in {"BUY", "SELL"}
            or (side == "BUY" and not stop < average < target)
            or (side == "SELL" and not target < average < stop)
        ):
            return False

        intent, order = self._reconstruct_intent_and_order_from_record(owner)
        return await self._local_mainnet_close_only_once(
            intent,
            order,
            dict(owner),
            reason="PILOT_UNPROTECTED_OWNER_RECOVERY",
            authority=authority,
        )

    async def _cancel_and_read_back_pilot_entry(
        self, record: Mapping[str, Any], *, authority: Any
    ) -> Optional[Dict[str, Any]]:
        """Cancel an owned entry remainder once, then prove its terminal fill quantity."""
        worker = self._worker_authority
        launch = getattr(authority, "_mainnet_launch_session", None)
        client_order_id = str(record.get("entry_client_order_id") or "").strip()
        symbol = str(record.get("symbol") or "").upper()
        try:
            requested = self._decimal_value(record.get("requested_quantity"), positive=True)
            owner_filled = self._decimal_value(record.get("filled_quantity"), nonnegative=True)
        except (InvalidOperation, TypeError, ValueError):
            requested = owner_filled = Decimal("NaN")
        if (
            self.env != BinanceEnvironment.MAINNET
            or not self._is_local_mainnet_runtime()
            or not self._worker_authorized(authority)
            or not isinstance(launch, Mapping)
            or launch.get("policy") != "LIVE_RESEARCH_PILOT"
            or record.get("mainnet_launch_id") != launch.get("launch_id")
            or record.get("venue") != "binance_mainnet"
            or symbol != MAINNET_RISK_POLICY.symbol
            or not client_order_id
            or not requested.is_finite()
            or not owner_filled.is_finite()
            or owner_filled < 0
            or owner_filled > requested
        ):
            await self._degrade_local_mainnet_protection(worker)
            return None

        # Resolve the exact durable owner before any cancellation. The client
        # order ID, side, symbol, and original quantity are the cancellation
        # boundary; exchange order IDs or symbol-wide cancellation are unused.
        try:
            before = await self.query_order(symbol, client_order_id)
        except Exception:
            before = None
        if not isinstance(before, Mapping):
            await self._degrade_local_mainnet_protection(worker)
            return None

        def order_matches(response: Mapping[str, Any]) -> bool:
            try:
                original_qty = self._decimal_value(response.get("origQty"), positive=True)
                executed_qty = self._decimal_value(response.get("executedQty"), nonnegative=True)
            except (InvalidOperation, TypeError, ValueError):
                return False
            return bool(
                response.get("clientOrderId") == client_order_id
                and str(response.get("symbol") or "").upper() == symbol
                and str(response.get("side") or "").upper()
                == str(record.get("entry_side") or "").upper()
                and original_qty == requested
                and owner_filled <= executed_qty <= requested
            )

        if not order_matches(before):
            await self._degrade_local_mainnet_protection(worker)
            return None
        terminal_statuses = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
        before_status = str(before.get("status") or "").upper()
        if before_status not in terminal_statuses:
            if before_status not in {"NEW", "PARTIALLY_FILLED"}:
                await self._degrade_local_mainnet_protection(worker)
                return None
            # Exactly one DELETE is permitted. Even if the response is
            # ambiguous, resolve it only with a read; never repeat the DELETE.
            # A fenced close reservation can replace a legacy reason during
            # integration. Its presence conservatively means entry cancellation
            # may already have been attempted; recover only by signed reads.
            if (
                "entry_cancel=ATTEMPTED_UNKNOWN" not in str(record.get("state_reason") or "")
                and "entry_cancel=CONFIRMED" not in str(record.get("state_reason") or "")
                and self._local_close_id_from_reason(record.get("state_reason")) is None
            ):
                record = dict(record)
                claim_cancel: Any = getattr(self, "on_local_mainnet_entry_cancel_claim", None)
                if not callable(claim_cancel):
                    await self._degrade_local_mainnet_protection(worker)
                    return None
                try:
                    claimed = await cast(Any, claim_cancel(record))
                except Exception:
                    # The durable claim may have committed despite a lost
                    # acknowledgement. Resolve only by exact-ID read-back.
                    claimed = False
                if claimed is True:
                    record["state_reason"] = self._local_recovery_reason(
                        record, entry_cancel="ATTEMPTED_UNKNOWN"
                    )
                    cancel_kwargs = {"authority": authority}
                    if getattr(self, "state", ConnectionState.READY) != ConnectionState.READY or getattr(authority, "kill_switch_active", False):
                        cancel_kwargs["allow_emergency_fallback"] = True
                    await self.cancel_order(symbol, client_order_id, **cancel_kwargs)
                else:
                    await self._degrade_local_mainnet_protection(worker)
            try:
                after = await self.query_order(symbol, client_order_id)
            except Exception:
                after = None
        else:
            after = before

        if (
            not isinstance(after, Mapping)
            or not order_matches(after)
            or str(after.get("status") or "").upper() not in terminal_statuses
        ):
            await self._degrade_local_mainnet_protection(worker)
            return None
        executed = self._decimal_value(after.get("executedQty"), nonnegative=True)
        updated = dict(record)
        updated["state_reason"] = self._local_recovery_reason(record, entry_cancel="CONFIRMED")
        updated["filled_quantity"] = executed
        updated["exchange_order_id"] = str(after.get("orderId") or record.get("exchange_order_id") or "")
        if executed == 0:
            terminal_status = str(after.get("status") or "").upper()
            if terminal_status not in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}:
                await self._degrade_local_mainnet_protection(worker)
                return None
            exchange_order_id = str(after.get("orderId") or "").strip()
            if not exchange_order_id.isdigit() or int(exchange_order_id) <= 0:
                await self._degrade_local_mainnet_protection(worker)
                return None
            verified_at = utc_now()
            updated.update(
                state="CLOSED",
                state_reason=self._local_recovery_reason(
                    updated, cause="pilot_entry_remainder_cancelled_zero_fill",
                ),
                closed_at=verified_at,
                unfilled_order_proof={
                    "order_id": exchange_order_id,
                    "client_order_id": client_order_id,
                    "order_status": terminal_status,
                    "executed_quantity": "0",
                    "original_quantity": str(requested),
                    "symbol": symbol,
                    "entry_side": str(record.get("entry_side") or "").upper(),
                    "position_side": str(record.get("position_side") or "").upper(),
                    "verified_at": verified_at,
                },
                last_reconciled_at=utc_now(),
            )
            if not await self._persist_local_mainnet_protection(updated):
                await self._degrade_local_mainnet_protection(worker)
                return None
            return updated

        ledger_order = await self.ledger.get_order_by_client_id(client_order_id)
        recover_fills: Any = getattr(self.reconciliation, "_recover_order_fills", None)
        if ledger_order is None or not callable(recover_fills):
            await self._degrade_local_mainnet_protection(worker)
            return None
        try:
            await cast(Any, recover_fills(ledger_order, dict(after)))
            fills = [
                fill for fill in await self.ledger.get_fills()
                if str(getattr(fill, "client_order_id", "")) == client_order_id
            ]
            total_filled = sum(
                (self._decimal_value(fill.quantity, positive=True) for fill in fills),
                Decimal("0"),
            )
            total_quote = sum(
                (
                    self._decimal_value(fill.quantity, positive=True)
                    * self._decimal_value(fill.price, positive=True)
                    for fill in fills
                ),
                Decimal("0"),
            )
            if total_filled != executed or total_filled <= 0:
                raise ValueError("durable fill total differs from terminal entry read-back")
            updated["entry_average_price"] = total_quote / total_filled
            updated["last_reconciled_at"] = utc_now()
            if not await self._persist_local_mainnet_protection(updated):
                raise ValueError("updated pilot protection owner was not persisted")
        except Exception:
            await self._degrade_local_mainnet_protection(worker)
            return None
        return updated

    def evaluate_quick_hold_timeout(
        self, record: Mapping[str, Any], now: Optional[datetime] = None
    ) -> bool:
        """Check whether a QUICK position has exceeded its maximum hold duration (24h)."""
        if not isinstance(record, Mapping):
            return False
        state = str(record.get("state", "")).upper()
        if state not in {"PROTECTED", "PENDING", "CLOSE_PENDING"}:
            return False
        entry_time = record.get("first_fill_at") or record.get("created_at")
        if entry_time is None:
            return True
        if isinstance(entry_time, str):
            try:
                entry_time = datetime.fromisoformat(entry_time)
            except ValueError:
                return True
        if not isinstance(entry_time, datetime):
            return True
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)
        current_time = now if isinstance(now, datetime) else utc_now()
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        hold_seconds = (current_time - entry_time).total_seconds()
        raw_max_hold = record.get("quick_max_hold_seconds")
        if isinstance(raw_max_hold, bool) or raw_max_hold is None:
            return True
        try:
            max_hold = Decimal(str(raw_max_hold))
        except (InvalidOperation, TypeError, ValueError):
            return True
        expected_max_hold = Decimal(
            str(LOCAL_LIVE_PILOT_POLICY["quick_max_hold_seconds"])
        )
        if not max_hold.is_finite() or max_hold != expected_max_hold or hold_seconds < 0:
            return True
        return hold_seconds >= float(max_hold)

    async def check_and_enforce_pilot_protections(self, authority: Any) -> Dict[str, Any]:
        async with self._mutation_scope():
            return await self._check_and_enforce_pilot_protections_locked(authority)

    async def _check_and_enforce_pilot_protections_locked(self, authority: Any) -> Dict[str, Any]:
        """Examine active protections for 24h QUICK hold expiry and drawdown breach."""
        actions: Dict[str, Any] = {
            "drawdown_triggered": False,
            "quick_expired_count": 0,
            "unmatched_owner_count": 0,
            "failed_action_count": 0,
            "protection_verified_count": 0,
            "closed_records": [],
        }
        session = getattr(authority, "_mainnet_launch_session", None)
        if not isinstance(session, Mapping) or session.get("policy") != "LIVE_RESEARCH_PILOT":
            return actions
        launch_id = str(session.get("launch_id") or "").strip()
        campaign_id = str(session.get("pilot_campaign_id") or "").strip()
        persistence = getattr(authority, "persistence", None)
        durable_loader: Any = getattr(persistence, "get_mainnet_launch_session", None)
        if (
            not self._is_local_mainnet_runtime()
            or not launch_id
            or not campaign_id
            or str(getattr(authority, "_mainnet_launch_id", "") or "") != launch_id
            or not callable(durable_loader)
        ):
            await self._degrade_local_mainnet_protection(authority)
            actions["unmatched_owner_count"] = 1
            return actions
        try:
            durable_session = await cast(Any, durable_loader(launch_id))
        except Exception:
            durable_session = None
        if (
            not isinstance(durable_session, Mapping)
            or str(durable_session.get("launch_id") or "") != launch_id
            or durable_session.get("policy") != "LIVE_RESEARCH_PILOT"
            or str(durable_session.get("pilot_campaign_id") or "") != campaign_id
            or str(durable_session.get("runtime_target") or "").upper() != "LOCAL"
            or str(durable_session.get("symbol") or "").upper() != MAINNET_RISK_POLICY.symbol
        ):
            await self._degrade_local_mainnet_protection(authority)
            actions["unmatched_owner_count"] = 1
            return actions
        session = dict(durable_session)
        setattr(authority, "_mainnet_launch_session", session)
        repository = getattr(getattr(authority, "persistence", None), "repository", None)
        protections_store = getattr(repository, "algo_protections", None)
        if protections_store is None:
            await self._degrade_local_mainnet_protection(authority)
            actions["unmatched_owner_count"] = 1
            return actions

        # Inspect the complete symbol-wide result before mutating anything.
        # Protection rows have no campaign column; their launch ID is resolved
        # through the exact durable pilot session fetched above.
        try:
            active = await protections_store.list_active_protections(
                venue="binance_mainnet", symbol=MAINNET_RISK_POLICY.symbol
            )
        except Exception as exc:
            logger.error("Failed to list active protections for pilot monitor: %s", type(exc).__name__)
            await self._degrade_local_mainnet_protection(authority)
            actions["failed_action_count"] += 1
            return actions
        if not isinstance(active, list):
            await self._degrade_local_mainnet_protection(authority)
            actions["unmatched_owner_count"] = 1
            return actions
        for owner in active:
            owner_matches_campaign = (
                isinstance(owner, Mapping)
                and str(owner.get("mainnet_launch_id") or "") == launch_id
                and str(owner.get("venue") or "").lower() == "binance_mainnet"
                and str(owner.get("symbol") or "").upper() == MAINNET_RISK_POLICY.symbol
                and str(owner.get("management_mode") or "").upper() == "QUICK"
                and (
                    owner.get("pilot_campaign_id") is None
                    or str(owner.get("pilot_campaign_id")) == campaign_id
                )
            )
            if not owner_matches_campaign:
                actions["unmatched_owner_count"] += 1
        if actions["unmatched_owner_count"]:
            await self._degrade_local_mainnet_protection(authority)
            return actions

        # A database owner row is not proof that Binance still holds both
        # protection orders. Verify the live bracket and signed position on
        # every monitor pass before trusting the local PnL or hold timer.
        for owner in active:
            if str(owner.get("state") or "").upper() != "PROTECTED":
                try:
                    recovered_close = await self._recover_unprotected_local_pilot_owner(
                        owner, authority=authority, launch_id=launch_id
                    )
                except Exception as exc:
                    logger.error(
                        "Unprotected pilot owner recovery failed: %s",
                        type(exc).__name__,
                    )
                    recovered_close = False
                if recovered_close:
                    actions["closed_records"].append(
                        owner.get("entry_client_order_id")
                    )
                await self._degrade_local_mainnet_protection(authority)
                actions["failed_action_count"] += 1
                return actions
            try:
                bracket = ProtectionIntent(
                    symbol=MAINNET_RISK_POLICY.symbol,
                    entry_side=str(owner.get("entry_side") or owner.get("side") or "").upper(),
                    position_side=str(owner.get("position_side") or "").upper(),
                    entry_qty=self._decimal_value(owner.get("filled_quantity"), positive=True),
                    stop_trigger=self._decimal_value(owner.get("stop_trigger_price"), positive=True),
                    take_profit_trigger=self._decimal_value(
                        owner.get("take_profit_trigger_price"), positive=True
                    ),
                    stop_algo_id=int(owner["stop_algo_id"]),
                    stop_client_algo_id=str(owner["stop_client_algo_id"]),
                    take_profit_algo_id=int(owner["take_profit_algo_id"]),
                    take_profit_client_algo_id=str(owner["take_profit_client_algo_id"]),
                )
                observed = await self.read_back_algo_protection(
                    bracket,
                    entry_client_order_id=str(owner["entry_client_order_id"]),
                )
            except Exception:
                observed = ProtectionResult(False, "AMBIGUOUS", ("owner_protection_identity_invalid",))
            if not observed.protected:
                await self._degrade_local_mainnet_protection(authority)
                actions["failed_action_count"] += 1
                if observed.state in {"UNPROTECTED", "AMBIGUOUS"}:
                    intent, order = self._reconstruct_intent_and_order_from_record(owner)
                    try:
                        if await self._local_mainnet_close_only_once(
                            intent, order, owner,
                            reason="PILOT_PROTECTION_UNVERIFIED",
                            authority=authority,
                        ):
                            actions["closed_records"].append(owner.get("entry_client_order_id"))
                        else:
                            actions["failed_action_count"] += 1
                    except Exception as exc:
                        logger.error("Pilot unprotected close unresolved: %s", type(exc).__name__)
                        actions["failed_action_count"] += 1
                return actions
            actions["protection_verified_count"] = actions.get("protection_verified_count", 0) + 1

        quick_expired = any(self.evaluate_quick_hold_timeout({
            **owner, "quick_max_hold_seconds": session.get("pilot_quick_max_hold_seconds"),
        }) for owner in active)
        accounting_verified = not active

        # Account updates are event-driven and do not guarantee a fresh mark
        # on every heartbeat. Read the signed position snapshot directly so
        # an open pilot position cannot rely on an old unrealized-PnL mark.
        # This is GET-only; any missing, duplicate, or unowned exposure fails
        # closed before accounting can be used for a drawdown decision.
        try:
            request_started_at_ms = int(time.time() * 1000)
            request_started_monotonic = time.monotonic()
            positions = await self.rest_client.request(
                "GET", self._position_risk_path, signed=True,
            )
            # Binance's position updateTime describes the latest position
            # update, not the generation time of the unrealized-PnL snapshot.
            # Use request-start as a conservative timestamp and bound the
            # request window instead of making an unsupported source-time claim.
            observed_at_ms = _local_pilot_position_mark_time_ms(
                request_started_at_ms,
                request_started_monotonic,
                time.monotonic(),
            )
            if not isinstance(positions, list) or any(
                not isinstance(row, dict) for row in positions
            ):
                raise ValueError("position-risk snapshot is not a complete list")
            symbol_positions = [
                row for row in positions
                if str(row.get("symbol", "")).strip().upper() == MAINNET_RISK_POLICY.symbol
                and str(row.get("positionSide", "BOTH")).strip().upper() == "BOTH"
            ]
            if len(symbol_positions) != 1:
                raise ValueError("pilot symbol position identity is not unique")
            position = symbol_positions[0]
            position_amount = Decimal(str(position["positionAmt"]))
            unrealized_pnl = Decimal(str(position["unRealizedProfit"]))
            if not position_amount.is_finite() or not unrealized_pnl.is_finite():
                raise ValueError("pilot position snapshot contains non-finite values")
            expected_amount = sum(
                (
                    self._decimal_value(owner.get("filled_quantity"), positive=True)
                    * (Decimal(1) if str(owner.get("entry_side") or owner.get("side") or "").upper() == "BUY" else Decimal(-1))
                    for owner in active
                ),
                Decimal(0),
            )
            if position_amount != expected_amount:
                raise ValueError("exchange position differs from durable pilot owners")
            last_mark_mono = getattr(authority, "_last_local_live_pilot_mark_monotonic", None)
            if (
                not isinstance(last_mark_mono, (int, float))
                or time.monotonic() - last_mark_mono >= 4
            ):
                mark_writer: Any = getattr(authority, "_persist_local_live_pilot_mark", None)
                if not callable(mark_writer) or not await cast(Any, mark_writer(
                    MAINNET_RISK_POLICY.symbol,
                    f"{observed_at_ms}:ETHUSDC:PILOT_MONITOR",
                    unrealized_pnl,
                    observed_at_ms,
                    snapshot_scope="ETHUSDC_SIGNED_POSITION_RISK_REQUEST_WINDOW_START",
                )):
                    raise ValueError("fresh pilot position mark was not durably recorded")
                authority._last_local_live_pilot_mark_monotonic = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - signed exchange reads must fail closed
            logger.error("Pilot signed position mark unavailable: %s", type(exc).__name__)
            await self._degrade_local_mainnet_protection(authority)
            actions["failed_action_count"] += 1
            if not quick_expired:
                return actions

        accounting_reader: Any = getattr(authority, "get_local_live_pilot_accounting", None)
        if active:
            try:
                accounting = await cast(Any, accounting_reader()) if callable(accounting_reader) else None
            except Exception:
                accounting = None
            if (
                not isinstance(accounting, Mapping)
                or accounting.get("status") != "VERIFIED"
                or accounting.get("campaign_id") != campaign_id
                or accounting.get("launch_id") != launch_id
            ):
                await self._degrade_local_mainnet_protection(authority)
                actions["failed_action_count"] += 1
                if not quick_expired:
                    return actions
            else:
                accounting_verified = True
                session["pilot_net_pnl_usdc"] = accounting["net_pnl_usdc"]
                session["pilot_peak_pnl_usdc"] = accounting["peak_net_pnl_usdc"]

        # Drawdown action is evaluated only after every active symbol owner is
        # proven to belong to this durable pilot campaign.
        current_net = Decimal(str(session.get("pilot_net_pnl_usdc") or "0")) if accounting_verified else Decimal(0)
        dd_breached = session.get("pilot_drawdown_triggered") is True or (
            accounting_verified and self.evaluate_pilot_drawdown(session, current_net)
        )
        if dd_breached and not session.get("pilot_drawdown_triggered"):
            trigger_func: Any = getattr(persistence, "trigger_pilot_drawdown", None)
            if callable(trigger_func):
                try:
                    transitioned = await cast(Any, trigger_func(
                        launch_id, reason="PILOT_DRAWDOWN_LIMIT_REACHED"
                    ))
                except Exception:
                    transitioned = None
                    await self._degrade_local_mainnet_protection(authority)
                if isinstance(transitioned, Mapping) and transitioned:
                    setattr(authority, "_mainnet_launch_session", dict(transitioned))
                    session = getattr(authority, "_mainnet_launch_session", None) or {}
                else:
                    actions["failed_action_count"] += 1
            else:
                actions["failed_action_count"] += 1
            actions["drawdown_triggered"] = True

        for record in active:
            policy_bound_record = {
                **record,
                "quick_max_hold_seconds": session.get("pilot_quick_max_hold_seconds"),
            }
            is_expired = self.evaluate_quick_hold_timeout(policy_bound_record)
            should_close = dd_breached or is_expired
            close_reason = "PILOT_DRAWDOWN_LIMIT_REACHED" if dd_breached else "QUICK_MAX_HOLD_EXPIRED"
            if is_expired:
                actions["quick_expired_count"] += 1
            if should_close:
                if is_expired and not dd_breached and session.get("pilot_status") != "CLOSE_ONLY":
                    transition: Any = getattr(persistence, "enter_local_live_pilot_close_only", None)
                    if callable(transition):
                        try:
                            transitioned = await cast(Any, transition(
                                launch_id, reason="QUICK_MAX_HOLD_EXPIRED"
                            ))
                            if isinstance(transitioned, Mapping) and transitioned:
                                setattr(authority, "_mainnet_launch_session", dict(transitioned))
                                session = getattr(authority, "_mainnet_launch_session", None) or {}
                        except Exception as exc:
                            logger.error(
                                "QUICK expiry could not persist CLOSE_ONLY: %s",
                                type(exc).__name__,
                            )
                            actions["failed_action_count"] += 1
                            await self._degrade_local_mainnet_protection(authority)
                record = await self._cancel_and_read_back_pilot_entry(
                    record, authority=authority
                )
                if record is None:
                    actions["failed_action_count"] += 1
                    continue
                if Decimal(str(record.get("filled_quantity") or "0")) == 0:
                    actions["closed_records"].append(record.get("entry_client_order_id"))
                    continue
                intent, order = self._reconstruct_intent_and_order_from_record(record)
                try:
                    closed = await self._local_mainnet_close_only_once(
                        intent, order, record, reason=close_reason, authority=authority
                    )
                    if closed:
                        actions["closed_records"].append(record.get("entry_client_order_id"))
                    else:
                        actions["failed_action_count"] += 1
                except Exception as exc:
                    actions["failed_action_count"] += 1
                    logger.error(
                        "Pilot emergency close failed for %s: %s",
                        record.get("entry_client_order_id"),
                        type(exc).__name__,
                    )
        return actions

    async def _protect_local_mainnet_entry(
        self,
        intent: OrderIntent,
        order: ExecutionOrder,
        response: Dict[str, Any],
        *,
        authority: Any,
    ) -> bool:
        """Persist the first fill, protect its exact size within 5s, else close once."""
        if (
            self.env != BinanceEnvironment.MAINNET
            or not self._is_local_mainnet_runtime()
            or not callable(self.on_local_mainnet_protection_update)
            or str(getattr(intent.position_side, "value", intent.position_side)).upper() != "BOTH"
        ):
            await self._degrade_local_mainnet_protection(self._worker_authority)
            return False
        symbol = order.symbol.upper()
        record = self._local_mainnet_protection_record(intent, order)
        deadline = time.monotonic() + 5.0
        submission_deadline = getattr(self, "_local_mainnet_entry_deadlines", {}).get(order.client_order_id)
        if isinstance(submission_deadline, (int, float)) and math.isfinite(submission_deadline):
            deadline = min(deadline, submission_deadline)

        def tighten_deadline(fill_times: List[int]) -> None:
            nonlocal deadline
            if not fill_times:
                return
            elapsed = time.time() - min(fill_times) / 1000
            if not math.isfinite(elapsed) or elapsed < 0:
                raise TimeoutError("first-fill timestamp is in the future or unverifiable")
            deadline = min(deadline, time.monotonic() + max(0.0, 5.0 - elapsed))
            if deadline <= time.monotonic():
                raise TimeoutError("post-first-fill 5-second deadline elapsed")

        async def within_deadline(operation: Any) -> Any:
            # Include cancellation, fill recovery and durable writes, not only
            # bracket placement, in the protection budget. Cancellation of an
            # exchange request never proves its execution outcome.
            return await asyncio.wait_for(
                operation, timeout=max(0.0, deadline - time.monotonic()),
            )

        first_fill_observed = False
        failure_reason = "protection_not_confirmed"
        try:
            response_executed = Decimal(str(response.get("executedQty", "0")))
            first_fill_observed = response_executed.is_finite() and response_executed > 0
        except (InvalidOperation, TypeError, ValueError):
            pass
        try:
            final_order = response
            # The live ledger is an in-memory cache, including when restored
            # from PostgreSQL. Inspect known fill times synchronously before
            # the first await; never grant a fresh budget to an old fill.
            cached_fills = [
                fill for fill in getattr(self.ledger, "fills", ())
                if str(getattr(fill, "client_order_id", "")) == order.client_order_id
                and str(getattr(fill, "symbol", "")).upper() == symbol
            ]
            known_times = self._extract_fill_event_times(cached_fills)
            if len(known_times) != len(cached_fills):
                raise RuntimeError("cached first-fill timestamp is unavailable")
            if cached_fills:
                first_fill_observed = True
                cached_quantity = sum((Decimal(str(fill.quantity)) for fill in cached_fills), Decimal(0))
                if cached_quantity > 0 and cached_quantity <= order.quantity:
                    record.update(
                        filled_quantity=cached_quantity,
                        entry_average_price=sum((
                            Decimal(str(fill.quantity)) * Decimal(str(fill.price))
                            for fill in cached_fills
                        ), Decimal(0)) / cached_quantity,
                        first_fill_at=datetime.fromtimestamp(min(known_times) / 1000, tz=timezone.utc),
                    )
            # Order creation time is a conservative bound preceding any fill;
            # updateTime is the latest update and cannot bound the first fill.
            if first_fill_observed and response.get("time") is not None:
                created_ms = int(response["time"])
                if created_ms <= 0:
                    raise ValueError("entry creation timestamp is invalid")
                known_times.append(created_ms)
            tighten_deadline(known_times)
            if first_fill_observed and not known_times and submission_deadline is None:
                raise RuntimeError("first-fill deadline cannot be established")
            existing_fills = await within_deadline(self.ledger.get_fills())
            tighten_deadline(self._extract_fill_event_times(
                fill for fill in existing_fills
                if str(getattr(fill, "client_order_id", "")) == order.client_order_id
                and str(getattr(fill, "symbol", "")).upper() == symbol
            ))
            terminal = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
            if str(order.status).upper() not in terminal:
                record["state_reason"] = self._local_recovery_reason(
                    record, entry_cancel="ATTEMPTED_UNKNOWN",
                )
                if not await within_deadline(self._persist_local_mainnet_protection(record)):
                    raise RuntimeError("entry cancellation attempt was not durably recorded")
                final_order = await within_deadline(self._cancel_testnet_entry_for_protection(
                    symbol, order.client_order_id
                )) or {}
                if str(final_order.get("status") or "").upper() not in terminal:
                    raise RuntimeError("entry remainder cancellation is unverified")
                record["state_reason"] = self._local_recovery_reason(record, entry_cancel="CONFIRMED")
                order.status = str(final_order["status"]).upper()
                await within_deadline(self.ledger.upsert_order(order))
            executed_qty = Decimal(str(final_order.get("executedQty", response.get("executedQty"))))
            if not executed_qty.is_finite() or executed_qty < 0:
                raise ValueError("entry executed quantity is invalid")
            if executed_qty == 0:
                terminal_status = str(final_order.get("status") or "").upper()
                exchange_order_id = str(final_order.get("orderId") or "").strip()
                response_client_id = str(
                    final_order.get("clientOrderId") or ""
                )
                original_qty = Decimal(str(final_order.get("origQty")))
                persisted_fills = [
                    fill for fill in await within_deadline(self.ledger.get_fills())
                    if str(getattr(fill, "client_order_id", "")) == order.client_order_id
                    or str(getattr(fill, "exchange_order_id", "")) == exchange_order_id
                ]
                if (
                    first_fill_observed
                    or persisted_fills
                    or terminal_status not in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
                    or not exchange_order_id.isdigit()
                    or int(exchange_order_id) <= 0
                    or response_client_id != order.client_order_id
                    or str(final_order.get("symbol") or "").upper() != symbol
                    or str(final_order.get("side") or "").upper()
                    != str(getattr(order.side, "value", order.side)).upper()
                    or str(final_order.get("positionSide") or "BOTH").upper()
                    != str(getattr(order.position_side, "value", order.position_side)).upper()
                    or not original_qty.is_finite()
                    or original_qty <= 0
                    or original_qty != order.quantity
                ):
                    raise RuntimeError("zero-fill entry lacks an exact terminal exchange order proof")
                order.exchange_order_id = exchange_order_id
                order.status = terminal_status
                await within_deadline(self.ledger.upsert_order(order))
                verified_at = utc_now()
                unfilled_proof = {
                    "order_id": exchange_order_id,
                    "client_order_id": order.client_order_id,
                    "order_status": terminal_status,
                    "executed_quantity": "0",
                    "original_quantity": str(original_qty),
                    "symbol": symbol,
                    "entry_side": str(getattr(order.side, "value", order.side)).upper(),
                    "position_side": str(
                        getattr(order.position_side, "value", order.position_side)
                    ).upper(),
                    "verified_at": verified_at,
                }
                record.update(
                    state="CLOSED",
                    state_reason=(
                        f"unfilled_entry_order_id={exchange_order_id};"
                        f"unfilled_entry_status={terminal_status};"
                        "unfilled_entry_executed_qty=0"
                    ),
                    closed_at=verified_at,
                    unfilled_order_proof=unfilled_proof,
                )
                if not await within_deadline(self._persist_local_mainnet_protection(record)):
                    raise RuntimeError("unfilled entry owner could not be closed durably")
                return True
            # A positive signed executedQty is sufficient to initiate a
            # close-only fallback when userTrades or fill timestamps are
            # unavailable; it is not sufficient to mark the owner CLOSED.
            first_fill_observed = True

            # The deadline starts at first fill, not at response or function entry.
            await within_deadline(self.reconciliation._recover_order_fills(order, final_order))
            fills = [
                fill for fill in await within_deadline(self.ledger.get_fills())
                if fill.client_order_id == order.client_order_id
                and str(fill.symbol).upper() == symbol
            ]
            fill_times = self._extract_fill_event_times(fills)
            if not fills or len(fill_times) != len(fills):
                raise RuntimeError("durable first-fill timestamp is unavailable")
            tighten_deadline(fill_times)
            filled_quantity = sum(
                (Decimal(str(fill.quantity)) for fill in fills), Decimal("0")
            )
            if filled_quantity != executed_qty or filled_quantity <= 0:
                raise RuntimeError("durable fill quantity does not match Binance")
            weighted_entry = sum(
                (
                    Decimal(str(fill.quantity)) * Decimal(str(fill.price))
                    for fill in fills
                ),
                Decimal("0"),
            ) / filled_quantity
            first_fill_ms = min(fill_times)
            record.update(
                filled_quantity=filled_quantity,
                entry_average_price=weighted_entry,
                first_fill_at=datetime.fromtimestamp(first_fill_ms / 1000, tz=timezone.utc),
            )
            if not await within_deadline(self._persist_local_mainnet_protection(record)):
                raise RuntimeError("filled owner was not read back from PostgreSQL")
            tighten_deadline(fill_times)

            exit_side = "SELL" if str(getattr(intent.side, "value", intent.side)).upper() == "BUY" else "BUY"
            stop_client_id = str(record["stop_client_algo_id"])
            target_client_id = str(record["take_profit_client_algo_id"])
            identifiers: Dict[str, int] = {}
            for role, order_type, trigger, client_id in (
                ("stop", "STOP_MARKET", Decimal(str(intent.stop_loss_price)), stop_client_id),
                ("target", "TAKE_PROFIT_MARKET", Decimal(str(intent.take_profit_price)), target_client_id),
            ):
                confirmed = await self._submit_local_mainnet_protection_algo(
                    symbol=symbol,
                    side=exit_side,
                    position_side="BOTH",
                    order_type=order_type,
                    trigger_price=trigger,
                    quantity=filled_quantity,
                    client_algo_id=client_id,
                    authority=authority,
                    deadline=deadline,
                )
                if confirmed is None:
                    raise RuntimeError(f"{role} Algo order was not confirmed")
                algo_id = int(confirmed["algoId"])
                identifiers[role] = algo_id
                record["stop_algo_id" if role == "stop" else "take_profit_algo_id"] = str(algo_id)
                if not await within_deadline(self._persist_local_mainnet_protection(record)):
                    raise RuntimeError(f"{role} Algo owner was not read back from PostgreSQL")

            verify_intent = ProtectionIntent(
                symbol=symbol,
                entry_side=str(getattr(intent.side, "value", intent.side)).upper(),
                position_side="BOTH",
                entry_qty=filled_quantity,
                stop_trigger=Decimal(str(intent.stop_loss_price)),
                take_profit_trigger=Decimal(str(intent.take_profit_price)),
                stop_algo_id=identifiers["stop"],
                stop_client_algo_id=stop_client_id,
                take_profit_algo_id=identifiers["target"],
                take_profit_client_algo_id=target_client_id,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("post-fill bracket verification exceeded 5 seconds")
            protection_result = await asyncio.wait_for(
                self.read_back_algo_protection(
                    verify_intent, entry_client_order_id=order.client_order_id
                ),
                timeout=remaining,
            )
            if not protection_result.protected:
                failure_reason = ",".join(protection_result.reasons) or protection_result.state
                raise RuntimeError("Binance stop/target pair did not verify")
            record.update(
                state="PROTECTED",
                state_reason=None,
                protection_verified_at=utc_now(),
                last_reconciled_at=utc_now(),
            )
            if not await within_deadline(self._persist_local_mainnet_protection(record)):
                raise RuntimeError("PROTECTED state was not acknowledged by PostgreSQL")
            self.last_local_mainnet_protection = {
                "status": "PROTECTED",
                "entry_client_order_id": order.client_order_id,
                "filled_quantity": str(filled_quantity),
                "stop_algo_id": identifiers["stop"],
                "take_profit_algo_id": identifiers["target"],
            }
            risk_evidence = self._last_local_mainnet_risk_evidence
            if (
                isinstance(risk_evidence, dict)
                and risk_evidence.get("client_order_id") == order.client_order_id
                and risk_evidence.get("basket_id") == str(intent.basket_id or "")
                and risk_evidence.get("policy_sha256") == MAINNET_RISK_POLICY_SHA256
            ):
                self._last_local_mainnet_protection_evidence = {
                    "launch_id": risk_evidence.get("launch_id"),
                    "client_order_id": order.client_order_id,
                    "basket_id": str(intent.basket_id or ""),
                    "policy_sha256": MAINNET_RISK_POLICY_SHA256,
                    "observed_monotonic": time.monotonic(),
                }
            return True
        except Exception as exc:
            failure_reason = failure_reason if failure_reason != "protection_not_confirmed" else type(exc).__name__
            logger.error(
                "Local Mainnet post-fill protection failed; starting one close-only flow: %s",
                failure_reason,
            )
            await self._degrade_local_mainnet_protection(self._worker_authority)
            if not first_fill_observed:
                # No fill was proven, but a submitted entry can still be live.
                # Keep an unresolved durable owner; never declare it protected.
                record.update(state="UNKNOWN", state_reason=self._local_recovery_reason(
                    record, cause="entry_or_fill_state_unknown",
                ))
                await self._persist_local_mainnet_protection(record)
                await self._degrade_local_mainnet_protection(self._worker_authority)
                return False
            if Decimal(str(record.get("filled_quantity") or "0")) > 0:
                # Protection has timed out, but the close still needs durable
                # ownership of already observed fills. This is close recovery,
                # never an extension of the Algo placement deadline.
                await self._persist_local_mainnet_protection(record)
            return await self._local_mainnet_close_only_once(
                intent,
                order,
                record,
                reason=failure_reason,
                authority=authority,
            )

    async def _close_testnet_protection_owner(
        self, intent: OrderIntent, order: Optional[ExecutionOrder], reason: str,
    ) -> bool:
        """Close a PENDING owner only when no entry request could have reached Binance."""
        if (
            self.env != BinanceEnvironment.TESTNET
            or order is None
            or not callable(self.on_testnet_protection_update)
        ):
            return False
        record = self._testnet_protection_record(intent, order)
        record.update(state="CLOSED", state_reason=reason[:256])
        try:
            closed = bool(await self.on_testnet_protection_update(record))
        except Exception as exc:
            logger.error(
                "Testnet pre-submit ownership close failed: %s", type(exc).__name__
            )
            closed = False
        if not closed:
            self.state = ConnectionState.DEGRADED
            logger.error("Testnet PENDING protection ownership remains unresolved")
            return False
        self.last_testnet_protection = {
            "status": "CLOSED",
            "entry_client_order_id": order.client_order_id,
            "reason": reason,
        }
        return True

    async def _assert_testnet_entry_position_flat(self, intent: OrderIntent) -> None:
        """Require an authoritative flat baseline before a protected entry.

        This makes the symbol-scoped emergency fallback attributable to the
        entry being attempted; it must not flatten pre-existing Testnet risk.
        """
        try:
            positions = await self.rest_client.request(
                "GET", self._position_risk_path, signed=True,
            )
        except Exception as exc:
            raise LeaseLostError(
                "Testnet entry blocked because its pre-entry position baseline is unavailable"
            ) from exc
        if not isinstance(positions, list):
            raise LeaseLostError(
                "Testnet entry blocked because its pre-entry position baseline is invalid"
            )
        symbol = str(intent.symbol or "").strip().upper()
        position_side = str(
            getattr(intent.position_side, "value", intent.position_side)
        ).strip().upper()
        matching = [
            row for row in positions
            if isinstance(row, dict)
            and str(row.get("symbol", "")).strip().upper() == symbol
            and str(row.get("positionSide", "BOTH")).strip().upper() == position_side
        ]
        if len(matching) != 1:
            raise LeaseLostError(
                "Testnet entry blocked because its pre-entry position identity is not unique"
            )
        try:
            amount = Decimal(str(matching[0]["positionAmt"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise LeaseLostError(
                "Testnet entry blocked because its pre-entry position quantity is invalid"
            ) from exc
        if not amount.is_finite() or amount != 0:
            raise LeaseLostError(
                "Testnet protected entry requires a flat position for this symbol and side"
            )

    async def _submit_testnet_protection_algo(
        self, *, symbol: str, side: str, position_side: str,
        order_type: str, trigger_price: Decimal, client_algo_id: str,
        authority: Any, deadline: float,
        quantity: Optional[Decimal] = None,
    ) -> Optional[Dict[str, Any]]:
        qty = quantity if quantity is not None and quantity > 0 else Decimal("0.1")
        return await self._submit_local_mainnet_protection_algo(
            symbol=symbol, side=side, position_side=position_side,
            order_type=order_type, trigger_price=trigger_price,
            quantity=qty, client_algo_id=client_algo_id,
            authority=authority, deadline=deadline,
        )


    async def _cancel_testnet_entry_for_protection(
        self, symbol: str, client_order_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Issue one cancel and resolve it by read-back without resubmitting."""
        try:
            await self.rest_client.request(
                "DELETE", self._order_path, signed=True,
                params={"symbol": symbol, "origClientOrderId": client_order_id},
            )
        except Exception as exc:
            # The DELETE may have reached Binance. Query the stable order ID;
            # never repeat a cancellation request based on a timeout.
            logger.warning("Testnet entry cancel needs status read-back: %s", type(exc).__name__)
        try:
            result = await self.query_order(symbol, client_order_id)
        except Exception as exc:
            logger.error("Testnet entry cancel status is unknown: %s", type(exc).__name__)
            return None
        if not isinstance(result, dict) or str(result.get("status", "")).upper() not in {
            "FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED",
        }:
            return None
        return result

    async def _protect_testnet_entry(
        self, intent: OrderIntent, order: ExecutionOrder,
        response: Dict[str, Any], *, authority: Any,
    ) -> bool:
        """Cancel any unfilled remainder, protect the filled position, or flatten."""
        if self.env != BinanceEnvironment.TESTNET:
            return False
        symbol = order.symbol.upper()
        entry_status = str(order.status).upper()
        terminal = {"FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
        final_order = response
        protection_ids: list[tuple[str, int]] = []
        protection_client_ids: list[str] = []
        deadline: Optional[float] = None
        failure_reason = "protection_not_confirmed"
        durable_record = self._testnet_protection_record(intent, order)
        try:
            if entry_status not in terminal:
                final_order = await self._cancel_testnet_entry_for_protection(
                    symbol, order.client_order_id,
                ) or {}
                entry_status = str(final_order.get("status", "UNKNOWN")).upper()
                if entry_status not in terminal:
                    raise RuntimeError("entry remainder cancellation is unverified")
                order.status = entry_status
                await self.ledger.upsert_order(order)
            raw_executed_qty = final_order.get("executedQty", response.get("executedQty"))
            if raw_executed_qty in (None, ""):
                raise RuntimeError("entry executed quantity is unavailable")
            executed_qty = Decimal(str(raw_executed_qty))
            if not executed_qty.is_finite() or executed_qty < 0:
                raise RuntimeError("entry executed quantity is invalid")
            if executed_qty == 0:
                durable_record.update(state="CLOSED", state_reason="entry_not_filled")
                writer = self.on_testnet_protection_update
                if not callable(writer) or not await writer(durable_record):
                    raise RuntimeError("unfilled entry ownership could not be closed durably")
                self.last_testnet_protection = {
                    "status": "CLOSED",
                    "entry_client_order_id": order.client_order_id,
                }
                return True
            await self.reconciliation._recover_order_fills(order, final_order)
            fills = [
                fill for fill in await self.ledger.get_fills()
                if fill.client_order_id == order.client_order_id and fill.symbol == symbol
            ]
            event_times = self._extract_fill_event_times(fills)
            if not fills or len(event_times) != len(fills):
                raise RuntimeError("entry fill time is unavailable")
            filled_quantity = sum(
                (Decimal(str(fill.quantity)) for fill in fills), Decimal("0")
            )
            if filled_quantity != executed_qty or filled_quantity <= 0:
                raise RuntimeError("durable entry fills do not match exchange executed quantity")
            average_entry_price = sum(
                (
                    Decimal(str(fill.quantity)) * Decimal(str(fill.price))
                    for fill in fills
                ),
                Decimal("0"),
            ) / filled_quantity
            durable_record.update(
                filled_quantity=filled_quantity,
                entry_average_price=average_entry_price,
                first_fill_at=datetime.fromtimestamp(
                    min(event_times) / 1000, tz=timezone.utc
                ),
            )
            writer = self.on_testnet_protection_update
            if not callable(writer) or not await writer(durable_record):
                raise RuntimeError("filled entry ownership could not be read back from PostgreSQL")
            elapsed = (time.time() * 1000 - min(event_times)) / 1000
            deadline = time.monotonic() + max(0.0, 5.0 - elapsed)
            if deadline <= time.monotonic():
                raise TimeoutError("protection deadline already elapsed")
            side = "SELL" if str(getattr(intent.side, "value", intent.side)).upper() == "BUY" else "BUY"
            position_side = str(getattr(intent.position_side, "value", intent.position_side)).upper()
            stop_client_id, target_client_id = self._testnet_protection_client_ids(
                order.client_order_id
            )
            protection_client_ids.extend((stop_client_id, target_client_id))
            for role, order_type, trigger, client_id in (
                ("stop", "STOP_MARKET", Decimal(str(intent.stop_loss_price)), stop_client_id),
                ("target", "TAKE_PROFIT_MARKET", Decimal(str(intent.take_profit_price)), target_client_id),
            ):
                confirmed = await self._submit_local_mainnet_protection_algo(
                    symbol=symbol, side=side, position_side=position_side,
                    order_type=order_type, trigger_price=trigger,
                    quantity=filled_quantity,
                    client_algo_id=client_id, authority=authority, deadline=deadline,
                )
                if confirmed is None:
                    raise RuntimeError(f"{role} Algo order was not confirmed")
                algo_id_raw = confirmed.get("algoId")
                if algo_id_raw is None:
                    raise RuntimeError(f"{role} Algo order was not confirmed")
                algo_id = int(algo_id_raw)
                protection_ids.append((role, algo_id))
                durable_record[
                    "stop_algo_id" if role == "stop" else "take_profit_algo_id"
                ] = str(algo_id)
                if not await writer(durable_record):
                    raise RuntimeError(f"{role} Algo ownership could not be read back from PostgreSQL")
            verification_intent = ProtectionIntent(
                symbol=symbol,
                entry_side=str(getattr(intent.side, "value", intent.side)).upper(),
                position_side=position_side,
                entry_qty=order.quantity,
                stop_trigger=Decimal(str(intent.stop_loss_price)),
                take_profit_trigger=Decimal(str(intent.take_profit_price)),
                stop_algo_id=next(value for role, value in protection_ids if role == "stop"),
                stop_client_algo_id=stop_client_id,
                take_profit_algo_id=next(value for role, value in protection_ids if role == "target"),
                take_profit_client_algo_id=target_client_id,
            )
            result = await self.read_back_algo_protection(
                verification_intent, entry_client_order_id=order.client_order_id,
            )
            if result.state != "PROTECTED":
                failure_reason = ",".join(result.reasons) or result.state
                raise RuntimeError("exchange did not confirm the complete protection pair")
            durable_record.update(
                state="PROTECTED",
                state_reason=None,
                protection_verified_at=datetime.now(timezone.utc),
                last_reconciled_at=datetime.now(timezone.utc),
            )
            if not await writer(durable_record):
                raise RuntimeError("protected lifecycle state could not be read back from PostgreSQL")
            self.last_testnet_protection = {
                "status": "PROTECTED",
                "entry_client_order_id": order.client_order_id,
                "stop_algo_id": verification_intent.stop_algo_id,
                "stop_client_algo_id": stop_client_id,
                "take_profit_algo_id": verification_intent.take_profit_algo_id,
                "take_profit_client_algo_id": target_client_id,
            }
            return True
        except Exception as exc:
            failure_reason = type(exc).__name__
            logger.error(
                "Testnet position protection failed; starting reduce-only flatten: %s",
                type(exc).__name__,
            )
        if entry_status not in terminal:
            try:
                await self._cancel_testnet_entry_for_protection(
                    symbol, order.client_order_id,
                )
            except Exception:
                pass
        await self._emergency_flatten(
            symbol,
            authority=authority,
            only_position_side=PositionSide(
                str(getattr(intent.position_side, "value", intent.position_side)).upper()
            ),
        )
        cleanup_confirmed = self.last_emergency_result.get("status") == "CONFIRMED"
        for client_id in protection_client_ids:
            try:
                result = await self._query_algo_order(symbol=symbol, client_algo_id=client_id)
                if result is None:
                    cleanup_confirmed = False
                    continue
                if str(result.get("algoStatus", "")).upper() == "NEW":
                    try:
                        await self.rest_client.request(
                            "DELETE", self._algo_order_path, signed=True,
                            params={"symbol": symbol, "algoId": int(result["algoId"])},
                        )
                    except Exception:
                        # Cancellation outcome may be ambiguous; query below
                        # exactly once and do not repeat the DELETE request.
                        pass
                    result = await self._query_algo_order(
                        symbol=symbol, client_algo_id=client_id,
                    )
                    if result is None or str(result.get("algoStatus", "")).upper() == "NEW":
                        cleanup_confirmed = False
            except Exception:
                cleanup_confirmed = False
        try:
            entry_after = await self.query_order(symbol, order.client_order_id)
            if entry_after is None or str(entry_after.get("status", "")).upper() in {
                "NEW", "PARTIALLY_FILLED", "UNKNOWN",
            }:
                cleanup_confirmed = False
            open_algos_after = await self.rest_client.request(
                "GET", self._open_algo_orders_path, signed=True,
                params={"symbol": symbol, "algoType": "CONDITIONAL"},
            )
            if not isinstance(open_algos_after, list) or any(
                isinstance(row, dict)
                and str(row.get("clientAlgoId", "")) in protection_client_ids
                for row in open_algos_after
            ):
                cleanup_confirmed = False
            positions = await self.rest_client.request("GET", self._position_risk_path, signed=True)
            remaining = [
                row for row in positions if isinstance(row, dict)
                and row.get("symbol") == symbol
                and row.get("positionSide", "BOTH") == str(getattr(intent.position_side, "value", intent.position_side)).upper()
                and Decimal(str(row.get("positionAmt", "0"))) != 0
            ]
            if remaining:
                cleanup_confirmed = False
        except Exception:
            cleanup_confirmed = False
        self.state = ConnectionState.DEGRADED
        self.reconciliation.last_status = "UNKNOWN"
        self.last_testnet_protection = {
            "status": "FAILED_CLOSED",
            "entry_client_order_id": order.client_order_id,
            "reason": failure_reason,
            "cleanup_verified": cleanup_confirmed,
            "emergency_result": dict(self.last_emergency_result),
        }
        writer = self.on_testnet_protection_update
        if callable(writer):
            durable_record.update(
                state="CLOSED" if cleanup_confirmed else "DEGRADED",
                state_reason=(
                    "protection_failed_and_flatten_verified"
                    if cleanup_confirmed
                    else failure_reason[:256]
                ),
            )
            try:
                if not await writer(durable_record):
                    logger.error("Testnet protection terminal state was not durably acknowledged")
            except Exception as exc:
                logger.error("Testnet protection terminal persistence failed: %s", type(exc).__name__)
        logger.error(
            "Testnet protected entry stopped reason=%s cleanup_verified=%s",
            failure_reason, cleanup_confirmed,
        )
        return False

    async def cancel_all_open_orders(
        self,
        *,
        authority: Optional[object] = None,
    ) -> Dict[str, Any]:
        """Cancel and verify all authoritative Binance open orders.

        This is the kill-switch mutation path. It shares the adapter's
        single-flight lock with submit, cancel, amend, and emergency flatten so
        a queued normal mutation cannot overtake local kill-switch activation.
        """

        if self.preflight_only:
            return {
                "status": "BLOCKED",
                "reason": "Read-only preflight adapter cannot cancel orders",
            }

        if not self._worker_authorized(authority):
            return {
                "status": "UNKNOWN",
                "reason": f"Worker authority is required for {self.environment_label} cancellation",
            }
        async with self._mutation_scope():
            try:
                open_orders = await self.rest_client.request(
                    "GET", self._open_orders_path, signed=True
                )
                if not isinstance(open_orders, list):
                    return {
                        "status": "UNKNOWN",
                        "reason": "Authoritative openOrders response is invalid",
                    }

                cancel_failures = 0
                for order in open_orders:
                    symbol = str(order.get("symbol", "")).upper()
                    order_id = order.get("orderId")
                    if not symbol or order_id in (None, ""):
                        cancel_failures += 1
                        continue
                    try:
                        response = await self.rest_client.request(
                            "DELETE",
                            self._order_path,
                            signed=True,
                            params={"symbol": symbol, "orderId": order_id},
                        )
                        if not isinstance(response, dict) or str(
                            response.get("status", "")
                        ).upper() not in {"CANCELED", "CANCELLED"}:
                            cancel_failures += 1
                    except BinanceAuthenticationError:
                        self.invalidate_authentication()
                        return {
                            "status": "UNKNOWN",
                            "reason": f"{self.environment_label} authentication failed; exchange cancellation is unknown",
                        }
                    except Exception as exc:
                        logger.error("%s kill-switch cancellation failed: %s", self.environment_label, exc)
                        cancel_failures += 1

                remaining = await self.rest_client.request(
                    "GET", self._open_orders_path, signed=True
                )
                if not isinstance(remaining, list):
                    return {
                        "status": "UNKNOWN",
                        "reason": "Open-order verification response is invalid",
                    }
                if remaining or cancel_failures:
                    return {
                        "status": "PARTIAL",
                        "remaining_orders": len(remaining),
                        "cancel_failures": cancel_failures,
                    }
                return {"status": "CONFIRMED", "remaining_orders": 0}
            except BinanceAuthenticationError:
                self.invalidate_authentication()
                return {
                    "status": "UNKNOWN",
                    "reason": f"{self.environment_label} authentication failed; exchange cancellation is unknown",
                }
            except Exception as exc:
                logger.error("%s kill-switch exchange cancellation is unknown: %s", self.environment_label, exc)
                return {
                    "status": "UNKNOWN",
                    "reason": "Exchange cancellation could not be verified",
                }

    async def cancel_order(
        self,
        symbol: str,
        orig_client_order_id: str,
        *,
        authority: Optional[object] = None,
        allow_emergency_fallback: bool = False,
    ) -> bool:
        if self.preflight_only:
            logger.error("Blocked %s cancellation from a read-only preflight adapter", self.environment_label)
            return False
        if not self._worker_authorized(authority):
            logger.error("Blocked direct %s cancel outside the Trading Worker", self.environment_label)
            return False
        async with self._mutation_scope():
            if not allow_emergency_fallback and getattr(authority, "kill_switch_active", False):
                logger.warning("Kill switch blocked %s cancel mutation", self.environment_label)
                return False
            return await self._cancel_order(
                symbol, orig_client_order_id, authority=authority,
                allow_emergency_fallback=allow_emergency_fallback,
            )

    async def _cancel_order(
        self,
        symbol: str,
        orig_client_order_id: str,
        *,
        authority: Optional[object] = None,
        allow_emergency_fallback: bool = False,
    ) -> bool:
        if self.preflight_only:
            logger.error("Blocked %s internal cancellation from a read-only preflight adapter", self.environment_label)
            return False
        if not self._worker_authorized(authority):
            logger.error("Blocked direct %s cancel outside the Trading Worker", self.environment_label)
            return False
        if self.state != ConnectionState.READY and not allow_emergency_fallback:
            return False
        try:
            async def before_cancel_send() -> None:
                if not self._worker_authorized(authority):
                    raise LeaseLostError("Worker authority closed during entry cancellation")
                await self._assert_execution_lease(EconomicRiskClass.EMERGENCY)

            response = await self.rest_client.request(
                "DELETE",
                self._order_path,
                signed=True,
                params={"symbol": symbol.upper(), "origClientOrderId": orig_client_order_id},
                **({"before_mutation": before_cancel_send} if allow_emergency_fallback else {}),
            )
            if not isinstance(response, dict) or response.get("status") != "CANCELED":
                resolved = await self._resolve_ambiguous_cancel(symbol, orig_client_order_id)
                return bool(
                    self.state == ConnectionState.READY
                    and resolved is not None
                    and resolved.get("status") in {"CANCELED", "CANCELLED"}
                )
            response_client_id = response.get("clientOrderId")
            response_order_id = response.get("orderId")
            if response_client_id not in (None, "", orig_client_order_id):
                resolved = await self._resolve_ambiguous_cancel(symbol, orig_client_order_id)
                return bool(
                    self.state == ConnectionState.READY
                    and resolved is not None
                    and resolved.get("status") in {"CANCELED", "CANCELLED"}
                )
            if response_client_id in (None, "") and response_order_id in (None, ""):
                resolved = await self._resolve_ambiguous_cancel(symbol, orig_client_order_id)
                return bool(
                    self.state == ConnectionState.READY
                    and resolved is not None
                    and resolved.get("status") in {"CANCELED", "CANCELLED"}
                )
            order = await self.ledger.get_order_by_client_id(orig_client_order_id)
            if order:
                order.status = "CANCELED"
                await self.ledger.upsert_order(order)
            self.reconciliation.last_status = "UNKNOWN"
            try:
                sync_result = await self.reconciliation.reconcile()
            except Exception as exc:
                logger.error("Post-cancel %s reconciliation failed: %s", self.environment_label, exc)
                sync_result = "UNKNOWN"
            verified = bool(
                sync_result == "IN_SYNC"
                and self.private_stream_healthy
                and self.authenticated
            )
            self.state = ConnectionState.READY if verified else ConnectionState.DEGRADED
            return verified
        except BinanceAuthenticationError:
            self.invalidate_authentication()
            return False
        except BinanceDefinitiveRejection as exc:
            logger.warning("Cancel was rejected; resolving authoritative order status: %s", exc)
            resolved = await self._resolve_ambiguous_cancel(symbol, orig_client_order_id)
            return bool(
                self.state == ConnectionState.READY
                and resolved is not None
                and resolved.get("status") in {"CANCELED", "CANCELLED"}
            )
        except (BinanceRateLimitError, BinanceTimestampError) as exc:
            logger.error("%s cancel was not submitted: %s", self.environment_label, exc)
            self.state = ConnectionState.DEGRADED
            return False
        except BinanceTransportAmbiguity as exc:
            logger.error("Cancel request is not verified: %s", exc)
            resolved = await self._resolve_ambiguous_cancel(symbol, orig_client_order_id)
            return bool(
                self.state == ConnectionState.READY
                and resolved is not None
                and resolved.get("status") in {"CANCELED", "CANCELLED"}
            )
        except Exception as exc:
            logger.error("Unexpected cancel failure: %s", exc)
            self.state = ConnectionState.DEGRADED
            return False

    async def modify_order(
        self,
        symbol: str,
        orig_client_order_id: str,
        new_price: Decimal,
        new_qty: Decimal,
        side: str,
        *,
        authority: Optional[object] = None,
    ) -> Optional[ExecutionOrder]:
        if self.preflight_only:
            logger.error("Blocked %s amendment from a read-only preflight adapter", self.environment_label)
            return None
        if not self._worker_authorized(authority):
            logger.error("Blocked direct %s amendment outside the Trading Worker", self.environment_label)
            return None
        async with self._mutation_scope():
            if getattr(authority, "kill_switch_active", False):
                logger.warning("Kill switch blocked %s amendment mutation", self.environment_label)
                return None
            return await self._modify_order(
                symbol,
                orig_client_order_id,
                new_price,
                new_qty,
                side,
                authority=authority,
            )

    async def _modify_order(
        self,
        symbol: str,
        orig_client_order_id: str,
        new_price: Decimal,
        new_qty: Decimal,
        side: str,
        *,
        authority: Optional[object] = None,
    ) -> Optional[ExecutionOrder]:
        if self.preflight_only:
            logger.error("Blocked %s internal amendment from a read-only preflight adapter", self.environment_label)
            return None
        if not self._worker_authorized(authority):
            logger.error(
                "Blocked direct %s amendment outside the Trading Worker",
                self.environment_label,
            )
            return None
        if self.state != ConnectionState.READY:
            return None
        existing = await self.ledger.get_order_by_client_id(orig_client_order_id)
        if existing is None or str(existing.status).upper() not in {"NEW", "PARTIALLY_FILLED"}:
            return None
        try:
            amendment_side = OrderSide(side)
        except (TypeError, ValueError):
            logger.warning("Order amendment has an invalid side: %s", side)
            return None
        if amendment_side != existing.side:
            logger.warning(
                "Order amendment cannot change side for %s: %s -> %s",
                orig_client_order_id,
                existing.side,
                amendment_side,
            )
            return None
        try:
            requested_qty = Decimal(str(new_qty))
            requested_price = Decimal(str(new_price))
            existing_qty = Decimal(str(existing.quantity))
            existing_price = Decimal(str(existing.price))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if (
            not requested_qty.is_finite()
            or not requested_price.is_finite()
            or requested_qty <= 0
            or requested_price <= 0
            or not existing_qty.is_finite()
            or not existing_price.is_finite()
            or existing_qty <= 0
            or existing_price <= 0
        ):
            return None

        # An amendment can increase economic exposure even though it is not a
        # new client order.  Classify the delta from the current exchange
        # working order so the same account/liquidation/cap gates apply.
        existing_notional = existing_qty * existing_price
        requested_notional = requested_qty * requested_price
        amendment_risk = (
            EconomicRiskClass.INCREASE_RISK
            if requested_notional > existing_notional
            else EconomicRiskClass.RECOVERY
        )
        if (
            self.require_testnet_protection
            and self.env == BinanceEnvironment.TESTNET
            and amendment_risk == EconomicRiskClass.INCREASE_RISK
        ):
            logger.error(
                "Blocked Testnet order amendment that increases risk outside the protected entry lifecycle"
            )
            return None
        if (
            self.env == BinanceEnvironment.TESTNET
            and amendment_risk == EconomicRiskClass.INCREASE_RISK
        ):
            logger.error(
                "Blocked Testnet order amendment that increases risk outside the protected entry lifecycle"
            )
            return None
        intent = OrderIntent(
            client_order_id=orig_client_order_id,
            symbol=symbol.upper(),
            market_type=existing.market_type,
            side=amendment_side,
            position_side=existing.position_side,
            order_type=OrderType.LIMIT,
            time_in_force=existing.time_in_force,
            quantity=new_qty,
            price=new_price,
            reduce_only=existing.reduce_only,
            strategy_id=existing.strategy_id,
            source_intent_ids=list(existing.source_intent_ids),
        )
        gate_result = await self.order_gate.check(
            intent,
            amendment_risk,
            exclude_client_order_id=orig_client_order_id,
            # A lower-notional amendment reduces resting order risk but is not
            # a position-closing order. Preserve reduceOnly for genuine
            # position reductions while retaining entry-order semantics here.
            require_reduce_only_for_risk_reduction=existing.reduce_only,
        )
        if not gate_result.allowed or gate_result.prepared is None:
            logger.warning("Order amendment blocked: %s", gate_result.reason)
            return None
        prepared = gate_result.prepared
        amendment_decision = ExecutionDecision(
            decision_id=existing.decision_id or f"AMEND-{orig_client_order_id}",
            symbol=prepared.symbol,
            action="AMEND_ORDER",
            risk_class=amendment_risk,
            orders=[intent],
            target_exposure_id=existing.target_exposure_id,
            source_intent_ids=list(existing.source_intent_ids),
        )
        worker_gate = getattr(self._worker_authority, "_evaluate_execution_gate", None)
        if not callable(worker_gate):
            logger.error("Blocked amendment because the Worker decision gate is unavailable")
            return None
        decision_allowed, decision_reason = cast(Tuple[bool, str], worker_gate(amendment_decision))
        if not decision_allowed:
            logger.warning("Worker decision gate blocked amendment: %s", decision_reason)
            return None
        try:
            params: Dict[str, Any] = {
                "symbol": prepared.symbol,
                "origClientOrderId": orig_client_order_id,
                "side": amendment_side.value,
                "quantity": str(prepared.quantity),
                "price": str(prepared.price),
                "timeInForce": (
                    "GTX"
                    if intent.time_in_force == TimeInForce.POST_ONLY
                    else intent.time_in_force.value
                ),
            }
            if self.capabilities.hedge_mode:
                params["positionSide"] = intent.position_side.value
            if intent.reduce_only and not self.capabilities.hedge_mode:
                params["reduceOnly"] = "true"
            await self._assert_execution_lease(amendment_risk)
            response = await self.rest_client.request(
                "PUT",
                self._order_path,
                signed=True,
                params=params,
                before_mutation=(
                    (
                        lambda: self._final_risk_increase_fence(
                            amendment_decision,
                            intent,
                            prepared,
                            client_order_id=orig_client_order_id,
                            reserved_open_orders=0,
                            reserved_notional=Decimal("0"),
                            allow_emergency_fallback=False,
                        )
                    )
                    if amendment_risk == EconomicRiskClass.INCREASE_RISK
                    else None
                ),
            )
            if not isinstance(response, dict):
                raise BinanceTransportAmbiguity("Binance amendment response is invalid")
            amended = self._order_from_response(
                intent, response, prepared, orig_client_order_id, amendment_decision
            )
            await self.ledger.upsert_order(amended)
            return amended if await self._post_mutation_reconcile(amended, response) else None
        except BinanceAuthenticationError:
            self.invalidate_authentication()
            return None
        except BinanceDefinitiveRejection as exc:
            logger.warning("Order amendment was rejected definitively: %s", exc)
            return None
        except (BinanceRateLimitError, BinanceTimestampError) as exc:
            logger.error("%s amendment was not submitted: %s", self.environment_label, exc)
            return None
        except BinanceTransportAmbiguity as exc:
            logger.error("%s amendment response is ambiguous: %s", self.environment_label, exc)
            return await self._resolve_ambiguous_order(
                intent, prepared, orig_client_order_id, amendment_decision
            )
        except LeaseLostError as exc:
            self.state = ConnectionState.DEGRADED
            logger.error("Order amendment fenced before submission: %s", exc)
            return None
        except Exception as exc:
            logger.error("Order amendment is not verified: %s", exc)
            self.state = ConnectionState.DEGRADED
            return None

    async def _resolve_ambiguous_cancel(
        self, symbol: str, client_order_id: str
    ) -> Optional[Dict[str, Any]]:
        """Resolve cancel status by client ID, then require authoritative reconciliation."""
        self.state = ConnectionState.RECONCILING
        order_status: Optional[Dict[str, Any]] = None
        status_known = False
        try:
            order_status = await self.query_order(symbol, client_order_id)
            status_known = True  # None is a definitive absent-order result.
            if order_status is not None:
                order = await self.ledger.get_order_by_client_id(client_order_id)
                if order is not None and order_status.get("status"):
                    order.status = str(order_status["status"])
                    if order_status.get("orderId") is not None:
                        order.exchange_order_id = str(order_status["orderId"])
                    await self.ledger.upsert_order(order)
        except BinanceAuthenticationError:
            self.invalidate_authentication()
            return None
        except Exception as exc:
            logger.error("Cancel status remains unknown after ambiguity: %s", exc)

        try:
            sync_result = await self.reconciliation.reconcile()
        except Exception as exc:
            logger.error("Authoritative reconciliation after cancel ambiguity failed: %s", exc)
            sync_result = "UNKNOWN"
        if (
            status_known
            and sync_result == "IN_SYNC"
            and self.private_stream_healthy
            and self.authenticated
        ):
            self.state = ConnectionState.READY
        else:
            self.state = ConnectionState.DEGRADED
        return order_status

    async def emergency_flatten(
        self,
        symbol: Optional[str] = None,
        *,
        authority: Optional[object] = None,
    ) -> List[ExecutionOrder]:
        if self.preflight_only:
            logger.error("Blocked emergency flatten from a read-only preflight adapter")
            self.last_emergency_result = {
                "status": "BLOCKED",
                "reason": "Read-only preflight adapter cannot mutate exchange state",
            }
            return []
        if not self._worker_authorized(authority):
            logger.error("Blocked direct emergency flatten outside the Trading Worker")
            self.last_emergency_result = {
                "status": "UNKNOWN",
                "reason": "Worker authority is required for emergency execution",
            }
            return []
        async with self._mutation_scope():
            return await self._emergency_flatten(symbol, authority=authority)

    @staticmethod
    def testnet_trial_close_client_order_id(entry_client_order_id: str) -> str:
        digest = hashlib.sha256(entry_client_order_id.encode("utf-8")).hexdigest()[:16]
        return f"BAI-TC-{digest}"

    async def close_owned_testnet_trial(
        self, owner: Dict[str, Any], *, authority: Any,
    ) -> List[ExecutionOrder]:
        """Reduce exactly the durable fill quantity for one Testnet trial owner."""
        async with self._mutation_scope():
            return await self._close_owned_testnet_trial_locked(owner, authority=authority)

    async def _close_owned_testnet_trial_locked(
        self, owner: Dict[str, Any], *, authority: Any,
    ) -> List[ExecutionOrder]:
        if (self.env != BinanceEnvironment.TESTNET or not self._worker_authorized(authority)
                or not isinstance(owner, dict) or owner.get("environment") != "TESTNET"
                or owner.get("venue") != "binance_testnet" or owner.get("symbol") != "ETHUSDC"
                or owner.get("state") != "CLOSE_PENDING" or owner.get("position_side") != "BOTH"):
            self.last_emergency_result = {"status": "BLOCKED", "reason": "trial_owner_binding_invalid"}
            return []
        entry_id = str(owner.get("entry_client_order_id") or "")
        client_order_id = self.testnet_trial_close_client_order_id(entry_id) if entry_id else ""
        protection_failure_reason: Optional[str] = None
        if (not client_order_id
                or owner.get("state_reason") != f"protected_ethusdc_testnet_trial_close:{client_order_id}:CLAIMED"):
            self.last_emergency_result = {"status": "BLOCKED", "reason": "trial_close_id_mismatch"}
            return []
        try:
            expected_qty = self._decimal_value(owner.get("filled_quantity"), positive=True)
            requested_qty = self._decimal_value(owner.get("requested_quantity"), positive=True)
            if expected_qty > requested_qty:
                raise ValueError("durable trial fill exceeds the requested entry quantity")
            entry_side = str(owner.get("entry_side") or "").upper()
            if entry_side not in {"BUY", "SELL"}:
                raise ValueError("invalid trial owner side")
            # A prior submission with this ID is evidence of an ambiguous
            # attempt. Read it once and stop; never send a duplicate close.
            existing = await self.query_order("ETHUSDC", client_order_id)
            if existing is not None:
                self.last_emergency_result = {
                    "status": "UNKNOWN", "reason": "trial_close_order_already_exists",
                    "client_order_id": client_order_id,
                }
                return []
            entry_order = await self.query_order("ETHUSDC", entry_id)
            if (not isinstance(entry_order, dict)
                    or str(entry_order.get("symbol", "")).upper() != "ETHUSDC"
                    or str(entry_order.get("clientOrderId", "")) != entry_id
                    or str(entry_order.get("side", "")).upper() != entry_side
                    or str(entry_order.get("positionSide", "")).upper() != "BOTH"
                    or Decimal(str(entry_order.get("origQty", "0"))) != requested_qty
                    or Decimal(str(entry_order.get("executedQty", "0"))) != expected_qty
                    or str(entry_order.get("status", "")).upper() not in {
                        "FILLED", "PARTIALLY_FILLED", "CANCELED", "EXPIRED",
                    }):
                self.last_emergency_result = {
                    "status": "UNKNOWN", "reason": "trial_entry_order_owner_unverified",
                    "client_order_id": client_order_id,
                }
                return []
            entry_order_id = str(entry_order.get("orderId") or "")
            local_entry = await self.ledger.get_order_by_client_id(entry_id)
            local_side_attr = getattr(local_entry, "side", None)
            local_side_val = getattr(local_side_attr, "value", local_side_attr)
            if (not entry_order_id or local_entry is None
                    or str(getattr(local_entry, "client_order_id", "")) != entry_id
                    or str(getattr(local_entry, "symbol", "")).upper() != "ETHUSDC"
                    or str(local_side_val or "").upper() != entry_side
                    or str(getattr(local_entry, "exchange_order_id", "")) != entry_order_id):
                self.last_emergency_result = {
                    "status": "UNKNOWN", "reason": "trial_entry_order_ledger_lineage_unverified",
                    "client_order_id": client_order_id,
                }
                return []
            await self.reconciliation._recover_order_fills(local_entry, entry_order)
            entry_fills = await self.ledger.get_fills()
            owned_fills = [
                fill for fill in entry_fills
                if str(getattr(fill, "client_order_id", "")) == entry_id
                and str(getattr(fill, "exchange_order_id", "")) == entry_order_id
                and str(getattr(fill, "symbol", "")).upper() == "ETHUSDC"
                and str(getattr(getattr(fill, "side", ""), "value", getattr(fill, "side", ""))).upper()
                == entry_side
                and str(getattr(getattr(fill, "position_side", ""), "value", getattr(fill, "position_side", ""))).upper()
                == "BOTH"
            ]
            if sum((Decimal(str(fill.quantity)) for fill in owned_fills), Decimal("0")) != expected_qty:
                self.last_emergency_result = {
                    "status": "UNKNOWN", "reason": "trial_entry_fill_lineage_unverified",
                    "client_order_id": client_order_id,
                }
                return []
            positions = await self.rest_client.request(
                "GET", self._position_risk_path, signed=True,
            )
            if not isinstance(positions, list):
                raise ValueError("position snapshot is invalid")
            matches = [row for row in positions if isinstance(row, dict)
                       and row.get("symbol") == "ETHUSDC"]
            if len(matches) != 1:
                raise ValueError("trial position identity is not unique")
            if str(matches[0].get("positionSide", "BOTH")).upper() != "BOTH":
                raise ValueError("trial position side differs from the durable owner")
            position_qty = self._decimal_value(matches[0].get("positionAmt"))
            expected_signed_qty = expected_qty if entry_side == "BUY" else -expected_qty
            if position_qty != expected_signed_qty:
                self.last_emergency_result = {
                    "status": "UNKNOWN",
                    "reason": "trial_position_quantity_or_direction_mismatch",
                    "client_order_id": client_order_id,
                }
                return []
            stop_algo_id_text = str(owner.get("stop_algo_id") or "")
            target_algo_id_text = str(owner.get("take_profit_algo_id") or "")
            stop_client_algo_id = str(owner.get("stop_client_algo_id") or "")
            target_client_algo_id = str(owner.get("take_profit_client_algo_id") or "")
            if (not re.fullmatch(r"[1-9][0-9]*", stop_algo_id_text)
                    or not re.fullmatch(r"[1-9][0-9]*", target_algo_id_text)
                    or not stop_client_algo_id or not target_client_algo_id
                    or stop_client_algo_id == target_client_algo_id):
                self.last_emergency_result = {
                    "status": "UNKNOWN", "reason": "trial_protection_owner_identity_invalid",
                    "client_order_id": client_order_id,
                }
                return []
            try:
                protection_intent = ProtectionIntent(
                    symbol="ETHUSDC", entry_side=entry_side, position_side="BOTH",
                    entry_qty=expected_qty,
                    stop_trigger=self._decimal_value(owner.get("stop_trigger_price"), positive=True),
                    take_profit_trigger=self._decimal_value(
                        owner.get("take_profit_trigger_price"), positive=True
                    ),
                    stop_algo_id=int(stop_algo_id_text), stop_client_algo_id=stop_client_algo_id,
                    take_profit_algo_id=int(target_algo_id_text),
                    take_profit_client_algo_id=target_client_algo_id,
                )
            except Exception as exc:
                self.last_emergency_result = {
                    "status": "UNKNOWN", "reason": "trial_protection_owner_invalid",
                    "client_order_id": client_order_id,
                }
                return []
            await self.ledger.replace_positions(positions, mark_initialized=False)
            mark_attempted = getattr(authority, "claim_testnet_trial_close_submission", None)
            if not callable(mark_attempted):
                self.last_emergency_result = {
                    "status": "BLOCKED", "reason": "durable_close_attempt_fence_unavailable",
                    "client_order_id": client_order_id,
                }
                return []
            submitting_owner = await cast(Any, mark_attempted(owner, client_order_id))
            if not isinstance(submitting_owner, dict) or submitting_owner.get("state_reason") != (
                f"protected_ethusdc_testnet_trial_close:{client_order_id}:SUBMITTING"
            ):
                self.last_emergency_result = {
                    "status": "UNKNOWN", "reason": "durable_close_attempt_fence_rejected",
                    "client_order_id": client_order_id,
                }
                return []
            intent = OrderIntent(
                client_order_id=client_order_id,
                symbol="ETHUSDC",
                market_type=MarketType.USDM_FUTURES,
                side=OrderSide.SELL if entry_side == "BUY" else OrderSide.BUY,
                position_side=PositionSide.BOTH,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
                quantity=expected_qty,
                reduce_only=True,
            )
            decision = ExecutionDecision(
                decision_id=f"TESTNET-CLOSE-{client_order_id}",
                symbol="ETHUSDC",
                action="CLOSE_POSITION",
                risk_class=EconomicRiskClass.EMERGENCY,
                orders=[intent],
            )

            protection_at_close: Dict[str, Any] = {}

            async def verify_protection_at_send_barrier() -> None:
                nonlocal protection_failure_reason
                observed_monotonic = time.monotonic()
                protection_result = await self.read_back_algo_protection(
                    protection_intent, entry_client_order_id=entry_id,
                )
                observed_at = datetime.now(timezone.utc)
                evidence = getattr(protection_result, "evidence", None)
                is_close_pos = isinstance(evidence, Mapping) and evidence.get("close_position") is True and evidence.get("reduce_only") is False
                is_reduce_only = isinstance(evidence, Mapping) and evidence.get("close_position") is False and evidence.get("reduce_only") is True
                if (getattr(protection_result, "state", None) != "PROTECTED"
                        or not isinstance(evidence, Mapping)
                        or evidence.get("stop_order_id") != protection_intent.stop_algo_id
                        or evidence.get("stop_client_order_id") != stop_client_algo_id
                        or evidence.get("stop_order_type") != "STOP_MARKET"
                        or evidence.get("stop_status") != "NEW"
                        or evidence.get("take_profit_order_id") != protection_intent.take_profit_algo_id
                        or evidence.get("take_profit_client_order_id") != target_client_algo_id
                        or evidence.get("take_profit_order_type") != "TAKE_PROFIT_MARKET"
                        or evidence.get("take_profit_status") != "NEW"
                        or evidence.get("position_side") != "BOTH"
                        or not (is_close_pos or is_reduce_only)):
                    protection_failure_reason = "trial_protection_not_confirmed_before_close"
                    raise RuntimeError("trial protection not confirmed at close submission barrier")
                close_submission_at = datetime.now(timezone.utc)
                if time.monotonic() - observed_monotonic > 5.0:
                    protection_failure_reason = "trial_protection_readback_stale_before_close"
                    raise RuntimeError("trial protection read-back exceeded freshness window")
                stop_quantity = str(
                    evidence.get("stop_quantity")
                    if evidence.get("stop_quantity") is not None
                    else expected_qty
                )
                target_quantity = str(
                    evidence.get("take_profit_quantity")
                    if evidence.get("take_profit_quantity") is not None
                    else expected_qty
                )
                protection_at_close.update({
                    "status": "PROTECTED",
                    "observed_at": observed_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "close_submission_at": close_submission_at.isoformat(
                        timespec="milliseconds"
                    ).replace("+00:00", "Z"),
                    "stop": {
                        "algo_id": stop_algo_id_text,
                        "client_algo_id": stop_client_algo_id,
                        "order_type": "STOP_MARKET", "status": "NEW",
                        "close_position": bool(evidence.get("close_position")),
                        "reduce_only": bool(evidence.get("reduce_only")),
                        "quantity": stop_quantity,
                    },
                    "target": {
                        "algo_id": target_algo_id_text,
                        "client_algo_id": target_client_algo_id,
                        "order_type": "TAKE_PROFIT_MARKET", "status": "NEW",
                        "close_position": bool(evidence.get("close_position")),
                        "reduce_only": bool(evidence.get("reduce_only")),
                        "quantity": target_quantity,
                    },
                })

            submitted = await self._execute_decision(
                decision, allow_emergency_fallback=True, authority=authority,
                before_mutation=verify_protection_at_send_barrier,
            )
            if len(submitted) != 1 or submitted[0].client_order_id != client_order_id:
                self.last_emergency_result = {
                    "status": "UNKNOWN", "reason": "trial_close_submission_unverified",
                    "client_order_id": client_order_id,
                }
                return []
            order = submitted[0]
            submitted_exchange_order_id = str(order.exchange_order_id or "")
            readback = await self.query_order("ETHUSDC", client_order_id)
            if (not submitted_exchange_order_id or not isinstance(readback, dict)
                    or str(readback.get("orderId", "")) != submitted_exchange_order_id
                    or str(readback.get("clientOrderId", "")) != client_order_id
                    or str(readback.get("symbol", "")).upper() != "ETHUSDC"
                    or str(readback.get("side", "")).upper() != intent.side.value
                    or str(readback.get("positionSide", "")).upper() != "BOTH"
                    or str(readback.get("type", "")).upper() != "MARKET"
                    or str(readback.get("reduceOnly", "")).lower() != "true"
                    or Decimal(str(readback.get("origQty", "0"))) != expected_qty
                    or str(readback.get("status", "")).upper() != "FILLED"
                    or Decimal(str(readback.get("executedQty", "0"))) != expected_qty):
                self.last_emergency_result = {
                    "status": "UNKNOWN", "reason": "trial_close_order_readback_mismatch",
                    "client_order_id": client_order_id,
                }
                return []
            await self.reconciliation._recover_order_fills(order, readback)
            final_positions = await self.rest_client.request(
                "GET", self._position_risk_path, signed=True,
            )
            if not isinstance(final_positions, list):
                raise ValueError("final position snapshot is invalid")
            remaining = [row for row in final_positions if isinstance(row, dict)
                         and row.get("symbol") == "ETHUSDC"
                         and Decimal(str(row.get("positionAmt", "0"))) != 0]
            sync = await self.reconciliation.reconcile()
            if remaining or sync != "IN_SYNC" or self.reconciliation.last_diffs:
                raise ValueError("trial close final state is not flat and reconciled")
            self.last_emergency_result = {
                "status": "CONFIRMED", "submitted_orders": 1,
                "reconciliation": sync, "client_order_id": client_order_id,
                "protection_at_close": protection_at_close,
            }
            return submitted
        except Exception as exc:
            self.last_emergency_result = {
                "status": "UNKNOWN", "reason": protection_failure_reason or type(exc).__name__,
                "client_order_id": client_order_id or None,
            }
            return []

    async def _emergency_flatten(
        self,
        symbol: Optional[str] = None,
        *,
        authority: Optional[object] = None,
        only_position_side: Optional[PositionSide] = None,
    ) -> List[ExecutionOrder]:
        """Reduce only Binance positions through the Worker-owned emergency path."""
        if self.preflight_only:
            logger.error("Blocked internal emergency flatten from a read-only preflight adapter")
            self.last_emergency_result = {
                "status": "BLOCKED",
                "reason": "Read-only preflight adapter cannot mutate exchange state",
            }
            return []
        if not self._worker_authorized(authority):
            logger.error("Blocked direct emergency flatten outside the Trading Worker")
            self.last_emergency_result = {
                "status": "UNKNOWN",
                "reason": "Worker authority is required for emergency execution",
            }
            return []

        self.last_emergency_result = {"status": "UNKNOWN", "submitted_orders": 0}
        try:
            positions = await self.rest_client.request(
                "GET", self._position_risk_path, signed=True
            )
        except BinanceAuthenticationError:
            self.invalidate_authentication()
            self.last_emergency_result = {
                "status": "UNKNOWN",
                "reason": f"{self.environment_label} authentication failed while reading positions",
            }
            return []
        except Exception as exc:
            self.last_emergency_result = {
                "status": "UNKNOWN",
                "reason": f"Authoritative {self.environment_label} position state is unknown: {exc}",
            }
            return []
        if not isinstance(positions, list):
            self.last_emergency_result = {
                "status": "UNKNOWN",
                "reason": f"Authoritative {self.environment_label} positionRisk response is invalid",
            }
            return []

        # The emergency source is authoritative. Refresh the ledger before
        # each generated reduce-only intent so the emergency order gate can
        # prove that its side and quantity reduce a real signed position even
        # when a private ACCOUNT_UPDATE event is delayed.
        try:
            await self.ledger.replace_positions(positions, mark_initialized=False)
        except Exception as exc:
            self.reconciliation.last_status = "UNKNOWN"
            self.state = ConnectionState.DEGRADED
            self.last_emergency_result = {
                "status": "UNKNOWN",
                "reason": f"Authoritative {self.environment_label} position state is invalid: {exc}",
            }
            return []
        flattened: List[ExecutionOrder] = []
        attempted = 0
        for position in positions:
            current_symbol = str(position.get("symbol", "")).upper()
            try:
                amount = Decimal(str(position.get("positionAmt", "0")))
                current_position_side = PositionSide(
                    str(position.get("positionSide", "BOTH")).upper()
                )
            except (InvalidOperation, TypeError, ValueError):
                self.last_emergency_result = {
                    "status": "UNKNOWN",
                    "reason": f"Active {self.environment_label} position contained invalid emergency fields",
                }
                return flattened
            if (
                not current_symbol
                or amount == 0
                or (symbol and current_symbol != symbol.upper())
                or (
                    only_position_side is not None
                    and current_position_side != only_position_side
                )
            ):
                continue
            attempted += 1
            side = OrderSide.SELL if amount > 0 else OrderSide.BUY
            intent = OrderIntent(
                client_order_id=self._generate_client_order_id(
                    "EMERGENCY", current_symbol, attempted
                ),
                symbol=current_symbol,
                market_type=MarketType.USDM_FUTURES,
                side=side,
                position_side=current_position_side,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
                quantity=abs(amount),
                reduce_only=True,
            )
            decision = ExecutionDecision(
                decision_id=f"EMERGENCY-{current_symbol}",
                symbol=current_symbol,
                action="CLOSE_POSITION",
                risk_class=EconomicRiskClass.EMERGENCY,
                orders=[intent],
            )
            flattened.extend(
                await self._execute_decision(
                    decision,
                    allow_emergency_fallback=True,
                    authority=authority,
                )
            )

        sync_result = self.reconciliation.last_status
        try:
            sync_result = await self.reconciliation.reconcile()
        except Exception as exc:
            logger.error("Emergency post-action reconciliation failed: %s", exc)
            sync_result = "UNKNOWN"
        try:
            final_positions = await self.rest_client.request(
                "GET", self._position_risk_path, signed=True
            )
            if not isinstance(final_positions, list):
                raise ValueError("post-close positionRisk response is invalid")
            scoped_positions = [
                row for row in final_positions
                if isinstance(row, Mapping)
                and (not symbol or str(row.get("symbol") or "").upper() == symbol.upper())
                and (
                    only_position_side is None
                    or str(row.get("positionSide") or "BOTH").upper()
                    == only_position_side.value
                )
            ]
            if any(
                Decimal(str(row.get("positionAmt", "NaN"))) != 0
                for row in scoped_positions
            ):
                raise ValueError("post-close position remains open")
            positions_flat_verified = True
        except Exception as exc:
            logger.error("Emergency close position read-back failed: %s", type(exc).__name__)
            positions_flat_verified = False
        stream_healthy = self.private_stream_healthy
        if (
            sync_result == "IN_SYNC"
            and stream_healthy
            and self.authenticated
            and positions_flat_verified
            and len(flattened) >= attempted
        ):
            self.state = ConnectionState.READY
            self.last_emergency_result = {
                "status": "CONFIRMED",
                "submitted_orders": len(flattened),
                "reconciliation": sync_result,
                "positions_flat_verified": True,
            }
        elif flattened or attempted:
            self.state = ConnectionState.DEGRADED
            self.last_emergency_result = {
                "status": "PARTIAL",
                "submitted_orders": len(flattened),
                "attempted_orders": attempted,
                "reconciliation": sync_result,
                "reason": "Emergency reduction was not fully verified",
            }
        else:
            self.last_emergency_result = {
                "status": "CONFIRMED" if sync_result == "IN_SYNC" and self.authenticated and positions_flat_verified else "UNKNOWN",
                "submitted_orders": 0,
                "reconciliation": sync_result,
                "positions_flat_verified": positions_flat_verified,
            }
        return flattened

    async def close(self):
        # Drain any mutation already holding the lock before releasing the
        # lease or closing transport; later Worker calls recheck authorization.
        async with self._mutation_scope():
            lease = self.execution_lease
            self.execution_lease = None
            if lease is not None:
                try:
                    await lease.release()
                except Exception as exc:
                    logger.warning(
                        "Unable to release execution lease cleanly: %s", type(exc).__name__
                    )
            await self.user_stream.close()
            await self.rest_client.close()
            self.capabilities.authenticated = False
            self.capabilities.account_request_succeeded = False
            self.capabilities.trade_authorized = False
            self.state = ConnectionState.DISCONNECTED
