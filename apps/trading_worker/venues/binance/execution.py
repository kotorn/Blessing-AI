"""Native, environment-aware Binance USDⓈ-M execution adapter.

The adapter can speak to Testnet or Mainnet, but the Trading Worker remains the
only mutable authority and Mainnet still requires the deployment launch gate.
"""

import asyncio
import hashlib
import logging
import math
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from domain.enums import (
    EconomicRiskClass,
    MarketType,
    OrderSide,
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
    ):
        if not isinstance(env, BinanceEnvironment):
            raise ValueError("Binance execution requires TESTNET or MAINNET")
        if env == BinanceEnvironment.MAINNET and os.getenv(
            "MAINNET_LIVE_APPROVED", ""
        ).strip().lower() not in {"1", "true", "yes", "on"} and not preflight_only:
            raise ValueError("Mainnet adapter construction requires MAINNET_LIVE_APPROVED=true")

        self.env = env
        self.preflight_only = bool(preflight_only)
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
        self.last_market_event_source: Dict[str, str] = {}
        self.last_market_event_venue: Dict[str, str] = {}
        self.last_market_event_market_type: Dict[str, str] = {}
        self.last_order_event_at: Dict[str, datetime] = {}
        self.last_emergency_result: Dict[str, Any] = {"status": "UNKNOWN"}
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
        # Cloud Run and Mainnet always require a distributed lease. Local
        # Testnet tests can opt into the same requirement with an env flag.
        self.execution_lease_required = bool(
            env == BinanceEnvironment.MAINNET or execution_lease_required()
        )

    @property
    def connection_state(self) -> ConnectionState:
        """Canonical adapter connection state consumed by the worker."""
        return self.state

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
    def account_snapshot(self):
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

    def _worker_authorized(self, authority: object) -> bool:
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
            return
        await lease.assert_valid()

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
        try:
            bid = Decimal(str(event.best_bid))
            ask = Decimal(str(event.best_ask))
            if bid.is_finite() and ask.is_finite() and bid > 0 and ask >= bid:
                self.last_market_bid[symbol] = bid
                self.last_market_ask[symbol] = ask
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
        event_at = (
            self.last_market_event_at.get(normalized_symbol)
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

    async def _on_user_stream_disconnect(self):
        logger.warning("[%s] User stream disconnected. Adapter transitioning to DEGRADED.", self.env)
        self.state = ConnectionState.DEGRADED
        try:
            await self.reconciliation.reconcile()
            if getattr(self.reconciliation, "authentication_failed", False):
                self.invalidate_authentication()
        except Exception as exc:
            logger.error("Reconciliation after user-stream disconnect failed: %s", exc)

    async def _on_user_stream_reconnected(self):
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

    async def _on_ws_event(self, event: Any):
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

            raw_positions = update_data.get("P", [])
            if not isinstance(raw_positions, list):
                raw_positions = []
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
            raw_balances = update_data.get("B", [])
            if not isinstance(raw_balances, list):
                raw_balances = []
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
            # B contains per-asset deltas, not the USDⓈ-M aggregate totals used
            # by ExchangeAccountSnapshot. Do not project one asset (for example
            # USDT) into wallet/margin totals. The next signed REST reconcile
            # must provide the authoritative aggregate account snapshot.
            # A delta event is not a complete account snapshot and cannot
            # prove reconciliation.  Force the next authoritative REST
            # snapshot/reconcile before any risk-increasing order.
            await self.ledger.set_account_snapshot(None)
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
        prepared,
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

        response_price = response.get("price")
        if response_price in (None, "", "0", 0):
            response_price = response.get("avgPrice")
        if response_price not in (None, "", "0", 0):
            try:
                price = Decimal(str(response_price))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise BinanceTransportAmbiguity("Binance order response price is invalid") from exc
            if not price.is_finite() or price <= 0:
                raise BinanceTransportAmbiguity("Binance order response price is unusable")
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
        prepared,
        client_order_id: str,
        decision: Optional[ExecutionDecision] = None,
    ) -> Optional[ExecutionOrder]:
        self.state = ConnectionState.RECONCILING
        recovered_order: Optional[ExecutionOrder] = None
        order_status_known = False
        fill_recovery_verified = True
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
                if str(recovered_order.status).upper() in {"FILLED", "PARTIALLY_FILLED"}:
                    try:
                        await self.reconciliation._recover_order_fills(
                            recovered_order, status_response
                        )
                    except BinanceAuthenticationError:
                        self.invalidate_authentication()
                        return None
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

        try:
            sync_result = await self.reconciliation.reconcile()
        except Exception as exc:
            logger.error("Authoritative reconciliation after ambiguity failed: %s", exc)
            sync_result = "UNKNOWN"
        if (
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
        return recovered_order if self.state == ConnectionState.READY else None

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
        gate = getattr(authority, "_evaluate_execution_gate", None)
        if not callable(gate):
            logger.error("Blocked Binance mutation because Worker gate is unavailable")
            return []
        allowed, reason = gate(decision)
        if not allowed:
            logger.warning("Worker decision gate blocked adapter mutation: %s", reason)
            return []
        async with self._mutation_lock:
            # The first gate check may have happened while another mutation was
            # in flight. Re-evaluate after acquiring the single-flight lock so
            # a kill switch or degraded state cannot release a queued order.
            allowed, reason = gate(decision)
            if not allowed:
                logger.warning(
                    "Worker decision gate blocked queued adapter mutation: %s", reason
                )
                return []
            return await self._execute_decision(decision, authority=authority)

    async def _execute_decision(
        self,
        decision: ExecutionDecision,
        *,
        allow_emergency_fallback: bool = False,
        authority: Optional[object] = None,
    ) -> List[ExecutionOrder]:
        if self.preflight_only:
            logger.error("Blocked internal order submission from a read-only preflight adapter")
            return []
        if not self._worker_authorized(authority):
            logger.error(
                "Blocked internal Binance mutation outside the bound Trading Worker"
            )
            return []
        if (
            not allow_emergency_fallback
            and (self.state != ConnectionState.READY or decision.action == "NOOP")
        ):
            return []
        if decision.action == "NOOP":
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
            gate_result = await self.order_gate.check(
                intent,
                decision.risk_class,
                reserved_open_orders=reserved_open_orders,
                reserved_notional=reserved_notional,
                allow_emergency_fallback=allow_emergency_fallback,
            )
            if not gate_result.allowed or gate_result.prepared is None:
                logger.warning("Order blocked by final gate: %s", gate_result.reason)
                continue
            prepared = gate_result.prepared
            # Mainnet retries/restarts must reuse the same exchange identity
            # for one worker decision. The risk governor's human-readable
            # intent id contains a runtime sequence, so it is not sufficient
            # as the exchange idempotency key on its own.
            client_order_id = (
                self._generate_client_order_id(
                    str(decision.decision_id), prepared.symbol, order_index=index
                )
                if self.env == BinanceEnvironment.MAINNET
                else intent.client_order_id or self._generate_client_order_id(
                    str(decision.decision_id), prepared.symbol, order_index=index
                )
            )
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
            submission_attempted = False
            try:
                planned_order = ExecutionOrder(
                    symbol=prepared.symbol,
                    side=intent.side,
                    quantity=prepared.quantity,
                    price=prepared.price or prepared.estimated_price,
                    order_type=prepared.order_type,
                    client_order_id=client_order_id,
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
                await self._assert_execution_lease(decision.risk_class)
                self.order_submission_attempts += 1
                submission_attempted = True
                response = await self.rest_client.request(
                    "POST", self._order_path, signed=True, params=params
                )
                if not isinstance(response, dict):
                    raise BinanceTransportAmbiguity("Binance order response is invalid")
                order = self._order_from_response(
                    intent, response, prepared, client_order_id, decision
                )
                await self.ledger.upsert_order(order)
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
            except LeaseLostError as exc:
                if planned_order is not None:
                    await self._notify_order_submission_result(planned_order, "REJECTED")
                self.state = ConnectionState.DEGRADED
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
                    intent, prepared, client_order_id, decision
                )
                if recovered is not None:
                    # _resolve_ambiguous_order only returns non-None once the
                    # order has been read back and reconciliation verified it
                    # -- the same bar the normal success path uses before its
                    # own "CONFIRMED" notification below.
                    await self._notify_order_submission_result(recovered, "CONFIRMED")
                    executed_orders.append(recovered)
            except Exception as exc:
                if planned_order is not None:
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
        async with self._mutation_lock:
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
    ) -> bool:
        if self.preflight_only:
            logger.error("Blocked %s cancellation from a read-only preflight adapter", self.environment_label)
            return False
        if not self._worker_authorized(authority):
            logger.error("Blocked direct %s cancel outside the Trading Worker", self.environment_label)
            return False
        async with self._mutation_lock:
            if getattr(authority, "kill_switch_active", False):
                logger.warning("Kill switch blocked %s cancel mutation", self.environment_label)
                return False
            return await self._cancel_order(
                symbol, orig_client_order_id, authority=authority
            )

    async def _cancel_order(
        self,
        symbol: str,
        orig_client_order_id: str,
        *,
        authority: Optional[object] = None,
    ) -> bool:
        if self.preflight_only:
            logger.error("Blocked %s internal cancellation from a read-only preflight adapter", self.environment_label)
            return False
        if not self._worker_authorized(authority):
            logger.error("Blocked direct %s cancel outside the Trading Worker", self.environment_label)
            return False
        if self.state != ConnectionState.READY:
            return False
        try:
            response = await self.rest_client.request(
                "DELETE",
                self._order_path,
                signed=True,
                params={"symbol": symbol.upper(), "origClientOrderId": orig_client_order_id},
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
        async with self._mutation_lock:
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
        decision_allowed, decision_reason = worker_gate(amendment_decision)
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
        async with self._mutation_lock:
            return await self._emergency_flatten(symbol, authority=authority)

    async def _emergency_flatten(
        self,
        symbol: Optional[str] = None,
        *,
        authority: Optional[object] = None,
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
                position_side = PositionSide(str(position.get("positionSide", "BOTH")).upper())
            except (InvalidOperation, TypeError, ValueError):
                self.last_emergency_result = {
                    "status": "UNKNOWN",
                    "reason": f"Active {self.environment_label} position contained invalid emergency fields",
                }
                return flattened
            if not current_symbol or amount == 0 or (symbol and current_symbol != symbol.upper()):
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
                position_side=position_side,
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
        stream_healthy = self.private_stream_healthy
        if (
            sync_result == "IN_SYNC"
            and stream_healthy
            and self.authenticated
            and len(flattened) >= attempted
        ):
            self.state = ConnectionState.READY
            self.last_emergency_result = {
                "status": "CONFIRMED",
                "submitted_orders": len(flattened),
                "reconciliation": sync_result,
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
                "status": "CONFIRMED" if sync_result == "IN_SYNC" and self.authenticated else "UNKNOWN",
                "submitted_orders": 0,
                "reconciliation": sync_result,
            }
        return flattened

    async def close(self):
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
