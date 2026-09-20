"""
Market Data Collector Service for Blessing AI v0.2
Phase 0 Public WebSocket Ingestion for BTCUSDT & ETHUSDT.
Publishes normalized MarketEvent instances to NATS JetStream.
"""

import asyncio
import logging
import sys
from typing import List, Optional

from domain.events import DomainEvent, EventSubjectBuilder
from domain.models import MarketEvent
from infrastructure.nats_client import NatsBus
from venues.binance.capabilities import BinanceCapabilityDiscovery
from apps.trading_worker.venues.binance.public_ws import BinancePublicWebSocket

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("blessing.collector")


class MarketDataCollectorApp:
    def __init__(self, symbols: Optional[List[str]] = None, nats_url: str = "nats://127.0.0.1:4222"):
        self.symbols = symbols if symbols is not None else ["BTCUSDT", "ETHUSDT"]
        self.nats_url = nats_url
        self.bus = NatsBus(nats_url)
        self.discovery = BinanceCapabilityDiscovery()
        self.ws_client: Optional[BinancePublicWebSocket] = None
        self.is_running = True

    async def handle_market_event(self, event: MarketEvent):
        subject = EventSubjectBuilder.market_trades("binance", "usdm", event.symbol)
        domain_event = DomainEvent(
            event_type="market.ticker",
            payload=event.model_dump() if hasattr(event, "model_dump") else event.__dict__,
            source="binance_public_ws",
            symbol=event.symbol,
            event_time=event.event_time,
        )
        await self.bus.publish_event(subject, domain_event)

    async def start(self):
        logger.info("Initializing Blessing AI Market Data Collector (Phase 0)...")
        await self.bus.connect()

        # Probe symbol capabilities
        for sym in self.symbols:
            try:
                inst = await self.discovery.fetch_futures_symbol_capabilities(sym)
                logger.info("Verified %s: tick=%s, step=%s", sym, inst.tick_size, inst.step_size)
            except Exception as err:
                logger.warning(
                    "Could not verify live capabilities for %s; continuing without exchange rules: %s",
                    sym,
                    err,
                )

        logger.info("Starting public WebSocket ingestion for: %s", self.symbols)
        self.ws_client = BinancePublicWebSocket(
            symbols=self.symbols,
            event_callback=self.handle_market_event,
        )

        logger.info("Collector service online. Press Ctrl+C to terminate.")
        while self.is_running:
            await asyncio.sleep(1)

    def stop(self):
        logger.info("Stopping Market Data Collector...")
        self.is_running = False


if __name__ == "__main__":
    app = MarketDataCollectorApp()
    try:
        asyncio.run(app.start())
    except KeyboardInterrupt:
        app.stop()
        sys.exit(0)
