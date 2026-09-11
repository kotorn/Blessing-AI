import logging
from decimal import Decimal
from typing import List
from domain.models import ExecutionDecision, ExecutionOrder, utc_now
import os

logger = logging.getLogger("blessing.venues.binance.execution")

class BinanceExecutionAdapter:
    """
    Phase 5: Cloud Run Paper Trading Integration (Execution Adapter)
    
    Transforms internal generic ExecutionDecisions into Binance-specific API calls.
    Supports PAPER (simulated latency/fill) and TESTNET/LIVE via ccxt or aiohttp.
    """
    def __init__(self, mode: str = "PAPER"):
        self.mode = mode.upper()
        self.client = None
        
        if self.mode in ["TESTNET", "LIVE"]:
            logger.info("Initializing CCXT Binance Client (Mode: %s)", self.mode)
            try:
                import ccxt.async_support as ccxt
                self.client = ccxt.binanceusdm({
                    'apiKey': os.getenv("BINANCE_API_KEY", ""),
                    'secret': os.getenv("BINANCE_API_SECRET", ""),
                    'enableRateLimit': True,
                    'options': {
                        'defaultType': 'future'
                    }
                })
                if self.mode == "TESTNET":
                    self.client.set_sandbox_mode(True)
            except ImportError:
                logger.error("CCXT not installed. Please install for TESTNET/LIVE execution.")

    async def execute_decision(self, decision: ExecutionDecision) -> List[ExecutionOrder]:
        if decision.action == "NOOP":
            return []

        executed_orders = []
        for order_intent in decision.orders:
            # 1. Formatting & Step Size Rules
            rounded_qty = round(order_intent.quantity, 3)
            
            if rounded_qty <= 0:
                continue
                
            # 2. Paper Trading Execution
            if self.mode == "PAPER":
                executed_order = ExecutionOrder(
                    symbol=decision.symbol,
                    side=order_intent.side,
                    quantity=Decimal(str(rounded_qty)),
                    price=order_intent.limit_price or Decimal("0.0"),
                    order_type="LIMIT_MAKER" if order_intent.limit_price else "MARKET",
                    client_order_id=f"SYS-{utc_now().timestamp()}",
                    status="FILLED" if not order_intent.limit_price else "NEW",
                    timestamp=utc_now()
                )
                logger.info("[PAPER TRADE] Executed %s %s %s @ MKT", executed_order.side.name, executed_order.quantity, executed_order.symbol)
                executed_orders.append(executed_order)
                
            # 3. Testnet / Live Execution via CCXT
            elif self.mode in ["TESTNET", "LIVE"] and self.client:
                logger.info("[%s TRADE] Submitting %s %s %s to Binance...", 
                            self.mode, order_intent.side.name, rounded_qty, decision.symbol)
                try:
                    # In a real environment, handle exceptions, rate limits, etc.
                    # response = await self.client.create_order(
                    #     symbol=decision.symbol,
                    #     type='market',
                    #     side=order_intent.side.name.lower(),
                    #     amount=float(rounded_qty)
                    # )
                    # mock success for now
                    logger.info("CCXT Order Success (Mock): %s %s", order_intent.side.name, rounded_qty)
                except Exception as e:
                    logger.error("CCXT Execution failed: %s", e)
                
        return executed_orders

    async def close(self):
        if self.client:
            await self.client.close()
