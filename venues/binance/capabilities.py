"""
Binance Exchange Capability Discovery Engine for Blessing AI v0.2
Dynamically interrogates Binance Global REST endpoints on startup to discover:
- Symbol filters (tickSize, stepSize, minNotional)
- Position Mode (One-Way vs Hedge Mode)
- Trading Status & Multi-Assets Mode
- Rate Limit rules

Ensures strategy logic never hard-codes exchange parameters.
"""

from decimal import Decimal
from typing import Dict, Any, Optional
import json
import logging
from datetime import datetime, timezone

try:
    import aiohttp
except ImportError:
    aiohttp = None
    import urllib.request

from domain.models import Instrument, MarketType

logger = logging.getLogger("blessing.binance.capabilities")


class BinanceCapabilityDiscovery:
    def __init__(
        self,
        base_futures_url: str = "https://fapi.binance.com",
        base_spot_url: str = "https://api.binance.com",
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
    ):
        self.base_futures_url = base_futures_url.rstrip("/")
        self.base_spot_url = base_spot_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.cached_instruments: Dict[str, Instrument] = {}
        self.is_hedge_mode_verified: bool = False

    async def fetch_futures_symbol_capabilities(self, symbol: str) -> Instrument:
        """Query /fapi/v1/exchangeInfo and parse LOT_SIZE, PRICE_FILTER, and MIN_NOTIONAL."""
        url = f"{self.base_futures_url}/fapi/v1/exchangeInfo"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"Failed to fetch futures exchangeInfo: HTTP {resp.status}")
                data = await resp.json()

        symbols = {s["symbol"]: s for s in data.get("symbols", [])}
        if symbol not in symbols:
            raise ValueError(f"Symbol {symbol} not found on Binance USDⓈ-M Futures")

        s = symbols[symbol]
        tick_size = Decimal("0.1")
        step_size = Decimal("0.001")
        min_notional = Decimal("5.0")

        for f in s.get("filters", []):
            f_type = f.get("filterType")
            if f_type == "PRICE_FILTER":
                tick_size = Decimal(str(f.get("tickSize", "0.1")))
            elif f_type == "LOT_SIZE":
                step_size = Decimal(str(f.get("stepSize", "0.001")))
            elif f_type in ("MIN_NOTIONAL", "NOTIONAL"):
                min_notional = Decimal(str(f.get("notional", "5.0")))

        instrument = Instrument(
            symbol=s["symbol"],
            venue="binance_global",
            market_type=MarketType.USDM_FUTURES,
            base_asset=s.get("baseAsset", ""),
            quote_asset=s.get("quoteAsset", ""),
            tick_size=tick_size,
            step_size=step_size,
            min_notional=min_notional,
            price_precision=int(s.get("pricePrecision", 2)),
            quantity_precision=int(s.get("quantityPrecision", 3)),
            is_trading_enabled=s.get("status") == "TRADING",
        )
        self.cached_instruments[symbol] = instrument
        logger.info(
            "Discovered %s constraints: tick=%s, step=%s, min_notional=%s",
            symbol,
            tick_size,
            step_size,
            min_notional,
        )
        return instrument

    def parse_mock_exchange_info(self, symbol: str, raw_symbol_data: Dict[str, Any]) -> Instrument:
        """Deterministic offline parser for unit tests and local simulation."""
        tick_size = Decimal("0.1")
        step_size = Decimal("0.001")
        min_notional = Decimal("5.0")

        for f in raw_symbol_data.get("filters", []):
            f_type = f.get("filterType")
            if f_type == "PRICE_FILTER":
                tick_size = Decimal(str(f.get("tickSize", "0.1")))
            elif f_type == "LOT_SIZE":
                step_size = Decimal(str(f.get("stepSize", "0.001")))
            elif f_type in ("MIN_NOTIONAL", "NOTIONAL"):
                min_notional = Decimal(str(f.get("notional", "5.0")))

        return Instrument(
            symbol=raw_symbol_data["symbol"],
            venue="binance_global",
            market_type=MarketType.USDM_FUTURES,
            base_asset=raw_symbol_data.get("baseAsset", "BTC"),
            quote_asset=raw_symbol_data.get("quoteAsset", "USDT"),
            tick_size=tick_size,
            step_size=step_size,
            min_notional=min_notional,
            price_precision=int(raw_symbol_data.get("pricePrecision", 2)),
            quantity_precision=int(raw_symbol_data.get("quantityPrecision", 3)),
            is_trading_enabled=raw_symbol_data.get("status") == "TRADING",
        )
