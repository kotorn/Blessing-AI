from decimal import Decimal
from typing import Protocol, List, Optional, Union
from domain.models import ExecutionOrder, ExchangeFill, ExchangePosition, PositionSide, OrderSide, utc_now
import logging

logger = logging.getLogger("blessing.binance.ledger")

class ExecutionLedger(Protocol):
    async def upsert_order(self, order: ExecutionOrder) -> None: ...
    async def upsert_raw_exchange_order(self, raw_order: dict) -> None: ...
    async def append_fill(self, fill: ExchangeFill) -> None: ...
    async def get_order_by_client_id(self, client_order_id: str) -> Optional[ExecutionOrder]: ...
    async def get_order_by_exchange_id(self, exchange_order_id: str) -> Optional[ExecutionOrder]: ...
    async def replace_positions(self, raw_positions: List[Union[dict, ExchangePosition]]) -> None: ...
    async def upsert_position(self, raw_position: Union[dict, ExchangePosition]) -> None: ...
    async def get_open_orders(self) -> List[ExecutionOrder]: ...
    async def get_positions(self) -> List[ExchangePosition]: ...
    async def is_initialized(self) -> bool: ...
    async def has_fill(self, deduplication_key: str) -> bool: ...
    async def update_balances(self, wallet_balance: Decimal, margin_balance: Decimal) -> None: ...
    async def get_balances(self) -> tuple[Decimal, Decimal]: ...

class InMemoryLedger:
    def __init__(self):
        self.orders: dict[str, ExecutionOrder] = {}
        self.fills: list[ExchangeFill] = []
        self._fill_keys: set[str] = set()
        self.positions: list[ExchangePosition] = []
        self._initialized = False
        self.wallet_balance: Decimal = Decimal("0")
        self.margin_balance: Decimal = Decimal("0")

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
        client_oid = raw_order.get("clientOrderId", "")
        status = raw_order.get("status", "NEW")
        try:
            side = OrderSide(raw_order.get("side", "BUY"))
        except Exception:
            side = OrderSide.BUY
        order = ExecutionOrder(
            symbol=raw_order.get("symbol", ""),
            side=side,
            quantity=Decimal(str(raw_order.get("origQty", "0"))),
            price=Decimal(str(raw_order.get("price", "0"))),
            order_type=raw_order.get("type", "LIMIT"),
            client_order_id=client_oid,
            status=status,
            exchange_order_id=str(raw_order.get("orderId", "")),
            timestamp=utc_now()
        )
        self.orders[client_oid] = order

    def _get_fill_key(self, fill: ExchangeFill) -> str:
        return f"{fill.symbol}_{fill.exchange_trade_id}"

    async def append_fill(self, fill: ExchangeFill) -> None:
        key = self._get_fill_key(fill)
        if key in self._fill_keys or await self.has_fill(fill.exchange_trade_id):
            logger.info("Ignoring duplicate fill event: %s", key)
            return
        self._fill_keys.add(key)
        self.fills.append(fill)
        
    async def has_fill(self, exchange_trade_id: str) -> bool:
        return any(f.exchange_trade_id == exchange_trade_id for f in self.fills)

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
        ps_str = str(pos.get("positionSide", "BOTH")).upper()
        try:
            ps = PositionSide(ps_str)
        except Exception:
            ps = PositionSide.BOTH
        return ExchangePosition(
            symbol=pos.get("symbol", ""),
            position_side=ps,
            quantity=Decimal(str(pos.get("positionAmt", "0"))),
            entry_price=Decimal(str(pos.get("entryPrice", "0"))),
            mark_price=Decimal(str(pos.get("markPrice", "0"))) if pos.get("markPrice") is not None else None,
            unrealized_pnl=Decimal(str(pos.get("unRealizedProfit", "0"))),
            margin_type=pos.get("marginType", "cross"),
            event_time=pos.get("eventTime"),
            source=pos.get("source", "BINANCE_TESTNET")
        )

    async def replace_positions(self, raw_positions: List[Union[dict, ExchangePosition]]) -> None:
        self.positions = [self._to_exchange_position(p) for p in raw_positions]
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

