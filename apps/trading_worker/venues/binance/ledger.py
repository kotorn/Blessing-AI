from decimal import Decimal, InvalidOperation
from typing import Protocol, List, Optional, Union
from domain.enums import EconomicRiskClass
from domain.models import (
    ExecutionOrder,
    ExchangeFill,
    ExchangePosition,
    MarketType,
    OrderSide,
    PositionSide,
    TimeInForce,
    utc_now,
)
import logging

logger = logging.getLogger("blessing.binance.ledger")

from .models import ExchangeAccountSnapshot


def _exchange_bool(value: object) -> bool:
    """Parse Binance boolean fields without making ``bool('false')`` true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


class ExecutionLedger(Protocol):
    async def upsert_order(self, order: ExecutionOrder) -> None: ...
    async def upsert_raw_exchange_order(self, raw_order: dict) -> None: ...
    async def append_fill(self, fill: ExchangeFill) -> None: ...
    async def get_order_by_client_id(self, client_order_id: str) -> Optional[ExecutionOrder]: ...
    async def get_order_by_exchange_id(self, exchange_order_id: str) -> Optional[ExecutionOrder]: ...
    async def replace_positions(
        self,
        raw_positions: List[Union[dict, ExchangePosition]],
        *,
        mark_initialized: bool = True,
    ) -> None: ...
    async def mark_initialized(self) -> None: ...
    async def upsert_position(self, raw_position: Union[dict, ExchangePosition]) -> None: ...
    async def get_open_orders(self) -> List[ExecutionOrder]: ...
    async def get_all_orders(self) -> List[ExecutionOrder]: ...
    async def get_fills(self) -> List[ExchangeFill]: ...
    async def get_positions(self) -> List[ExchangePosition]: ...
    async def clear_positions_for_symbol(self, symbol: str) -> None: ...
    async def is_initialized(self) -> bool: ...
    async def has_fill(self, deduplication_key: str) -> bool: ...
    async def update_balances(self, wallet_balance: Decimal, margin_balance: Decimal) -> None: ...
    async def get_balances(self) -> tuple[Decimal, Decimal]: ...
    async def set_account_snapshot(
        self, snapshot: Optional[ExchangeAccountSnapshot]
    ) -> None: ...
    async def get_account_snapshot(self) -> Optional[ExchangeAccountSnapshot]: ...

class InMemoryLedger:
    def __init__(self):
        self.orders: dict[str, ExecutionOrder] = {}
        self.fills: list[ExchangeFill] = []
        self._fill_keys: set[str] = set()
        self.positions: list[ExchangePosition] = []
        self._initialized = False
        self.wallet_balance: Decimal = Decimal("0")
        self.margin_balance: Decimal = Decimal("0")
        self.account_snapshot: Optional[ExchangeAccountSnapshot] = None
        self.on_order_update = None
        self.on_fill_update = None
        self.on_position_update = None

    async def set_account_snapshot(self, snapshot: Optional[ExchangeAccountSnapshot]) -> None:
        self.account_snapshot = snapshot

    async def get_account_snapshot(self) -> Optional[ExchangeAccountSnapshot]:
        return self.account_snapshot

    async def update_balances(self, wallet_balance: Decimal, margin_balance: Decimal) -> None:
        self.wallet_balance = wallet_balance
        self.margin_balance = margin_balance

    async def get_balances(self) -> tuple[Decimal, Decimal]:
        return self.wallet_balance, self.margin_balance

    async def is_initialized(self) -> bool:
        return self._initialized

    async def upsert_order(self, order: ExecutionOrder) -> None:
        self.orders[order.client_order_id] = order

    async def upsert_raw_exchange_order(self, raw_order: dict) -> None:
        required_fields = ("symbol", "clientOrderId", "status", "side", "origQty", "orderId")
        missing = [field for field in required_fields if raw_order.get(field) in (None, "")]
        if missing:
            raise ValueError(f"Exchange order is missing required fields: {', '.join(missing)}")

        client_oid = raw_order["clientOrderId"]
        status = raw_order["status"]
        try:
            side = OrderSide(raw_order["side"])
        except Exception:
            raise ValueError(f"Unsupported exchange order side: {raw_order.get('side')}")
        raw_time_in_force = str(raw_order.get("timeInForce", "GTC")).upper()
        time_in_force = (
            TimeInForce.POST_ONLY
            if raw_time_in_force == "GTX"
            else TimeInForce(raw_time_in_force)
        )
        order = ExecutionOrder(
            symbol=str(raw_order["symbol"]).upper(),
            side=side,
            quantity=Decimal(str(raw_order["origQty"])),
            price=Decimal(str(raw_order.get("price", "0"))),
            order_type=raw_order.get("type", "LIMIT"),
            client_order_id=client_oid,
            status=status,
            exchange_order_id=str(raw_order["orderId"]),
            timestamp=utc_now(),
            market_type=MarketType.USDM_FUTURES,
            position_side=PositionSide(str(raw_order.get("positionSide", "BOTH")).upper()),
            reduce_only=_exchange_bool(raw_order.get("reduceOnly", False)),
            time_in_force=time_in_force,
            strategy_id=(self.orders.get(client_oid).strategy_id if client_oid in self.orders else "portfolio"),
            decision_id=(self.orders.get(client_oid).decision_id if client_oid in self.orders else None),
            target_exposure_id=(
                self.orders.get(client_oid).target_exposure_id
                if client_oid in self.orders
                else None
            ),
            source_intent_ids=(
                list(self.orders[client_oid].source_intent_ids)
                if client_oid in self.orders
                else []
            ),
            risk_class=(
                self.orders[client_oid].risk_class
                if client_oid in self.orders
                else EconomicRiskClass.NOOP
            ),
        )
        self.orders[client_oid] = order
        if self.on_order_update:
            self.on_order_update(order)

    def _get_fill_key(self, fill: ExchangeFill) -> str:
        # Binance trade ids are only unique within an exchange account. Keep
        # the venue/environment in the in-memory key so a Testnet fill can
        # never suppress or masquerade as a Mainnet fill after a restart. The
        # reconciliation phase (BOOTSTRAP vs RECOVERY) is not part of the
        # exchange identity: the same trade may be observed in both phases.
        source = str(getattr(fill, "source", "UNKNOWN")).strip().upper() or "UNKNOWN"
        venue = next(
            (
                label
                for label in ("BINANCE_TESTNET", "BINANCE_MAINNET")
                if source == label or source.startswith(label + "_")
            ),
            source,
        )
        return f"{venue}:{str(fill.symbol).upper()}:{fill.exchange_trade_id}"

    async def append_fill(self, fill: ExchangeFill) -> None:
        key = self._get_fill_key(fill)
        if key in self._fill_keys or await self.has_fill(key):
            logger.info("Ignoring duplicate fill event: %s", key)
            return
        self._fill_keys.add(key)
        self.fills.append(fill)
        if self.on_fill_update:
            self.on_fill_update(fill)
        
    async def has_fill(self, deduplication_key: str) -> bool:
        parts = deduplication_key.split(":", 2)
        if len(parts) == 3:
            source, symbol, trade_id = parts
            normalized_source = source.upper()
            return any(
                self._get_fill_key(f).split(":", 1)[0] == normalized_source
                and str(f.symbol).upper() == symbol.upper()
                and str(f.exchange_trade_id) == trade_id
                for f in self.fills
            )
        if len(parts) == 2:
            symbol, trade_id = parts
            return any(
                str(f.symbol).upper() == symbol.upper()
                and str(f.exchange_trade_id) == trade_id
                for f in self.fills
            )
        return any(str(f.exchange_trade_id) == deduplication_key for f in self.fills)

    async def get_order_by_client_id(self, client_order_id: str) -> Optional[ExecutionOrder]:
        return self.orders.get(client_order_id)

    async def get_order_by_exchange_id(self, exchange_order_id: str) -> Optional[ExecutionOrder]:
        for o in self.orders.values():
            if str(o.exchange_order_id) == str(exchange_order_id):
                return o
        return None
        
    def _to_exchange_position(self, pos: Union[dict, ExchangePosition]) -> ExchangePosition:
        if isinstance(pos, ExchangePosition):
            return pos
        if not isinstance(pos, dict):
            raise ValueError("Exchange position must be an object")
        required_fields = ("symbol", "positionAmt")
        missing = [field for field in required_fields if pos.get(field) in (None, "")]
        if missing:
            raise ValueError(
                f"Exchange position is missing required fields: {', '.join(missing)}"
            )

        try:
            quantity = Decimal(str(pos["positionAmt"]))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Exchange position has an invalid positionAmt") from exc
        if not quantity.is_finite():
            raise ValueError("Exchange position has a non-finite positionAmt")

        is_active = quantity != 0
        if is_active:
            active_required = (
                "positionSide",
                "markPrice",
            )
            active_missing = [
                field for field in active_required if pos.get(field) in (None, "")
            ]
            if active_missing:
                raise ValueError(
                    "Active exchange position is missing required fields: "
                    + ", ".join(active_missing)
                )

        ps_str = str(pos.get("positionSide", "BOTH")).upper()
        try:
            ps = PositionSide(ps_str)
        except ValueError as exc:
            raise ValueError(f"Unsupported exchange position side: {ps_str}") from exc

        def parse_decimal(
            field: str,
            *,
            default: Optional[Decimal] = None,
            positive: bool = False,
            nonnegative: bool = False,
        ) -> Decimal:
            raw_value = pos.get(field)
            if raw_value in (None, ""):
                if default is not None:
                    return default
                raise ValueError(f"Exchange position is missing required field: {field}")
            try:
                parsed = Decimal(str(raw_value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(f"Exchange position has invalid {field}") from exc
            if not parsed.is_finite():
                raise ValueError(f"Exchange position has non-finite {field}")
            if positive and parsed <= 0:
                raise ValueError(f"Exchange position has unusable {field}")
            if nonnegative and parsed < 0:
                raise ValueError(f"Exchange position has negative {field}")
            return parsed

        entry_price = parse_decimal(
            "entryPrice", default=Decimal("0"), positive=is_active
        )
        raw_mark_price = pos.get("markPrice")
        mark_price = (
            None
            if raw_mark_price in (None, "") and not is_active
            else parse_decimal("markPrice", positive=is_active, nonnegative=not is_active)
        )
        unrealized_pnl = parse_decimal(
            "unRealizedProfit", default=Decimal("0")
        )
        raw_margin_type = pos.get("marginType")
        if raw_margin_type in (None, ""):
            # Missing exchange margin mode is an unknown observation.  Never
            # turn it into a cross-margin claim because Mainnet risk gates must
            # fail closed when the mode cannot be verified.
            margin_type = "UNKNOWN"
        elif not isinstance(raw_margin_type, str) or not raw_margin_type.strip():
            raise ValueError("Exchange position has invalid marginType")
        else:
            margin_type = raw_margin_type
        leverage = parse_decimal(
            "leverage", default=Decimal("0"), positive=is_active, nonnegative=True
        )
        raw_liquidation_price = pos.get("liquidationPrice")
        liquidation_price = None
        if raw_liquidation_price not in (None, ""):
            parsed_liquidation_price = parse_decimal(
                "liquidationPrice", nonnegative=True
            )
            # Binance uses zero to signal that a liquidation price is not
            # available. Keep that as UNKNOWN rather than a fake usable price.
            if parsed_liquidation_price > 0:
                liquidation_price = parsed_liquidation_price

        return ExchangePosition(
            symbol=str(pos.get("symbol", "")).upper(),
            position_side=ps,
            quantity=quantity,
            entry_price=entry_price,
            mark_price=mark_price,
            unrealized_pnl=unrealized_pnl,
            margin_type=margin_type,
            event_time=pos.get("eventTime"),
            source=pos.get("source", "UNKNOWN"),
            liquidation_price=liquidation_price,
            leverage=leverage,
        )

    async def replace_positions(
        self,
        raw_positions: List[Union[dict, ExchangePosition]],
        *,
        mark_initialized: bool = True,
    ) -> None:
        self.positions = [self._to_exchange_position(p) for p in raw_positions]
        if self.on_position_update:
            for p in self.positions:
                self.on_position_update(p)
        if mark_initialized:
            self._initialized = True

    async def mark_initialized(self) -> None:
        self._initialized = True

    async def upsert_position(self, raw_position: Union[dict, ExchangePosition]) -> None:
        norm_pos = self._to_exchange_position(raw_position)
        for i, p in enumerate(self.positions):
            if p.symbol == norm_pos.symbol and p.position_side == norm_pos.position_side:
                self.positions[i] = norm_pos
                if self.on_position_update:
                    self.on_position_update(norm_pos)
                return
        self.positions.append(norm_pos)
        if self.on_position_update:
            self.on_position_update(norm_pos)
        
    async def get_open_orders(self) -> List[ExecutionOrder]:
        return [o for o in self.orders.values() if o.status in ("NEW", "PARTIALLY_FILLED")]

    async def get_all_orders(self) -> List[ExecutionOrder]:
        return list(self.orders.values())

    async def get_fills(self) -> List[ExchangeFill]:
        return list(self.fills)
        
    async def get_positions(self) -> List[ExchangePosition]:
        return self.positions

    async def clear_positions_for_symbol(self, symbol: str) -> None:
        normalized = str(symbol).upper()
        self.positions = [
            position for position in self.positions
            if str(position.symbol).upper() != normalized
        ]
