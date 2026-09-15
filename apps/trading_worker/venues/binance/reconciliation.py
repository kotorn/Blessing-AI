"""Authoritative Binance USDⓈ-M account, order, fill, and position reconciliation."""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

from domain.enums import OrderSide, PositionSide
from domain.models import ExchangeFill, utc_now

from .ledger import ExecutionLedger
from .config import BinanceEnvironment, environment_label
from .models import BinanceAuthenticationError, ExchangeAccountSnapshot
from .rest_client import BinanceRestClient

logger = logging.getLogger("blessing.binance.reconciliation")


def _exchange_bool(value: object) -> bool:
    """Parse Binance boolean fields without making ``bool('false')`` true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


class ReconciliationDiff(BaseModel):
    code: str
    symbol: Optional[str] = None
    local_value: Optional[object] = None
    exchange_value: Optional[object] = None


class FillRecoveryError(RuntimeError):
    """The exchange reported execution but the authoritative fills were not recovered."""


def _required_decimal(payload: Dict[str, Any], field: str) -> Decimal:
    value = payload.get(field)
    if value in (None, ""):
        raise ValueError(f"Missing required Binance account field: {field}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid Binance account field: {field}") from exc
    if not result.is_finite():
        raise ValueError(f"Non-finite Binance account field: {field}")
    return result


def _required_asset(account: Dict[str, Any], asset_name: str) -> Dict[str, Any]:
    """Return exactly one explicit Binance account-asset record.

    The account-level USD totals are not interchangeable with a USDC (or
    USDT) collateral balance, especially when Binance multi-assets mode is
    enabled.  Risk gates therefore use this record exclusively.
    """

    assets = account.get("assets")
    if not isinstance(assets, list):
        raise ValueError("Binance account response is missing the assets array")
    normalized = str(asset_name).strip().upper()
    matches = [
        item
        for item in assets
        if isinstance(item, dict)
        and str(item.get("asset", "")).strip().upper() == normalized
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Binance account does not contain exactly one collateral asset {normalized}"
        )
    return matches[0]


def _utc_day_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the current UTC calendar-day window, using an exclusive end."""

    current = now or utc_now()
    if current.tzinfo is None:
        raise ValueError("reconciliation timestamps must be timezone-aware")
    current = current.astimezone(timezone.utc)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _margin_mode_observation(
    account: Dict[str, Any], position_risk: List[Dict[str, Any]]
) -> tuple[str, bool]:
    """Derive a supported margin-mode observation without inventing defaults."""

    # This account-level signal takes precedence over per-position
    # ``marginType``. Binance can report individual positions as CROSS while
    # the account is in multi-assets mode; that mode must not be interpreted
    # as single-asset USDC collateral by the Mainnet gate.
    if "multiAssetsMargin" in account and _exchange_bool(account.get("multiAssetsMargin")):
        return "MULTI_ASSET_CROSS", True

    modes: set[str] = set()
    unknown_active_mode = False
    for position in position_risk:
        if not isinstance(position, dict):
            continue
        try:
            active = _position_amount(position) != 0
        except ValueError:
            active = True
        raw_mode = position.get("marginType")
        if raw_mode in (None, ""):
            if active:
                unknown_active_mode = True
            continue
        mode = str(raw_mode).strip().upper()
        if mode not in {"CROSS", "ISOLATED"}:
            return mode or "UNKNOWN", False
        modes.add(mode)

    if unknown_active_mode or len(modes) > 1:
        return ("UNKNOWN" if unknown_active_mode else "MIXED"), False
    if len(modes) == 1:
        return next(iter(modes)), True

    # A flat account still has an account-level mode signal.
    if "multiAssetsMargin" in account:
        return "SINGLE_ASSET_CROSS", True
    return "UNKNOWN", False


def _configured_leverage_observation(
    position_risk: List[Dict[str, Any]],
    *,
    environment: str,
    configured_symbol: str,
    explicit: Decimal | None,
) -> tuple[Decimal | None, bool]:
    """Read the exchange-configured leverage for the exact launch symbol."""

    if environment != environment_label(BinanceEnvironment.MAINNET):
        return None, False
    if explicit is not None:
        if explicit.is_finite() and explicit > 0:
            return explicit, True
        return None, False

    candidates: list[Decimal] = []
    saw_target = False
    for position in position_risk:
        if not isinstance(position, dict):
            continue
        if str(position.get("symbol", "")).strip().upper() != configured_symbol:
            continue
        saw_target = True
        raw = position.get("leverage")
        if raw in (None, ""):
            return None, False
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return None, False
        if not value.is_finite() or value <= 0:
            return None, False
        candidates.append(value)
    if not saw_target or not candidates or len(set(candidates)) != 1:
        return None, False
    return candidates[0], True


def _position_amount(position: Dict[str, Any]) -> Decimal:
    value = position.get("positionAmt")
    if value in (None, ""):
        raise ValueError("Position is missing positionAmt")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Position has an invalid positionAmt") from exc
    if not amount.is_finite():
        raise ValueError("Position has a non-finite positionAmt")
    return amount


def _liquidation_distance(position: Dict[str, Any], mark_price: Decimal) -> Optional[Decimal]:
    raw_liquidation_price = position.get("liquidationPrice")
    if raw_liquidation_price in (None, ""):
        return None
    try:
        liquidation_price = Decimal(str(raw_liquidation_price))
    except (InvalidOperation, ValueError):
        return None
    if not liquidation_price.is_finite() or liquidation_price <= 0 or mark_price <= 0:
        return None

    position_side = str(position.get("positionSide", "BOTH")).upper()
    amount = _position_amount(position)
    if amount == 0:
        return None
    if position_side == PositionSide.LONG.value and amount > 0:
        distance = (mark_price - liquidation_price) / mark_price
    elif position_side == PositionSide.SHORT.value and amount < 0:
        distance = (liquidation_price - mark_price) / mark_price
    elif position_side == PositionSide.BOTH.value and amount > 0:
        distance = (mark_price - liquidation_price) / mark_price
    elif position_side == PositionSide.BOTH.value and amount < 0:
        distance = (liquidation_price - mark_price) / mark_price
    else:
        return None

    if not distance.is_finite():
        return None
    return min(Decimal("100"), max(Decimal("0"), distance * Decimal("100")))


