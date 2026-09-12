"""Native, Testnet-only Binance USDⓈ-M execution adapter."""

import hashlib
import logging
import math
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from domain.enums import EconomicRiskClass, OrderSide, OrderType, PositionSide, TimeInForce
from domain.models import (
    ExchangeFill,
    ExecutionDecision,
    ExecutionOrder,
    MarketEvent,
    OrderIntent,
    utc_now,
)

from .capabilities import BinanceCapabilities
from .config import BinanceEnvironment
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
from .reconciliation import BinanceReconciliation
from .rest_client import BinanceAPIError, BinanceRestClient
from .symbol_rules import SymbolTradingRules
from .user_stream import BinanceUserStream


logger = logging.getLogger("blessing.venues.binance.execution")


class BinanceExecutionAdapter:
    """Blessing AI's sole mutable exchange adapter, restricted to Testnet."""

    testnet_environment = BinanceEnvironment.TESTNET

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        env: BinanceEnvironment = BinanceEnvironment.TESTNET,
        ledger: Optional[ExecutionLedger] = None,
    ):
        if env != BinanceEnvironment.TESTNET:
            raise ValueError("LIVE/Mainnet mutable execution is permanently blocked.")

        self.env = env
        self.api_key = api_key
        self.api_secret = api_secret
        self.ledger = ledger or InMemoryLedger()
        self.safety_limits = TestnetSafetyLimits.from_environment()
        self.rest_client = BinanceRestClient(api_key, api_secret, env)
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
        self.last_order_event_at: Dict[str, datetime] = {}

    @property
    def connection_state(self) -> ConnectionState:
        """Canonical adapter connection state consumed by the worker."""
        return self.state

    @property
    def authenticated(self) -> bool:
        """Authentication is true only after signed account capability discovery."""
        return bool(
            self.env == BinanceEnvironment.TESTNET
            and getattr(self.capabilities, "account_request_succeeded", False)
            and getattr(self.capabilities, "authenticated", False)
        )

    @property
    def symbol_rules(self) -> Dict[str, SymbolTradingRules]:
        """Canonical symbol-rule API for worker readiness and order validation."""
        return self.capabilities.symbol_rules

    @property
    def account_snapshot(self):
        return getattr(self.ledger, "account_snapshot", None)

    def is_account_snapshot_fresh(self) -> bool:
        snapshot = self.account_snapshot
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
        self.state = ConnectionState.DEGRADED

    def record_market_event(self, event: MarketEvent) -> bool:
        """Record a real market sample for per-symbol freshness checks."""
        raw_price = event.mark_price or event.last_price
        try:
            price = Decimal(str(raw_price))
        except (InvalidOperation, ValueError):
            return False
        if not price.is_finite() or price <= 0:
            return False
        timestamp = event.event_time
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        symbol = str(event.symbol).upper()
        self.last_market_event_at[symbol] = timestamp
        self.last_market_price[symbol] = price
        return True

    def _market_data_max_age(self) -> float:
        raw = os.getenv("MAX_MARKET_DATA_AGE_SEC", "3.0")
        try:
            value = float(raw)
        except ValueError:
            return 3.0
        return value if value > 0 and value != float("inf") and value != float("-inf") else 3.0

    async def get_fresh_market_price(self, symbol: str) -> Optional[Decimal]:
        """Return a fresh real Testnet mark price; never synthesize one."""
        normalized_symbol = symbol.upper()
        event_at = self.last_market_event_at.get(normalized_symbol)
        cached = self.last_market_price.get(normalized_symbol)
        if event_at and cached:
            if event_at.tzinfo is None:
                event_at = event_at.replace(tzinfo=timezone.utc)
            age = (utc_now() - event_at).total_seconds()
            if 0 <= age <= self._market_data_max_age():
                return cached

        try:
            payload = await self.rest_client.request(
                "GET", "/fapi/v1/premiumIndex", params={"symbol": normalized_symbol}
            )
            price = Decimal(str(payload.get("markPrice")))
            if not price.is_finite() or price <= 0:
                return None
            self.last_market_event_at[normalized_symbol] = utc_now()
            self.last_market_price[normalized_symbol] = price
            return price
        except Exception as exc:
            logger.warning("Fresh market price unavailable for %s: %s", normalized_symbol, exc)
            return None

    async def get_best_bid_ask(self, symbol: str) -> Optional[Tuple[Decimal, Decimal]]:
        """Fetch a current Testnet book quote for passive manual validation."""
        normalized_symbol = symbol.upper()
        try:
            payload = await self.rest_client.request(
                "GET", "/fapi/v1/ticker/bookTicker", params={"symbol": normalized_symbol}
            )
            bid = Decimal(str(payload.get("bidPrice")))
            ask = Decimal(str(payload.get("askPrice")))
            if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
                return None
            now = utc_now()
            self.last_market_event_at[normalized_symbol] = now
            self.last_market_price[normalized_symbol] = (bid + ask) / Decimal("2")
            return bid, ask
        except Exception as exc:
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
        if sync_result == "IN_SYNC" and self.user_stream.is_connected and self.authenticated:
            self.state = ConnectionState.READY
            logger.info("[%s] Reconnected and IN_SYNC. State transitioned to READY.", self.env)
        else:
            self.state = ConnectionState.DEGRADED
            logger.warning(
                "[%s] Reconnection verification failed: sync=%s ws=%s auth=%s",
                self.env,
                sync_result,
                self.user_stream.is_connected,
                self.authenticated,
            )

    async def connect(self) -> bool:
        if self.env != BinanceEnvironment.TESTNET:
            raise ValueError("Mutable Binance connection is restricted to Testnet")
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
            if sync_result == "IN_SYNC" and self.user_stream.is_connected and self.authenticated:
                self.state = ConnectionState.READY
                return True
            self.state = ConnectionState.DEGRADED
            return False
        except Exception as exc:
            self.invalidate_authentication()
            logger.error("Binance Testnet adapter connection failed: %s", exc)
            return False

    async def arm(self) -> bool:
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
            if existing_order:
                existing_order.status = status
                if order_info.get("i") is not None:
                    existing_order.exchange_order_id = str(order_info["i"])
                await self.ledger.upsert_order(existing_order)
            else:
                try:
                    side = OrderSide(str(order_info.get("S")))
                except ValueError:
                    logger.warning("Ignoring order update with invalid side: %s", order_info)
                    return
                new_order = ExecutionOrder(
                    symbol=symbol,
                    side=side,
                    quantity=Decimal(str(order_info.get("q", "0"))),
                    price=Decimal(str(order_info.get("p", "0"))),
                    order_type=str(order_info.get("ot") or order_info.get("o") or "LIMIT"),
                    client_order_id=client_order_id,
                    status=status,
                    exchange_order_id=str(order_info.get("i", "")),
                    timestamp=utc_now(),
                    market_type="USDM_FUTURES",
                    position_side=PositionSide(str(order_info.get("ps", "BOTH"))),
                    reduce_only=bool(order_info.get("R", False)),
                    time_in_force=(
                        TimeInForce.POST_ONLY
                        if str(order_info.get("f", "GTC")).upper() == "GTX"
                        else TimeInForce(str(order_info.get("f", "GTC")).upper())
                    ),
                )
                await self.ledger.upsert_order(new_order)
                existing_order = new_order

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
                    maker=bool(order_info.get("m", False)),
                    event_time=event.get("E", 0),
                    transaction_time=order_info.get("T", event.get("E", 0)),
                    source="BINANCE_TESTNET",
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
            except (InvalidOperation, ValueError, TypeError) as exc:
                logger.error("Invalid Testnet fill event ignored: %s", exc)
        elif event_type == "ACCOUNT_UPDATE":
            update_data = event.get("a", {})
            for position in update_data.get("P", []):
                await self.ledger.upsert_position(
                    {
                        "symbol": position.get("s"),
                        "positionSide": position.get("ps"),
                        "positionAmt": position.get("pa"),
                        "entryPrice": position.get("ep"),
                        "unRealizedProfit": position.get("up"),
                        "marginType": position.get("mt", "cross"),
                        "eventTime": event.get("E"),
                        "source": "BINANCE_TESTNET",
                    }
                )
            for balance in update_data.get("B", []):
                if balance.get("a") == "USDT":
                    await self.ledger.update_balances(
                        Decimal(str(balance.get("wb"))),
                        Decimal(str(balance.get("cw"))),
                    )

    def _generate_client_order_id(
        self, context_id: str, symbol: str, order_index: int = 0, attempt: int = 1
    ) -> str:
        raw_str = f"{context_id}-{symbol}"
        hash_str = hashlib.md5(raw_str.encode()).hexdigest()[:8]
        return f"BAI-{hash_str}-{order_index}-{attempt}"

    @staticmethod
    def _order_from_response(
        intent: OrderIntent,
        response: Dict[str, Any],
        prepared,
        client_order_id: str,
        decision: Optional[ExecutionDecision] = None,
    ) -> ExecutionOrder:
        order_id = response.get("orderId")
        status = response.get("status")
        if order_id in (None, "") or status in (None, ""):
            raise BinanceTransportAmbiguity("Binance order response did not contain orderId/status")
        response_price = response.get("price") or response.get("avgPrice")
        price = (
            Decimal(str(response_price))
            if response_price not in (None, "", "0", 0)
            else prepared.estimated_price
        )
        return ExecutionOrder(
            symbol=prepared.symbol,
            side=intent.side,
            quantity=prepared.quantity,
            price=price,
            order_type=prepared.order_type,
            client_order_id=str(response.get("clientOrderId") or client_order_id),
            status=str(status),
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
        try:
            status_response = await self.rest_client.request(
                "GET",
                "/fapi/v1/order",
                signed=True,
                params={"symbol": prepared.symbol, "origClientOrderId": client_order_id},
            )
            recovered_order = self._order_from_response(
                intent, status_response, prepared, client_order_id, decision
            )
            await self.ledger.upsert_order(recovered_order)
            order_status_known = True
        except BinanceAuthenticationError:
            self.invalidate_authentication()
            return None
        except Exception as exc:
            message = str(exc)
            if "-2013" not in message and "does not exist" not in message.lower():
                logger.error("Ambiguous order status remains unknown: %s", exc)
            else:
                logger.info("Ambiguous order %s confirmed absent on exchange", client_order_id)
                order_status_known = True

        try:
            sync_result = await self.reconciliation.reconcile()
        except Exception as exc:
            logger.error("Authoritative reconciliation after ambiguity failed: %s", exc)
            sync_result = "UNKNOWN"
        if (
            sync_result == "IN_SYNC"
            and self.user_stream.is_connected
            and self.authenticated
            and order_status_known
        ):
            self.state = ConnectionState.READY
        else:
            self.state = ConnectionState.DEGRADED
        # The recovered record remains in the ledger for reconciliation, but
        # it is not reported as an executable success while the authoritative
        # post-mutation checks are degraded.
        return recovered_order if self.state == ConnectionState.READY else None

    async def execute_decision(self, decision: ExecutionDecision) -> List[ExecutionOrder]:
        if self.state != ConnectionState.READY or decision.action == "NOOP":
            return []

        executed_orders: List[ExecutionOrder] = []
        reserved_open_orders = 0
        reserved_notional = Decimal("0")
        for index, intent in enumerate(decision.orders):
            gate_result = await self.order_gate.check(
                intent,
                decision.risk_class,
                reserved_open_orders=reserved_open_orders,
                reserved_notional=reserved_notional,
            )
            if not gate_result.allowed or gate_result.prepared is None:
                logger.warning("Order blocked by final gate: %s", gate_result.reason)
                continue
            prepared = gate_result.prepared
            client_order_id = intent.client_order_id or self._generate_client_order_id(
                str(decision.decision_id), prepared.symbol, order_index=index
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

            try:
                response = await self.rest_client.request(
                    "POST", "/fapi/v1/order", signed=True, params=params
                )
                if not isinstance(response, dict):
                    raise BinanceTransportAmbiguity("Binance order response is invalid")
                order = self._order_from_response(
                    intent, response, prepared, client_order_id, decision
                )
                await self.ledger.upsert_order(order)
                executed_orders.append(order)
                if order.status in ("NEW", "PARTIALLY_FILLED"):
                    reserved_open_orders += 1
                    reserved_notional += prepared.notional
            except BinanceAuthenticationError as exc:
                self.invalidate_authentication()
                logger.error("Testnet authentication failed while submitting %s: %s", client_order_id, exc)
            except (BinanceRateLimitError, BinanceTimestampError) as exc:
                logger.error("Testnet mutable request was not submitted: %s", exc)
            except BinanceDefinitiveRejection as exc:
                logger.warning("Testnet order rejected definitively: %s", exc)
                await self.ledger.upsert_order(
                    ExecutionOrder(
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
                )
            except (BinanceTransportAmbiguity, BinanceAPIError) as exc:
                logger.error("Testnet order response is ambiguous: %s", exc)
                recovered = await self._resolve_ambiguous_order(
                    intent, prepared, client_order_id, decision
                )
                if recovered is not None:
                    executed_orders.append(recovered)
            except Exception as exc:
                logger.error("Unexpected Testnet order execution failure: %s", exc)
                self.state = ConnectionState.DEGRADED

        return executed_orders

    async def query_order(self, symbol: str, client_order_id: str) -> Optional[Dict[str, Any]]:
        try:
            return await self.rest_client.request(
                "GET",
                "/fapi/v1/order",
                signed=True,
                params={"symbol": symbol.upper(), "origClientOrderId": client_order_id},
            )
        except Exception as exc:
            if "-2013" in str(exc) or "does not exist" in str(exc).lower():
                return None
            raise

    async def cancel_order(self, symbol: str, orig_client_order_id: str) -> bool:
        if self.state != ConnectionState.READY:
            return False
        try:
            response = await self.rest_client.request(
                "DELETE",
                "/fapi/v1/order",
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
            return True
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
            logger.error("Testnet cancel was not submitted: %s", exc)
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
    ) -> Optional[ExecutionOrder]:
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
            response = await self.rest_client.request(
                "PUT",
                "/fapi/v1/order",
                signed=True,
                params=params,
            )
            if not isinstance(response, dict):
                raise BinanceTransportAmbiguity("Binance amendment response is invalid")
            amended = self._order_from_response(
                intent, response, prepared, orig_client_order_id, amendment_decision
            )
            await self.ledger.upsert_order(amended)
            return amended
        except BinanceAuthenticationError:
            self.invalidate_authentication()
            return None
        except BinanceDefinitiveRejection as exc:
            logger.warning("Order amendment was rejected definitively: %s", exc)
            return None
        except (BinanceRateLimitError, BinanceTimestampError) as exc:
            logger.error("Testnet amendment was not submitted: %s", exc)
            return None
        except BinanceTransportAmbiguity as exc:
            logger.error("Testnet amendment response is ambiguous: %s", exc)
            return await self._resolve_ambiguous_order(
                intent, prepared, orig_client_order_id, amendment_decision
            )
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
            and self.user_stream.is_connected
            and self.authenticated
        ):
            self.state = ConnectionState.READY
        else:
            self.state = ConnectionState.DEGRADED
        return order_status

    async def emergency_flatten(self, symbol: Optional[str] = None) -> List[ExecutionOrder]:
        """Reduce only Testnet positions; never routes to Mainnet."""
        positions = await self.rest_client.request(
            "GET", "/fapi/v2/positionRisk", signed=True
        )
        if not isinstance(positions, list):
            raise BinanceTransportAmbiguity(
                "Authoritative Testnet positionRisk response is invalid"
            )
        # The emergency source is authoritative.  Refresh the ledger before
        # each generated reduce-only intent so the final per-order gate can
        # prove that its side and quantity reduce a real signed position even
        # when a private ACCOUNT_UPDATE event is delayed.
        await self.ledger.replace_positions(positions, mark_initialized=False)
        flattened: List[ExecutionOrder] = []
        for position in positions:
            current_symbol = str(position.get("symbol", "")).upper()
            amount = Decimal(str(position.get("positionAmt", "0")))
            if not current_symbol or amount == 0 or (symbol and current_symbol != symbol.upper()):
                continue
            position_side = PositionSide(str(position.get("positionSide", "BOTH")))
            side = OrderSide.SELL if amount > 0 else OrderSide.BUY
            intent = OrderIntent(
                client_order_id=self._generate_client_order_id(
                    "EMERGENCY", current_symbol, len(flattened)
                ),
                symbol=current_symbol,
                market_type="USDM_FUTURES",
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
            flattened.extend(await self.execute_decision(decision))
        return flattened

    async def close(self):
        await self.user_stream.close()
        await self.rest_client.close()
        self.capabilities.authenticated = False
        self.capabilities.account_request_succeeded = False
        self.state = ConnectionState.DISCONNECTED
