"""Fail-closed execution gates shared by the worker and Binance adapter."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
import os
from typing import Any, Optional

from domain.enums import EconomicRiskClass, MarketType, OrderSide, OrderType, PositionSide, TimeInForce
from domain.models import ExecutionDecision, OrderIntent

from .models import ConnectionState


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str
    prepared: Optional["PreparedOrder"] = None


@dataclass(frozen=True)
class PreparedOrder:
    symbol: str
    order_type: str
    quantity: Decimal
    price: Optional[Decimal]
    estimated_price: Decimal
    notional: Decimal


def _risk_class(value: Any) -> Optional[EconomicRiskClass]:
    try:
        return value if isinstance(value, EconomicRiskClass) else EconomicRiskClass(str(value))
    except (TypeError, ValueError):
        return None


def _is_risk_increasing(value: Any) -> bool:
    return _risk_class(value) in {
        EconomicRiskClass.NEW_RISK,
        EconomicRiskClass.INCREASE_RISK,
    }


def _is_risk_reducing(value: Any) -> bool:
    return _risk_class(value) in {
        EconomicRiskClass.REDUCE_RISK,
        EconomicRiskClass.RECOVERY,
        EconomicRiskClass.CLOSE,
        EconomicRiskClass.EMERGENCY,
    }


def _liquidation_safety_is_known_and_positive(snapshot: Any) -> bool:
    if getattr(snapshot, "liquidation_safety", "UNKNOWN") != "KNOWN":
        return False
    distance = getattr(snapshot, "min_liquidation_distance_pct", None)
    # A flat account has no applicable liquidation distance.  For an active
    # account, KNOWN must include a strictly positive, finite distance; zero
    # is a known danger state and cannot authorize more exposure.
    if distance is None:
        try:
            notional = Decimal(str(getattr(snapshot, "total_position_notional", "")))
        except (InvalidOperation, TypeError, ValueError):
            return False
        # ``None`` is only acceptable when the authoritative snapshot proves
        # that the account is flat. An active position without a usable
        # liquidation price remains UNKNOWN and must fail closed.
        return notional.is_finite() and notional == 0
    try:
        parsed = Decimal(str(distance))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return parsed.is_finite() and parsed > 0


def _available_balance_is_positive(snapshot: Any) -> bool:
    try:
        available = Decimal(str(getattr(snapshot, "available_balance", "")))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return available.is_finite() and available > 0


def _margin_utilization_is_safe(snapshot: Any) -> bool:
    try:
        utilization = Decimal(str(getattr(snapshot, "margin_utilization_pct", "")))
        configured_limit = Decimal(os.getenv("MAX_MARGIN_UTILIZATION_PCT", "70"))
    except (InvalidOperation, TypeError, ValueError):
        configured_limit = Decimal("70")
        try:
            utilization = Decimal(str(getattr(snapshot, "margin_utilization_pct", "")))
        except (InvalidOperation, TypeError, ValueError):
            return False
    if (
        not configured_limit.is_finite()
        or configured_limit <= 0
        or configured_limit > 100
    ):
        configured_limit = Decimal("70")
    return utilization.is_finite() and utilization >= 0 and utilization < configured_limit


def _positive_float(name: str, fallback: float) -> float:
    import os

    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        value = float(raw)
    except ValueError:
        return fallback
    return value if math.isfinite(value) and value > 0 else fallback


def _age_seconds(timestamp: Any) -> Optional[float]:
    if not isinstance(timestamp, datetime):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - timestamp).total_seconds()


class DecisionExecutionGate:
    """Checks worker-wide conditions before any decision reaches the adapter."""

    def __init__(self, worker: Any):
        self.worker = worker

    def check(self, decision: ExecutionDecision) -> GateResult:
        mode = getattr(self.worker.execution_mode, "value", self.worker.execution_mode)
        if mode != "TESTNET":
            return GateResult(False, "Execution mode is not TESTNET")
        engine_state = getattr(self.worker, "engine_state", None)
        engine_state = getattr(engine_state, "value", engine_state)
        if engine_state not in {"ARMED", "PAUSED_NEW_RISK", "RECOVERY_ONLY"}:
            return GateResult(False, "Worker is not in an executable armed state")
        if getattr(self.worker, "kill_switch_active", False):
            return GateResult(False, "Kill switch is active")

        testnet_configured = getattr(self.worker, "_testnet_configured", None)
        if callable(testnet_configured) and not testnet_configured():
            return GateResult(False, "Binance Testnet configuration is not verified")

        adapter = getattr(self.worker, "execution_adapter", None)
        if adapter is None:
            return GateResult(False, "Execution adapter is unavailable")
        adapter_state = getattr(adapter, "connection_state", None)
        adapter_state = getattr(adapter_state, "value", adapter_state)
        if adapter_state != ConnectionState.READY.value:
            return GateResult(False, "Adapter not READY")
        if not bool(getattr(adapter, "authenticated", False)):
            return GateResult(False, "Adapter is not authenticated")
        if not bool(
            getattr(getattr(adapter, "capabilities", None), "trade_authorized", False)
        ):
            return GateResult(False, "Testnet trade permission is not verified")
        stream_health = getattr(adapter, "private_stream_healthy", None)
        if stream_health is None:
            stream_health = bool(
                getattr(adapter, "user_stream", None)
                and getattr(adapter.user_stream, "is_connected", False)
            )
        if not bool(stream_health):
            return GateResult(False, "Private stream disconnected")
        if getattr(adapter.reconciliation, "last_status", "UNKNOWN") != "IN_SYNC":
            return GateResult(False, "Reconciliation not IN_SYNC")

        risk_class = _risk_class(getattr(decision, "risk_class", None))
        if risk_class is None or risk_class == EconomicRiskClass.NOOP:
            return GateResult(False, "Decision has no executable economic risk class")
        if not getattr(decision, "orders", None):
            return GateResult(False, "Decision contains no orders")
        if str(getattr(decision, "action", "")).upper() == "NOOP":
            return GateResult(False, "NOOP decisions cannot reach execution")

        if _is_risk_increasing(risk_class):
            # Treat canonical engine state as a safety input as well as the
            # compatibility flags. Any disagreement fails closed instead of
            # allowing a stale control-plane flag to authorize new exposure.
            if (
                getattr(self.worker, "pause_new_risk", False)
                or engine_state == "PAUSED_NEW_RISK"
            ):
                return GateResult(False, "Paused new risk")
            if (
                getattr(self.worker, "recovery_only", False)
                or engine_state == "RECOVERY_ONLY"
            ):
                return GateResult(False, "Recovery only mode active")

        account_snapshot_ready = self.worker.is_account_snapshot_ready()
        if _is_risk_increasing(risk_class) and not account_snapshot_ready:
            return GateResult(False, "Account snapshot is missing, stale, invalid, or not Testnet")

        snapshot = getattr(adapter, "account_snapshot", None)
        if snapshot is None:
            snapshot = getattr(getattr(adapter, "ledger", None), "account_snapshot", None)
        if _is_risk_increasing(risk_class) and not _available_balance_is_positive(snapshot):
            return GateResult(False, "Available Testnet balance is not positive")
        if _is_risk_increasing(risk_class) and not _margin_utilization_is_safe(snapshot):
            return GateResult(False, "Margin utilization is at or above the Testnet safety limit")
        if _is_risk_increasing(risk_class) and not _liquidation_safety_is_known_and_positive(snapshot):
            return GateResult(False, "Liquidation safety is UNKNOWN")

        # Reductions may use an emergency fallback when the market stream is
        # stale.  MARKET orders still require a fresh REST mark price in the
        # individual order gate.  Risk-increasing decisions require both the
        # worker-wide health bit and per-symbol event freshness here.
        if _is_risk_increasing(risk_class):
            if not getattr(self.worker, "market_data_healthy", False):
                return GateResult(False, "Market data is stale")
            max_age = _positive_float("MAX_MARKET_DATA_AGE_SEC", 3.0)
            timestamps = getattr(self.worker, "last_market_event_at", {})
            adapter_timestamps = getattr(adapter, "last_market_event_at", {})
            for intent in decision.orders:
                symbol = str(intent.symbol).upper()
                has_market_sample = getattr(
                    adapter, "has_authoritative_market_sample", None
                )
                if callable(has_market_sample) and not has_market_sample(symbol):
                    return GateResult(
                        False,
                        f"Authoritative Testnet market sample unavailable for {symbol}",
                    )
                last_event = timestamps.get(symbol) or adapter_timestamps.get(symbol)
                age = _age_seconds(last_event)
                if age is None or age < 0 or age > max_age:
                    return GateResult(False, f"Market data stale for {symbol}")

        return GateResult(True, "Passed")


class OrderExecutionGate:
    """Validates and prepares each individual OrderIntent at the last boundary."""

    def __init__(self, adapter: Any):
        self.adapter = adapter

    async def check(
        self,
        intent: OrderIntent,
        risk_class: EconomicRiskClass,
        *,
        reserved_open_orders: int = 0,
        reserved_notional: Decimal = Decimal("0"),
        exclude_client_order_id: Optional[str] = None,
        require_reduce_only_for_risk_reduction: bool = True,
        allow_emergency_fallback: bool = False,
    ) -> GateResult:
        if self.adapter.env != self.adapter.testnet_environment:
            return GateResult(False, "Mutable execution is restricted to Binance Testnet")
        risk = _risk_class(risk_class)
        if risk is None or risk == EconomicRiskClass.NOOP:
            return GateResult(False, "Invalid economic risk class")
        emergency_fallback = (
            allow_emergency_fallback and risk == EconomicRiskClass.EMERGENCY
        )
        adapter_state = getattr(self.adapter.connection_state, "value", self.adapter.connection_state)
        if not emergency_fallback and adapter_state != ConnectionState.READY.value:
            return GateResult(False, "Adapter not READY")
        if not bool(getattr(self.adapter, "authenticated", False)):
            return GateResult(False, "Adapter is not authenticated")
        if not bool(
            getattr(getattr(self.adapter, "capabilities", None), "trade_authorized", False)
        ):
            return GateResult(False, "Testnet trade permission is not verified")
        stream_health = getattr(self.adapter, "private_stream_healthy", None)
        if stream_health is None:
            stream_health = bool(
                getattr(self.adapter, "user_stream", None)
                and getattr(self.adapter.user_stream, "is_connected", False)
            )
        if not emergency_fallback and not bool(stream_health):
            return GateResult(False, "Private stream disconnected")
        if not emergency_fallback and getattr(self.adapter.reconciliation, "last_status", "UNKNOWN") != "IN_SYNC":
            return GateResult(False, "Reconciliation not IN_SYNC")

        risk_increasing = risk in {
            EconomicRiskClass.NEW_RISK,
            EconomicRiskClass.INCREASE_RISK,
        }
        snapshot_checker = getattr(self.adapter, "is_account_snapshot_fresh", None)
        if (
            risk_increasing
            and not emergency_fallback
            and (not callable(snapshot_checker) or not snapshot_checker())
        ):
            return GateResult(False, "Account snapshot is missing, stale, invalid, or not Testnet")
        if risk_increasing and not emergency_fallback:
            snapshot = getattr(self.adapter, "account_snapshot", None)
            if snapshot is None:
                snapshot = getattr(getattr(self.adapter, "ledger", None), "account_snapshot", None)
            if not _available_balance_is_positive(snapshot):
                return GateResult(False, "Available Testnet balance is not positive")
            if not _margin_utilization_is_safe(snapshot):
                return GateResult(False, "Margin utilization is at or above the Testnet safety limit")
            if not _liquidation_safety_is_known_and_positive(snapshot):
                return GateResult(False, "Liquidation safety is UNKNOWN")

        symbol = str(intent.symbol).upper()
        limits = self.adapter.safety_limits
        if symbol not in limits.allowed_symbols:
            return GateResult(False, f"Symbol {symbol} is not allowed by Testnet limits")
        if intent.market_type != MarketType.USDM_FUTURES:
            return GateResult(False, "Only USDⓈ-M Futures intents are supported")
        order_type = getattr(intent.order_type, "value", intent.order_type)
        order_type = str(order_type).upper()
        if order_type not in {OrderType.LIMIT.value, OrderType.MARKET.value}:
            return GateResult(False, f"Unsupported order type {order_type}")

        rules = self.adapter.symbol_rules.get(symbol)
        if rules is None or not rules.is_ready_for(order_type):
            return GateResult(False, f"Trading rules are unavailable or symbol is not TRADING: {symbol}")

        try:
            side = (
                intent.side
                if isinstance(intent.side, OrderSide)
                else OrderSide(str(intent.side))
            )
        except (TypeError, ValueError):
            return GateResult(False, "Invalid order side")

        try:
            position_side = (
                intent.position_side
                if isinstance(intent.position_side, PositionSide)
                else PositionSide(str(intent.position_side))
            )
        except (TypeError, ValueError):
            return GateResult(False, "Invalid positionSide")
        if self.adapter.capabilities.hedge_mode and position_side == PositionSide.BOTH:
            return GateResult(False, "Hedge Mode requires LONG or SHORT positionSide")
        if not self.adapter.capabilities.hedge_mode and position_side != PositionSide.BOTH:
            return GateResult(False, "One-Way Mode requires BOTH positionSide")

        try:
            time_in_force = (
                intent.time_in_force
                if isinstance(intent.time_in_force, TimeInForce)
                else TimeInForce(str(intent.time_in_force).upper())
            )
        except (TypeError, ValueError):
            return GateResult(False, "Invalid timeInForce")

        try:
            quantity = Decimal(str(intent.quantity))
        except (InvalidOperation, ValueError):
            return GateResult(False, "Invalid quantity")
        if not quantity.is_finite() or quantity <= 0:
            return GateResult(False, "Quantity must be positive and finite")
        quantity = rules.normalize_quantity(quantity, is_market=order_type == OrderType.MARKET.value)
        if not quantity.is_finite() or quantity <= 0:
            return GateResult(False, "Quantity becomes zero after exchange step normalization")
        min_qty = rules.market_min_qty if order_type == OrderType.MARKET.value and rules.market_min_qty else rules.min_qty
        max_qty = rules.market_max_qty if order_type == OrderType.MARKET.value and rules.market_max_qty else rules.max_qty
        if quantity < min_qty:
            return GateResult(False, f"Quantity {quantity} is below minimum {min_qty}")
        if max_qty > 0 and quantity > max_qty:
            return GateResult(False, f"Quantity {quantity} exceeds maximum {max_qty}")

        price: Optional[Decimal] = None
        if order_type == OrderType.LIMIT.value:
            if time_in_force not in {
                TimeInForce.GTC,
                TimeInForce.IOC,
                TimeInForce.FOK,
                TimeInForce.POST_ONLY,
            }:
                return GateResult(False, "Unsupported LIMIT timeInForce")
            if intent.price is None:
                return GateResult(False, "LIMIT order requires a price")
            try:
                price = rules.normalize_price(Decimal(str(intent.price)))
            except (InvalidOperation, ValueError):
                return GateResult(False, "Invalid limit price")
            if not price.is_finite() or price <= 0:
                return GateResult(False, "Limit price must be positive and finite")
            if rules.parsed_from_exchange_info and (
                price < rules.min_price or price > rules.max_price
            ):
                return GateResult(False, "Limit price is outside the exchange price bounds")
            estimated_price = price
        else:
            if time_in_force != TimeInForce.GTC:
                return GateResult(False, "MARKET orders do not support this timeInForce")
            if intent.price is not None:
                return GateResult(False, "MARKET order must not provide a limit price")
            estimated_price = await self.adapter.get_fresh_market_price(
                symbol,
                getattr(intent.side, "value", intent.side),
            )
            if estimated_price is None:
                return GateResult(False, f"Fresh market price unavailable for {symbol}")

        # Check the timestamp after price discovery.  A MARKET order may have
        # had no usable cached quote and therefore refresh from the Testnet
        # book; that fresh REST sample must be accepted only if its own event
        # or receipt timestamp is within the same per-symbol bound.
        if risk_increasing and not emergency_fallback:
            has_market_sample = getattr(
                self.adapter, "has_authoritative_market_sample", None
            )
            if callable(has_market_sample) and not has_market_sample(symbol):
                return GateResult(
                    False,
                    f"Authoritative Testnet market sample unavailable for {symbol}",
                )
            last_event = getattr(self.adapter, "last_market_event_at", {}).get(symbol)
            age = _age_seconds(last_event)
            if age is None or age < 0 or age > _positive_float("MAX_MARKET_DATA_AGE_SEC", 3.0):
                return GateResult(False, f"Market data stale for {symbol}")

            if order_type == OrderType.MARKET.value:
                depth = (
                    getattr(self.adapter, "last_market_ask_qty", {}).get(symbol)
                    if side == OrderSide.BUY
                    else getattr(self.adapter, "last_market_bid_qty", {}).get(symbol)
                )
                if depth is None or not depth.is_finite() or depth <= 0:
                    return GateResult(False, f"Top-of-book depth is unknown for {symbol}")
                if quantity > depth:
                    return GateResult(
                        False,
                        f"Market quantity exceeds available top-of-book depth for {symbol}",
                    )

        notional = quantity * estimated_price
        if notional.is_nan() or notional.is_infinite() or notional <= 0:
            return GateResult(False, "Order notional is invalid")
        if rules.min_notional > 0 and notional < rules.min_notional:
            return GateResult(False, f"Notional {notional} is below minimum {rules.min_notional}")
        if intent.reduce_only and risk in {
            EconomicRiskClass.NEW_RISK,
            EconomicRiskClass.INCREASE_RISK,
        }:
            return GateResult(False, "reduceOnly order cannot be classified as risk increasing")
        if (
            require_reduce_only_for_risk_reduction
            and _is_risk_reducing(risk)
            and not intent.reduce_only
        ):
            return GateResult(False, "Risk-reducing and emergency orders must be reduceOnly")

        if intent.reduce_only and _is_risk_reducing(risk):
            reducible_exposure = Decimal("0")
            for position in await self.adapter.ledger.get_positions():
                if position.symbol != symbol:
                    continue
                position_side_matches = (
                    position_side == PositionSide.BOTH
                    or position.position_side in {position_side, PositionSide.BOTH}
                )
                if position_side_matches:
                    position_quantity = position.quantity
                    if not position_quantity.is_finite() or position_quantity == 0:
                        continue
                    # Binance positionAmt is signed: SELL reduces a positive
                    # long/BOTH position and BUY reduces a negative short/BOTH
                    # position.  A reduceOnly flag alone is not enough to
                    # prove that the requested side actually de-risks.
                    if (
                        side == OrderSide.SELL and position_quantity > 0
                    ) or (
                        side == OrderSide.BUY and position_quantity < 0
                    ):
                        reducible_exposure += abs(position_quantity)
            if reducible_exposure < quantity:
                return GateResult(
                    False,
                    "reduceOnly side or quantity exceeds known Testnet exposure",
                )

        if risk_increasing:
            open_orders = await self.adapter.ledger.get_open_orders()
            current_open_orders = [
                order
                for order in open_orders
                if order.client_order_id != exclude_client_order_id
            ]
            if len(current_open_orders) + reserved_open_orders >= limits.max_open_orders:
                return GateResult(False, "Maximum Testnet open-order count reached")

            existing_notional = Decimal("0")
            for order in current_open_orders:
                if order.price <= 0:
                    return GateResult(False, "Existing open-order notional is unknown")
                existing_notional += abs(order.quantity * order.price)
            for position in await self.adapter.ledger.get_positions():
                if position.quantity == 0:
                    continue
                mark_price = position.mark_price
                if mark_price is None or not mark_price.is_finite() or mark_price <= 0:
                    return GateResult(False, "Existing position notional is unknown")
                existing_notional += abs(position.quantity * mark_price)
            if existing_notional + reserved_notional + notional > limits.max_total_open_notional:
                return GateResult(False, "Maximum Testnet total open notional exceeded")
            if notional > limits.max_single_order_notional:
                return GateResult(False, "Maximum Testnet single-order notional exceeded")

            active_symbols = {
                order.symbol for order in current_open_orders
            }
            for position in await self.adapter.ledger.get_positions():
                if position.quantity != 0:
                    active_symbols.add(position.symbol)
            if symbol not in active_symbols and len(active_symbols) >= limits.max_active_exposure_chains:
                return GateResult(False, "Maximum Testnet active exposure chains exceeded")

        return GateResult(
            True,
            "Passed",
            PreparedOrder(
                symbol=symbol,
                order_type=order_type,
                quantity=quantity,
                price=price,
                estimated_price=estimated_price,
                notional=notional,
            ),
        )
