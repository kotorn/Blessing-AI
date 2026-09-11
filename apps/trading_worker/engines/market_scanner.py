import logging
import asyncio
from typing import List, Dict, Any
from decimal import Decimal

logger = logging.getLogger("blessing.engines.market_scanner")

class MarketScannerEngine:
    """
    Dynamically scans the market to discover the most active and volatile trading pairs.
    Filters by volume, filters out stablecoin pairs, and ranks by combination of liquidity and price action.
    """
    def __init__(self, top_n: int = 10, min_volume_usd: float = 100_000_000.0, refresh_interval_sec: int = 3600):
        self.top_n = top_n
        self.min_volume_usd = min_volume_usd
        self.refresh_interval_sec = refresh_interval_sec
        self.base_url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
        self.active_symbols = ["BTCUSDT", "ETHUSDT"] # Always start with core pairs
        
    async def scan_active_symbols(self) -> List[str]:
        """
        Polls Binance REST API to rank USDT-M Futures pairs.
        In a production environment, this requires ccxt or httpx. We mock the HTTP logic for stability here,
        but return a dynamic list simulating a real scan result.
        """
        try:
            logger.info("Scanning Binance USD-M Futures for active pairs (Volume > %s)...", self.min_volume_usd)
            
            # Using standard library urllib or a mock due to httpx availability
            import urllib.request
            import json
            
            # We use an executor to prevent blocking the async loop with urllib
            def fetch_data():
                req = urllib.request.Request(self.base_url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as response:
                    return json.loads(response.read().decode())
                    
            loop = asyncio.get_event_loop()
            tickers = await loop.run_in_executor(None, fetch_data)
            
            valid_pairs = []
            for t in tickers:
                symbol = t.get('symbol', '')
                if not symbol.endswith("USDT"):
                    continue
                # Filter out stables and index tokens
                if symbol in ["BUSDUSDT", "USDCUSDT", "TUSDUSDT", "FDUSDUSDT", "BTCDOMUSDT"]:
                    continue

                vol_usd = float(t.get('quoteVolume', 0))
                if vol_usd < self.min_volume_usd:
                    continue
                    
                price_change_pct = abs(float(t.get('priceChangePercent', 0)))

                valid_pairs.append({
                    "symbol": symbol,
                    "volume": vol_usd,
                    "volatility": price_change_pct
                })

            # Sort by volume first, pick top N
            sorted_by_vol = sorted(valid_pairs, key=lambda x: x['volume'], reverse=True)
            top_symbols = [x['symbol'] for x in sorted_by_vol[:self.top_n]]
            
            # Always ensure BTC and ETH are in the mix
            for core in ["BTCUSDT", "ETHUSDT"]:
                if core not in top_symbols:
                    top_symbols.insert(0, core)
                    
            # Deduplicate and trim
            top_symbols = list(dict.fromkeys(top_symbols))[:self.top_n]
            
            logger.info("Market Scanner selected %d active symbols: %s", len(top_symbols), top_symbols)
            self.active_symbols = top_symbols
            return self.active_symbols
            
        except Exception as e:
            logger.error("Market Scanner failed (fallback to core): %s", e)
            self.active_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
            return self.active_symbols
