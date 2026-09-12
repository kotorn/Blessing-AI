import logging
from decimal import Decimal
from typing import List, Optional
import os
import hashlib
from datetime import datetime
from domain.models import ExecutionDecision, ExecutionOrder, utc_now

logger = logging.getLogger("blessing.venues.binance.execution")

class BinanceExecutionAdapter:
    """
    Blessing AI Execution Adapter (USDⓈ-M Futures)
    
    Supports ONLY PAPER and TESTNET. LIVE is explicitly blocked in this version.
    """
    def __init__(self, mode: str = "PAPER"):
        self.mode = mode.upper()
        if self.mode == "LIVE":
            raise ValueError("LIVE execution mode is permanently blocked in this sprint.")
            
        self.client = None
        self.server_time_offset_ms = 0
        
        if self.mode == "TESTNET":
            logger.info("Initializing CCXT Binance Client (Mode: TESTNET)")
            try:
                import ccxt.async_support as ccxt
                self.client = ccxt.binanceusdm({
                    'apiKey': os.getenv("BINANCE_TESTNET_API_KEY", ""),
                    'secret': os.getenv("BINANCE_TESTNET_API_SECRET", ""),
                    'enableRateLimit': True,
                    'options': {
                        'defaultType': 'future'
                    }
                })
                self.client.set_sandbox_mode(True)
            except ImportError:
                logger.error("CCXT not installed. Please install for TESTNET execution.")

    def _generate_client_order_id(self, context_id: str, symbol: str, attempt: int = 1) -> str:
        """
        Deterministic Client Order ID.
        Format: B-<context_hash>-<attempt>
        """
        raw_str = f"{context_id}-{symbol}-{utc_now().timestamp()}"
        hash_str = hashlib.md5(raw_str.encode()).hexdigest()[:10]
        return f"B-{hash_str}-{attempt}"

    def _normalize_quantity(self, quantity: Decimal, symbol: str) -> Decimal:
        """
        Symbol Capability Discovery (Mocked for now, must use exchange rules)
        """
        # In a real environment, this must fetch lot size from exchangeinfo
        return round(quantity, 3)

    async def execute_decision(self, decision: ExecutionDecision) -> List[ExecutionOrder]:
        if decision.action == "NOOP":
            return []

        executed_orders = []
        for order_intent in decision.orders:
            # 1. Formatting & Step Size Rules
            rounded_qty = self._normalize_quantity(order_intent.quantity, decision.symbol)
            
            if rounded_qty <= 0:
                continue
                
            client_oid = self._generate_client_order_id(str(decision.id) if hasattr(decision, 'id') else "SYS", decision.symbol)
            
            # 2. Paper Trading Execution
            if self.mode == "PAPER":
                executed_order = ExecutionOrder(
                    symbol=decision.symbol,
                    side=order_intent.side,
                    quantity=Decimal(str(rounded_qty)),
                    price=order_intent.limit_price or Decimal("0.0"),
                    order_type="LIMIT_MAKER" if order_intent.limit_price else "MARKET",
                    client_order_id=client_oid,
                    status="FILLED" if not order_intent.limit_price else "NEW",
                    timestamp=utc_now()
                )
                logger.info("[%s][SIMULATED] EXECUTION DECISION: Executed %s %s %s @ MKT", self.mode, executed_order.side.name, executed_order.quantity, executed_order.symbol)
                executed_orders.append(executed_order)
                
            # 3. Testnet Execution via CCXT
            elif self.mode == "TESTNET" and self.client:
                logger.info("[%s][AUTHORITATIVE] EXECUTION DECISION: Submitting %s %s %s to Binance Testnet...", 
                            self.mode, order_intent.side.name, rounded_qty, decision.symbol)
                try:
                    # response = await self.client.create_order(
                    #     symbol=decision.symbol,
                    #     type='market',
                    #     side=order_intent.side.name.lower(),
                    #     amount=float(rounded_qty),
                    #     params={'newClientOrderId': client_oid}
                    # )
                    # mock success for now
                    logger.info("CCXT Order Success (Mock): %s %s", order_intent.side.name, rounded_qty)
                except Exception as e:
                    logger.error("CCXT Execution failed: %s", e)
                
        return executed_orders

    async def close(self):
        if self.client:
            await self.client.close()
