import logging
from .rest_client import BinanceRestClient
from .config import BinanceEnvironment
from .symbol_rules import SymbolTradingRules
from typing import Dict

logger = logging.getLogger("blessing.binance.capabilities")

class BinanceCapabilities:
    def __init__(self):
        self.authenticated = False
        self.environment = BinanceEnvironment.TESTNET
        self.usdm_futures = True
        self.hedge_mode = False
        self.symbol_rules: Dict[str, SymbolTradingRules] = {}

    async def discover(self, rest_client: BinanceRestClient):
        try:
            # Check auth by querying account
            account = await rest_client.request("GET", "/fapi/v2/account", signed=True)
            self.authenticated = True
            
            # Check position mode
            pos_mode = await rest_client.request("GET", "/fapi/v1/positionSide/dual", signed=True)
            self.hedge_mode = pos_mode.get("dualSidePosition", False)
            
            # Fetch Exchange Info
            exchange_info = await rest_client.request("GET", "/fapi/v1/exchangeInfo")
            for s in exchange_info.get("symbols", []):
                symbol_name = s["symbol"]
                rules = SymbolTradingRules(symbol_name)
                rules.parse_exchange_info(s)
                self.symbol_rules[symbol_name] = rules
                
            logger.info("Capability discovery complete. Hedge Mode: %s, Symbols loaded: %d", self.hedge_mode, len(self.symbol_rules))
            return True
        except Exception as e:
            logger.error("Failed to discover capabilities: %s", e)
            self.authenticated = False
            return False
