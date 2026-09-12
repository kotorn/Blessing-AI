from decimal import Decimal
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
    async def get_positions(self) -> List[ExchangePosition]: ...
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
            symbol=raw_order["symbol"],
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
            reduce_only=bool(raw_order.get("reduceOnly", False)),
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

    def _get_fill_key(self, fill: ExchangeFill) -> str:
        return f"{fill.symbol}:{fill.exchange_trade_id}"

    async def append_fill(self, fill: ExchangeFill) -> None:
        key = self._get_fill_key(fill)
        if key in self._fill_keys or await self.has_fill(key):
            logger.info("Ignoring duplicate fill event: %s", key)
            return
        self._fill_keys.add(key)
        self.fills.append(fill)
        
    async def has_fill(self, deduplication_key: str) -> bool:
        if ":" in deduplication_key:
            symbol, trade_id = deduplication_key.split(":", 1)
            return any(
                f.symbol == symbol and f.exchange_trade_id == trade_id for f in self.fills
            )
        return any(f.exchange_trade_id == deduplication_key for f in self.fills)

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
        required_fields = ("symbol", "positionAmt")
        missing = [field for field in required_fields if pos.get(field) in (None, "")]
        if missing:
            raise ValueError(
                f"Exchange position is missing required fields: {', '.join(missing)}"
            )
        ps_str = str(pos.get("positionSide", "BOTH")).upper()
        try:
            ps = PositionSide(ps_str)
        except ValueError as exc:
            raise ValueError(f"Unsupported exchange position side: {ps_str}") from exc
        return ExchangePosition(
            symbol=pos.get("symbol", ""),
            position_side=ps,
            quantity=Decimal(str(pos.get("positionAmt", "0"))),
            entry_price=Decimal(str(pos.get("entryPrice", "0"))),
            mark_price=Decimal(str(pos.get("markPrice", "0"))) if pos.get("markPrice") is not None else None,
            unrealized_pnl=Decimal(str(pos.get("unRealizedProfit", "0"))),
            margin_type=pos.get("marginType", "cross"),
            event_time=pos.get("eventTime"),
            source=pos.get("source", "BINANCE_TESTNET"),
            liquidation_price=(
                Decimal(str(pos["liquidationPrice"]))
                if pos.get("liquidationPrice") not in (None, "")
                else None
            ),
            leverage=(
                Decimal(str(pos["leverage"]))
                if pos.get("leverage") not in (None, "")
                else Decimal("0")
            ),
        )

    async def replace_positions(
        self,
        raw_positions: List[Union[dict, ExchangePosition]],
        *,
        mark_initialized: bool = True,
    ) -> None:
        self.positions = [self._to_exchange_position(p) for p in raw_positions]
        if mark_initialized:
            self._initialized = True

    async def mark_initialized(self) -> None:
        self._initialized = True

    async def upsert_position(self, raw_position: Union[dict, ExchangePosition]) -> None:
        norm_pos = self._to_exchange_position(raw_position)
        for i, p in enumerate(self.positions):
            if p.symbol == norm_pos.symbol and p.position_side == norm_pos.position_side:
                self.positions[i] = norm_pos
                return
        self.positions.append(norm_pos)
        
    async def get_open_orders(self) -> List[ExecutionOrder]:
        return [o for o in self.orders.values() if o.status in ("NEW", "PARTIALLY_FILLED")]
        
    async def get_positions(self) -> List[ExchangePosition]:
        return self.positions

