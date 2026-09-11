"""
Phase 0 Market Data Collector
Connects to Binance Public REST & WebSocket feeds for BTCUSDT and ETHUSDT.
Ingests Trades, Orderbook, Mark Price, Funding Rate, and Open Interest.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, List
import aiohttp
import structlog

logger = structlog.get_logger()


class BinanceMarketCollector:
    """
    Phase 0 Foundation Collector.
    Collects high-frequency tick data and periodic funding/OI snapshots without requiring API keys.
    """

    def __init__(
        self,
        symbols: List[str] = ["BTCUSDT", "ETHUSDT"],
        ws_endpoint: str = "wss://fstream.binance.com/ws",
        rest_endpoint: str = "https://fapi.binance.com",
    ):
        self.symbols = [s.lower() for s in symbols]
        self.ws_endpoint = ws_endpoint
        self.rest_endpoint = rest_endpoint
        self._running = False
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self._running = True
        self._session = aiohttp.ClientSession()
        logger.info("market_collector_starting", symbols=self.symbols)

        # Build combined stream payload
        # Streams: <symbol>@trade, <symbol>@depth5@100ms, <symbol>@markPrice@1s
        streams = []
        for s in self.symbols:
            streams.append(f"{s}@trade")
            streams.append(f"{s}@depth5@100ms")
            streams.append(f"{s}@markPrice@1s")

        combined_url = f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"

        # Run background loops
        ws_task = asyncio.create_task(self._run_websocket(combined_url))
        oi_task = asyncio.create_task(self._poll_open_interest())

        await asyncio.gather(ws_task, oi_task)

    async def stop(self) -> None:
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()
        logger.info("market_collector_stopped")

    async def _run_websocket(self, url: str) -> None:
        while self._running:
            try:
                assert self._session is not None
                async with self._session.ws_connect(url, heartbeat=20.0) as ws:
                    logger.info("binance_ws_connected", url=url)
                    async for msg in ws:
                        if not self._running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            self._handle_stream_message(payload)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warn("binance_ws_disconnected", msg_type=msg.type)
                            break
            except Exception as e:
                logger.error("binance_ws_reconnect_error", error=str(e))
                await asyncio.sleep(3.0)

    def _handle_stream_message(self, data: Dict[str, Any]) -> None:
        stream_name = data.get("stream", "")
        payload = data.get("data", {})

        if "@trade" in stream_name:
            # Public trade event
            # Event format: s=symbol, p=price, q=qty, T=timestamp, m=isBuyerMaker
            pass
        elif "@markPrice" in stream_name:
            # Mark price & funding rate event
            # p=markPrice, i=indexPrice, r=fundingRate, T=nextFundingTime
            pass
        elif "@depth" in stream_name:
            # Order book snapshot
            pass

    async def _poll_open_interest(self) -> None:
        while self._running:
            try:
                assert self._session is not None
                for symbol in self.symbols:
                    url = f"{self.rest_endpoint}/fapi/v1/openInterest?symbol={symbol.upper()}"
                    async with self._session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            logger.debug("open_interest_polled", symbol=symbol, oi=data.get("openInterest"))
                await asyncio.sleep(60.0)  # Poll OI every minute
            except Exception as e:
                logger.error("oi_poll_error", error=str(e))
                await asyncio.sleep(10.0)


if __name__ == "__main__":
    collector = BinanceMarketCollector()
    try:
        asyncio.run(collector.start())
    except KeyboardInterrupt:
        asyncio.run(collector.stop())
