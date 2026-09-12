import logging
from decimal import Decimal
from typing import Dict, Any

logger = logging.getLogger("blessing.binance.symbol_rules")

class SymbolTradingRules:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.status = "UNKNOWN"
        self.tick_size = Decimal("0.0")
        self.step_size = Decimal("0.0")
        self.min_qty = Decimal("0.0")
        self.max_qty = Decimal("0.0")
        self.min_notional = Decimal("0.0")
        self.supported_order_types = []

    def parse_exchange_info(self, symbol_data: Dict[str, Any]):
        self.status = symbol_data.get("status", "UNKNOWN")
        self.supported_order_types = symbol_data.get("orderTypes", [])
        
        for f in symbol_data.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                self.tick_size = Decimal(f["tickSize"])
            elif f["filterType"] == "LOT_SIZE":
                self.min_qty = Decimal(f["minQty"])
                self.max_qty = Decimal(f["maxQty"])
                self.step_size = Decimal(f["stepSize"])
            elif f["filterType"] == "MIN_NOTIONAL":
                self.min_notional = Decimal(f.get("notional", "0.0"))

    def normalize_quantity(self, quantity: Decimal) -> Decimal:
        if self.step_size == Decimal("0.0"):
            return quantity
        # Round down to nearest step size
        steps = int(quantity / self.step_size)
        return Decimal(str(steps)) * self.step_size

    def normalize_price(self, price: Decimal) -> Decimal:
        if self.tick_size == Decimal("0.0"):
            return price
        steps = round(price / self.tick_size)
        return Decimal(str(steps)) * self.tick_size
