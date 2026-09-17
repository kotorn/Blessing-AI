import logging
from .rest_client import BinanceRestClient
from .config import BinanceEnvironment
from .symbol_rules import SymbolTradingRules
from typing import Dict

logger = logging.getLogger("blessing.binance.capabilities")


def _exchange_bool(value: object) -> bool:
    """Parse Binance booleans without treating the string ``"false"`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


class BinanceCapabilities:
    def __init__(self):
        self.authenticated = False
        self.account_request_succeeded = False
        self.trade_authorized = False
        self.position_mode_known = False
        self.environment = BinanceEnvironment.TESTNET
        self.usdm_futures = True
        self.hedge_mode = False
        self.symbol_rules: Dict[str, SymbolTradingRules] = {}

    async def discover(self, rest_client: BinanceRestClient) -> bool:
        self.authenticated = False
        self.account_request_succeeded = False
        self.trade_authorized = False
        self.position_mode_known = False
        self.hedge_mode = False
        self.symbol_rules.clear()
        self.environment = rest_client.env
        if rest_client.env not in {BinanceEnvironment.TESTNET, BinanceEnvironment.MAINNET}:
            logger.error("Capability discovery rejected for an unknown Binance environment.")
            return False

        try:
            if getattr(rest_client, "portfolio_margin", False):
                papi_acc = await rest_client.request("GET", "/papi/v1/account", signed=True)
                if not isinstance(papi_acc, dict) or papi_acc.get("accountStatus") != "NORMAL":
                    raise ValueError("Portfolio Margin account is not NORMAL or valid")

                pos_mode = await rest_client.request("GET", "/papi/v1/um/positionSide/dual", signed=True)
                if not isinstance(pos_mode, dict) or "dualSidePosition" not in pos_mode:
                    raise ValueError("Position mode response is invalid")
                self.account_request_succeeded = True
                self.trade_authorized = True
                self.hedge_mode = _exchange_bool(pos_mode["dualSidePosition"])
                self.position_mode_known = True
            else:
                # Authentication truth starts with a successful signed account call.
                account = await rest_client.request("GET", "/fapi/v2/account", signed=True)
                if (
                    not isinstance(account, dict)
                    or not isinstance(account.get("assets"), list)
                    or "canTrade" not in account
                ):
                    raise ValueError("Signed account response is missing explicit assets or canTrade")
                self.account_request_succeeded = True
                self.trade_authorized = _exchange_bool(account["canTrade"])
                
                # Check position mode
                pos_mode = await rest_client.request("GET", "/fapi/v1/positionSide/dual", signed=True)
                if not isinstance(pos_mode, dict) or "dualSidePosition" not in pos_mode:
                    raise ValueError("Position mode response is invalid")
                self.hedge_mode = _exchange_bool(pos_mode["dualSidePosition"])
                self.position_mode_known = True
            
            # Fetch Exchange Info
            exchange_info = await rest_client.request("GET", "/fapi/v1/exchangeInfo")
            if not isinstance(exchange_info, dict) or not isinstance(exchange_info.get("symbols"), list):
                raise ValueError("Exchange information response is invalid")
            for s in exchange_info.get("symbols", []):
                symbol_name = s.get("symbol")
                if not symbol_name:
                    continue
                rules = SymbolTradingRules(symbol_name)
                rules.parse_exchange_info(s)
                self.symbol_rules[str(symbol_name).upper()] = rules

            self.authenticated = self.account_request_succeeded and bool(self.symbol_rules)
            logger.info(
                "Capability discovery complete. Hedge Mode: %s, canTrade: %s, Symbols loaded: %d",
                self.hedge_mode,
                self.trade_authorized,
                len(self.symbol_rules),
            )
            return self.authenticated and self.trade_authorized
        except Exception as e:
            logger.error("Failed to discover capabilities: %s", e)
            self.authenticated = False
            self.account_request_succeeded = False
            self.trade_authorized = False
            self.position_mode_known = False
            self.symbol_rules.clear()
            return False