def build_account_snapshot(
    account: Dict[str, Any],
    position_risk: List[Dict[str, Any]],
    *,
    environment: str = "BINANCE_TESTNET",
    daily_realized_pnl: Decimal | None = None,
    daily_loss_known: bool = False,
    daily_loss_asset: str = "UNKNOWN",
    daily_pnl_includes_fees: bool = False,
    daily_pnl_includes_funding: bool = False,
    daily_loss_window_start: datetime | None = None,
    daily_loss_window_end: datetime | None = None,
    configured_leverage: Decimal | None = None,
    configured_symbol: str = "ETHUSDC",
) -> ExchangeAccountSnapshot:
    """Build a truthful snapshot from current Binance v2 account and position responses."""

    if environment not in {
        environment_label(BinanceEnvironment.TESTNET),
        environment_label(BinanceEnvironment.MAINNET),
    }:
        raise ValueError("Account snapshots are accepted only from fixed Binance environments")
    if not isinstance(account, dict) or not isinstance(position_risk, list):
        raise ValueError("Binance account snapshot payload is invalid")

    collateral_asset = (
        "USDC"
        if environment == environment_label(BinanceEnvironment.MAINNET)
        else "USDT"
    )
    asset = _required_asset(account, collateral_asset)
    wallet_balance = _required_decimal(asset, "walletBalance")
    margin_balance = _required_decimal(asset, "marginBalance")
    available_balance = _required_decimal(asset, "availableBalance")
    unrealized_pnl = _required_decimal(asset, "unrealizedProfit")
    total_initial_margin = _required_decimal(asset, "initialMargin")
    total_maint_margin = _required_decimal(asset, "maintMargin")
    position_initial_margin = _required_decimal(asset, "positionInitialMargin")

    for field, value in (
        ("totalWalletBalance", wallet_balance),
        ("totalMarginBalance", margin_balance),
        ("availableBalance", available_balance),
        ("totalInitialMargin", total_initial_margin),
        ("totalMaintMargin", total_maint_margin),
        ("totalPositionInitialMargin", position_initial_margin),
    ):
        if value < 0:
            raise ValueError(f"Binance account field cannot be negative: {field}")

    total_notional = Decimal("0")
    liquidation_distances: List[Decimal] = []
    liquidation_known = True
    has_active_position = False

    for position in position_risk:
        if not isinstance(position, dict):
            raise ValueError("Binance position risk entry is invalid")
        amount = _position_amount(position)
        if amount == 0:
            continue
        has_active_position = True
        symbol = position.get("symbol")
        if not symbol:
            raise ValueError("Active Binance position is missing symbol")
        if (
            environment == environment_label(BinanceEnvironment.MAINNET)
            and str(symbol).strip().upper() != str(configured_symbol).strip().upper()
        ):
            raise ValueError(
                f"Mainnet active position is outside the configured symbol {configured_symbol}"
            )
        raw_position_side = position.get("positionSide")
        if raw_position_side in (None, ""):
            raise ValueError(f"Active position {symbol} is missing positionSide")
        position_side = str(raw_position_side).upper()
        try:
            PositionSide(position_side)
        except ValueError as exc:
            raise ValueError(
                f"Active position {symbol} has unsupported positionSide: {position_side}"
            ) from exc
        raw_mark = position.get("markPrice")
        if raw_mark in (None, ""):
            raise ValueError(f"Active position {symbol} is missing markPrice")
        try:
            mark_price = Decimal(str(raw_mark))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Active position {symbol} has invalid markPrice") from exc
        if not mark_price.is_finite() or mark_price <= 0:
            raise ValueError(f"Active position {symbol} has unusable markPrice")

        computed_notional = abs(amount) * mark_price
        if not computed_notional.is_finite() or computed_notional <= 0:
            raise ValueError(f"Active position {symbol} has uncomputable notional")
        raw_notional = position.get("notional")
        if raw_notional not in (None, ""):
            try:
                parsed_notional = abs(Decimal(str(raw_notional)))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(f"Active position {symbol} has invalid notional") from exc
            if (
                not parsed_notional.is_finite()
                or parsed_notional <= 0
                or abs(parsed_notional - computed_notional)
                > max(Decimal("0.00000001"), computed_notional * Decimal("0.01"))
            ):
                raise ValueError(
                    f"Active position {symbol} notional disagrees with positionAmt*markPrice"
                )
        notional = computed_notional
        if notional <= 0 or not notional.is_finite():
            raise ValueError(f"Active position {symbol} has uncomputable notional")
        total_notional += notional

        distance = _liquidation_distance(position, mark_price)
        if distance is None:
            liquidation_known = False
        else:
            liquidation_distances.append(distance)

    margin_mode, margin_mode_known = _margin_mode_observation(account, position_risk)
    observed_configured_leverage, configured_leverage_known = _configured_leverage_observation(
        position_risk,
        environment=environment,
        configured_symbol=str(configured_symbol).strip().upper(),
        explicit=configured_leverage,
    )

    if margin_balance > 0:
        effective_leverage = total_notional / margin_balance
        margin_utilization_pct = (total_initial_margin / margin_balance) * Decimal("100")
    elif has_active_position or total_initial_margin != 0:
        raise ValueError("Account margin balance cannot support exposure calculations")
    else:
        effective_leverage = Decimal("0")
        margin_utilization_pct = Decimal("0")

    return ExchangeAccountSnapshot(
        wallet_balance=wallet_balance,
        margin_balance=margin_balance,
        available_balance=available_balance,
        unrealized_pnl=unrealized_pnl,
        total_initial_margin=total_initial_margin,
        total_maint_margin=total_maint_margin,
        position_initial_margin=position_initial_margin,
        total_position_notional=total_notional,
        effective_leverage=effective_leverage,
        margin_utilization_pct=margin_utilization_pct,
        min_liquidation_distance_pct=(
            min(liquidation_distances) if has_active_position and liquidation_known else None
        ),
        liquidation_safety="KNOWN" if liquidation_known else "UNKNOWN",
        exchange_environment=environment,
        daily_realized_pnl=daily_realized_pnl,
        daily_loss_known=daily_loss_known,
        collateral_asset=collateral_asset,
        risk_currency=collateral_asset,
        daily_loss_asset=(str(daily_loss_asset).strip().upper() or "UNKNOWN"),
        daily_pnl_includes_fees=daily_pnl_includes_fees,
        daily_pnl_includes_funding=daily_pnl_includes_funding,
        daily_loss_window_start=daily_loss_window_start,
        daily_loss_window_end=daily_loss_window_end,
        configured_leverage=observed_configured_leverage,
        configured_leverage_known=configured_leverage_known,
        margin_mode=margin_mode,
        margin_mode_known=margin_mode_known,
        valid=True,
        timestamp=utc_now(),
    )


