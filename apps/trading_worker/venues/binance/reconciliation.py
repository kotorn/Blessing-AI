"""Authoritative Binance USDⓈ-M account, order, fill, and position reconciliation."""

import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel

from domain.enums import OrderSide, PositionSide
from domain.models import ExchangeFill, utc_now

from .ledger import ExecutionLedger
from .models import BinanceAuthenticationError, ExchangeAccountSnapshot
from .rest_client import BinanceRestClient

logger = logging.getLogger("blessing.binance.reconciliation")


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
    return max(Decimal("0"), distance * Decimal("100"))


def build_account_snapshot(
    account: Dict[str, Any],
    position_risk: List[Dict[str, Any]],
    *,
    environment: str = "BINANCE_TESTNET",
) -> ExchangeAccountSnapshot:
    """Build a truthful snapshot from current Binance v2 account and position responses."""

    if environment != "BINANCE_TESTNET":
        raise ValueError("Account snapshots are accepted only from Binance Testnet")
    if not isinstance(account, dict) or not isinstance(position_risk, list):
        raise ValueError("Binance account snapshot payload is invalid")

    wallet_balance = _required_decimal(account, "totalWalletBalance")
    margin_balance = _required_decimal(account, "totalMarginBalance")
    available_balance = _required_decimal(account, "availableBalance")
    unrealized_pnl = _required_decimal(account, "totalUnrealizedProfit")
    total_initial_margin = _required_decimal(account, "totalInitialMargin")
    total_maint_margin = _required_decimal(account, "totalMaintMargin")
    position_initial_margin = _required_decimal(account, "totalPositionInitialMargin")

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
        position_side = str(position.get("positionSide", "BOTH")).upper()
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

        notional: Optional[Decimal] = None
        raw_notional = position.get("notional")
        if raw_notional not in (None, ""):
            try:
                parsed_notional = abs(Decimal(str(raw_notional)))
                if parsed_notional.is_finite() and parsed_notional > 0:
                    notional = parsed_notional
            except (InvalidOperation, ValueError):
                notional = None
        if notional is None:
            notional = abs(amount) * mark_price
        if notional <= 0 or not notional.is_finite():
            raise ValueError(f"Active position {symbol} has uncomputable notional")
        total_notional += notional

        distance = _liquidation_distance(position, mark_price)
        if distance is None:
            liquidation_known = False
        else:
            liquidation_distances.append(distance)

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

    symbol = str(trade.get("symbol") or getattr(local_order, "symbol", ""))
    client_order_id = str(
        trade.get("clientOrderId") or getattr(local_order, "client_order_id", "")
    )
    if not symbol or not client_order_id:
        raise FillRecoveryError("Trade is missing symbol or clientOrderId")
    try:
        side = OrderSide(str(trade.get("side") or local_order.side.value))
        position_side = PositionSide(
            str(trade.get("positionSide") or local_order.position_side.value)
        )
        quantity = Decimal(str(trade["qty"]))
        price = Decimal(str(trade["price"]))
        commission = Decimal(str(trade["commission"]))
        realized_pnl = Decimal(str(trade.get("realizedPnl", "0")))
    except (AttributeError, InvalidOperation, ValueError) as exc:
        raise FillRecoveryError("Trade contains invalid fill fields") from exc
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
        maker=bool(trade.get("maker", False)),
        event_time=trade["time"],
        transaction_time=trade.get("time"),
        source=source,
    )


class BinanceReconciliation:
    def __init__(self, rest_client: BinanceRestClient, ledger: ExecutionLedger):
        self.rest_client = rest_client
        self.ledger = ledger
        self.last_diffs: List[ReconciliationDiff] = []
        self.last_status = "UNKNOWN"
        self.authentication_failed = False

    def _set_status(
        self, status: str, diffs: Optional[List[ReconciliationDiff]] = None
    ) -> str:
        self.last_status = status
        if diffs is not None:
            self.last_diffs = diffs
        return status

    async def _recover_recent_trades(self, symbols: set[str]) -> None:
        for symbol in sorted(symbols):
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
                if local_order is None and not trade.get("clientOrderId"):
                    # Binance userTrades does not always return clientOrderId;
                    # do not invent one for historical trades that are not
                    # associated with a local order.
                    continue
                fill = _exchange_fill_from_trade(
                    trade, local_order, source="BINANCE_TESTNET_BOOTSTRAP"
                )
                await self.ledger.append_fill(fill)

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
        recovered = 0
        for trade in matching_trades:
            fill = _exchange_fill_from_trade(
                trade, local_order, source="BINANCE_TESTNET_RECOVERY"
            )
            await self.ledger.append_fill(fill)
            recovered += 1
        return recovered

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
            side = str(position.get("positionSide", "BOTH")).upper()
            if not symbol:
                raise ValueError("Active position is missing symbol")
            try:
                PositionSide(side)
            except ValueError as exc:
                raise ValueError(f"Active position has unsupported positionSide: {side}") from exc
            result[(str(symbol), side)] = amount
        return result

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
        diffs: List[ReconciliationDiff] = []
        recovered_position_symbols: set[str] = set()

        for local_order in await self.ledger.get_open_orders():
            found = bool(
                local_order.exchange_order_id
                and str(local_order.exchange_order_id) in exchange_order_ids
            ) or bool(
                local_order.client_order_id
                and local_order.client_order_id in exchange_client_ids
            )
            if found:
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

            status = str(order_status.get("status", "UNKNOWN"))
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
            elif status in ("CANCELED", "CANCELLED", "EXPIRED", "REJECTED"):
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

        # A locally tracked order can disappear from openOrders after a fill
        # before its private-stream position event arrives. Once the order
        # status and canonical userTrades have both been recovered, seed only
        # that symbol from the authoritative positionRisk response. Other
        # symbols remain subject to the normal mismatch checks below.
        for position in exchange_positions:
            if str(position.get("symbol", "")).upper() in recovered_position_symbols:
                await self.ledger.upsert_position(position)

        exchange_position_map = self._active_position_map(exchange_positions)
        local_position_map: Dict[Tuple[str, str], Decimal] = {}
        for position in await self.ledger.get_positions():
            if position.quantity == 0:
                continue
            local_position_map[(position.symbol, position.position_side.value)] = position.quantity

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
        return diffs

    async def bootstrap(self) -> bool:
        """Fetch, seed, compare, and only then mark the ledger synchronized."""
        self.authentication_failed = False
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

            active_positions = [p for p in positions if _position_amount(p) != 0]
            # Seed data provisionally.  The ledger must not become initialized
            # until all exchange snapshots and fill verification succeed.
            await self.ledger.replace_positions(active_positions, mark_initialized=False)
            await self.ledger.set_account_snapshot(None)
            for order_data in open_orders:
                await self.ledger.upsert_raw_exchange_order(order_data)
            await self.ledger.update_balances(
                _required_decimal(account, "totalWalletBalance"),
                _required_decimal(account, "totalMarginBalance"),
            )

            symbols = {
                str(position.get("symbol")) for position in active_positions if position.get("symbol")
            }
            symbols.update(
                str(order.get("symbol")) for order in open_orders if order.get("symbol")
            )
            await self._recover_recent_trades(symbols)

            diffs = await self._collect_diffs(positions, open_orders)
            if diffs:
                self._set_status("MISMATCH", diffs)
                return False
            snapshot = build_account_snapshot(account, positions)
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

            diffs = await self._collect_diffs(exchange_positions, exchange_open_orders)
            if diffs:
                self._set_status("MISMATCH", diffs)
                return self.last_status

            snapshot = build_account_snapshot(account, exchange_positions)
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
