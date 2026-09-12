from decimal import Decimal
from typing import Protocol, List, Optional
from domain.models import ExecutionOrder, ExchangeFill, OrderSide, utc_now
import logging

logger = logging.getLogger("blessing.binance.ledger")

class ExecutionLedger(Protocol):
    async def upsert_order(self, order: ExecutionOrder) -> None: ...
    async def upsert_raw_exchange_order(self, raw_order: dict) -> None: ...
    async def append_fill(self, fill: ExchangeFill) -> None: ...
    async def get_order_by_client_id(self, client_order_id: str) -> Optional[ExecutionOrder]: ...
    async def get_order_by_exchange_id(self, exchange_order_id: str) -> Optional[ExecutionOrder]: ...
    async def replace_positions(self, raw_positions: List[dict]) -> None: ...
    async def upsert_position(self, raw_position: dict) -> None: ...
    async def get_open_orders(self) -> List[ExecutionOrder]: ...
    async def get_positions(self) -> List[dict]: ...
    async def is_initialized(self) -> bool: ...
    async def has_fill(self, exchange_trade_id: str) -> bool: ...

class InMemoryLedger:
    def __init__(self):
        self.orders = {}
        self.fills = []
        self.positions = []
        self._initialized = False
        
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

    async def append_fill(self, fill: ExchangeFill) -> None:
        if await self.has_fill(fill.exchange_trade_id):
            return
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
        
    async def replace_positions(self, raw_positions: List[dict]) -> None:
        self.positions = raw_positions
        self._initialized = True

    async def upsert_position(self, raw_position: dict) -> None:
        sym = raw_position.get("symbol")
        ps = raw_position.get("positionSide")
        # Replace existing or append
        for i, p in enumerate(self.positions):
            if p.get("symbol") == sym and p.get("positionSide") == ps:
                self.positions[i] = raw_position
                return
        self.positions.append(raw_position)
        
    async def get_open_orders(self) -> List[ExecutionOrder]:
        return [o for o in self.orders.values() if o.status in ("NEW", "PARTIALLY_FILLED")]
        
    async def get_positions(self) -> List[dict]:
        return self.positions