def _exchange_fill_from_trade(
    trade: Dict[str, Any],
    local_order: Optional[Any],
    *,
    source: str,
) -> ExchangeFill:
    required = (
        "id",
        "orderId",
        "symbol",
        "side",
        "positionSide",
        "qty",
        "price",
        "commission",
        "commissionAsset",
        "realizedPnl",
        "maker",
        "time",
    )
    missing = [field for field in required if trade.get(field) in (None, "")]
    if missing:
        raise FillRecoveryError(f"Trade is missing required fields: {', '.join(missing)}")

    symbol = str(trade.get("symbol") or getattr(local_order, "symbol", "")).upper()
    client_order_id = str(
        trade.get("clientOrderId") or getattr(local_order, "client_order_id", "")
    )
    if not symbol or not client_order_id:
        raise FillRecoveryError("Trade is missing symbol or clientOrderId")
    if local_order is not None:
        if symbol != str(local_order.symbol).upper():
            raise FillRecoveryError("Trade symbol does not match the local order")
        if local_order.exchange_order_id and str(trade["orderId"]) != str(local_order.exchange_order_id):
            raise FillRecoveryError("Trade order ID does not match the local order")
        if trade.get("clientOrderId") and str(trade["clientOrderId"]) != str(local_order.client_order_id):
            raise FillRecoveryError("Trade clientOrderId does not match the local order")
    try:
        side = OrderSide(str(trade.get("side") or local_order.side.value))
        position_side = PositionSide(
            str(trade.get("positionSide") or local_order.position_side.value).upper()
        )
        quantity = Decimal(str(trade["qty"]))
        price = Decimal(str(trade["price"]))
        commission = Decimal(str(trade["commission"]))
        realized_pnl = Decimal(str(trade.get("realizedPnl", "0")))
    except (AttributeError, InvalidOperation, TypeError, ValueError) as exc:
        raise FillRecoveryError("Trade contains invalid fill fields") from exc
    if local_order is not None:
        if side != local_order.side:
            raise FillRecoveryError("Trade side does not match the local order")
        if position_side != local_order.position_side:
            raise FillRecoveryError("Trade positionSide does not match the local order")
    if any(not value.is_finite() for value in (quantity, price, commission, realized_pnl)):
        raise FillRecoveryError("Trade contains non-finite fill fields")
    if quantity <= 0 or price <= 0 or commission < 0 or not str(trade["commissionAsset"]):
        raise FillRecoveryError("Trade contains unusable fill economics")

    return ExchangeFill(
        exchange_trade_id=str(trade["id"]),
        exchange_order_id=str(trade["orderId"]),
        client_order_id=client_order_id,
        symbol=symbol,
        side=side,
        position_side=position_side,
        quantity=quantity,
        price=price,
        commission=commission,
        commission_asset=str(trade["commissionAsset"]),
        realized_pnl=realized_pnl,
        maker=_exchange_bool(trade.get("maker", False)),
        event_time=trade["time"],
        transaction_time=trade.get("time"),
        source=source,
        strategy_id=getattr(local_order, "strategy_id", "portfolio"),
        decision_id=getattr(local_order, "decision_id", None),
        target_exposure_id=getattr(local_order, "target_exposure_id", None),
        source_intent_ids=list(getattr(local_order, "source_intent_ids", []) or []),
    )


