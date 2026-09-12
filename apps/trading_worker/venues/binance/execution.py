import logging
import hashlib
from decimal import Decimal
from typing import List, Optional, Any

from domain.models import ExecutionDecision, ExecutionOrder, OrderSide, PositionSide, ExchangeFill, utc_now
from .config import BinanceEnvironment
from .rest_client import BinanceRestClient, BinanceAPIError
from .capabilities import BinanceCapabilities
from .user_stream import BinanceUserStream
from .reconciliation import BinanceReconciliation
from .ledger import ExecutionLedger, InMemoryLedger
from .models import ConnectionState

logger = logging.getLogger("blessing.venues.binance.execution")

class BinanceExecutionAdapter:
    """
    Blessing AI Execution Adapter (USDⓈ-M Futures) Native implementation.
    """
    def __init__(self, api_key: str = "", api_secret: str = "", env: BinanceEnvironment = BinanceEnvironment.TESTNET, ledger: Optional[ExecutionLedger] = None):
        if env == BinanceEnvironment.MAINNET:
            raise ValueError("LIVE execution mode is permanently blocked in this sprint.")
            
        self.env = env
        self.api_key = api_key
        self.api_secret = api_secret
        self.ledger = ledger or InMemoryLedger()
        
        self.rest_client = BinanceRestClient(api_key, api_secret, env)
        self.capabilities = BinanceCapabilities()
        self.user_stream = BinanceUserStream(self.rest_client, env, on_disconnect=self._on_user_stream_disconnect)
        self.reconciliation = BinanceReconciliation(self.rest_client, self.ledger)
        
        self.state = ConnectionState.DISCONNECTED

    async def _on_user_stream_disconnect(self):
        logger.warning("[%s] User stream disconnected. Pausing execution and reconciling.", self.env)
        self.state = ConnectionState.SYNCING
        sync_result = await self.reconciliation.reconcile()
        if sync_result == "IN_SYNC":
            self.state = ConnectionState.READY
        else:
            self.state = ConnectionState.DEGRADED

    async def connect(self):
        self.state = ConnectionState.CONNECTING
        await self.rest_client.init_session()
        
        self.state = ConnectionState.AUTHENTICATING
        success = await self.capabilities.discover(self.rest_client)
        if not success:
            self.state = ConnectionState.DEGRADED
            return False
            
        self.state = ConnectionState.STREAM_STARTING
        await self.user_stream.start(self._on_ws_event)
        
        self.state = ConnectionState.SYNCING
        sync_result = await self.reconciliation.reconcile()
        if sync_result == "IN_SYNC":
            self.state = ConnectionState.READY
            return True
        else:
            self.state = ConnectionState.DEGRADED
            return False

    async def arm(self) -> bool:
        """Alias for connect() to establish session, discover capabilities, start stream, and reconcile."""
        return await self.connect()

    async def _on_ws_event(self, event: Any):
        event_type = event.get("e")
        if event_type == "ORDER_TRADE_UPDATE":
            logger.info("WS Order Update: %s", event)
            order_info = event.get("o", {})
            symbol = order_info.get("s")
            client_order_id = order_info.get("c")
            status = order_info.get("X")
            
            existing_order = await self.ledger.get_order_by_client_id(client_order_id)
            if existing_order:
                existing_order.status = status
                await self.ledger.upsert_order(existing_order)
            else:
                # Create a minimal tracking representation if absent locally
                new_order = ExecutionOrder(
                    symbol=symbol,
                    side=OrderSide(order_info.get("S")),
                    quantity=Decimal(str(order_info.get("q", "0"))),
                    price=Decimal(str(order_info.get("p", "0"))),
                    order_type=str(order_info.get("ot") or order_info.get("o") or "LIMIT"),
                    client_order_id=client_order_id,
                    status=status,
                    timestamp=utc_now()
                )
                new_order.exchange_order_id = str(order_info.get("i"))
                await self.ledger.upsert_order(new_order)
                
            exec_type = order_info.get("x")
            if exec_type == "TRADE":
                fill = ExchangeFill(
                    exchange_trade_id=str(order_info.get("t")),
                    exchange_order_id=str(order_info.get("i")),
                    client_order_id=client_order_id,
                    symbol=symbol,
                    side=OrderSide(order_info.get("S")),
                    position_side=PositionSide(order_info.get("ps")),
                    quantity=Decimal(str(order_info.get("l"))),
                    price=Decimal(str(order_info.get("L"))),
                    commission=Decimal(str(order_info.get("n", "0"))),
                    commission_asset=order_info.get("N"),
                    realized_pnl=Decimal(str(order_info.get("rp", "0"))),
                    maker=order_info.get("m", False),
                    event_time=event.get("E", 0),
                    transaction_time=order_info.get("T", 0),
                    source="BINANCE_TESTNET" if self.env == BinanceEnvironment.TESTNET else "BINANCE_MAINNET"
                )
                await self.ledger.append_fill(fill)
        elif event_type == "ACCOUNT_UPDATE":
            logger.info("WS Account Update: %s", event)
            update_data = event.get("a", {})
            positions = update_data.get("P", [])
            for p in positions:
                await self.ledger.upsert_position({
                    "symbol": p.get("s"),
                    "positionSide": p.get("ps"),
                    "positionAmt": p.get("pa"),
                    "entryPrice": p.get("ep"),
                    "unRealizedProfit": p.get("up"),
                    "marginType": p.get("mt", "cross")
                })

    def _generate_client_order_id(self, context_id: str, symbol: str, order_index: int = 0, attempt: int = 1) -> str:
        """
        Deterministic Client Order ID.
        Format: BAI-<context_hash>-<order_index>-<attempt>
        """
        raw_str = f"{context_id}-{symbol}"
        hash_str = hashlib.md5(raw_str.encode()).hexdigest()[:8]
        return f"BAI-{hash_str}-{order_index}-{attempt}"

    async def execute_decision(self, decision: ExecutionDecision) -> List[ExecutionOrder]:
        if self.state != ConnectionState.READY:
            logger.warning("Cannot execute decision. State is %s", self.state)
            return []

        if decision.action == "NOOP":
            return []

        executed_orders = []
        for i, order_intent in enumerate(decision.orders):
            symbol = decision.symbol
            rules = self.capabilities.symbol_rules.get(symbol)
            if not rules:
                logger.error("No rules for %s. Skipping.", symbol)
                continue
                
            rounded_qty = rules.normalize_quantity(order_intent.quantity)
            
            if rounded_qty <= 0:
                continue
                
            price_str = None
            if order_intent.limit_price:
                rounded_price = rules.normalize_price(order_intent.limit_price)
                price_str = str(rounded_price)

            # Notional check against min_notional
            est_price = rounded_price if price_str else (rules.min_price or Decimal("1"))
            if rules.min_notional and (rounded_qty * est_price) < rules.min_notional:
                logger.error(
                    "Order notional %s below min_notional %s for %s. Skipping.",
                    rounded_qty * est_price, rules.min_notional, symbol
                )
                continue
                
            client_oid = self._generate_client_order_id(str(decision.decision_id), symbol, order_index=i, attempt=1)
            
            params = {
                "symbol": symbol,
                "side": order_intent.side.name,
                "type": "LIMIT" if price_str else "MARKET",
                "quantity": str(rounded_qty),
                "newClientOrderId": client_oid,
            }
            if price_str:
                params["price"] = price_str
                params["timeInForce"] = "GTC"
                
            if self.capabilities.hedge_mode:
                params["positionSide"] = order_intent.position_side.name
                if order_intent.reduce_only:
                    # In hedge mode, reduceOnly cannot be sent per Binance futures documentation
                    pass 
            else:
                if order_intent.reduce_only:
                    params["reduceOnly"] = "true"
            
            logger.info("[%s] Submitting order: %s", self.env, params)
            try:
                resp = await self.rest_client.request("POST", "/fapi/v1/order", signed=True, params=params)
                logger.info("Order success: %s", resp)
                
                # Update ledger
                executed_order = ExecutionOrder(
                    symbol=symbol,
                    side=order_intent.side,
                    quantity=rounded_qty,
                    price=Decimal(str(resp.get("price", "0"))),
                    order_type=params["type"],
                    client_order_id=client_oid,
                    status=resp.get("status", "NEW"),
                    exchange_order_id=str(resp.get("orderId", "")),
                    timestamp=utc_now()
                )
                await self.ledger.upsert_order(executed_order)
                executed_orders.append(executed_order)
            except BinanceAPIError as api_err:
                # Definitive rejection from exchange (invalid params, min notional, margin, etc.)
                logger.error("Order %s definitively rejected by Binance API (code %s): %s", client_oid, api_err.code, api_err)
                executed_order = ExecutionOrder(
                    symbol=symbol,
                    side=order_intent.side,
                    quantity=rounded_qty,
                    price=Decimal(price_str or "0"),
                    order_type=params["type"],
                    client_order_id=client_oid,
                    status="REJECTED",
                    timestamp=utc_now()
                )
                await self.ledger.upsert_order(executed_order)
                executed_orders.append(executed_order)
            except Exception as e:
                logger.error("Order execution failed, ambiguity triggered: %s", e)
                # Implement timeout ambiguity handling here: QUERY order state.
                self.state = ConnectionState.RECONCILING
                try:
                    logger.info("Querying ambiguous order by client ID: %s", client_oid)
                    query_params = {"symbol": symbol, "origClientOrderId": client_oid}
                    status_resp = await self.rest_client.request("GET", "/fapi/v1/order", signed=True, params=query_params)
                    logger.info("Ambiguous order found on exchange: %s", status_resp)
                    executed_order = ExecutionOrder(
                        symbol=symbol,
                        side=order_intent.side,
                        quantity=rounded_qty,
                        price=Decimal(str(status_resp.get("price", "0"))),
                        order_type=params["type"],
                        client_order_id=client_oid,
                        status=status_resp.get("status", "NEW"),
                        exchange_order_id=str(status_resp.get("orderId", "")),
                        timestamp=utc_now()
                    )
                    await self.ledger.upsert_order(executed_order)
                    executed_orders.append(executed_order)
                    self.state = ConnectionState.READY # Recovered
                except Exception as query_err:
                    err_msg = str(query_err)
                    if "Order does not exist" in err_msg or "-2013" in err_msg:
                        logger.warning("Order %s confirmed absent. It was not placed.", client_oid)
                        self.state = ConnectionState.READY # Safe to resume
                    else:
                        logger.error("Order %s status STILL UNKNOWN. Blocking new risk. Error: %s", client_oid, query_err)
                        self.state = ConnectionState.DEGRADED
                
        return executed_orders


    async def cancel_order(self, symbol: str, orig_client_order_id: str) -> bool:
        if self.state != ConnectionState.READY:
            return False
        try:
            params = {
                "symbol": symbol,
                "origClientOrderId": orig_client_order_id
            }
            logger.info("[%s] Canceling order: %s", self.env, params)
            resp = await self.rest_client.request("DELETE", "/fapi/v1/order", signed=True, params=params)
            logger.info("Cancel success: %s", resp)
            return True
        except Exception as e:
            logger.error("Failed to cancel order: %s", e)
            return False

    async def modify_order(self, symbol: str, orig_client_order_id: str, new_price: Decimal, new_qty: Decimal, side: str) -> Optional[ExecutionOrder]:
        if self.state != ConnectionState.READY:
            return None
        rules = self.capabilities.symbol_rules.get(symbol)
        if not rules:
            return None
            
        rounded_qty = rules.normalize_quantity(new_qty)
        rounded_price = rules.normalize_price(new_price)
        
        try:
            params = {
                "symbol": symbol,
                "origClientOrderId": orig_client_order_id,
                "side": side,
                "quantity": str(rounded_qty),
                "price": str(rounded_price)
            }
            logger.info("[%s] Modifying order (PUT): %s", self.env, params)
            resp = await self.rest_client.request("PUT", "/fapi/v1/order", signed=True, params=params)
            logger.info("Modify success: %s", resp)
            
            new_order = ExecutionOrder(
                symbol=symbol,
                side=OrderSide(side) if isinstance(side, str) else side,
                quantity=rounded_qty,
                price=rounded_price,
                order_type="LIMIT",
                client_order_id=resp.get("clientOrderId", orig_client_order_id),
                status=resp.get("status", "NEW"),
                exchange_order_id=str(resp.get("orderId", "")),
                timestamp=utc_now()
            )
            await self.ledger.upsert_order(new_order)
            return new_order

        except Exception as e:
            logger.error("Failed to modify order: %s", e)
            return None

    async def close(self):
        await self.user_stream.close()
        await self.rest_client.close()
        self.state = ConnectionState.DISCONNECTED
