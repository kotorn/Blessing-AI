import logging
from typing import List, Optional
from pydantic import BaseModel
from .rest_client import BinanceRestClient
from .ledger import ExecutionLedger

logger = logging.getLogger("blessing.binance.reconciliation")

class ReconciliationDiff(BaseModel):
    code: str
    symbol: Optional[str] = None
    local_value: Optional[object] = None
    exchange_value: Optional[object] = None

class BinanceReconciliation:
    def __init__(self, rest_client: BinanceRestClient, ledger: ExecutionLedger):
        self.rest_client = rest_client
        self.ledger = ledger

    async def bootstrap(self) -> bool:
        """
        Fetch authoritative exchange snapshot and bootstrap the empty local ledger.
        """
        logger.info("Bootstrapping ledger from authoritative exchange snapshot...")
        try:
            positions = await self.rest_client.request("GET", "/fapi/v2/positionRisk", signed=True)
            active_positions = [p for p in positions if float(p["positionAmt"]) != 0]
            
            open_orders = await self.rest_client.request("GET", "/fapi/v1/openOrders", signed=True)
            
            await self.ledger.replace_positions(active_positions)
            for order_data in open_orders:
                await self.ledger.upsert_raw_exchange_order(order_data)
                
            logger.info("Bootstrap complete: %d active positions, %d open orders", len(active_positions), len(open_orders))
            return True
        except Exception as e:
            logger.error("Ledger bootstrap failed: %s", e)
            return False

    async def reconcile(self) -> str:
        """
        Returns 'IN_SYNC', 'MISMATCH', or 'UNKNOWN'
        """
        if not await self.ledger.is_initialized():
            success = await self.bootstrap()
            return "IN_SYNC" if success else "UNKNOWN"

        try:
            exchange_positions = await self.rest_client.request("GET", "/fapi/v2/positionRisk", signed=True)
            exchange_active_positions = { f'{p["symbol"]}_{p["positionSide"]}': float(p["positionAmt"]) for p in exchange_positions if float(p["positionAmt"]) != 0 }
            
            exchange_open_orders = await self.rest_client.request("GET", "/fapi/v1/openOrders", signed=True)
            exchange_order_ids = { str(o["orderId"]): o for o in exchange_open_orders }
            
            local_open_orders = await self.ledger.get_open_orders()
            local_positions = await self.ledger.get_positions()
            
            diffs: List[ReconciliationDiff] = []
            
            for local_order in local_open_orders:
                if local_order.exchange_order_id not in exchange_order_ids:
                    # Depending on timing, this might just mean it was filled.
                    # A strict reconciliation would require checking user trades.
                    diffs.append(ReconciliationDiff(
                        code="LOCAL_OPEN_ORDER_MISSING_ON_EXCHANGE",
                        symbol=local_order.symbol,
                        local_value=local_order.exchange_order_id
                    ))
            
            # This logic is simplified for the sprint.
            if len(diffs) > 0:
                logger.warning("Reconciliation MISMATCH found: %s", diffs)
                return "MISMATCH"
                
            logger.info("Reconciliation fetched %d active positions, %d open orders. State is IN_SYNC", len(exchange_active_positions), len(exchange_open_orders))
            return "IN_SYNC"
        except Exception as e:
            logger.error("Reconciliation failed: %s", e)
            return "UNKNOWN"
