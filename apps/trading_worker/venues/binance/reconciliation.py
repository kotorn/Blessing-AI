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
            
            account = await self.rest_client.request("GET", "/fapi/v2/account", signed=True)
            
            await self.ledger.replace_positions(active_positions)
            for order_data in open_orders:
                await self.ledger.upsert_raw_exchange_order(order_data)
                
            wb = Decimal(str(account.get("totalWalletBalance", "0")))
            mb = Decimal(str(account.get("totalMarginBalance", "0")))
            await self.ledger.update_balances(wb, mb)
            
            # Reconcile recent fills for active symbols
            active_symbols = set(p.get("symbol") for p in active_positions)
            for sym in active_symbols:
                try:
                    trades = await self.rest_client.request("GET", "/fapi/v1/userTrades", signed=True, params={"symbol": sym, "limit": 20})
                    for t in trades:
                        # minimal fill representation for bootstrap
                        from domain.models import ExchangeFill, OrderSide, PositionSide, utc_now
                        side = OrderSide(t.get("side", "BUY"))
                        ps = PositionSide(t.get("positionSide", "BOTH"))
                        fill = ExchangeFill(
                            exchange_trade_id=str(t.get("id")),
                            exchange_order_id=str(t.get("orderId")),
                            client_order_id="",
                            symbol=sym,
                            side=side,
                            position_side=ps,
                            quantity=Decimal(str(t.get("qty", "0"))),
                            price=Decimal(str(t.get("price", "0"))),
                            commission=Decimal(str(t.get("commission", "0"))),
                            commission_asset=t.get("commissionAsset"),
                            realized_pnl=Decimal(str(t.get("realizedPnl", "0"))),
                            maker=t.get("maker", False),
                            event_time=t.get("time", 0),
                            transaction_time=t.get("time", 0),
                            source="BINANCE_TESTNET"
                        )
                        await self.ledger.append_fill(fill)
                except Exception as t_err:
                    logger.warning("Failed to fetch recent trades for %s: %s", sym, t_err)
                
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
                        
                        if status in ("FILLED", "PARTIALLY_FILLED"):
                            local_order.status = status
                            
                            # GATE 20: MUST recover execution details for FILLED orders
                            try:
                                trades = await self.rest_client.request(
                                    "GET", 
                                    "/fapi/v1/userTrades", 
                                    signed=True, 
                                    params={"symbol": local_order.symbol}
                                )
                                from domain.models import ExchangeFill, OrderSide, PositionSide
                                for t in trades:
                                    # match by orderId
                                    if str(t.get("orderId")) == str(order_status.get("orderId")):
                                        fill = ExchangeFill(
                                            symbol=local_order.symbol,
                                            exchange_trade_id=str(t.get("id", "")),
                                            exchange_order_id=str(t.get("orderId", "")),
                                            client_order_id=local_order.client_order_id,
                                            side=OrderSide(t.get("side", local_order.side.value)),
                                            position_side=PositionSide(t.get("positionSide", local_order.position_side.value)),
                                            price=Decimal(str(t.get("price", "0"))),
                                            quantity=Decimal(str(t.get("qty", "0"))),
                                            commission=Decimal(str(t.get("commission", "0"))),
                                            commission_asset=t.get("commissionAsset", ""),
                                            realized_pnl=Decimal(str(t.get("realizedPnl", "0"))),
                                            maker=t.get("maker", False),
                                            event_time=t.get("time", 0),
                                            transaction_time=t.get("time", 0),
                                            source="BINANCE_TESTNET_RECOVERY"
                                        )
                                        await self.ledger.append_fill(fill)
                                logger.info("Recovered fills for order %s", local_order.client_order_id)
                                await self.ledger.upsert_order(local_order)
                                logger.info("Resolved local order %s status to terminal state: %s", local_order.client_order_id, status)
                                resolved = True
                            except Exception as t_err:
                                logger.warning("Failed to fetch userTrades for recovery of %s: %s", local_order.client_order_id, t_err)
                                pass
                            
                        elif status in ("CANCELED", "EXPIRED", "REJECTED"):
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
                if isinstance(p, dict):
                    amt = Decimal(str(p.get("positionAmt", "0")))
                    pos_side = str(p.get("positionSide", "BOTH")).upper()
                    sym = p.get("symbol", "")
                else:
                    amt = p.quantity
                    pos_side = p.position_side.name if hasattr(p.position_side, "name") else str(p.position_side).upper()
                    sym = p.symbol
                if amt != Decimal("0"):
                    local_pos_map[(sym, pos_side)] = amt

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

            # Re-sync balances as part of IN_SYNC
            try:
                from .models import ExchangeAccountSnapshot
                from datetime import datetime, timezone
                account = await self.rest_client.request("GET", "/fapi/v2/account", signed=True)
                wb = Decimal(str(account.get("totalWalletBalance", "0")))
                mb = Decimal(str(account.get("totalMarginBalance", "0")))
                await self.ledger.update_balances(wb, mb)
                
                snap = ExchangeAccountSnapshot(
                    wallet_balance=wb,
                    margin_balance=mb,
                    available_balance=Decimal(str(account.get("availableBalance", "0"))),
                    unrealized_pnl=Decimal(str(account.get("totalUnrealizedProfit", "0"))),
                    total_initial_margin=Decimal(str(account.get("totalInitialMargin", "0"))),
                    total_maint_margin=Decimal(str(account.get("totalMaintMargin", "0"))),
                    position_initial_margin=Decimal(str(account.get("totalPositionInitialMargin", "0"))),
                    total_position_notional=Decimal("0.0"),
                    effective_leverage=Decimal("0.0"),
                    margin_utilization_pct=Decimal("0.0"),
                    timestamp=datetime.now(timezone.utc)
                )
                
                pos_risk = await self.rest_client.request("GET", "/fapi/v2/positionRisk", signed=True)
                
                equity = snap.wallet_balance + snap.unrealized_pnl
                missing_mark_price = False
                min_liq_dist = Decimal("1.0")
                has_active_pos = False
                
                if equity > 0:
                    tot_notional = Decimal("0.0")
                    for p in pos_risk:
                        amt = abs(Decimal(str(p.get("positionAmt", "0"))))
                        if amt > 0:
                            has_active_pos = True
                            if "markPrice" not in p or p.get("markPrice") is None:
                                missing_mark_price = True
                            else:
                                mp = Decimal(str(p.get("markPrice", "0")))
                                tot_notional += amt * mp
                                liq = Decimal(str(p.get("liquidationPrice", "0")))
                                if liq > 0 and mp > 0:
                                    dist = abs(mp - liq) / mp
                                    if dist < min_liq_dist:
                                        min_liq_dist = dist
                    
                    if missing_mark_price:
                        snap.total_position_notional = Decimal("0.0")
                        snap.liquidation_safety = "UNKNOWN"
                    else:
                        snap.total_position_notional = tot_notional
                        snap.effective_leverage = snap.total_position_notional / equity
                        snap.margin_utilization_pct = (snap.total_maint_margin / equity) * Decimal("100")
                        
                        if has_active_pos:
                            snap.min_liquidation_distance_pct = min_liq_dist * Decimal("100")

                    
                await self.ledger.set_account_snapshot(snap)
            except Exception as e:
                logger.warning("Failed to update balances and account snapshot during reconciliation: %s", e)

            logger.info(
                "Reconciliation successful: %d exchange positions, %d open orders. State: IN_SYNC",
                len(exchange_pos_map),
                len(exchange_order_ids)
            )
            return "IN_SYNC"
        except Exception as e:
            logger.error("Reconciliation execution failed: %s", e)
            return "UNKNOWN"