class BinanceReconciliation:
    def __init__(self, rest_client: BinanceRestClient, ledger: ExecutionLedger):
        self.rest_client = rest_client
        self.ledger = ledger
        # Test doubles may not expose the enum, but production clients always
        # do.  Keeping this fallback preserves read-only reconciliation tests
        # without weakening the fixed-environment production client.
        client_environment = getattr(rest_client, "env", BinanceEnvironment.TESTNET)
        self.environment = environment_label(client_environment)
        self.last_diffs: List[ReconciliationDiff] = []
        self.last_status = "UNKNOWN"
        self.authentication_failed = False
        self._unattributed_fill_diffs: List[ReconciliationDiff] = []
        self.daily_loss_window_start: datetime | None = None
        self.daily_loss_window_end: datetime | None = None

    def _validate_mainnet_scope(
        self,
        positions: List[Dict[str, Any]],
        open_orders: List[Dict[str, Any]],
    ) -> None:
        """Reject exchange state outside the single configured Mainnet chain."""

        if self.environment != environment_label(BinanceEnvironment.MAINNET):
            return
        for order in open_orders:
            symbol = str(order.get("symbol", "")).strip().upper()
            if symbol != "ETHUSDC":
                raise ValueError(
                    f"Mainnet open order is outside the configured symbol ETHUSDC: {symbol or 'UNKNOWN'}"
                )
        # ``build_account_snapshot`` validates active positions, including
        # mark/notional/position-side fields, so this loop only documents the
        # scope boundary for callers that invoke the validator independently.
        for position in positions:
            if _position_amount(position) == 0:
                continue
            symbol = str(position.get("symbol", "")).strip().upper()
            if symbol != "ETHUSDC":
                raise ValueError(
                    f"Mainnet position is outside the configured symbol ETHUSDC: {symbol or 'UNKNOWN'}"
                )

    async def _daily_realized_pnl(self) -> tuple[Decimal | None, bool]:
        """Read restart-safe UTC-day net PnL for the exact Mainnet launch pair.

        Binance exposes pagination by ``page`` and returns all income types when
        ``incomeType`` is omitted.  We deliberately include only realized PnL,
        commissions, and funding for USDC/ETHUSDC.  A short page is complete;
        a full page at the configured safety bound is unknown and therefore
        blocks Mainnet rather than silently under-counting loss.
        """

        if getattr(self.rest_client, "env", BinanceEnvironment.TESTNET) != BinanceEnvironment.MAINNET:
            return None, True
        start, end = _utc_day_window()
        query_end = utc_now()
        self.daily_loss_window_start = start
        self.daily_loss_window_end = end
        try:
            total = Decimal("0")
            max_pages = 100
            page = 1
            while page <= max_pages:
                payload = await self.rest_client.request(
                    "GET",
                    "/fapi/v1/income",
                    signed=True,
                    params={
                        "symbol": "ETHUSDC",
                        "startTime": int(start.timestamp() * 1000),
                        "endTime": int(query_end.timestamp() * 1000),
                        "page": page,
                        "limit": 1000,
                    },
                )
                if not isinstance(payload, list):
                    return None, False
                for item in payload:
                    if not isinstance(item, dict):
                        return None, False
                    item_asset = str(item.get("asset", "")).strip().upper()
                    item_symbol = str(item.get("symbol", "")).strip().upper()
                    item_type = str(item.get("incomeType", "")).strip().upper()
                    if item_asset != "USDC" or item_symbol != "ETHUSDC":
                        # The server-side symbol filter is an optimization,
                        # not an authorization boundary.  Ignore unrelated
                        # rows, but never let them enter USDC pair PnL.
                        continue
                    if item_type not in {"REALIZED_PNL", "COMMISSION", "FUNDING_FEE"}:
                        continue
                    if item.get("income") in (None, ""):
                        return None, False
                    value = Decimal(str(item["income"]))
                    if not value.is_finite():
                        return None, False
                    total += value
                if len(payload) < 1000:
                    return total, True
                page += 1
            # A full page on the last allowed request means there may be more
            # USDC income records.  The daily loss value is not complete.
            return None, False
        except Exception as exc:
            logger.warning("Unable to verify Mainnet UTC-day net PnL: %s", type(exc).__name__)
            return None, False

    def _set_status(
        self, status: str, diffs: Optional[List[ReconciliationDiff]] = None
    ) -> str:
        self.last_status = status
        if diffs is not None:
            self.last_diffs = diffs
        if status == "MISMATCH":
            logger.error(
                "monitor_event=reconciliation_drift environment=%s diff_count=%d",
                self.environment,
                len(diffs or []),
            )
        elif status == "UNKNOWN":
            logger.error(
                "monitor_event=readiness_degraded environment=%s reason=reconciliation_unknown",
                self.environment,
            )
        return status

    async def _recover_recent_trades(
        self, symbols: set[str]
    ) -> List[ReconciliationDiff]:
        """Recover only fills with local lineage and report foreign fills."""
        diffs: List[ReconciliationDiff] = []
        for symbol in sorted({str(item).upper() for item in symbols if item}):
            trades = await self.rest_client.request(
                "GET",
                "/fapi/v1/userTrades",
                signed=True,
                params={"symbol": symbol, "limit": 1000},
            )
            if not isinstance(trades, list):
                raise FillRecoveryError(f"userTrades response for {symbol} is invalid")
            for trade in trades:
                local_order = await self.ledger.get_order_by_exchange_id(
                    str(trade.get("orderId"))
                )
                if local_order is None and trade.get("clientOrderId"):
                    local_order = await self.ledger.get_order_by_client_id(
                        str(trade["clientOrderId"])
                    )
                if local_order is None:
                    # Binance userTrades does not always return clientOrderId.
                    # An exchange fill with no local order lineage is an
                    # unresolved reconciliation difference, never an order we
                    # silently adopt into the local ledger.
                    diffs.append(
                        ReconciliationDiff(
                            code="EXCHANGE_FILL_UNKNOWN_LOCALLY",
                            symbol=str(trade.get("symbol") or symbol).upper(),
                            exchange_value=str(trade.get("id") or trade.get("orderId") or "UNKNOWN"),
                        )
                    )
                    continue
                fill = _exchange_fill_from_trade(
                    trade, local_order, source=f"{self.environment}_BOOTSTRAP"
                )
                await self.ledger.append_fill(fill)
        self._unattributed_fill_diffs = diffs
        return diffs

    async def _recover_order_fills(self, local_order: Any, order_status: Dict[str, Any]) -> int:
        order_id = order_status.get("orderId") or local_order.exchange_order_id
        if not order_id:
            raise FillRecoveryError("Filled order has no exchange order ID")
        trades = await self.rest_client.request(
            "GET",
            "/fapi/v1/userTrades",
            signed=True,
            params={"symbol": local_order.symbol, "orderId": order_id, "limit": 1000},
        )
        if not isinstance(trades, list):
            raise FillRecoveryError("userTrades recovery response is invalid")
        matching_trades = [t for t in trades if str(t.get("orderId")) == str(order_id)]
        if not matching_trades:
            raise FillRecoveryError(
                f"No fills recovered for exchange order {order_id} reported {order_status.get('status')}"
            )
        recovered_fills: List[ExchangeFill] = []
        for trade in matching_trades:
            recovered_fills.append(
                _exchange_fill_from_trade(
                    trade, local_order, source=f"{self.environment}_RECOVERY"
                )
            )
        recovered_quantity = sum(
            (fill.quantity for fill in recovered_fills), Decimal("0")
        )
        expected_raw = order_status.get("executedQty")
        if expected_raw not in (None, ""):
            try:
                expected_quantity = Decimal(str(expected_raw))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise FillRecoveryError("Order executedQty is invalid") from exc
            if (
                not expected_quantity.is_finite()
                or expected_quantity < 0
                or recovered_quantity != expected_quantity
            ):
                raise FillRecoveryError(
                    "Recovered fill quantity does not match exchange executedQty"
                )
        if str(order_status.get("status", "")).upper() == "FILLED":
            if recovered_quantity != local_order.quantity:
                raise FillRecoveryError(
                    "FILLED order recovered fill quantity does not match origQty"
                )
        elif recovered_quantity <= 0 or recovered_quantity > local_order.quantity:
            raise FillRecoveryError("PARTIALLY_FILLED order has invalid recovered fill quantity")
        for fill in recovered_fills:
            await self.ledger.append_fill(fill)
        return len(recovered_fills)

    @staticmethod
    def _active_position_map(
        positions: List[Dict[str, Any]]
    ) -> Dict[Tuple[str, str], Decimal]:
        result: Dict[Tuple[str, str], Decimal] = {}
        for position in positions:
            amount = _position_amount(position)
            if amount == 0:
                continue
            symbol = position.get("symbol")
            raw_side = position.get("positionSide")
            if raw_side in (None, ""):
                raise ValueError("Active position is missing positionSide")
            side = str(raw_side).upper()
            if not symbol:
                raise ValueError("Active position is missing symbol")
            try:
                PositionSide(side)
            except ValueError as exc:
                raise ValueError(f"Active position has unsupported positionSide: {side}") from exc
            result[(str(symbol).upper(), side)] = amount
        return result

    @staticmethod
    def _compare_open_order(
        local_order: Any, exchange_order: Dict[str, Any]
    ) -> List[ReconciliationDiff]:
        """Compare exchange economics, not only order identity."""
        diffs: List[ReconciliationDiff] = []
        symbol = str(exchange_order.get("symbol", local_order.symbol)).upper()

        def add(code: str, local_value: Any, exchange_value: Any) -> None:
            if local_value != exchange_value:
                diffs.append(
                    ReconciliationDiff(
                        code=code,
                        symbol=symbol,
                        local_value=str(local_value),
                        exchange_value=str(exchange_value),
                    )
                )

        add("ORDER_STATUS_MISMATCH", str(local_order.status).upper(), str(exchange_order.get("status", "UNKNOWN")).upper())
        add("ORDER_SIDE_MISMATCH", local_order.side.value, str(exchange_order.get("side", "UNKNOWN")).upper())
        add(
            "ORDER_POSITION_SIDE_MISMATCH",
            local_order.position_side.value,
            str(exchange_order.get("positionSide", "BOTH")).upper(),
        )
        add(
            "ORDER_REDUCE_ONLY_MISMATCH",
            bool(local_order.reduce_only),
            _exchange_bool(exchange_order.get("reduceOnly", False)),
        )

        try:
            exchange_quantity = Decimal(str(exchange_order.get("origQty")))
        except (InvalidOperation, TypeError, ValueError):
            exchange_quantity = None
        if exchange_quantity is None or not exchange_quantity.is_finite() or exchange_quantity <= 0:
            diffs.append(
                ReconciliationDiff(
                    code="ORDER_QUANTITY_UNKNOWN",
                    symbol=symbol,
                    local_value=str(local_order.quantity),
                    exchange_value=str(exchange_order.get("origQty")),
                )
            )
        else:
            add("ORDER_QUANTITY_MISMATCH", local_order.quantity, exchange_quantity)

        order_type = str(exchange_order.get("type", local_order.order_type)).upper()
        raw_price = exchange_order.get("price")
        if order_type != "MARKET":
            try:
                exchange_price = Decimal(str(raw_price))
            except (InvalidOperation, TypeError, ValueError):
                exchange_price = None
            if exchange_price is None or not exchange_price.is_finite() or exchange_price <= 0:
                diffs.append(
                    ReconciliationDiff(
                        code="ORDER_PRICE_UNKNOWN",
                        symbol=symbol,
                        local_value=str(local_order.price),
                        exchange_value=str(raw_price),
                    )
                )
            else:
                add("ORDER_PRICE_MISMATCH", local_order.price, exchange_price)

        if local_order.exchange_order_id and str(local_order.exchange_order_id) != str(exchange_order.get("orderId")):
            diffs.append(
                ReconciliationDiff(
                    code="ORDER_EXCHANGE_ID_MISMATCH",
                    symbol=symbol,
                    local_value=str(local_order.exchange_order_id),
                    exchange_value=str(exchange_order.get("orderId")),
                )
            )
        if local_order.client_order_id and exchange_order.get("clientOrderId") and local_order.client_order_id != str(exchange_order.get("clientOrderId")):
            diffs.append(
                ReconciliationDiff(
                    code="ORDER_CLIENT_ID_MISMATCH",
                    symbol=symbol,
                    local_value=local_order.client_order_id,
                    exchange_value=str(exchange_order.get("clientOrderId")),
                )
            )
        return diffs

    async def _collect_diffs(
        self,
        exchange_positions: List[Dict[str, Any]],
        exchange_open_orders: List[Dict[str, Any]],
    ) -> List[ReconciliationDiff]:
        for exchange_order in exchange_open_orders:
            if not isinstance(exchange_order, dict):
                raise ValueError("Binance open order entry is invalid")
            required = ("symbol", "orderId", "clientOrderId", "status", "side", "origQty")
            missing = [field for field in required if exchange_order.get(field) in (None, "")]
            if missing:
                raise ValueError(
                    "Binance open order is missing required fields: " + ", ".join(missing)
                )

        exchange_order_ids = {str(o.get("orderId")): o for o in exchange_open_orders}
        exchange_client_ids = {
            str(o.get("clientOrderId")): o
            for o in exchange_open_orders
            if o.get("clientOrderId")
        }
        diffs: List[ReconciliationDiff] = list(self._unattributed_fill_diffs)
        recovered_position_symbols: set[str] = set()
        recovered_reduction_symbols: set[str] = set()
        get_all_orders = getattr(self.ledger, "get_all_orders", None)
        all_orders = (
            await get_all_orders()
            if callable(get_all_orders)
            else await self.ledger.get_open_orders()
        )

        for local_order in all_orders:
            local_status = str(local_order.status).upper()
            active_local_order = local_status in {"NEW", "PARTIALLY_FILLED"}
            terminal_execution = local_status in {"FILLED", "PARTIALLY_FILLED"}
            found = bool(
                local_order.exchange_order_id
                and str(local_order.exchange_order_id) in exchange_order_ids
            ) or bool(
                local_order.client_order_id
                and local_order.client_order_id in exchange_client_ids
            )
            if found:
                exchange_order = (
                    exchange_order_ids.get(str(local_order.exchange_order_id))
                    if local_order.exchange_order_id
                    else None
                )
                if exchange_order is None and local_order.client_order_id:
                    exchange_order = exchange_client_ids.get(local_order.client_order_id)
                if exchange_order is not None:
                    diffs.extend(self._compare_open_order(local_order, exchange_order))
                    if local_status == "PARTIALLY_FILLED":
                        try:
                            executed_qty = Decimal(str(exchange_order.get("executedQty", "0")))
                        except (InvalidOperation, TypeError, ValueError):
                            executed_qty = None
                        if executed_qty is None or executed_qty < 0:
                            diffs.append(
                                ReconciliationDiff(
                                    code="PARTIAL_EXECUTED_QTY_UNKNOWN",
                                    symbol=local_order.symbol,
                                    local_value=local_order.client_order_id,
                                )
                            )
                        elif executed_qty > 0:
                            try:
                                await self._recover_order_fills(local_order, exchange_order)
                            except BinanceAuthenticationError:
                                raise
                            except Exception as exc:
                                logger.warning(
                                    "Fill recovery failed for open partial order %s: %s",
                                    local_order.client_order_id,
                                    exc,
                                )
                                diffs.append(
                                    ReconciliationDiff(
                                        code="FILL_RECOVERY_FAILED",
                                        symbol=local_order.symbol,
                                        local_value=local_order.client_order_id,
                                        exchange_value=str(exchange_order.get("orderId", "")),
                                    )
                                )
                    elif local_status == "FILLED":
                        try:
                            await self._recover_order_fills(local_order, exchange_order)
                        except BinanceAuthenticationError:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "Fill recovery failed for terminal open order %s: %s",
                                local_order.client_order_id,
                                exc,
                            )
                            diffs.append(
                                ReconciliationDiff(
                                    code="FILL_RECOVERY_FAILED",
                                    symbol=local_order.symbol,
                                    local_value=local_order.client_order_id,
                                    exchange_value=str(exchange_order.get("orderId", "")),
                                )
                            )
                # A FILLED/PARTIALLY_FILLED local order is still queried below
                # when it is absent from openOrders; an open match already gave
                # us the authoritative order record and any recoverable fills.
                if active_local_order or terminal_execution:
                    continue

            query_params = {"symbol": local_order.symbol}
            if local_order.client_order_id:
                query_params["origClientOrderId"] = local_order.client_order_id
            elif local_order.exchange_order_id:
                query_params["orderId"] = local_order.exchange_order_id
            try:
                order_status = await self.rest_client.request(
                    "GET", "/fapi/v1/order", signed=True, params=query_params
                )
            except BinanceAuthenticationError:
                raise
            except Exception as exc:
                logger.warning(
                    "Order query failed for missing order %s: %s",
                    local_order.client_order_id,
                    exc,
                )
                diffs.append(
                    ReconciliationDiff(
                        code="LOCAL_OPEN_ORDER_STATUS_UNKNOWN",
                        symbol=local_order.symbol,
                        local_value=local_order.client_order_id,
                    )
                )
                continue

            if not isinstance(order_status, dict):
                diffs.append(
                    ReconciliationDiff(
                        code="LOCAL_ORDER_STATUS_UNKNOWN",
                        symbol=local_order.symbol,
                        local_value=local_order.client_order_id or local_order.exchange_order_id,
                    )
                )
                continue
            status = str(order_status.get("status", "UNKNOWN")).upper()
            if status in ("FILLED", "PARTIALLY_FILLED"):
                try:
                    await self._recover_order_fills(local_order, order_status)
                except BinanceAuthenticationError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Fill recovery failed for %s: %s", local_order.client_order_id, exc
                    )
                    diffs.append(
                        ReconciliationDiff(
                            code="FILL_RECOVERY_FAILED",
                            symbol=local_order.symbol,
                            local_value=local_order.client_order_id,
                            exchange_value=str(order_status.get("orderId", "")),
                        )
                    )
                    continue
                local_order.status = status
                local_order.exchange_order_id = str(
                    order_status.get("orderId") or local_order.exchange_order_id or ""
                )
                await self.ledger.upsert_order(local_order)
                recovered_position_symbols.add(str(local_order.symbol).upper())
                if local_order.reduce_only or local_order.risk_class in {
                    "REDUCE_RISK",
                    "RECOVERY",
                    "CLOSE",
                    "EMERGENCY",
                }:
                    recovered_reduction_symbols.add(str(local_order.symbol).upper())
            elif status in ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED"):
                if terminal_execution:
                    diffs.append(
                        ReconciliationDiff(
                            code="TERMINAL_ORDER_STATUS_MISMATCH",
                            symbol=local_order.symbol,
                            local_value=local_status,
                            exchange_value=status,
                        )
                    )
                elif active_local_order:
                    local_order.status = status
                    await self.ledger.upsert_order(local_order)
            else:
                diffs.append(
                    ReconciliationDiff(
                        code="LOCAL_OPEN_ORDER_MISSING_ON_EXCHANGE",
                        symbol=local_order.symbol,
                        local_value=local_order.client_order_id or local_order.exchange_order_id,
                        exchange_value=status,
                    )
                )

        for exchange_order_id, exchange_order in exchange_order_ids.items():
            local_match = None
            client_id = exchange_order.get("clientOrderId")
            if client_id:
                local_match = await self.ledger.get_order_by_client_id(str(client_id))
            if local_match is None:
                local_match = await self.ledger.get_order_by_exchange_id(exchange_order_id)
            if local_match is None:
                diffs.append(
                    ReconciliationDiff(
                        code="EXCHANGE_OPEN_ORDER_UNKNOWN_LOCALLY",
                        symbol=exchange_order.get("symbol"),
                        exchange_value=exchange_order_id,
                    )
                )
            elif str(local_match.status).upper() not in {"NEW", "PARTIALLY_FILLED"}:
                # A terminal local record cannot coexist with an exchange
                # order that is still open, even when the IDs match.
                diffs.extend(self._compare_open_order(local_match, exchange_order))

        # A locally tracked order can disappear from openOrders after a fill
        # before its private-stream position event arrives. Once the order
        # status and canonical userTrades have both been recovered, seed only
        # that symbol from the authoritative positionRisk response. Other
        # symbols remain subject to the normal mismatch checks below.
        for position in exchange_positions:
            if str(position.get("symbol", "")).upper() in recovered_position_symbols:
                await self.ledger.upsert_position(position)

        exchange_position_map = self._active_position_map(exchange_positions)
        for symbol in recovered_reduction_symbols:
            if not any(key[0] == symbol for key in exchange_position_map):
                clear_positions = getattr(self.ledger, "clear_positions_for_symbol", None)
                if callable(clear_positions):
                    await clear_positions(symbol)
                else:
                    diffs.append(
                        ReconciliationDiff(
                            code="LOCAL_CLOSED_POSITION_NOT_CLEARED",
                            symbol=symbol,
                        )
                    )
        local_position_map: Dict[Tuple[str, str], Decimal] = {}
        for position in await self.ledger.get_positions():
            if position.quantity == 0:
                continue
            local_position_map[
                (str(position.symbol).upper(), position.position_side.value)
            ] = position.quantity

        for key, local_amount in local_position_map.items():
            exchange_amount = exchange_position_map.get(key)
            if exchange_amount is None:
                diffs.append(
                    ReconciliationDiff(
                        code="LOCAL_POSITION_MISSING_ON_EXCHANGE",
                        symbol=key[0],
                        local_value=str(local_amount),
                        exchange_value="0",
                    )
                )
            elif exchange_amount != local_amount:
                diffs.append(
                    ReconciliationDiff(
                        code="POSITION_QTY_MISMATCH",
                        symbol=key[0],
                        local_value=str(local_amount),
                        exchange_value=str(exchange_amount),
                    )
                )
        for key, exchange_amount in exchange_position_map.items():
            if key not in local_position_map:
                diffs.append(
                    ReconciliationDiff(
                        code="EXCHANGE_POSITION_UNKNOWN_LOCALLY",
                        symbol=key[0],
                        local_value="0",
                        exchange_value=str(exchange_amount),
                    )
                )

        # A terminal local order is not proof of execution. Every FILLED or
        # PARTIALLY_FILLED record must have at least one canonical ExchangeFill
        # linked by exchange order ID or client order ID before reconciliation
        # can report IN_SYNC.
        get_fills = getattr(self.ledger, "get_fills", None)
        fills = await get_fills() if callable(get_fills) else []
        for local_order in all_orders:
            local_status = str(local_order.status).upper()
            if local_status not in {"FILLED", "PARTIALLY_FILLED"}:
                continue
            linked_fills = [
                fill
                for fill in fills
                if (
                    local_order.exchange_order_id
                    and str(fill.exchange_order_id) == str(local_order.exchange_order_id)
                )
                or (
                    local_order.client_order_id
                    and str(fill.client_order_id) == str(local_order.client_order_id)
                )
            ]
            has_linked_fill = bool(linked_fills)
            if not has_linked_fill:
                diffs.append(
                    ReconciliationDiff(
                        code="TERMINAL_ORDER_FILL_MISSING",
                        symbol=str(local_order.symbol).upper(),
                        local_value=local_order.client_order_id
                        or local_order.exchange_order_id,
                    )
                )
                continue
            recovered_quantity = sum(
                (fill.quantity for fill in linked_fills), Decimal("0")
            )
            if local_status == "FILLED" and recovered_quantity != local_order.quantity:
                diffs.append(
                    ReconciliationDiff(
                        code="FILL_QUANTITY_MISMATCH",
                        symbol=str(local_order.symbol).upper(),
                        local_value=str(local_order.quantity),
                        exchange_value=str(recovered_quantity),
                    )
                )
            elif local_status == "PARTIALLY_FILLED" and (
                recovered_quantity <= 0 or recovered_quantity > local_order.quantity
            ):
                diffs.append(
                    ReconciliationDiff(
                        code="PARTIAL_FILL_QUANTITY_INVALID",
                        symbol=str(local_order.symbol).upper(),
                        local_value=str(local_order.quantity),
                        exchange_value=str(recovered_quantity),
                    )
                )
        return diffs

    async def bootstrap(self) -> bool:
        """Fetch, seed, compare, and only then mark the ledger synchronized."""
        self.authentication_failed = False
        self._unattributed_fill_diffs = []
        self._set_status("RECONCILING", [])
        try:
            positions = await self.rest_client.request(
                "GET", "/fapi/v2/positionRisk", signed=True
            )
            open_orders = await self.rest_client.request(
                "GET", "/fapi/v1/openOrders", signed=True
            )
            account = await self.rest_client.request("GET", "/fapi/v2/account", signed=True)
            if not isinstance(positions, list) or not isinstance(open_orders, list):
                raise ValueError("Binance bootstrap response is invalid")
            self._validate_mainnet_scope(positions, open_orders)
            # Validate account math and every active position before any
            # recovery/ledger mutation. A malformed mark/notional row must not
            # leave a partial position with fabricated zero fields behind.
            daily_realized_pnl, daily_loss_known = await self._daily_realized_pnl()
            snapshot = build_account_snapshot(
                account,
                positions,
                environment=self.environment,
                daily_realized_pnl=daily_realized_pnl,
                daily_loss_known=daily_loss_known,
                daily_loss_asset=("USDC" if self.environment == environment_label(BinanceEnvironment.MAINNET) else "UNKNOWN"),
                daily_pnl_includes_fees=(daily_loss_known and self.environment == environment_label(BinanceEnvironment.MAINNET)),
                daily_pnl_includes_funding=(daily_loss_known and self.environment == environment_label(BinanceEnvironment.MAINNET)),
                daily_loss_window_start=self.daily_loss_window_start,
                daily_loss_window_end=self.daily_loss_window_end,
            )

            active_positions = [p for p in positions if _position_amount(p) != 0]
            # Bootstrap never silently adopts exchange exposure or orders that
            # have no local lineage.  An empty, uninitialized ledger is safe
            # to initialize only when the authoritative Testnet account is
            # also empty of positions and open orders.
            local_open_orders = await self.ledger.get_open_orders()
            local_positions = await self.ledger.get_positions()
            get_all_orders = getattr(self.ledger, "get_all_orders", None)
            local_orders = (
                await get_all_orders() if callable(get_all_orders) else local_open_orders
            )
            get_fills = getattr(self.ledger, "get_fills", None)
            local_fills = await get_fills() if callable(get_fills) else []
            has_local_exchange_state = bool(local_orders) or bool(local_fills) or any(
                position.quantity != 0 for position in local_positions
            )
            await self.ledger.set_account_snapshot(None)
            if not has_local_exchange_state and (active_positions or open_orders):
                self._set_status(
                    "UNKNOWN",
                    [
                        ReconciliationDiff(
                            code="EXCHANGE_STATE_UNOWNED",
                            local_value="EMPTY_LEDGER",
                            exchange_value={
                                "active_positions": len(active_positions),
                                "open_orders": len(open_orders),
                            },
                        )
                    ],
                )
                return False

            symbols = {
                str(position.get("symbol")) for position in active_positions if position.get("symbol")
            }
            symbols.update(
                str(order.get("symbol")) for order in open_orders if order.get("symbol")
            )
            tracked_orders = (
                await get_all_orders()
                if callable(get_all_orders)
                else await self.ledger.get_open_orders()
            )
            symbols.update(
                str(order.symbol)
                for order in tracked_orders
                if getattr(order, "symbol", None)
            )
            await self._recover_recent_trades(symbols)

            diffs = await self._collect_diffs(positions, open_orders)
            if diffs:
                self._set_status("MISMATCH", diffs)
                return False
            # Refresh local snapshots only after the authoritative comparison
            # succeeds. This keeps a mismatch inspectable and prevents stale
            # mark/quantity fields from feeding subsequent risk gates.
            await self.ledger.replace_positions(
                active_positions, mark_initialized=False
            )
            for order_data in open_orders:
                await self.ledger.upsert_raw_exchange_order(order_data)
            await self.ledger.set_account_snapshot(snapshot)
            await self.ledger.mark_initialized()
            self._set_status("IN_SYNC", [])
            logger.info(
                "Bootstrap verified: %d active positions, %d open orders",
                len(active_positions),
                len(open_orders),
            )
            return True
        except BinanceAuthenticationError as exc:
            self.authentication_failed = True
            logger.error("Ledger bootstrap authentication failed: %s", exc)
            self._set_status(
                "UNKNOWN",
                [ReconciliationDiff(code="BOOTSTRAP_AUTHENTICATION_FAILED", exchange_value=str(exc))],
            )
            return False
        except Exception as exc:
            logger.error("Ledger bootstrap failed: %s", exc)
            self._set_status(
                "UNKNOWN",
                [ReconciliationDiff(code="BOOTSTRAP_VERIFICATION_FAILED", exchange_value=str(exc))],
            )
            return False

    async def reconcile(self) -> str:
        """Return IN_SYNC only after authoritative orders, fills, positions, and account math pass."""
        self.authentication_failed = False
        if not await self.ledger.is_initialized():
            await self.bootstrap()
            return self.last_status

        self._unattributed_fill_diffs = []
        self._set_status("RECONCILING", [])
        try:
            exchange_positions = await self.rest_client.request(
                "GET", "/fapi/v2/positionRisk", signed=True
            )
            exchange_open_orders = await self.rest_client.request(
                "GET", "/fapi/v1/openOrders", signed=True
            )
            account = await self.rest_client.request("GET", "/fapi/v2/account", signed=True)
            if not isinstance(exchange_positions, list) or not isinstance(exchange_open_orders, list):
                raise ValueError("Binance reconciliation response is invalid")
            self._validate_mainnet_scope(exchange_positions, exchange_open_orders)
            # Validate the authoritative account/position snapshot before
            # _collect_diffs can seed any recovered position into the ledger.
            daily_realized_pnl, daily_loss_known = await self._daily_realized_pnl()
            snapshot = build_account_snapshot(
                account,
                exchange_positions,
                environment=self.environment,
                daily_realized_pnl=daily_realized_pnl,
                daily_loss_known=daily_loss_known,
                daily_loss_asset=("USDC" if self.environment == environment_label(BinanceEnvironment.MAINNET) else "UNKNOWN"),
                daily_pnl_includes_fees=(daily_loss_known and self.environment == environment_label(BinanceEnvironment.MAINNET)),
                daily_pnl_includes_funding=(daily_loss_known and self.environment == environment_label(BinanceEnvironment.MAINNET)),
                daily_loss_window_start=self.daily_loss_window_start,
                daily_loss_window_end=self.daily_loss_window_end,
            )

            symbols = {
                str(position.get("symbol"))
                for position in exchange_positions
                if position.get("symbol") and _position_amount(position) != 0
            }
            symbols.update(
                str(order.get("symbol"))
                for order in exchange_open_orders
                if order.get("symbol")
            )
            get_all_orders = getattr(self.ledger, "get_all_orders", None)
            tracked_orders = (
                await get_all_orders()
                if callable(get_all_orders)
                else await self.ledger.get_open_orders()
            )
            symbols.update(
                str(order.symbol)
                for order in tracked_orders
                if getattr(order, "symbol", None)
            )
            await self._recover_recent_trades(symbols)
            diffs = await self._collect_diffs(exchange_positions, exchange_open_orders)
            if diffs:
                self._set_status("MISMATCH", diffs)
                return self.last_status

            # Keep the local position-risk fields (mark, liquidation price,
            # leverage, and quantity) authoritative after every successful
            # reconciliation, not only during bootstrap.
            await self.ledger.replace_positions(
                exchange_positions, mark_initialized=False
            )
            await self.ledger.update_balances(snapshot.wallet_balance, snapshot.margin_balance)
            await self.ledger.set_account_snapshot(snapshot)
            self._set_status("IN_SYNC", [])
            return self.last_status
        except BinanceAuthenticationError as exc:
            self.authentication_failed = True
            logger.error("Binance reconciliation authentication failed: %s", exc)
            self._set_status(
                "UNKNOWN",
                [ReconciliationDiff(code="RECONCILIATION_AUTHENTICATION_FAILED", exchange_value=str(exc))],
            )
            return self.last_status
        except Exception as exc:
            logger.error("Reconciliation execution failed: %s", exc)
            self._set_status(
                "UNKNOWN",
                [ReconciliationDiff(code="RECONCILIATION_FAILED", exchange_value=str(exc))],
            )
            return self.last_status
