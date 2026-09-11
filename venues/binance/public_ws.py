"""
Binance USDⓈ-M Futures Public WebSocket Stream Client
Streams real-time mark price, book ticker, and depth updates into normalized MarketEvents.
"""

import asyncio
import json
import logging
from decimal import Decimal
from typing import Callable, Coroutine, Any, List
from datetime import datetime, timezone

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

    def construct_combined_stream_url(self) -> str:
        # Example streams: btcusdt@bookTicker / ethusdt@bookTicker / btcusdt@markPrice@1s
        streams = []
        for s in self.symbols:
            streams.append(f"{s}@bookTicker")
            streams.append(f"{s}@markPrice@1s")
        return f"wss://fstream.binance.com/stream?streams={'/'.join(streams)}"

    def parse_payload(self, raw_msg: str) -> Optional[MarketEvent]:
        try:
            data = json.loads(raw_msg)
            stream = data.get("stream", "")
            payload = data.get("data", data)
            event_type = payload.get("e")

            if "bookTicker" in stream or event_type == "bookTicker":
                sym = payload.get("s", "").upper()
                return MarketEvent(
                    event_id=f"ws_{payload.get('u', 0)}",
                    event_time=datetime.fromtimestamp(payload.get("E", 0) / 1000, tz=timezone.utc),
                    symbol=sym,
                    venue="binance_global",
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
                    venue="binance_global",
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
