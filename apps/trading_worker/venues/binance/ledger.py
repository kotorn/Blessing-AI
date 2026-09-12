from typing import Protocol, List, Optional
from domain.models import ExecutionOrder, ExchangeFill

class ExecutionLedger(Protocol):
    async def upsert_order(self, order: ExecutionOrder) -> None:
        ...
        
    async def append_fill(self, fill: ExchangeFill) -> None:
        ...
        
    async def get_order_by_client_id(self, client_order_id: str) -> Optional[ExecutionOrder]:
        ...

class InMemoryLedger:
    def __init__(self):
        self.orders = {}
        self.fills = []

    async def upsert_order(self, order: ExecutionOrder) -> None:
        self.orders[order.client_order_id] = order

    async def append_fill(self, fill: ExchangeFill) -> None:
        self.fills.append(fill)

    async def get_order_by_client_id(self, client_order_id: str) -> Optional[ExecutionOrder]:
        return self.orders.get(client_order_id)
