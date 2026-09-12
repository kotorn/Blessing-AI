import logging
import hashlib
from decimal import Decimal
from typing import List, Optional, Any

from domain.models import ExecutionDecision, ExecutionOrder, OrderSide, utc_now
from .config import BinanceEnvironment
from .rest_client import BinanceRestClient
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
        
        self.rest_client = BinanceRestClient(api_key, api_secret, env)
        self.capabilities = BinanceCapabilities()
        self.user_stream = BinanceUserStream(self.rest_client, env)
        self.reconciliation = BinanceReconciliation(self.rest_client)
        self.ledger = ledger or InMemoryLedger()
        
        self.state = ConnectionState.DISCONNECTED

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

    async def _on_ws_event(self, event: Any):
        event_type = event.get("e")
        if event_type == "ORDER_TRADE_UPDATE":
            logger.info("WS Order Update: %s", event)
            # Parse fill and update ledger
        elif event_type == "ACCOUNT_UPDATE":
            logger.info("WS Account Update: %s", event)

    def _generate_client_order_id(self, context_id: str, symbol: str, attempt: int = 1) -> str:
        """
        Deterministic Client Order ID.
        Format: BAI-<context_hash>-<attempt>
        """
        raw_str = f"{context_id}-{symbol}"
        hash_str = hashlib.md5(raw_str.encode()).hexdigest()[:10]
        return f"BAI-{hash_str}-{attempt}"

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
                
            client_oid = self._generate_client_order_id(str(decision.decision_id), symbol, attempt=1)
            
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
                    # In hedge mode, reduceOnly must not be sent if it conflicts or is handled implicitly by side.
                    # Actually Binance docs say: "reduceOnly cannot be sent in Hedge Mode".
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
                    price=Decimal(resp.get("price", "0")),
                    order_type=params["type"],
                    client_order_id=client_oid,
                    status=resp.get("status", "NEW"),
                    timestamp=utc_now()
                )
                await self.ledger.upsert_order(executed_order)
                executed_orders.append(executed_order)
            except Exception as e:
                logger.error("Order execution failed: %s", e)
                # Implement timeout ambiguity handling here: QUERY order state.
                
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
            return None # Should return new ExecutionOrder in full implementation
        except Exception as e:
            logger.error("Failed to modify order: %s", e)
            return None

    async def close(self):
        await self.user_stream.close()
        await self.rest_client.close()
        self.state = ConnectionState.DISCONNECTED
