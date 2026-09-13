"""
Binance Exchange Capability Discovery Engine for Blessing AI v0.2
Dynamically interrogates Binance Global REST endpoints on startup to discover:
- Symbol filters (tickSize, stepSize, minNotional)
- Position Mode (One-Way vs Hedge Mode)
- Trading Status & Multi-Assets Mode
- Rate Limit rules

Ensures strategy logic never hard-codes exchange parameters.
"""

from decimal import Decimal, InvalidOperation
from typing import Dict, Any, Optional
import logging

try:
    import aiohttp
except ImportError:
    aiohttp = None
    import urllib.request

from domain.models import Instrument, MarketType

logger = logging.getLogger("blessing.binance.capabilities")


def _required_positive_decimal(value: Any, field_name: str) -> Decimal:
    if value in (None, ""):
        raise ValueError(f"ExchangeInfo is missing {field_name}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"ExchangeInfo has invalid {field_name}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"ExchangeInfo has unusable {field_name}")
    return parsed


def _parse_symbol_filters(raw_symbol_data: Dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    filters = raw_symbol_data.get("filters")
    if not isinstance(filters, list):
        raise ValueError("ExchangeInfo symbol is missing filters")
    by_type = {
        str(item.get("filterType", "")).upper(): item
        for item in filters
        if isinstance(item, dict)
    }
    price_filter = by_type.get("PRICE_FILTER")
    lot_filter = by_type.get("LOT_SIZE")
    notional_filter = by_type.get("MIN_NOTIONAL") or by_type.get("NOTIONAL")
    if not price_filter or not lot_filter or not notional_filter:
        raise ValueError(
            "ExchangeInfo symbol must include PRICE_FILTER, LOT_SIZE, and MIN_NOTIONAL/NOTIONAL"
        )
    tick_size = _required_positive_decimal(price_filter.get("tickSize"), "tickSize")
    step_size = _required_positive_decimal(lot_filter.get("stepSize"), "stepSize")
    min_notional = _required_positive_decimal(
        notional_filter.get("notional", notional_filter.get("minNotional")),
        "minNotional",
    )
    return tick_size, step_size, min_notional


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

        if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
            raise RuntimeError("Binance exchangeInfo response is invalid")
        symbols = {
            s["symbol"]: s
            for s in data["symbols"]
            if isinstance(s, dict) and s.get("symbol")
        }
        if symbol not in symbols:
            raise ValueError(f"Symbol {symbol} not found on Binance USDⓈ-M Futures")

        s = symbols[symbol]
        if s.get("status") != "TRADING":
            raise ValueError(f"Symbol {symbol} is not TRADING")
        tick_size, step_size, min_notional = _parse_symbol_filters(s)
        if not s.get("baseAsset") or not s.get("quoteAsset"):
            raise ValueError(f"ExchangeInfo symbol {symbol} is missing asset metadata")

        instrument = Instrument(
            symbol=s["symbol"],
            venue="binance_global",
            market_type=MarketType.USDM_FUTURES,
            base_asset=s["baseAsset"],
            quote_asset=s["quoteAsset"],
            tick_size=tick_size,
            step_size=step_size,
            min_notional=min_notional,
            price_precision=int(s["pricePrecision"]),
            quantity_precision=int(s["quantityPrecision"]),
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
        if raw_symbol_data.get("status") != "TRADING":
            raise ValueError(f"Symbol {symbol} is not TRADING")
        tick_size, step_size, min_notional = _parse_symbol_filters(raw_symbol_data)
        if not raw_symbol_data.get("baseAsset") or not raw_symbol_data.get("quoteAsset"):
            raise ValueError(f"ExchangeInfo symbol {symbol} is missing asset metadata")

        return Instrument(
            symbol=raw_symbol_data["symbol"],
            venue="binance_global",
            market_type=MarketType.USDM_FUTURES,
            base_asset=raw_symbol_data["baseAsset"],
            quote_asset=raw_symbol_data["quoteAsset"],
            tick_size=tick_size,
            step_size=step_size,
            min_notional=min_notional,
            price_precision=int(raw_symbol_data["pricePrecision"]),
            quantity_precision=int(raw_symbol_data["quantityPrecision"]),
            is_trading_enabled=raw_symbol_data.get("status") == "TRADING",
        )
