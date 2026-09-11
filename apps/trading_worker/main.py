import asyncio
import logging
import signal
import sys
from typing import List

from domain.models import MarketEvent, MarketType
from apps.trading_worker.engines.price_action import PriceActionEngine
from apps.trading_worker.engines.market_state import MarketStateClassifier
from apps.trading_worker.engines.grid_strategy import GridStrategyEngine
from apps.trading_worker.engines.trend_strategy import TrendStrategyEngine
from apps.trading_worker.engines.shock_strategy import ShockStrategyEngine
from apps.trading_worker.engines.exposure_recovery import ExposureRecoveryEngine
from apps.trading_worker.engines.funding_carry import FundingCarryEngine
from apps.trading_worker.engines.market_scanner import MarketScannerEngine
from apps.trading_worker.engines.meta_allocator import MetaAllocator
from apps.trading_worker.engines.risk_governor import RiskGovernor
from domain.models import RiskSnapshot, utc_now
from domain.enums import RiskState
from decimal import Decimal

# Optional: NATS integration when distributed
# from infrastructure.nats_client import NatsBus
# For this monolith MVP, we can subscribe directly to Binance WebSocket if desired
from venues.binance.public_ws import BinancePublicWebSocket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("blessing.worker")

class TradingWorkerApp:
    def __init__(self, symbols: List[str] = ["BTCUSDT", "ETHUSDT"]):
        self.symbols = symbols
        self.is_running = True
        
        # Initialize Engines
        self.pa_engine = PriceActionEngine()
        self.market_state_engine = MarketStateClassifier()
        self.grid_engine = GridStrategyEngine()
        self.trend_engine = TrendStrategyEngine()
        self.shock_engine = ShockStrategyEngine()
        self.carry_engine = FundingCarryEngine()
        self.recovery_engine = ExposureRecoveryEngine()
        self.meta_allocator = MetaAllocator()
        self.risk_governor = RiskGovernor()
        self.scanner = MarketScannerEngine(top_n=8)
        self.scan_task = None
        
        self.ws_client = None

    async def handle_market_event(self, event: MarketEvent):
        """
        The Core Execution Loop for Market Data
        """
        # 1. Update Price Action & Microstructure
        pa_state = self.pa_engine.process_event(event)
        if not pa_state:
            return  # Need more data

        # 2. Classify Market Regime
        market_state = self.market_state_engine.classify(pa_state)

        # 3. Generate Strategy Intents
        grid_intent = self.grid_engine.evaluate(pa_state, market_state)
        trend_intent = self.trend_engine.evaluate(pa_state, market_state)
        shock_intent = self.shock_engine.evaluate(pa_state, market_state)
        carry_intent = self.carry_engine.evaluate(event, market_state)
        
        intents = [i for i in [grid_intent, trend_intent, shock_intent, carry_intent] if i]
        
        for intent in intents:
            logger.info("Generated Intent: %s | Score: %s | Delta: %s | Regime: %s", 
                        intent.strategy_id, 
                        intent.opportunity_score, 
                        intent.desired_delta_qty,
                        market_state.primary_regime.name)

        # 4. Meta Allocation
                # In a real environment, RiskSnapshot is maintained continuously by an account sync task
        mock_risk = RiskSnapshot(
            portfolio_equity=Decimal("100000.0"),
            unrealized_pnl=Decimal("0.0"),
            realized_pnl_24h=Decimal("0.0"),
            margin_utilization_pct=Decimal("5.0"),
            effective_leverage=Decimal("0.5"),
            current_drawdown_pct=Decimal("1.2"),
            liquidation_distance_pct=Decimal("45.0"),
            risk_state=RiskState.NORMAL
        )

        raw_target_exposure = self.meta_allocator.allocate(intents, event.symbol)
        
        # 4b. Exposure Recovery & Grid Brake (Phase 3)
        current_position_qty = Decimal("1.2") # Mock existing long position inventory
        
        target_exposure = self.recovery_engine.process(
            target=raw_target_exposure,
            risk=mock_risk,
            current_position_qty=current_position_qty
        )
        

        
        decision = self.risk_governor.evaluate(target_exposure, mock_risk, current_position_qty=Decimal("0.0"))
        
        if decision.action != "NOOP":
            logger.info("EXECUTION DECISION: %s | Action: %s | Qty: %s", 
                        decision.symbol, decision.action, decision.orders[0].quantity if decision.orders else 0)

    async def start(self):
        logger.info("Initializing Blessing AI Trading Worker v0.2 (Paper/Live Mode)...")
        
        # Phase 5: Initial Market Scan
        self.symbols = await self.scanner.scan_active_symbols()
        
        logger.info("Connecting to Binance WS for: %s", self.symbols)
        self.ws_client = BinancePublicWebSocket(
            symbols=self.symbols,
            event_callback=self.handle_market_event,
        )
        
        # Phase 5: Background Scanner Task (Updates WS subscriptions if symbols change)
        self.scan_task = asyncio.create_task(self._periodic_scanner())
        
        logger.info("Worker Event Loop Running. Press Ctrl+C to terminate.")
        while self.is_running:
            await asyncio.sleep(1)

    async def _periodic_scanner(self):
        """Periodically scans market and updates WebSocket subscription if active pairs change."""
        while self.is_running:
            await asyncio.sleep(self.scanner.refresh_interval_sec)
            try:
                new_symbols = await self.scanner.scan_active_symbols()
                if set(new_symbols) != set(self.symbols):
                    logger.info("Market regime shifted. Active pairs updated: %s", new_symbols)
                    self.symbols = new_symbols
                    if self.ws_client:
                        # In production, call ws_client.subscribe(new_symbols) to dynamically update
                        pass
            except Exception as e:
                logger.error("Periodic scanner failed: %s", e)

    def stop(self):
        logger.info("Gracefully stopping Trading Worker...")
        if self.ws_client:
            # Note: WS client stops gracefully if possible
            pass
        self.is_running = False

if __name__ == "__main__":
    app = TradingWorkerApp()
    try:
        asyncio.run(app.start())
    except KeyboardInterrupt:
        app.stop()
        sys.exit(0)
