"""Authoritative Binance USDⓈ-M account, order, fill, and position reconciliation."""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

from domain.enums import OrderSide, PositionSide
from domain.models import ExchangeFill, utc_now

from .ledger import ExecutionLedger
from .config import BinanceEnvironment, environment_label
from .models import BinanceAuthenticationError, BinanceDefinitiveRejection, ExchangeAccountSnapshot
from .rest_client import BinanceRestClient

logger = logging.getLogger("blessing.binance.reconciliation")

# FAPI time-filtered history queries accept at most a seven-day interval.
# Use six-day chunks with an inclusive one-millisecond overlap, then recursively
# split saturated pages. History older than each endpoint's proven retention
# is never treated as covered by a short page.
_HISTORY_WINDOW_MS = 6 * 24 * 60 * 60 * 1000
_HISTORY_OVERLAP_MS = 1
_ALL_ORDERS_RETENTION = timedelta(days=30)  # zero-fill canceled/expired rows may disappear sooner
_USER_TRADES_RETENTION = timedelta(days=90)
_ALL_ALGO_RETENTION = timedelta(days=3)
_HISTORY_PAGE_LIMIT = 1000
_HISTORY_MAX_SPLIT_DEPTH = 32


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
    if account.get("portfolioMargin"):
        return "CROSS", True

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
    observed_at: datetime | None = None,
) -> ExchangeAccountSnapshot:
    """Build a truthful snapshot from current Binance v2 account and position responses."""

    if environment not in {
        environment_label(BinanceEnvironment.TESTNET),
        environment_label(BinanceEnvironment.MAINNET),
    }:
        raise ValueError("Account snapshots are accepted only from fixed Binance environments")
    if not isinstance(account, dict) or not isinstance(position_risk, list):
        raise ValueError("Binance account snapshot payload is invalid")
    if observed_at is not None and observed_at.tzinfo is None:
        raise ValueError("Binance account snapshot observation time must be timezone-aware")

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
            if account.get("portfolioMargin") and margin_balance > 0 and computed_notional > 0:
                # In Binance Portfolio Margin (PAPI), liquidation is managed at the unified account level.
                # When collateral covers maintenance margin, calculate the liquidation buffer from margin headroom.
                headroom = max(Decimal("0"), margin_balance - total_maint_margin)
                distance = min(
                    Decimal("100"),
                    max(
                        Decimal("0"),
                        (headroom / computed_notional) * Decimal("100")
                    )
                )
                liquidation_distances.append(distance)
            else:
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
        timestamp=observed_at or utc_now(),
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
        self._history_diffs: List[ReconciliationDiff] = []
        self._recovered_trade_ids: set[str] = set()
        self.daily_loss_window_start: datetime | None = None
        self.daily_loss_window_end: datetime | None = None
        self.algo_protection_repository: Any = None
        self.history_repository: Any = None
        # A Testnet baseline is usable only when a separate caller establishes
        # a durable run anchor and explicitly selects that run here.
        self.testnet_history_run_id: str | None = None
        self.require_testnet_algo_ownership = False

    def _resolve_history_repository(self) -> Any:
        if self.history_repository is not None:
            return self.history_repository
        callbacks = (
            getattr(self.ledger, "on_order_update", None),
            getattr(self.ledger, "on_fill_update", None),
            getattr(self.ledger, "on_position_update", None),
        )
        for callback in callbacks:
            manager = getattr(callback, "__self__", None)
            persistence = getattr(manager, "repository", None)
            history = getattr(persistence, "binance_history", None)
            if history is not None:
                self.history_repository = history
                return history
        return None

    @staticmethod
    def _history_timestamp(row: Dict[str, Any], fields: tuple[str, ...]) -> datetime:
        raw = next((row.get(field) for field in fields if row.get(field) not in (None, "")), None)
        if isinstance(raw, datetime):
            parsed = raw
        elif isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool):
            number = float(raw)
            if not number.is_integer() or number <= 0:
                raise FillRecoveryError("Binance history timestamp is invalid")
            if number > 100_000_000_000:
                number /= 1000.0
            try:
                parsed = datetime.fromtimestamp(number, tz=timezone.utc)
            except (OverflowError, OSError, ValueError) as exc:
                raise FillRecoveryError("Binance history timestamp is invalid") from exc
        elif isinstance(raw, str):
            normalized = raw.strip()
            if normalized.endswith("Z"):
                normalized = normalized[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise FillRecoveryError("Binance history timestamp is invalid") from exc
        else:
            raise FillRecoveryError("Binance history row has no usable event timestamp")
        if parsed.tzinfo is None:
            raise FillRecoveryError("Binance history timestamp is not timezone-aware")
        return parsed.astimezone(timezone.utc)

    @classmethod
    def _history_item(
        cls,
        row: Dict[str, Any],
        *,
        history_kind: str,
        id_field: str,
        client_field: str,
        time_fields: tuple[str, ...],
    ) -> dict[str, Any]:
        try:
            item_id = int(row[id_field])
        except (KeyError, TypeError, ValueError) as exc:
            raise FillRecoveryError(f"{history_kind} item ID is invalid") from exc
        if isinstance(row.get(id_field), bool) or item_id <= 0:
            raise FillRecoveryError(f"{history_kind} item ID is invalid")
        try:
            canonical = json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise FillRecoveryError(f"{history_kind} row cannot be fingerprinted") from exc
        client_id = str(row.get(client_field) or "").strip() or None
        return {
            "item_id": item_id,
            "client_id": client_id,
            "event_at": cls._history_timestamp(row, time_fields),
            "payload_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "payload": dict(row),
        }

    @staticmethod
    def _history_millis(value: datetime) -> int:
        if value.tzinfo is None:
            raise FillRecoveryError("Binance history window timestamp is not timezone-aware")
        return int(value.astimezone(timezone.utc).timestamp() * 1000)

    @classmethod
    def _bounded_history_windows(cls, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
        if start_ms <= 0 or end_ms <= start_ms:
            raise FillRecoveryError("Binance history coverage window did not advance")
        windows: list[tuple[int, int]] = []
        cursor = start_ms
        while cursor < end_ms:
            window_end = min(cursor + _HISTORY_WINDOW_MS, end_ms)
            if window_end <= cursor:
                raise FillRecoveryError("Binance history window cursor did not advance")
            windows.append((cursor, window_end))
            if window_end == end_ms:
                break
            cursor = window_end - _HISTORY_OVERLAP_MS
        return windows

    async def _scan_history_window(
        self,
        *,
        repository: Any,
        checkpoint: dict[str, Any],
        symbol: str,
        history_kind: str,
        path: str,
        start_ms: int,
        end_ms: int,
        cursor_id: int,
        depth: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        if end_ms <= start_ms:
            raise FillRecoveryError("Binance history sub-window did not advance")
        response = await self.rest_client.request(
            "GET",
            path,
            signed=True,
            params={
                "symbol": symbol,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": _HISTORY_PAGE_LIMIT,
            },
        )
        if not isinstance(response, list) or len(response) > _HISTORY_PAGE_LIMIT:
            raise FillRecoveryError(f"{history_kind} bounded time-window response is invalid")
        if len(response) == _HISTORY_PAGE_LIMIT:
            midpoint = start_ms + (end_ms - start_ms) // 2
            if depth >= _HISTORY_MAX_SPLIT_DEPTH or midpoint <= start_ms or midpoint >= end_ms:
                raise FillRecoveryError(
                    f"{history_kind} saturated at an unsplittable timestamp interval"
                )
            left, cursor_id = await self._scan_history_window(
                repository=repository,
                checkpoint=checkpoint,
                symbol=symbol,
                history_kind=history_kind,
                path=path,
                start_ms=start_ms,
                end_ms=midpoint,
                cursor_id=cursor_id,
                depth=depth + 1,
            )
            right_start = max(start_ms, midpoint - _HISTORY_OVERLAP_MS)
            right, cursor_id = await self._scan_history_window(
                repository=repository,
                checkpoint=checkpoint,
                symbol=symbol,
                history_kind=history_kind,
                path=path,
                start_ms=right_start,
                end_ms=end_ms,
                cursor_id=cursor_id,
                depth=depth + 1,
            )
            return [*left, *right], cursor_id

        normalized_symbol = str(symbol).upper()
        rows: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        id_field, client_field, time_fields = {
            "ALL_ORDERS": ("orderId", "clientOrderId", ("time", "updateTime")),
            "USER_TRADES": ("id", "clientOrderId", ("time",)),
            "ALL_ALGO_ORDERS": ("algoId", "clientAlgoId", ("createTime", "time", "updateTime")),
        }[history_kind]
        for raw in response:
            if not isinstance(raw, dict) or str(raw.get("symbol", "")).upper() != normalized_symbol:
                raise FillRecoveryError(f"{history_kind} row has an invalid symbol")
            item = self._history_item(
                raw,
                history_kind=history_kind,
                id_field=id_field,
                client_field=client_field,
                time_fields=time_fields,
            )
            event_ms = self._history_millis(item["event_at"])
            if event_ms < start_ms or event_ms > end_ms:
                raise FillRecoveryError(f"{history_kind} row escaped its requested time window")
            items.append(item)
            rows.append(raw)

        try:
            cursor_id = await repository.persist_history_page(
                checkpoint=checkpoint,
                expected_cursor_id=cursor_id,
                items=items,
                observed_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            raise FillRecoveryError(f"{history_kind} page could not be durably deduplicated") from exc
        return rows, cursor_id

    async def _scan_launch_history(
        self, *, symbol: str, history_kind: str, path: str, retention: timedelta
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        repository = self._resolve_history_repository()
        if repository is None:
            raise FillRecoveryError("durable Binance launch-history repository is unavailable")
        now = datetime.now(timezone.utc)
        try:
            if self.environment == environment_label(BinanceEnvironment.TESTNET):
                if not self.testnet_history_run_id:
                    raise RuntimeError("Testnet caller did not select a durable run anchor")
                checkpoint = await repository.begin_testnet_scan(
                    run_id=self.testnet_history_run_id,
                    symbol=symbol,
                    history_kind=history_kind,
                    retention_seconds=int(retention.total_seconds()),
                    now=now,
                )
            else:
                checkpoint = await repository.begin_mainnet_scan(
                    symbol=symbol,
                    history_kind=history_kind,
                    retention_seconds=int(retention.total_seconds()),
                    now=now,
                )
        except Exception as exc:
            raise FillRecoveryError("durable Binance launch-history anchor/checkpoint is unavailable") from exc
        try:
            start_ms = self._history_millis(checkpoint["scan_from_at"])
            end_ms = self._history_millis(checkpoint["scan_to_at"])
            windows = self._bounded_history_windows(start_ms, end_ms)
            cursor_id = int(checkpoint["cursor_id"])
            rows: list[dict[str, Any]] = []
            for window_start, window_end in windows:
                page_rows, cursor_id = await self._scan_history_window(
                    repository=repository,
                    checkpoint=checkpoint,
                    symbol=symbol,
                    history_kind=history_kind,
                    path=path,
                    start_ms=window_start,
                    end_ms=window_end,
                    cursor_id=cursor_id,
                )
                rows.extend(page_rows)
            await repository.complete_history_scan(checkpoint)
            persisted = await repository.list_history_items(checkpoint)
        except FillRecoveryError:
            raise
        except Exception as exc:
            raise FillRecoveryError(f"{history_kind} coverage could not be completed") from exc
        # Downstream ownership/trigger/fill linkage must survive an empty
        # overlap scan after restart. The repository returns the latest durable
        # observation per exchange ID, including rows from earlier scans.
        durable_rows = self._durable_history_payloads(persisted, history_kind, symbol)
        id_field = {"ALL_ORDERS": "orderId", "USER_TRADES": "id", "ALL_ALGO_ORDERS": "algoId"}[history_kind]
        durable_by_id = {str(row[id_field]): row for row in durable_rows}
        for row in rows:
            if durable_by_id.get(str(row.get(id_field))) != row:
                raise FillRecoveryError("latest exchange history page is missing from durable read-back")
        return checkpoint, durable_rows, persisted

    @staticmethod
    def _durable_history_payloads(items: list[dict[str, Any]], kind: str, symbol: str) -> list[dict[str, Any]]:
        id_field, client_field = {
            "ALL_ORDERS": ("orderId", "clientOrderId"),
            "USER_TRADES": ("id", "clientOrderId"),
            "ALL_ALGO_ORDERS": ("algoId", "clientAlgoId"),
        }[kind]
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            payload = item.get("payload")
            if not isinstance(payload, dict):
                raise FillRecoveryError("durable history observation has no payload")
            item_id = str(item.get("item_id") or "")
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if (
                not item_id.isdigit() or int(item_id) <= 0 or item_id in seen
                or str(payload.get(id_field)) != item_id
                or str(payload.get("symbol") or "").upper() != symbol.upper()
                or (str(payload.get(client_field) or "") != str(item.get("client_id") or ""))
                or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != item.get("payload_sha256")
            ):
                raise FillRecoveryError("durable history observation identity or fingerprint is invalid")
            seen.add(item_id)
            result.append(dict(payload))
        return result

    async def _persist_exact_history_rows(
        self, checkpoint: dict[str, Any], rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Persist exact-ID refreshes without moving time-window coverage."""
        repository = self._resolve_history_repository()
        observe = getattr(repository, "record_history_observation", None)
        if not callable(observe):
            raise FillRecoveryError("durable exact-order/trade observation storage is unavailable")
        kind = checkpoint["history_kind"]
        id_field, client_field, time_fields = {
            "ALL_ORDERS": ("orderId", "clientOrderId", ("time", "updateTime")),
            "USER_TRADES": ("id", "clientOrderId", ("time",)),
        }[kind]
        persisted_rows = []
        for row in rows:
            item = self._history_item(
                row, history_kind=kind, id_field=id_field,
                client_field=client_field, time_fields=time_fields,
            )
            try:
                persisted = await observe(
                    checkpoint=checkpoint, item=item, observed_at=utc_now(),
                )
                payloads = self._durable_history_payloads(
                    [persisted], kind, str(checkpoint["symbol"]),
                )
            except Exception as exc:
                raise FillRecoveryError("exact-order/trade observation read-back failed") from exc
            if payloads != [row]:
                raise FillRecoveryError("exact-order/trade observation read-back differs from exchange")
            persisted_rows.extend(payloads)
        return persisted_rows

    async def _fetch_exact_order_trades(
        self, symbol: str, order_id: str
    ) -> list[dict[str, Any]]:
        """Recover delayed executions independently of a covered time window."""
        trades = await self.rest_client.request(
            "GET", self._user_trades_path, signed=True,
            params={"symbol": symbol, "orderId": order_id, "limit": _HISTORY_PAGE_LIMIT},
        )
        if not isinstance(trades, list) or len(trades) >= _HISTORY_PAGE_LIMIT:
            raise FillRecoveryError("exact-order userTrades response is invalid or incomplete")
        seen: set[str] = set()
        for trade in trades:
            if (
                not isinstance(trade, dict)
                or str(trade.get("orderId")) != str(order_id)
                or str(trade.get("symbol", "")).upper() != symbol.upper()
            ):
                raise FillRecoveryError("exact-order userTrades row has a foreign identity")
            trade_id = self._positive_exchange_id(trade.get("id"), field="trade ID")
            if trade_id in seen:
                raise FillRecoveryError("exact-order userTrades contains duplicate trade IDs")
            seen.add(trade_id)
        return trades

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
    def _user_trades_path(self) -> str:
        return "/papi/v1/um/userTrades" if self.portfolio_margin else "/fapi/v1/userTrades"

    @property
    def _all_orders_path(self) -> str:
        return "/papi/v1/um/allOrders" if self.portfolio_margin else "/fapi/v1/allOrders"

    @property
    def _open_algo_orders_path(self) -> str:
        return "/papi/v1/um/algo/openAlgoOrders" if self.portfolio_margin else "/fapi/v1/openAlgoOrders"

    @property
    def _algo_order_query_path(self) -> str:
        return "/papi/v1/um/algo/algoOrder" if self.portfolio_margin else "/fapi/v1/algoOrder"

    @property
    def _all_algo_orders_path(self) -> str:
        return "/papi/v1/um/algo/allAlgoOrders" if self.portfolio_margin else "/fapi/v1/allAlgoOrders"

    @property
    def _algo_order_path(self) -> str:
        return "/papi/v1/um/algo/order" if self.portfolio_margin else "/fapi/v1/algoOrder"

    @staticmethod
    def _testnet_algo_row_matches_owner(
        order: Any, owner: dict[str, Any], *, source: str
    ) -> bool:
        record = owner["record"]
        role = owner["role"]
        try:
            algo_id = order.get("algoId")
            valid_algo_id = (
                not isinstance(algo_id, bool)
                and str(int(algo_id)) == owner["algo_id"]
            )
            actual_trigger = Decimal(str(order.get("triggerPrice")))
            expected_trigger = Decimal(str(record[f"{role}_trigger_price"]))
            valid_trigger = (
                actual_trigger.is_finite()
                and expected_trigger.is_finite()
                and actual_trigger == expected_trigger
            )
        except (AttributeError, InvalidOperation, TypeError, ValueError):
            return False
        expected_type = "STOP_MARKET" if role == "stop" else "TAKE_PROFIT_MARKET"
        expected_side = "SELL" if record["entry_side"] == "BUY" else "BUY"
        close_pos = _exchange_bool(order.get("closePosition"))
        reduce_only = _exchange_bool(order.get("reduceOnly"))
        bracket_shape_valid = (close_pos and not reduce_only) or (not close_pos and reduce_only)
        return bool(
            valid_algo_id
            and order.get("clientAlgoId") == owner["client_algo_id"]
            and str(order.get("symbol", "")).upper() == str(record["symbol"]).upper()
            and order.get("algoType") == "CONDITIONAL"
            and order.get("orderType") == expected_type
            and order.get("algoStatus") == "NEW"
            and order.get("positionSide") == record["position_side"]
            and order.get("side") == expected_side
            and order.get("workingType") == "MARK_PRICE"
            and valid_trigger
            and bracket_shape_valid
            and source in {"open", "query"}
        )

    @staticmethod
    def _testnet_algo_history_row_matches_owner(
        order: Any, owner: dict[str, Any]
    ) -> bool:
        """Match a completed or open Testnet Algo row to its durable owner."""
        record = owner["record"]
        role = owner["role"]
        try:
            algo_id = order.get("algoId")
            valid_algo_id = (
                not isinstance(algo_id, bool)
                and str(int(algo_id)) == owner["algo_id"]
            )
            actual_trigger = Decimal(str(order.get("triggerPrice")))
            expected_trigger = Decimal(str(record[f"{role}_trigger_price"]))
            valid_trigger = (
                actual_trigger.is_finite()
                and expected_trigger.is_finite()
                and actual_trigger == expected_trigger
            )
        except (AttributeError, InvalidOperation, TypeError, ValueError):
            return False
        expected_type = "STOP_MARKET" if role == "stop" else "TAKE_PROFIT_MARKET"
        expected_side = "SELL" if record["entry_side"] == "BUY" else "BUY"
        close_pos = _exchange_bool(order.get("closePosition"))
        reduce_only = _exchange_bool(order.get("reduceOnly"))
        bracket_shape_valid = (close_pos and not reduce_only) or (not close_pos and reduce_only)
        return bool(
            valid_algo_id
            and order.get("clientAlgoId") == owner["client_algo_id"]
            and str(order.get("symbol", "")).upper() == str(record["symbol"]).upper()
            and order.get("algoType") == "CONDITIONAL"
            and order.get("orderType") == expected_type
            and str(order.get("algoStatus", "")).upper()
            in {"NEW", "CANCELED", "CANCELLED", "EXPIRED", "TRIGGERED", "FINISHED", "REJECTED"}
            and order.get("positionSide") == record["position_side"]
            and order.get("side") == expected_side
            and order.get("workingType") == "MARK_PRICE"
            and valid_trigger
            and bracket_shape_valid
        )

    @classmethod
    def _testnet_algo_row_matches_baseline(
        cls, order: Any, baseline: dict[str, Any]
    ) -> bool:
        """Match only the exact immutable, terminal read-only baseline fact."""
        try:
            algo_id = order.get("algoId")
            created_at = cls._history_timestamp(
                order, ("createTime", "time", "updateTime")
            )
            expected_created_at = baseline["algo_created_at"]
            anchor_at = baseline["anchor_at"]
            if not isinstance(expected_created_at, datetime) or not isinstance(anchor_at, datetime):
                return False
            if expected_created_at.tzinfo is None or anchor_at.tzinfo is None:
                return False
        except (AttributeError, KeyError, FillRecoveryError, TypeError, ValueError):
            return False
        status = str(order.get("algoStatus") or "").strip().upper()
        try:
            valid_algo_id = (
                not isinstance(algo_id, bool)
                and str(int(algo_id)) == str(int(baseline["algo_id"]))
            )
        except (KeyError, TypeError, ValueError):
            return False
        return bool(
            valid_algo_id
            and str(order.get("clientAlgoId") or "") == str(baseline["client_algo_id"])
            and str(order.get("symbol") or "").upper() == str(baseline["symbol"]).upper()
            and status == str(baseline["terminal_status"]).upper()
            and created_at == expected_created_at.astimezone(timezone.utc)
            and created_at < anchor_at.astimezone(timezone.utc)
        )

    async def _audit_algo_orders(
        self,
        tracked_orders: List[Any],
        *,
        exchange_positions: List[Dict[str, Any]] | None = None,
        exchange_open_orders: List[Dict[str, Any]] | None = None,
    ) -> None:
        """Fence Mainnet when conditional orders exist or their history is incomplete.

        Algo orders are separate from ordinary openOrders/allOrders. This
        launch does not own an Algo order, so any observed one is unowned.
        """
        if (
            self.environment == environment_label(BinanceEnvironment.TESTNET)
            and self.require_testnet_algo_ownership
        ):
            repository = self.algo_protection_repository
            open_orders = await self.rest_client.request(
                "GET", self._open_algo_orders_path, signed=True,
            )
            if not isinstance(open_orders, list) or any(
                not isinstance(row, dict) for row in open_orders
            ):
                raise FillRecoveryError("Testnet openAlgoOrders response is invalid")
            if repository is None and open_orders:
                raise FillRecoveryError(
                    "Testnet open Algo orders exist without durable ownership storage"
                )
            active_records = (
                await repository.list_active_protections(venue="binance_testnet")
                if repository is not None else []
            )
            list_all = getattr(repository, "list_protections", None)
            history_records = (
                await list_all(venue="binance_testnet")
                if callable(list_all) else active_records
            )
            expected: dict[tuple[str, str], dict[str, Any]] = {}
            for record in active_records:
                if record.get("environment") != "TESTNET" or record.get("state") != "PROTECTED":
                    raise FillRecoveryError("Testnet Algo lifecycle is not durably PROTECTED")
                for role in ("stop", "take_profit"):
                    algo_id = record.get(f"{role}_algo_id")
                    client_id = record.get(f"{role}_client_algo_id")
                    if algo_id in (None, "") or not client_id:
                        raise FillRecoveryError("Testnet protection identity is incomplete")
                    owner_key = (str(record["symbol"]).upper(), str(client_id))
                    if owner_key in expected:
                        raise FillRecoveryError("Testnet protection client identity is duplicated")
                    expected[owner_key] = {
                        "algo_id": str(algo_id),
                        "client_algo_id": str(client_id),
                        "record": record,
                        "role": role,
                    }
            history_expected: dict[tuple[str, str], dict[str, Any]] = {}
            for record in history_records:
                if record.get("environment") != "TESTNET":
                    raise FillRecoveryError("Testnet Algo history owner has an invalid environment")
                for role in ("stop", "take_profit"):
                    client_id = record.get(f"{role}_client_algo_id")
                    algo_id = record.get(f"{role}_algo_id")
                    if not client_id:
                        raise FillRecoveryError("Testnet Algo history owner identity is incomplete")
                    owner_key = (str(record["symbol"]).upper(), str(client_id))
                    if owner_key in history_expected:
                        raise FillRecoveryError("Testnet Algo history owner identity is duplicated")
                    history_expected[owner_key] = {
                        "algo_id": str(algo_id) if algo_id not in (None, "") else None,
                        "client_algo_id": str(client_id),
                        "record": record,
                        "role": role,
                    }
            seen: set[tuple[str, str]] = set()
            for order in open_orders:
                symbol = str(order.get("symbol") or "").upper()
                client_id = str(order.get("clientAlgoId") or "")
                key = (symbol, client_id)
                owner = expected.get(key)
                if owner is None or key in seen:
                    raise FillRecoveryError("Testnet open Algo order is unowned or duplicated")
                seen.add(key)
                if not self._testnet_algo_row_matches_owner(order, owner, source="open"):
                    raise FillRecoveryError("Testnet open Algo order differs from durable protection")
            if seen != set(expected):
                raise FillRecoveryError("Testnet durable protection is missing an open Algo order")
            if active_records:
                positions = await self.rest_client.request(
                    "GET", self._position_risk_path, signed=True,
                )
                if not isinstance(positions, list):
                    raise FillRecoveryError("Testnet position-risk snapshot is invalid")
                for record in active_records:
                    symbol = str(record["symbol"]).upper()
                    position_side = str(record["position_side"]).upper()
                    matching_positions = [
                        row for row in positions
                        if isinstance(row, dict)
                        and str(row.get("symbol", "")).upper() == symbol
                        and str(row.get("positionSide", "BOTH")).upper() == position_side
                    ]
                    if len(matching_positions) != 1:
                        raise FillRecoveryError("Testnet protected position identity is not unique")
                    try:
                        amount = Decimal(str(matching_positions[0]["positionAmt"]))
                        filled = Decimal(str(record["filled_quantity"]))
                    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                        raise FillRecoveryError("Testnet protected position quantity is invalid") from exc
                    positive_position = (
                        position_side == "LONG"
                        or (position_side == "BOTH" and record["entry_side"] == "BUY")
                    )
                    if (
                        not amount.is_finite()
                        or not filled.is_finite()
                        or amount == 0
                        or abs(amount) != filled
                        or (amount > 0) != positive_position
                    ):
                        raise FillRecoveryError("Testnet position no longer matches the protected fill")

                for key, owner in expected.items():
                    query = await self.rest_client.request(
                        "GET", self._algo_order_query_path, signed=True,
                        params={"symbol": key[0], "algoId": owner["algo_id"]},
                    )
                    if (
                        not isinstance(query, dict)
                        or not self._testnet_algo_row_matches_owner(
                            query, owner, source="query"
                        )
                    ):
                        raise FillRecoveryError(
                            "Testnet Algo query differs from durable protection"
                        )
            history_repository = self._resolve_history_repository()
            if history_repository is None or not self.testnet_history_run_id:
                raise FillRecoveryError(
                    "Testnet Algo history requires a durable run anchor and checkpoint repository"
                )
            try:
                checkpoint, raw_history, persisted_history = await self._scan_launch_history(
                    symbol="ETHUSDC",
                    history_kind="ALL_ALGO_ORDERS",
                    path=self._all_algo_orders_path,
                    retention=_ALL_ALGO_RETENTION,
                )
                baselines = await history_repository.list_preexisting_algo_baselines(
                    run_id=self.testnet_history_run_id,
                    symbol="ETHUSDC",
                )
            except Exception as exc:
                if isinstance(exc, FillRecoveryError):
                    raise
                raise FillRecoveryError("Testnet Algo history anchor or baseline is invalid") from exc

            baselines_by_key: dict[tuple[str, str], dict[str, Any]] = {}
            for baseline in baselines:
                key = (str(baseline["symbol"]).upper(), str(baseline["client_algo_id"]))
                if key in baselines_by_key:
                    raise FillRecoveryError("Testnet Algo baseline identity is duplicated")
                baselines_by_key[key] = baseline

            # Endpoint rows are not ordered reliably: this account returned
            # newest-first despite the inclusive algoId cursor. Deduplicate by
            # ID and normalize before comparing owners or recording coverage.
            history_by_id: dict[str, dict[str, Any]] = {}
            for row in raw_history:
                algo_id = str(int(row["algoId"]))
                previous = history_by_id.get(algo_id)
                if previous is not None and previous != row:
                    raise FillRecoveryError("Testnet Algo page repeats an ID with conflicting data")
                history_by_id[algo_id] = row
            history_seen: set[tuple[str, str]] = set()
            history_ids: dict[str, str] = {}
            for row in (history_by_id[key] for key in sorted(history_by_id, key=int)):
                algo_id = str(int(row["algoId"]))
                client_id = str(row.get("clientAlgoId") or "")
                key = ("ETHUSDC", client_id)
                owner = history_expected.get(key)
                baseline = baselines_by_key.get(key)
                previous_id = history_ids.get(client_id)
                if not client_id or key in history_seen or (
                    previous_id is not None and previous_id != algo_id
                ):
                    raise FillRecoveryError("Testnet Algo history is duplicated or has no client identity")
                if owner is not None:
                    if (
                        owner["algo_id"] is None
                        or owner["algo_id"] != algo_id
                        or not self._testnet_algo_history_row_matches_owner(row, owner)
                    ):
                        raise FillRecoveryError("Testnet Algo history differs from durable ownership")
                elif baseline is None or not self._testnet_algo_row_matches_baseline(row, baseline):
                    raise FillRecoveryError("Testnet Algo history is unowned and has no exact baseline proof")
                history_seen.add(key)
                history_ids[client_id] = algo_id

            # Previously validated rows remain durable across restarts and
            # beyond Binance's short Algo-history retention, but only the exact
            # exchange ID/client ID pair can satisfy an existing owner.
            durable_history = {
                (str(item["client_id"]), str(item["item_id"]))
                for item in persisted_history
                if item.get("client_id") not in (None, "")
            }
            expected_history = {
                key for key, owner in history_expected.items()
                if owner["algo_id"] is not None
            }
            for key in expected_history - history_seen:
                owner = history_expected[key]
                if (key[1], owner["algo_id"]) not in durable_history:
                    raise FillRecoveryError("Testnet durable Algo owner is missing from history")
            return
        if self.environment != environment_label(BinanceEnvironment.MAINNET):
            return
        open_algo_orders = await self.rest_client.request(
            "GET", self._open_algo_orders_path, signed=True,
        )
        if not isinstance(open_algo_orders, list) or any(
            not isinstance(row, dict) for row in open_algo_orders
        ):
            raise FillRecoveryError("openAlgoOrders response is invalid")
        if (
            self.algo_protection_repository is not None
            and (exchange_positions is None or exchange_open_orders is None)
        ):
            _, fetched_positions, fetched_orders = (
                await self._fetch_reconciliation_snapshot_inputs()
            )
            exchange_positions = fetched_positions
            exchange_open_orders = fetched_orders
        await self._audit_mainnet_algo_lifecycle(
            open_algo_orders,
            exchange_positions=exchange_positions or [],
            exchange_open_orders=exchange_open_orders or [],
        )

    @staticmethod
    def _positive_exchange_id(value: object, *, field: str) -> str:
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise FillRecoveryError(f"Binance {field} is missing or invalid") from exc
        if isinstance(value, bool) or parsed <= 0 or str(parsed) != str(value):
            raise FillRecoveryError(f"Binance {field} is missing or invalid")
        return str(parsed)

    @classmethod
    def _triggered_algo_order_id(cls, algo: Dict[str, Any]) -> str:
        values = [
            algo.get(field)
            for field in ("actualOrderId", "orderId")
            if algo.get(field) not in (None, "", 0, "0")
        ]
        normalized = {
            cls._positive_exchange_id(value, field="triggered Algo order ID")
            for value in values
        }
        if len(normalized) != 1:
            raise FillRecoveryError(
                "triggered Algo history does not identify exactly one normal order"
            )
        return next(iter(normalized))

    @staticmethod
    def _matching_position_amount(
        positions: List[Dict[str, Any]], *, symbol: str, position_side: str
    ) -> Decimal:
        matches = [
            row for row in positions
            if isinstance(row, dict)
            and str(row.get("symbol", "")).upper() == symbol.upper()
            and str(row.get("positionSide", "")).upper() == position_side.upper()
        ]
        if len(matches) != 1:
            raise FillRecoveryError("Algo owner position snapshot is missing or ambiguous")
        try:
            amount = Decimal(str(matches[0]["positionAmt"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise FillRecoveryError("Algo owner position quantity is invalid") from exc
        if not amount.is_finite():
            raise FillRecoveryError("Algo owner position quantity is non-finite")
        return amount

    @staticmethod
    def _mainnet_closure_proof_matches_owner(
        record: Dict[str, Any], *, launch_id: str
    ) -> bool:
        raw = record.get("closure_evidence")
        try:
            evidence = json.loads(raw) if isinstance(raw, str) else dict(raw) if isinstance(raw, dict) else raw
            if not isinstance(evidence, dict) or evidence.get("kind") not in {
                "BINANCE_ALGO_CLOSE_VERIFIED",
                "LOCAL_EMERGENCY_CLOSE_VERIFIED",
            }:
                return False
            emergency_close = evidence.get("kind") == "LOCAL_EMERGENCY_CLOSE_VERIFIED"
            proof_sha256 = str(evidence.pop("proof_sha256", ""))
            canonical = json.dumps(
                evidence, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != proof_sha256:
                return False
            return (
                evidence.get("symbol") == str(record.get("symbol", "")).upper()
                and evidence.get("entry_client_order_id")
                == str(record.get("entry_client_order_id", ""))
                and evidence.get("mainnet_launch_id") == launch_id
                and evidence.get("basket_id") == record.get("basket_id")
                and (
                    evidence.get("algo_id") == "LOCAL_EMERGENCY_CLOSE"
                    if emergency_close
                    else str(evidence.get("algo_id", ""))
                    in {
                        str(record.get("stop_algo_id") or ""),
                        str(record.get("take_profit_algo_id") or ""),
                    }
                )
                and evidence.get("order_status") == "FILLED"
                and Decimal(str(evidence.get("executed_quantity")))
                == Decimal(str(record.get("filled_quantity")))
                and Decimal(str(evidence.get("trade_quantity")))
                == Decimal(str(record.get("filled_quantity")))
                and Decimal(str(evidence.get("position_quantity"))) == 0
                and evidence.get("open_child_order_ids") == []
                and evidence.get("open_owner_algo_ids") == []
            )
        except (InvalidOperation, TypeError, ValueError, json.JSONDecodeError):
            return False

    async def _mainnet_unfilled_entry_proof_matches_owner(
        self, record: Dict[str, Any], *, launch_id: str
    ) -> bool:
        """Verify a durable zero-fill terminal entry using its exact exchange order."""
        try:
            if (
                str(record.get("environment") or "").upper() != "MAINNET"
                or str(record.get("venue") or "").lower() != "binance_mainnet"
                or str(record.get("mainnet_launch_id") or "") != launch_id
                or Decimal(str(record.get("filled_quantity"))) != 0
                or record.get("entry_average_price") is not None
            ):
                return False
            proof_fields = {}
            for part in str(record.get("state_reason") or "").split(";"):
                if "=" not in part:
                    return False
                key, value = part.split("=", 1)
                if key in proof_fields:
                    return False
                proof_fields[key] = value
            if set(proof_fields) != {
                "unfilled_entry_order_id",
                "unfilled_entry_status",
                "unfilled_entry_executed_qty",
            } or proof_fields["unfilled_entry_executed_qty"] != "0":
                return False
            terminal = {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
            order_id = proof_fields["unfilled_entry_order_id"]
            status = proof_fields["unfilled_entry_status"].upper()
            if not order_id.isdigit() or int(order_id) <= 0 or status not in terminal:
                return False
            client_id = str(record.get("entry_client_order_id") or "")
            symbol = str(record.get("symbol") or "").upper()
            owner_order = await self.ledger.get_order_by_client_id(client_id)
            if (
                owner_order is None
                or str(getattr(owner_order, "exchange_order_id", "") or "") != order_id
                or str(getattr(owner_order, "status", "")).upper() != status
                or str(getattr(owner_order, "client_order_id", "")) != client_id
                or str(getattr(owner_order, "symbol", "")).upper() != symbol
                or str(getattr(
                    getattr(owner_order, "side", ""), "value", getattr(owner_order, "side", "")
                )).upper() != str(record.get("entry_side") or "").upper()
                or str(getattr(
                    getattr(owner_order, "position_side", ""),
                    "value",
                    getattr(owner_order, "position_side", ""),
                )).upper() != str(record.get("position_side") or "").upper()
                or Decimal(str(getattr(owner_order, "quantity", "NaN")))
                != Decimal(str(record.get("requested_quantity")))
            ):
                return False
            response = await self.rest_client.request(
                "GET", self._order_path, signed=True,
                params={"symbol": symbol, "orderId": int(order_id)},
            )
            if not isinstance(response, dict):
                return False
            executed = Decimal(str(response.get("executedQty")))
            original = Decimal(str(response.get("origQty")))
            return bool(
                str(response.get("symbol") or "").upper() == symbol
                and str(response.get("orderId") or "") == order_id
                and str(response.get("clientOrderId") or "") == client_id
                and str(response.get("side") or "").upper()
                == str(record.get("entry_side") or "").upper()
                and str(response.get("positionSide") or "BOTH").upper()
                == str(record.get("position_side") or "").upper()
                and str(response.get("status") or "").upper() == status
                and status in terminal
                and executed.is_finite()
                and executed == 0
                and original.is_finite()
                and original > 0
                and original == Decimal(str(record.get("requested_quantity")))
                and not any(
                    str(getattr(fill, "client_order_id", "")) == client_id
                    or str(getattr(fill, "exchange_order_id", "")) == order_id
                    for fill in await self.ledger.get_fills()
                )
            )
        except (InvalidOperation, TypeError, ValueError, KeyError):
            return False

    async def _query_algo_order(
        self, *, symbol: str, client_algo_id: str
    ) -> Optional[Dict[str, Any]]:
        try:
            result = await self.rest_client.request(
                "GET",
                self._algo_order_query_path,
                signed=True,
                params={"symbol": symbol, "clientAlgoId": client_algo_id},
            )
        except BinanceDefinitiveRejection as exc:
            if exc.code == -2013:
                return None
            raise
        return result if isinstance(result, dict) else None

    async def _cancel_local_mainnet_owned_algos(
        self, record: Dict[str, Any]
    ) -> bool:
        """Cancel owned still-open brackets once and prove they left openAlgoOrders."""
        symbol = str(record.get("symbol") or "").upper()
        client_ids = {
            str(record.get("stop_client_algo_id") or "").strip(),
            str(record.get("take_profit_client_algo_id") or "").strip(),
        }
        if not symbol or "" in client_ids or len(client_ids) != 2:
            return False
        terminal = {
            "CANCELED", "CANCELLED", "EXPIRED", "FINISHED", "TRIGGERED", "REJECTED"
        }
        for client_id in client_ids:
            try:
                current = await self._query_algo_order(
                    symbol=symbol, client_algo_id=client_id
                )
            except Exception:
                return False
            if current is None:
                continue
            if str(current.get("clientAlgoId") or "") != client_id:
                return False
            state = str(current.get("algoStatus") or "").upper()
            if state == "NEW":
                try:
                    await self.rest_client.request(
                        "DELETE",
                        self._algo_order_path,
                        signed=True,
                        params={
                            "symbol": symbol,
                            "algoId": int(current["algoId"]),
                        },
                    )
                except Exception:
                    # DELETE may have reached the exchange. Read back once;
                    # never repeat an ambiguous cancel.
                    pass
                try:
                    current = await self._query_algo_order(
                        symbol=symbol, client_algo_id=client_id
                    )
                except Exception:
                    return False
                if current is not None and str(
                    current.get("algoStatus") or ""
                ).upper() == "NEW":
                    return False
            elif state not in terminal:
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

    async def _audit_mainnet_algo_lifecycle(
        self,
        open_algo_orders: List[Dict[str, Any]],
        *,
        exchange_positions: List[Dict[str, Any]],
        exchange_open_orders: List[Dict[str, Any]],
    ) -> None:
        """Reconcile Local Mainnet Algo owners and close only proven-flat chains.

        This routine is read-only against Binance. It can append an exchange
        child order to the local ledger only after the durable Algo owner and
        Binance's linked order ID agree exactly; this gives subsequent fill
        recovery a durable lineage rather than silently adopting an unknown
        account order.
        """
        repository = self.algo_protection_repository
        if repository is None:
            if open_algo_orders:
                raise FillRecoveryError(
                    "unowned open Mainnet Algo orders exist without durable owner storage"
                )
            checkpoint, _, persisted = await self._scan_launch_history(
                symbol="ETHUSDC",
                history_kind="ALL_ALGO_ORDERS",
                path=self._all_algo_orders_path,
                retention=_ALL_ALGO_RETENTION,
            )
            anchor_at = checkpoint["anchor_at"].astimezone(timezone.utc)
            if any(
                isinstance(row.get("event_at"), datetime)
                and row["event_at"].astimezone(timezone.utc) >= anchor_at
                for row in persisted
            ):
                raise FillRecoveryError("unowned Algo order history exists")
            return

        list_all = getattr(repository, "list_protections", None)
        list_active = getattr(repository, "list_active_protections", None)
        if not callable(list_all) or not callable(list_active):
            raise FillRecoveryError("Mainnet Algo repository cannot enumerate durable owners")
        checkpoint, algo_history, persisted_history = await self._scan_launch_history(
            symbol="ETHUSDC",
            history_kind="ALL_ALGO_ORDERS",
            path=self._all_algo_orders_path,
            retention=_ALL_ALGO_RETENTION,
        )
        mainnet_launch_id = str(checkpoint.get("run_id") or "")
        if not mainnet_launch_id:
            raise FillRecoveryError("Mainnet Algo reconciliation has no durable launch identity")
        anchor_at = checkpoint["anchor_at"].astimezone(timezone.utc)
        all_records = await list_all(venue="binance_mainnet")
        all_active_records = await list_active(venue="binance_mainnet")
        if any(
            not isinstance(record, dict)
            or record.get("environment") != "MAINNET"
            or str(record.get("symbol", "")).upper() != "ETHUSDC"
            for record in all_records
        ):
            raise FillRecoveryError("Mainnet Algo owner scope or environment is invalid")
        if any(
            str(record.get("mainnet_launch_id") or "") != mainnet_launch_id
            for record in all_active_records
        ):
            raise FillRecoveryError("active Mainnet Algo owner belongs to a different or unbound launch")
        # Closed history from earlier launches remains auditable in PostgreSQL,
        # but it must not be mixed into this launch's owner map.
        all_records = [
            record
            for record in all_records
            if str(record.get("mainnet_launch_id") or "") == mainnet_launch_id
        ]
        active_records = [
            record
            for record in all_active_records
            if str(record.get("mainnet_launch_id") or "") == mainnet_launch_id
        ]

        owners: Dict[Tuple[str, str], Dict[str, Any]] = {}
        owners_by_entry: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for record in all_records:
            owner_entry = (
                str(record["symbol"]).upper(),
                str(record["entry_client_order_id"]),
            )
            if owner_entry in owners_by_entry:
                raise FillRecoveryError("Mainnet Algo entry owner identity is duplicated")
            owners_by_entry[owner_entry] = record
            for role in ("stop", "take_profit"):
                client_id = str(record.get(f"{role}_client_algo_id") or "")
                algo_id = record.get(f"{role}_algo_id")
                if not client_id:
                    raise FillRecoveryError("Mainnet Algo protection client identity is missing")
                key = (owner_entry[0], client_id)
                if key in owners:
                    raise FillRecoveryError("Mainnet Algo protection identity is duplicated")
                owners[key] = {
                    "algo_id": str(algo_id) if algo_id not in (None, "") else None,
                    "client_algo_id": client_id,
                    "record": record,
                    "role": role,
                }
        active_keys = {
            (str(record.get("symbol", "")).upper(), str(record.get("entry_client_order_id", "")))
            for record in active_records
        }
        if not active_keys.issubset(owners_by_entry):
            raise FillRecoveryError("active Mainnet Algo owner is missing from owner history")

        launch_rows: Dict[str, Dict[str, Any]] = {}
        launch_clients: Dict[Tuple[str, str], str] = {}
        triggered_by_entry: Dict[Tuple[str, str], List[Tuple[Dict[str, Any], Dict[str, Any]]]] = {}
        for row in algo_history:
            algo_id = self._positive_exchange_id(row.get("algoId"), field="Algo ID")
            previous = launch_rows.get(algo_id)
            if previous is not None and previous != row:
                raise FillRecoveryError("Mainnet Algo history repeats an ID with conflicting data")
            launch_rows[algo_id] = row
        for row in launch_rows.values():
            client_id = str(row.get("clientAlgoId") or "")
            symbol = str(row.get("symbol") or "").upper()
            owner_key = (symbol, client_id)
            owner = owners.get(owner_key)
            if owner is None:
                raise FillRecoveryError("unowned Algo order history exists")
            if owner["algo_id"] != str(int(row["algoId"])) or not self._testnet_algo_history_row_matches_owner(row, owner):
                raise FillRecoveryError("Mainnet Algo history differs from its durable owner")
            previous_id = launch_clients.get(owner_key)
            if previous_id is not None and previous_id != owner["algo_id"]:
                raise FillRecoveryError("Mainnet Algo client ID maps to multiple exchange IDs")
            launch_clients[owner_key] = owner["algo_id"]
            status = str(row.get("algoStatus") or "").strip().upper()
            if status in {"TRIGGERED", "FINISHED"}:
                entry_key = (symbol, str(owner["record"]["entry_client_order_id"]))
                triggered_by_entry.setdefault(entry_key, []).append((row, owner))

        # allAlgoOrders is a create-time history route: an Algo created before
        # the last durable checkpoint can trigger later and never appear in a
        # subsequent create-time window. Query every active owned Algo by its
        # exact exchange ID so the lifecycle uses a fresh exchange state.
        for record in active_records:
            owner_state = str(record.get("state") or "").upper()
            try:
                owner_filled = Decimal(str(record.get("filled_quantity")))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise FillRecoveryError("active Mainnet Algo owner fill quantity is invalid") from exc
            if not owner_filled.is_finite() or owner_filled < 0:
                raise FillRecoveryError("active Mainnet Algo owner fill quantity is invalid")
            if owner_state == "PENDING" and owner_filled == 0:
                if record.get("stop_algo_id") or record.get("take_profit_algo_id"):
                    raise FillRecoveryError("unfilled Mainnet PENDING owner has assigned protection IDs")
                continue
            for role in ("stop", "take_profit"):
                owner_key = (
                    str(record["symbol"]).upper(),
                    str(record[f"{role}_client_algo_id"]),
                )
                owner = owners.get(owner_key)
                if owner is None or owner["algo_id"] is None:
                    raise FillRecoveryError("active Mainnet Algo owner has no assigned exchange ID")
                try:
                    current_row = await self.rest_client.request(
                        "GET",
                        self._algo_order_query_path,
                        signed=True,
                        params={"symbol": owner_key[0], "algoId": owner["algo_id"]},
                    )
                except Exception as exc:
                    raise FillRecoveryError(
                        "active Mainnet Algo state could not be refreshed by exact exchange ID"
                    ) from exc
                if (
                    not isinstance(current_row, dict)
                    or not self._testnet_algo_history_row_matches_owner(current_row, owner)
                ):
                    raise FillRecoveryError(
                        "fresh Mainnet Algo state differs from its durable owner"
                    )
                history_repository = self._resolve_history_repository()
                observe = getattr(history_repository, "record_algo_history_observation", None)
                if not callable(observe):
                    raise FillRecoveryError("durable exact-ID Algo observation storage is unavailable")
                exact_item = self._history_item(
                    current_row,
                    history_kind="ALL_ALGO_ORDERS",
                    id_field="algoId",
                    client_field="clientAlgoId",
                    time_fields=("createTime", "time", "updateTime"),
                )
                exact_item["payload"] = current_row
                try:
                    await observe(
                        checkpoint=checkpoint,
                        item=exact_item,
                        observed_at=datetime.now(timezone.utc),
                    )
                except Exception as exc:
                    raise FillRecoveryError(
                        "fresh exact-ID Algo state could not be durably recorded"
                    ) from exc
                launch_rows[owner["algo_id"]] = current_row

        # Rebuild trigger ownership from the latest exact-ID snapshots above;
        # historical pages alone may contain only the original NEW state.
        triggered_by_entry.clear()
        for row in launch_rows.values():
            status = str(row.get("algoStatus") or "").strip().upper()
            if status not in {"TRIGGERED", "FINISHED"}:
                continue
            owner = owners.get(
                (str(row.get("symbol") or "").upper(), str(row.get("clientAlgoId") or ""))
            )
            if owner is None:
                raise FillRecoveryError("triggered Mainnet Algo has no durable owner")
            entry_key = (
                str(owner["record"]["symbol"]).upper(),
                str(owner["record"]["entry_client_order_id"]),
            )
            triggered_by_entry.setdefault(entry_key, []).append((row, owner))

        # Every currently open Algo must map back to one exact, still-open
        # owner. The matcher also compares side, position side, trigger,
        # closePosition semantics, and conditional type.
        open_by_owner: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for row in open_algo_orders:
            key = (
                str(row.get("symbol") or "").upper(),
                str(row.get("clientAlgoId") or ""),
            )
            owner = owners.get(key)
            if owner is None or key in open_by_owner:
                raise FillRecoveryError("open Mainnet Algo order is unowned or duplicated")
            if owner["algo_id"] is None or not self._testnet_algo_row_matches_owner(
                row, owner, source="open"
            ):
                raise FillRecoveryError("open Mainnet Algo order differs from durable protection")
            open_by_owner[key] = row

        for entry_key, record in owners_by_entry.items():
            state = str(record.get("state") or "").upper()
            try:
                filled_quantity = Decimal(str(record.get("filled_quantity")))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise FillRecoveryError("Mainnet Algo owner fill quantity is invalid") from exc
            if not filled_quantity.is_finite() or filled_quantity < 0:
                raise FillRecoveryError("Mainnet Algo owner fill quantity is invalid")
            if state in {"DEGRADED", "UNKNOWN"}:
                raise FillRecoveryError("Mainnet Algo owner is degraded or unknown")
            triggered = triggered_by_entry.get(entry_key, [])
            if state == "PENDING" and filled_quantity == 0:
                if triggered:
                    raise FillRecoveryError("unfilled Mainnet owner has a triggered Algo order")
                if any(key[0] == entry_key[0] and owner["record"] is record and key in open_by_owner
                       for key, owner in owners.items()):
                    raise FillRecoveryError("unfilled Mainnet Algo owner unexpectedly has open protection")
                continue
            if filled_quantity > 0 and state == "PENDING":
                raise FillRecoveryError("filled Mainnet Algo owner has no verified protection state")
            if state == "PROTECTED" and not triggered:
                expected_keys = {
                    (entry_key[0], str(record[f"{role}_client_algo_id"]))
                    for role in ("stop", "take_profit")
                }
                if not expected_keys.issubset(open_by_owner):
                    raise FillRecoveryError("durable Mainnet protection is missing an open Algo")
            if state == "CLOSE_PENDING" and not triggered:
                self._history_diffs.append(ReconciliationDiff(
                    code="ALGO_OWNER_CLOSE_PENDING",
                    symbol=entry_key[0],
                    local_value=entry_key[1],
                ))
            if state == "CLOSED" and not triggered:
                closure_proven = (
                    await self._mainnet_unfilled_entry_proof_matches_owner(
                        record, launch_id=mainnet_launch_id
                    )
                    if filled_quantity == 0
                    else self._mainnet_closure_proof_matches_owner(
                        record, launch_id=mainnet_launch_id
                    )
                )
                if not closure_proven:
                    raise FillRecoveryError(
                        "Mainnet CLOSED owner has no valid launch-scoped reconciliation proof"
                    )
                owner_open_algos = [
                    key for key in open_by_owner
                    if key[0] == entry_key[0] and owners[key]["record"] is record
                ]
                current_amount = self._matching_position_amount(
                    exchange_positions,
                    symbol=entry_key[0],
                    position_side=str(record["position_side"]),
                )
                if owner_open_algos or (filled_quantity > 0 and current_amount != 0):
                    raise FillRecoveryError(
                        "CLOSED Mainnet Algo owner still has exchange protection or position exposure"
                    )
            if state == "CLOSED" and triggered:
                # A previously closed chain is still re-verified below; a
                # reopened position or surviving protection will invalidate it.
                pass

        if not triggered_by_entry:
            if any(
                isinstance(item.get("event_at"), datetime)
                and item["event_at"].astimezone(timezone.utc) >= anchor_at
                and (str(item.get("client_id") or ""), str(item.get("item_id") or ""))
                not in {
                    (owner["client_algo_id"], str(owner["algo_id"]))
                    for owner in owners.values() if owner["algo_id"] is not None
                }
                for item in persisted_history
            ):
                raise FillRecoveryError("Mainnet durable Algo history contains an unowned identity")
            return

        all_order_checkpoint, all_order_history, _ = await self._scan_launch_history(
            symbol="ETHUSDC",
            history_kind="ALL_ORDERS",
            path=self._all_orders_path,
            retention=_ALL_ORDERS_RETENTION,
        )
        trade_checkpoint, trade_history, _ = await self._scan_launch_history(
            symbol="ETHUSDC",
            history_kind="USER_TRADES",
            path=self._user_trades_path,
            retention=_USER_TRADES_RETENTION,
        )
        if any(
            current.get(field) != checkpoint.get(field)
            for current in (all_order_checkpoint, trade_checkpoint)
            for field in ("anchor_at", "runtime_target", "run_id", "symbol")
        ):
            raise FillRecoveryError("Mainnet Algo/order/fill history anchors differ")

        get_protection = getattr(repository, "get_protection", None)
        set_state = getattr(repository, "set_protection_state", None)
        close_with_proof = getattr(repository, "close_mainnet_protection_with_proof", None)
        if not callable(get_protection) or not callable(set_state) or not callable(close_with_proof):
            raise FillRecoveryError("Mainnet Algo repository cannot durably transition owner state")

        for entry_key, triggered in triggered_by_entry.items():
            if len(triggered) != 1:
                for row, owner in triggered:
                    record = owner["record"]
                    if str(record.get("state", "")).upper() not in {"CLOSED", "DEGRADED", "UNKNOWN"}:
                        await set_state(
                            "binance_mainnet", entry_key[0], entry_key[1], "CLOSE_PENDING",
                            reason=f"multiple Algo triggers; algo {row.get('algoId')}",
                        )
                raise FillRecoveryError("multiple Algo protections triggered for one owner")
            algo_row, owner = triggered[0]
            record = owner["record"]
            state = str(record.get("state") or "").upper()
            algo_id = self._positive_exchange_id(algo_row.get("algoId"), field="Algo ID")
            if state not in {"CLOSED", "CLOSE_PENDING"}:
                pending = await set_state(
                    "binance_mainnet", entry_key[0], entry_key[1], "CLOSE_PENDING",
                    reason=f"Algo {algo_id} triggered; child order pending verification",
                )
                if pending is None or str(pending.get("state", "")).upper() != "CLOSE_PENDING":
                    raise FillRecoveryError("Mainnet Algo owner did not durably enter CLOSE_PENDING")
            owner_readback = await get_protection("binance_mainnet", entry_key[0], entry_key[1])
            if owner_readback is None or str(owner_readback.get("state", "")).upper() not in {
                "CLOSE_PENDING", "CLOSED"
            }:
                raise FillRecoveryError("Mainnet CLOSE_PENDING owner read-back failed")
            order_id = self._triggered_algo_order_id(algo_row)
            if state == "PENDING" or Decimal(str(record.get("filled_quantity", "0"))) <= 0:
                raise FillRecoveryError("triggered Algo has no durable entry fill owner")

            linked_history = [
                row for row in all_order_history
                if str(row.get("orderId")) == order_id
                and str(row.get("symbol", "")).upper() == entry_key[0]
            ]
            if len(linked_history) > 1:
                raise FillRecoveryError("triggered Algo normal order is duplicated in allOrders")
            normal_order = await self.rest_client.request(
                "GET", self._order_path, signed=True,
                params={"symbol": entry_key[0], "orderId": order_id},
            )
            if not isinstance(normal_order, dict):
                raise FillRecoveryError("triggered Algo normal order query is invalid")
            expected_side = "SELL" if str(record["entry_side"]).upper() == "BUY" else "BUY"
            for row in [*linked_history, normal_order]:
                if (
                    self._positive_exchange_id(row.get("orderId"), field="triggered order ID") != order_id
                    or str(row.get("symbol", "")).upper() != entry_key[0]
                    or str(row.get("side", "")).upper() != expected_side
                    or str(row.get("positionSide", "")).upper() != str(record["position_side"]).upper()
                    or not str(row.get("clientOrderId") or "")
                ):
                    raise FillRecoveryError("triggered Algo child order differs from its durable owner")
            if linked_history and (
                str(linked_history[0].get("clientOrderId"))
                != str(normal_order.get("clientOrderId"))
            ):
                raise FillRecoveryError(
                    "triggered Algo order query differs from durable allOrders history"
                )
            try:
                original_quantity = Decimal(str(normal_order["origQty"]))
                executed_quantity = Decimal(str(normal_order["executedQty"]))
            except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                raise FillRecoveryError("triggered Algo child order quantity is invalid") from exc
            if (
                not original_quantity.is_finite()
                or not executed_quantity.is_finite()
                or original_quantity <= 0
                or executed_quantity < 0
                or executed_quantity > original_quantity
            ):
                raise FillRecoveryError("triggered Algo child order quantity is outside bounds")
            order_status = str(normal_order.get("status") or "").upper()
            if order_status not in {"NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
                raise FillRecoveryError("triggered Algo child order status is unknown")
            # allOrders is filtered by creation time. A child captured as NEW
            # can fill after that window closes; persist its exact-ID refresh
            # before lifecycle comparison or ledger adoption, even on restart.
            normal_order = (await self._persist_exact_history_rows(
                all_order_checkpoint, [normal_order],
            ))[0]

            child_trades = [
                row for row in trade_history
                if str(row.get("orderId")) == order_id
                and str(row.get("symbol", "")).upper() == entry_key[0]
            ]
            try:
                history_quantity = sum(
                    (Decimal(str(trade["qty"])) for trade in child_trades), Decimal(0)
                )
            except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                raise FillRecoveryError("triggered Algo fill quantity is invalid") from exc
            if history_quantity != executed_quantity:
                exact_trades = await self._fetch_exact_order_trades(entry_key[0], order_id)
                exact_trades = await self._persist_exact_history_rows(
                    trade_checkpoint, exact_trades,
                )
                # Keep earlier durable trades if the exact query is lagging;
                # canonical trade IDs prevent overlap from double-counting.
                by_trade_id = {str(trade.get("id")): trade for trade in child_trades}
                by_trade_id.update({str(trade["id"]): trade for trade in exact_trades})
                child_trades = list(by_trade_id.values())
            trade_ids: set[str] = set()
            trade_quantity = Decimal("0")
            for trade in child_trades:
                trade_id = self._positive_exchange_id(trade.get("id"), field="triggered trade ID")
                if trade_id in trade_ids:
                    raise FillRecoveryError("triggered Algo child order has duplicate trade IDs")
                trade_ids.add(trade_id)
                if (
                    str(trade.get("side", "")).upper() != expected_side
                    or str(trade.get("positionSide", "")).upper()
                    != str(record["position_side"]).upper()
                ):
                    raise FillRecoveryError("triggered Algo fill differs from its owner side")
                try:
                    qty = Decimal(str(trade["qty"]))
                except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
                    raise FillRecoveryError("triggered Algo fill quantity is invalid") from exc
                if not qty.is_finite() or qty <= 0:
                    raise FillRecoveryError("triggered Algo fill quantity is invalid")
                trade_quantity += qty
            if trade_quantity != executed_quantity:
                raise FillRecoveryError("triggered Algo fills do not equal the child order execution quantity")

            # This is the one narrowly authorized local adoption path: exact
            # owner -> Algo ID -> child order ID -> allOrders proof. It lets
            # the ordinary durable fill-recovery pipeline attach the child
            # fills to a local order and rejects any alternate lineage.
            if not hasattr(self.ledger, "upsert_raw_exchange_order"):
                raise FillRecoveryError("execution ledger cannot persist a triggered Algo child order")
            await self.ledger.upsert_raw_exchange_order(normal_order)

            open_child = any(
                str(row.get("orderId")) == order_id
                or str(row.get("clientOrderId")) == str(normal_order.get("clientOrderId"))
                for row in exchange_open_orders
            )
            current_amount = self._matching_position_amount(
                exchange_positions,
                symbol=entry_key[0],
                position_side=str(record["position_side"]),
            )
            try:
                owner_filled_quantity = Decimal(str(record.get("filled_quantity")))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise FillRecoveryError("Mainnet Algo owner fill quantity is invalid") from exc
            owner_open_algos = [
                key for key in open_by_owner
                if key[0] == entry_key[0]
                and owners[key]["record"] is record
            ]
            if (
                order_status == "FILLED"
                and executed_quantity > 0
                and owner_filled_quantity.is_finite()
                and executed_quantity == owner_filled_quantity
                and not open_child
                and current_amount == 0
                and owner_open_algos
            ):
                cancelled = await self._cancel_local_mainnet_owned_algos(record)
                if cancelled:
                    owner_open_algos = []
            closed = (
                order_status == "FILLED"
                and executed_quantity > 0
                and owner_filled_quantity.is_finite()
                and executed_quantity == owner_filled_quantity
                and not open_child
                and current_amount == 0
                and not owner_open_algos
            )
            if not closed:
                self._history_diffs.append(ReconciliationDiff(
                    code="ALGO_TRIGGER_REMAINS_CLOSE_ONLY",
                    symbol=entry_key[0],
                    local_value=entry_key[1],
                    exchange_value={
                        "order_status": order_status,
                        "executed_quantity": str(executed_quantity),
                        "owner_filled_quantity": str(owner_filled_quantity),
                        "position_amount": str(current_amount),
                        "open_child_order": open_child,
                        "open_owner_algos": len(owner_open_algos),
                    },
                ))
                continue

            if state != "CLOSED":
                closed_record = await close_with_proof(
                    entry_key[0],
                    entry_key[1],
                    {
                        "algo_id": algo_id,
                        "order_id": order_id,
                        "client_order_id": str(normal_order["clientOrderId"]),
                        "order_status": order_status,
                        "executed_quantity": str(executed_quantity),
                        "trade_quantity": str(trade_quantity),
                        "position_quantity": str(current_amount),
                        "open_child_order_ids": [],
                        "open_owner_algo_ids": [],
                        "verified_at": datetime.now(timezone.utc),
                    },
                )
                if closed_record is None or str(closed_record.get("state", "")).upper() != "CLOSED":
                    raise FillRecoveryError("Mainnet Algo owner could not durably transition to CLOSED")
            closed_readback = await get_protection("binance_mainnet", entry_key[0], entry_key[1])
            if (
                closed_readback is None
                or str(closed_readback.get("state", "")).upper() != "CLOSED"
                or str(closed_readback.get("entry_client_order_id")) != entry_key[1]
                or not self._mainnet_closure_proof_matches_owner(
                    closed_readback, launch_id=mainnet_launch_id
                )
            ):
                raise FillRecoveryError("Mainnet Algo CLOSED owner read-back failed")

    async def _audit_exchange_order_history(self, tracked_orders: List[Any]) -> List[ReconciliationDiff]:
        """Fail closed if Mainnet order history cannot prove tracked exchange identity."""
        if self.environment != environment_label(BinanceEnvironment.MAINNET):
            return []
        by_symbol: Dict[str, List[Any]] = {}
        for order in tracked_orders:
            symbol = str(getattr(order, "symbol", "")).upper()
            if not symbol:
                raise FillRecoveryError("allOrders symbol is unavailable")
            by_symbol.setdefault(symbol, []).append(order)
            if (
                str(getattr(order, "status", "")).upper() == "REJECTED"
                and not getattr(order, "exchange_order_id", None)
            ):
                # A definitive pre-acceptance rejection has no exchange
                # identity to match, but still establishes a symbol whose
                # complete exchange history must be scanned. PENDING and
                # ambiguous records are never omitted.
                continue
            if not getattr(order, "exchange_order_id", None):
                raise FillRecoveryError(
                    f"allOrders anchor for {symbol} is unavailable for a non-terminal order"
                )
        if not by_symbol:
            by_symbol["ETHUSDC"] = []
        for symbol, local_orders in sorted(by_symbol.items()):
            expected = {
                str(order.client_order_id): str(order.exchange_order_id)
                for order in local_orders
                if order.client_order_id and getattr(order, "exchange_order_id", None)
            }
            checkpoint, _, persisted = await self._scan_launch_history(
                symbol=symbol,
                history_kind="ALL_ORDERS",
                path=self._all_orders_path,
                retention=_ALL_ORDERS_RETENTION,
            )
            anchor_at = checkpoint["anchor_at"].astimezone(timezone.utc)
            launch_items = [
                item for item in persisted
                if isinstance(item.get("event_at"), datetime)
                and item["event_at"].astimezone(timezone.utc) >= anchor_at
            ]
            seen_clients: Dict[str, str] = {}
            for item in launch_items:
                order_id = str(item["item_id"])
                client_id = str(item.get("client_id") or "")
                if not client_id:
                    raise FillRecoveryError(f"allOrders client ID for {symbol} is missing")
                previous_id = seen_clients.get(client_id)
                if previous_id is not None and previous_id != order_id:
                    self._history_diffs.append(ReconciliationDiff(
                        code="DUPLICATE_EXCHANGE_CLIENT_ORDER_ID", symbol=symbol,
                        local_value=previous_id, exchange_value=order_id,
                    ))
                seen_clients[client_id] = order_id
                expected_id = expected.get(client_id)
                if expected_id is not None and expected_id != order_id:
                    self._history_diffs.append(ReconciliationDiff(
                        code="EXCHANGE_ORDER_ID_MISMATCH", symbol=symbol,
                        local_value=expected_id, exchange_value=order_id,
                    ))
                elif expected_id is None:
                    self._history_diffs.append(ReconciliationDiff(
                        code="EXCHANGE_ORDER_UNKNOWN_LOCALLY", symbol=symbol,
                        exchange_value=order_id,
                    ))
            for client_id, expected_id in expected.items():
                if seen_clients.get(client_id) != expected_id:
                    self._history_diffs.append(ReconciliationDiff(
                        code="TRACKED_ORDER_MISSING_FROM_HISTORY", symbol=symbol,
                        local_value=expected_id,
                    ))
        return self._history_diffs

    @property
    def _income_path(self) -> str:
        return "/papi/v1/um/income" if self.portfolio_margin else "/fapi/v1/income"

    async def _fetch_reconciliation_snapshot_inputs(
        self,
    ) -> tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Fetch account, positions, and open orders truthfully for classic or portfolio margin."""
        if self.portfolio_margin:
            positions = await self.rest_client.request(
                "GET", "/papi/v1/um/positionRisk", signed=True
            )
            open_orders = await self.rest_client.request(
                "GET", "/papi/v1/um/openOrders", signed=True
            )
            balances = await self.rest_client.request(
                "GET", "/papi/v1/balance", signed=True
            )
            um_account = await self.rest_client.request(
                "GET", "/papi/v1/um/account", signed=True
            )
            if (
                not isinstance(positions, list)
                or not isinstance(open_orders, list)
                or not isinstance(balances, list)
                or not isinstance(um_account, dict)
            ):
                raise ValueError("Binance Portfolio Margin reconciliation response is invalid")

            usdc_bal = next(
                (b for b in balances if isinstance(b, dict) and b.get("asset") == "USDC"),
                {},
            )
            cross_asset = usdc_bal.get("crossMarginAsset", "0")
            cross_free = usdc_bal.get("crossMarginFree", "0")
            unrealized_pnl = usdc_bal.get("umUnrealizedPNL", "0")

            um_usdc = next(
                (
                    a
                    for a in um_account.get("assets", [])
                    if isinstance(a, dict) and a.get("asset") == "USDC"
                ),
                {},
            )

            cross_asset_dec = Decimal(str(cross_asset or "0"))
            unrealized_pnl_dec = Decimal(str(unrealized_pnl or "0"))
            margin_balance_dec = cross_asset_dec + unrealized_pnl_dec

            papi_usdc_asset = {
                "asset": "USDC",
                "walletBalance": str(cross_asset),
                "marginBalance": str(margin_balance_dec),
                "availableBalance": str(cross_free),
                "unrealizedProfit": str(unrealized_pnl),
                "initialMargin": str(um_usdc.get("initialMargin", "0")),
                "maintMargin": str(um_usdc.get("maintMargin", "0")),
                "positionInitialMargin": str(um_usdc.get("positionInitialMargin", "0")),
            }
            account = {
                "assets": [papi_usdc_asset],
                "multiAssetsMargin": False,
                "portfolioMargin": True,
            }

            existing_symbols = {
                str(p.get("symbol", "")).upper()
                for p in positions
                if isinstance(p, dict)
            }
            merged_positions = list(positions)
            for p in um_account.get("positions", []):
                if isinstance(p, dict):
                    sym = str(p.get("symbol", "")).upper()
                    if sym and sym not in existing_symbols:
                        merged_positions.append(p)
                        existing_symbols.add(sym)

            return account, merged_positions, open_orders

        positions = await self.rest_client.request(
            "GET", "/fapi/v2/positionRisk", signed=True
        )
        open_orders = await self.rest_client.request(
            "GET", "/fapi/v1/openOrders", signed=True
        )
        account = await self.rest_client.request("GET", "/fapi/v2/account", signed=True)
        if not isinstance(positions, list) or not isinstance(open_orders, list):
            raise ValueError("Binance bootstrap response is invalid")
        return account, positions, open_orders

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
        """Read restart-safe account-wide UTC-day USDC trading PnL.

        Binance exposes pagination by ``page`` and returns all income types when
        ``incomeType`` is omitted. We deliberately include realized PnL,
        commissions, and funding for every USDC-settled contract. A short page is complete;
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
                    self._income_path,
                    signed=True,
                    params={
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
                    item_type = str(item.get("incomeType", "")).strip().upper()
                    if item_asset != "USDC":
                        # Daily loss is account-wide but denominated in the
                        # policy collateral. Never assume parity for other assets.
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
                "monitor_event=reconciliation_drift environment=%s diff_count=%d diffs=%s",
                self.environment,
                len(diffs or []),
                [(d.code, d.symbol, str(d.local_value), str(d.exchange_value)) for d in (diffs or [])],
            )
        elif status == "UNKNOWN":
            logger.error(
                "monitor_event=readiness_degraded environment=%s reason=reconciliation_unknown",
                self.environment,
            )
        return status

    async def _resolve_missing_exchange_order_ids(self, tracked_orders: List[Any]) -> None:
        """Resolve exchange_order_id via order endpoint for tracked orders before trade recovery."""
        for order in tracked_orders:
            if getattr(order, "client_order_id", None) and not getattr(order, "exchange_order_id", None):
                try:
                    order_info = await self.rest_client.request(
                        "GET",
                        self._order_path,
                        signed=True,
                        params={"symbol": order.symbol, "origClientOrderId": order.client_order_id},
                    )
                    if isinstance(order_info, dict) and order_info.get("orderId"):
                        order.exchange_order_id = str(order_info["orderId"])
                        await self.ledger.upsert_order(order)
                except Exception as exc:
                    logger.debug("Pre-trade recovery order query skipped for %s: %s", order.client_order_id, exc)

    async def _recover_recent_trades(
        self, symbols: set[str], tracked_orders: List[Any] | None = None
    ) -> List[ReconciliationDiff]:
        """Recover only fills with local lineage and report foreign fills."""
        diffs: List[ReconciliationDiff] = []
        for symbol in sorted({str(item).upper() for item in symbols if item}):
            trades = await self._fetch_user_trade_history(symbol)
            exchange_orders_by_client_id: Dict[str, str] = {}
            for trade in trades:
                client_id = str(trade.get("clientOrderId") or "")
                exchange_order_id = str(trade.get("orderId") or "")
                previous_exchange_order_id = exchange_orders_by_client_id.get(client_id)
                if client_id and previous_exchange_order_id and previous_exchange_order_id != exchange_order_id:
                    diffs.append(
                        ReconciliationDiff(
                            code="DUPLICATE_EXCHANGE_CLIENT_ORDER_ID",
                            symbol=str(trade.get("symbol") or symbol).upper(),
                            local_value=previous_exchange_order_id,
                            exchange_value=exchange_order_id,
                        )
                    )
                    continue
                if client_id:
                    exchange_orders_by_client_id[client_id] = exchange_order_id
                local_order = await self.ledger.get_order_by_exchange_id(
                    exchange_order_id
                )
                if local_order is None and trade.get("clientOrderId"):
                    local_order = await self.ledger.get_order_by_client_id(
                        str(trade["clientOrderId"])
                    )
                if (
                    local_order is not None
                    and local_order.exchange_order_id
                    and str(local_order.exchange_order_id) != str(trade.get("orderId"))
                ):
                    # Client order IDs are only unique among open exchange orders.
                    # A later execution may reuse one; never assign that fill to
                    # a different exchange order in the durable ledger.
                    diffs.append(
                        ReconciliationDiff(
                            code="EXCHANGE_ORDER_ID_MISMATCH",
                            symbol=str(trade.get("symbol") or symbol).upper(),
                            local_value=str(local_order.exchange_order_id),
                            exchange_value=str(trade.get("orderId")),
                        )
                    )
                    continue
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

    async def _fetch_user_trade_history(self, symbol: str) -> List[Dict[str, Any]]:
        """Read per-symbol trades through durable Mainnet launch coverage."""
        if self.environment != environment_label(BinanceEnvironment.MAINNET):
            page = await self.rest_client.request(
                "GET", self._user_trades_path, signed=True,
                params={"symbol": symbol, "limit": 1000},
            )
            if not isinstance(page, list):
                raise FillRecoveryError(f"userTrades response for {symbol} is invalid")
            if len(page) >= 1000:
                raise FillRecoveryError(f"userTrades history for {symbol} is incomplete")
            return page
        _, trades, _ = await self._scan_launch_history(
            symbol=str(symbol).upper(),
            history_kind="USER_TRADES",
            path=self._user_trades_path,
            retention=_USER_TRADES_RETENTION,
        )
        return trades

    async def _recover_order_fills(self, local_order: Any, order_status: Dict[str, Any]) -> int:
        order_id = order_status.get("orderId") or local_order.exchange_order_id
        if not order_id:
            raise FillRecoveryError("Filled order has no exchange order ID")
        matching_trades = await self._fetch_exact_order_trades(local_order.symbol, str(order_id))
        if not matching_trades:
            raise FillRecoveryError(
                f"No fills recovered for exchange order {order_id} reported {order_status.get('status')}"
            )
        recovered_fills: List[ExchangeFill] = []
        for trade in matching_trades:
            for key in ("id", "orderId"):
                raw_id = trade.get(key)
                if raw_id not in (None, ""):
                    self._recovered_trade_ids.add(str(raw_id))
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
        diffs: List[ReconciliationDiff] = [*self._unattributed_fill_diffs, *self._history_diffs]
        recovered_reduction_symbols: set[str] = set()
        terminal_executed_quantities: dict[int, Decimal] = {}
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
                    "GET", self._order_path, signed=True, params=query_params
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
                if local_order.reduce_only or local_order.risk_class in {
                    "REDUCE_RISK",
                    "RECOVERY",
                    "CLOSE",
                    "EMERGENCY",
                }:
                    recovered_reduction_symbols.add(str(local_order.symbol).upper())
            elif status in ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED"):
                try:
                    executed_qty = _required_decimal(order_status, "executedQty")
                    if executed_qty < 0 or executed_qty > local_order.quantity:
                        raise FillRecoveryError("terminal executedQty is outside order bounds")
                    if executed_qty > 0:
                        await self._recover_order_fills(local_order, order_status)
                    terminal_executed_quantities[id(local_order)] = executed_qty
                except BinanceAuthenticationError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "Terminal fill verification failed for %s: %s",
                        local_order.client_order_id, exc,
                    )
                    diffs.append(ReconciliationDiff(
                        code="FILL_RECOVERY_FAILED", symbol=local_order.symbol,
                        local_value=local_order.client_order_id, exchange_value=str(exc),
                    ))
                    continue
                if local_status == "FILLED":
                    diffs.append(
                        ReconciliationDiff(
                            code="TERMINAL_ORDER_STATUS_MISMATCH",
                            symbol=local_order.symbol,
                            local_value=local_status,
                            exchange_value=status,
                        )
                    )
                else:
                    local_order.status = status
                    local_order.exchange_order_id = str(
                        order_status.get("orderId") or local_order.exchange_order_id or ""
                    )
                    await self.ledger.upsert_order(local_order)
                    if executed_qty > 0:
                        if local_order.reduce_only or local_order.risk_class in {
                            "REDUCE_RISK", "RECOVERY", "CLOSE", "EMERGENCY",
                        }:
                            recovered_reduction_symbols.add(str(local_order.symbol).upper())
            else:
                diffs.append(
                    ReconciliationDiff(
                        code="LOCAL_OPEN_ORDER_MISSING_ON_EXCHANGE",
                        symbol=local_order.symbol,
                        local_value=local_order.client_order_id or local_order.exchange_order_id,
                        exchange_value=status,
                    )
                )

        if self._recovered_trade_ids:
            diffs = [
                d
                for d in diffs
                if not (
                    d.code == "EXCHANGE_FILL_UNKNOWN_LOCALLY"
                    and str(d.exchange_value) in self._recovered_trade_ids
                )
            ]

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

        # A status alone is not execution proof. Filled/partial orders require
        # canonical fills; canceled/expired/rejected orders also require their
        # canonical totals to equal the freshly queried executedQty.
        get_fills = getattr(self.ledger, "get_fills", None)
        fills = await get_fills() if callable(get_fills) else []
        for local_order in all_orders:
            local_status = str(local_order.status).upper()
            terminal_executed_qty = terminal_executed_quantities.get(id(local_order))
            if local_status not in {"FILLED", "PARTIALLY_FILLED"} and terminal_executed_qty is None:
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
            if not has_linked_fill and terminal_executed_qty != 0:
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
            if terminal_executed_qty is not None and recovered_quantity != terminal_executed_qty:
                diffs.append(ReconciliationDiff(
                    code="FILL_QUANTITY_MISMATCH", symbol=str(local_order.symbol).upper(),
                    local_value=str(terminal_executed_qty), exchange_value=str(recovered_quantity),
                ))
            elif local_status == "FILLED" and recovered_quantity != local_order.quantity:
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
        self._history_diffs = []
        self._set_status("RECONCILING", [])
        try:
            snapshot_observed_at = utc_now()
            account, positions, open_orders = (
                await self._fetch_reconciliation_snapshot_inputs()
            )
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
                observed_at=snapshot_observed_at,
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
            await self._audit_algo_orders(
                tracked_orders,
                exchange_positions=positions,
                exchange_open_orders=open_orders,
            )
            if callable(get_all_orders):
                tracked_orders = await get_all_orders()
            await self._resolve_missing_exchange_order_ids(tracked_orders)
            await self._audit_exchange_order_history(tracked_orders)
            symbols.update(
                str(order.symbol)
                for order in tracked_orders
                if getattr(order, "symbol", None)
            )
            await self._recover_recent_trades(symbols, tracked_orders)

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
        self._history_diffs = []
        self._set_status("RECONCILING", [])
        try:
            snapshot_observed_at = utc_now()
            account, exchange_positions, exchange_open_orders = (
                await self._fetch_reconciliation_snapshot_inputs()
            )
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
                observed_at=snapshot_observed_at,
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
            await self._audit_algo_orders(
                tracked_orders,
                exchange_positions=exchange_positions,
                exchange_open_orders=exchange_open_orders,
            )
            if callable(get_all_orders):
                tracked_orders = await get_all_orders()
            await self._resolve_missing_exchange_order_ids(tracked_orders)
            await self._audit_exchange_order_history(tracked_orders)
            symbols.update(
                str(order.symbol)
                for order in tracked_orders
                if getattr(order, "symbol", None)
            )
            await self._recover_recent_trades(symbols, tracked_orders)
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
