import asyncio
import os
import logging
from decimal import Decimal
from typing import Optional

from .config import BinanceEnvironment
from .execution import BinanceExecutionAdapter
from domain.models import ExecutionDecision, OrderIntent, OrderSide, PositionSide, MarketType, utc_now

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("manual_testnet")

async def manual_testnet_workflow():
    logger.info("Initializing Manual Testnet Execution Primitive")
    api_key = os.getenv("BINANCE_TESTNET_API_KEY")
    api_secret = os.getenv("BINANCE_TESTNET_API_SECRET")
    
    if not api_key or not api_secret:
        logger.error("Missing BINANCE_TESTNET_API_KEY or BINANCE_TESTNET_API_SECRET")
        return
        
    adapter = BinanceExecutionAdapter(api_key, api_secret, BinanceEnvironment.TESTNET)
    success = await adapter.connect()
    
    if not success:
        logger.error("Failed to connect / discover capabilities / reconcile.")
        return
        
    # Check Safety Ceilings
    symbol = "BTCUSDT"
    if symbol not in ["BTCUSDT", "ETHUSDT"]:
        logger.error("Safety ceiling violation: symbol %s not allowed", symbol)
        return
        
    # Tiny LIMIT order
    price = Decimal("30000.0")
    qty = Decimal("0.002")
    
    intent = OrderIntent(
        client_order_id="MANUAL-TEST-1",
        symbol=symbol,
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.LONG,
        order_type="LIMIT",
        time_in_force="GTC",
        quantity=qty,
        price=price
    )
    
    decision = ExecutionDecision(
        decision_id="MANUAL_TRIAL_1",
        symbol=symbol,
        action="SUBMIT_ORDER",
        orders=[intent],
        net_exposure_delta=qty
    )
    
    logger.info("Submitting tiny Testnet order: %s", intent)
    # UNCOMMENT TO ACTUALLY SUBMIT:
    # executed = await adapter.execute_decision(decision)
    # logger.info("Order returned: %s", executed)
    
    await adapter.close()

if __name__ == "__main__":
    asyncio.run(manual_testnet_workflow())
