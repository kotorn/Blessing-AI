"""
Binance USDⓈ-M Futures Public WebSocket Stream Client
Streams real-time mark price, book ticker, and depth updates into normalized MarketEvents.
"""

import asyncio
import json
import logging
import random
from decimal import Decimal
from typing import Callable, Coroutine, Any, List, Optional
from datetime import datetime, timezone

try:
    import websockets
except ImportError:
    websockets = None

from domain.models import MarketEvent, MarketType

logger = logging.getLogger("blessing.binance.ws")


class BinancePublicWebSocket:
    def __init__(
        self,
        symbols: List[str],
        base_ws_url: str = "wss://fstream.binance.com/ws",
        event_callback: Optional[Callable[[MarketEvent], Coroutine[Any, Any, None]]] = None,
    ):
        self.symbols = [s.lower() for s in symbols]
        self.base_ws_url = base_ws_url
        self.event_callback = event_callback
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self.is_connected = False
        self._backoff_steps = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

    async def start(self) -> bool:
        """Start bounded public-market stream supervision without blocking the caller."""
        if websockets is None:
            logger.error("websockets package is not installed; market stream is unavailable")
            return False
        if self.is_running and self._task and not self._task.done():
            return True
        self.is_running = True
        self._task = asyncio.create_task(self._run())
        return True

    async def _run(self) -> None:
        attempt = 0
        while self.is_running:
            try:
                url = self.construct_combined_stream_url()
                async with websockets.connect(url) as websocket:
                    self.is_connected = True
                    attempt = 0
                    logger.info("Public market stream connected: %s", self.base_ws_url)
                    async for raw_message in websocket:
                        event = self.parse_payload(raw_message)
                        if event is not None and self.event_callback is not None:
                            await self.event_callback(event)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("Public market stream disconnected: %s", exc)
            finally:
                self.is_connected = False

            if self.is_running:
                delay = self._backoff_steps[min(attempt, len(self._backoff_steps) - 1)]
                attempt += 1
                await asyncio.sleep(delay + random.uniform(0.1, 0.5))

    async def stop(self) -> None:
        self.is_running = False
        self.is_connected = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def construct_combined_stream_url(self) -> str:
        # Example streams: btcusdt@bookTicker / ethusdt@bookTicker / btcusdt@markPrice@1s
        streams = []
        for s in self.symbols:
            streams.append(f"{s}@bookTicker")
            streams.append(f"{s}@markPrice@1s")
        stream_base = self.base_ws_url
        if stream_base.endswith("/ws"):
            stream_base = f"{stream_base[:-3]}/stream"
        return f"{stream_base}?streams={'/'.join(streams)}"

    def parse_payload(self, raw_msg: str) -> Optional[MarketEvent]:
        try:
            data = json.loads(raw_msg)
            stream = data.get("stream", "")
            payload = data.get("data", data)
            if not isinstance(payload, dict):
                return None
            event_type = payload.get("e")

            if "bookTicker" in stream or event_type == "bookTicker":
                sym = payload.get("s", "").upper()
                return MarketEvent(
                    event_id=f"ws_{payload.get('u', 0)}",
                    event_time=datetime.fromtimestamp(payload.get("E", 0) / 1000, tz=timezone.utc),
                    symbol=sym,
                    venue="BINANCE_TESTNET",
                    market_type=MarketType.USDM_FUTURES,
                    last_price=Decimal(str(payload.get("b", "0"))),
                    best_bid=Decimal(str(payload.get("b", "0"))),
                    best_ask=Decimal(str(payload.get("a", "0"))),
                )
            elif "markPrice" in stream or event_type == "markPriceUpdate":
                sym = payload.get("s", "").upper()
                return MarketEvent(
                    event_id=f"mp_{payload.get('E', 0)}",
                    event_time=datetime.fromtimestamp(payload.get("E", 0) / 1000, tz=timezone.utc),
                    symbol=sym,
                    venue="BINANCE_TESTNET",
                    market_type=MarketType.USDM_FUTURES,
                    last_price=Decimal(str(payload.get("p", "0"))),
                    best_bid=Decimal(str(payload.get("p", "0"))),
                    best_ask=Decimal(str(payload.get("p", "0"))),
                    mark_price=Decimal(str(payload.get("p", "0"))),
                    index_price=Decimal(str(payload.get("i", "0"))),
                    funding_rate=Decimal(str(payload.get("r", "0"))),
                )
        except Exception as err:
            logger.warning("Failed to parse Binance WS frame: %s", err)
        return None
