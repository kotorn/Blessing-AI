import logging
from decimal import Decimal
from typing import List, Optional, Dict, Tuple
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
        self.last_diffs: List[ReconciliationDiff] = []

    async def bootstrap(self) -> bool:
        """
        Fetch authoritative exchange snapshot and bootstrap the empty local ledger.
        Explicit state flow: UNINITIALIZED -> exchange snapshot -> bootstrap ledger -> verification -> IN_SYNC.
        """
        logger.info("Bootstrapping ledger from authoritative exchange snapshot...")
        try:
            positions = await self.rest_client.request("GET", "/fapi/v2/positionRisk", signed=True)
            active_positions = [p for p in positions if Decimal(str(p.get("positionAmt", "0"))) != Decimal("0")]
            
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
        Performs bidirectional reconciliation between local ledger and exchange state.
        Returns 'IN_SYNC', 'MISMATCH', or 'UNKNOWN'.
        """
        if not await self.ledger.is_initialized():
            success = await self.bootstrap()
            return "IN_SYNC" if success else "UNKNOWN"

        try:
            exchange_positions = await self.rest_client.request("GET", "/fapi/v2/positionRisk", signed=True)
            exchange_open_orders = await self.rest_client.request("GET", "/fapi/v1/openOrders", signed=True)

            exchange_order_ids: Dict[str, dict] = {str(o["orderId"]): o for o in exchange_open_orders}
            exchange_client_ids: Dict[str, dict] = {str(o.get("clientOrderId", "")): o for o in exchange_open_orders if o.get("clientOrderId")}

            local_open_orders = await self.ledger.get_open_orders()
            local_positions = await self.ledger.get_positions()

            diffs: List[ReconciliationDiff] = []

            # 1. Check local open orders against exchange open orders
            for local_order in local_open_orders:
                found_on_exchange = False
                if local_order.exchange_order_id and str(local_order.exchange_order_id) in exchange_order_ids:
                    found_on_exchange = True
                elif local_order.client_order_id and local_order.client_order_id in exchange_client_ids:
                    found_on_exchange = True

                if not found_on_exchange:
                    # Before declaring mismatch: query the specific order from exchange.
                    # It may have been filled, canceled, expired, or rejected.
                    resolved = False
                    try:
                        query_params = {"symbol": local_order.symbol}
                        if local_order.client_order_id:
                            query_params["origClientOrderId"] = local_order.client_order_id
                        elif local_order.exchange_order_id:
                            query_params["orderId"] = local_order.exchange_order_id

                        order_status = await self.rest_client.request("GET", "/fapi/v1/order", signed=True, params=query_params)
                        status = order_status.get("status")
                        if status in ("FILLED", "CANCELED", "EXPIRED", "REJECTED"):
                            local_order.status = status
                            await self.ledger.upsert_order(local_order)
                            logger.info("Resolved local order %s status to terminal state: %s", local_order.client_order_id, status)
                            resolved = True
                    except Exception as query_err:
                        logger.warning("Order query check failed for missing order %s: %s", local_order.client_order_id, query_err)

                    if not resolved:
                        diffs.append(ReconciliationDiff(
                            code="LOCAL_OPEN_ORDER_MISSING_ON_EXCHANGE",
                            symbol=local_order.symbol,
                            local_value=local_order.client_order_id or local_order.exchange_order_id,
                            exchange_value=None
                        ))

            # 2. Check exchange open orders against local ledger
            for ex_order_id, ex_order in exchange_order_ids.items():
                local_match = None
                cl_id = ex_order.get("clientOrderId")
                if cl_id:
                    local_match = await self.ledger.get_order_by_client_id(cl_id)
                if not local_match and hasattr(self.ledger, "get_order_by_exchange_id"):
                    local_match = await self.ledger.get_order_by_exchange_id(str(ex_order_id))

                if not local_match:
                    diffs.append(ReconciliationDiff(
                        code="EXCHANGE_OPEN_ORDER_UNKNOWN_LOCALLY",
                        symbol=ex_order.get("symbol"),
                        local_value=None,
                        exchange_value=ex_order_id
                    ))

            # 3. Decimal-safe positions comparison
            # Build normalized exchange positions map: (symbol, positionSide) -> Decimal(positionAmt)
            exchange_pos_map: Dict[Tuple[str, str], Decimal] = {}
            for p in exchange_positions:
                amt = Decimal(str(p.get("positionAmt", "0")))
                if amt != Decimal("0"):
                    pos_side = str(p.get("positionSide", "BOTH")).upper()
                    exchange_pos_map[(p["symbol"], pos_side)] = amt

            # Build normalized local positions map
            local_pos_map: Dict[Tuple[str, str], Decimal] = {}
            for p in local_positions:
                amt = Decimal(str(p.get("positionAmt", "0")))
                if amt != Decimal("0"):
                    pos_side = str(p.get("positionSide", "BOTH")).upper()
                    local_pos_map[(p.get("symbol", ""), pos_side)] = amt

            # Compare local positions to exchange
            for (sym, pside), local_amt in local_pos_map.items():
                key = (sym, pside)
                if key not in exchange_pos_map:
                    diffs.append(ReconciliationDiff(
                        code="LOCAL_POSITION_MISSING_ON_EXCHANGE",
                        symbol=sym,
                        local_value=str(local_amt),
                        exchange_value="0"
                    ))
                elif exchange_pos_map[key] != local_amt:
                    diffs.append(ReconciliationDiff(
                        code="POSITION_QTY_MISMATCH",
                        symbol=sym,
                        local_value=str(local_amt),
                        exchange_value=str(exchange_pos_map[key])
                    ))

            # Compare exchange positions to local
            for (sym, pside), ex_amt in exchange_pos_map.items():
                key = (sym, pside)
                if key not in local_pos_map:
                    diffs.append(ReconciliationDiff(
                        code="EXCHANGE_POSITION_UNKNOWN_LOCALLY",
                        symbol=sym,
                        local_value="0",
                        exchange_value=str(ex_amt)
                    ))

            self.last_diffs = diffs

            if len(diffs) > 0:
                logger.warning("Reconciliation MISMATCH detected with %d differences: %s", len(diffs), diffs)
                return "MISMATCH"

            logger.info(
                "Reconciliation successful: %d exchange positions, %d open orders. State: IN_SYNC",
                len(exchange_pos_map),
                len(exchange_order_ids)
            )
            return "IN_SYNC"
        except Exception as e:
            logger.error("Reconciliation execution failed: %s", e)
            return "UNKNOWN"
