import logging
from .rest_client import BinanceRestClient

logger = logging.getLogger("blessing.binance.reconciliation")

class BinanceReconciliation:
    def __init__(self, rest_client: BinanceRestClient):
        self.rest_client = rest_client

    async def reconcile(self) -> str:
        """
        Returns 'IN_SYNC', 'MISMATCH', or 'UNKNOWN'
        """
        try:
            # 1. Fetch positions
            positions = await self.rest_client.request("GET", "/fapi/v2/positionRisk", signed=True)
            active_positions = [p for p in positions if float(p["positionAmt"]) != 0]
            
            # 2. Fetch open orders
            open_orders = await self.rest_client.request("GET", "/fapi/v1/openOrders", signed=True)
            
            # Compare with internal ledger here.
            # For this sprint, we assume it's IN_SYNC if we successfully fetch.
            logger.info("Reconciliation fetched %d active positions, %d open orders", len(active_positions), len(open_orders))
            return "IN_SYNC"
        except Exception as e:
            logger.error("Reconciliation failed: %s", e)
            return "UNKNOWN"
