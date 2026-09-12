import logging
from decimal import Decimal
from typing import Dict, Any

logger = logging.getLogger("blessing.binance.symbol_rules")

class SymbolTradingRules:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.status = "UNKNOWN"
        self.tick_size = Decimal("0.0")
        self.min_price = Decimal("0.0")
        self.max_price = Decimal("0.0")
        self.step_size = Decimal("0.0")
        self.min_qty = Decimal("0.0")
        self.max_qty = Decimal("0.0")
        self.market_min_qty = None
        self.market_max_qty = None
        self.market_step_size = None
        self.min_notional = Decimal("0.0")
        self.supported_order_types = []
        self._parsed_from_exchange_info = False
        self._seen_filter_types: set[str] = set()

    @property
    def parsed_from_exchange_info(self) -> bool:
        return self._parsed_from_exchange_info

    def parse_exchange_info(self, symbol_data: Dict[str, Any]):
        self.status = symbol_data.get("status", "UNKNOWN")
        self.supported_order_types = [
            str(order_type).upper() for order_type in symbol_data.get("orderTypes", [])
        ]
        self._parsed_from_exchange_info = True
        self._seen_filter_types = set()
        self.tick_size = Decimal("0.0")
        self.min_price = Decimal("0.0")
        self.max_price = Decimal("0.0")
        self.step_size = Decimal("0.0")
        self.min_qty = Decimal("0.0")
        self.max_qty = Decimal("0.0")
        self.market_min_qty = None
        self.market_max_qty = None
        self.market_step_size = None
        self.min_notional = Decimal("0.0")
        
        for f in symbol_data.get("filters", []):
            filter_type = str(f.get("filterType", "")).upper()
            self._seen_filter_types.add(filter_type)
            if filter_type == "PRICE_FILTER":
                self.min_price = Decimal(f["minPrice"])
                self.max_price = Decimal(f["maxPrice"])
                self.tick_size = Decimal(f["tickSize"])
            elif filter_type == "LOT_SIZE":
                self.min_qty = Decimal(f["minQty"])
                self.max_qty = Decimal(f["maxQty"])
                self.step_size = Decimal(f["stepSize"])
            elif filter_type == "MARKET_LOT_SIZE":
                self.market_min_qty = Decimal(f["minQty"])
                self.market_max_qty = Decimal(f["maxQty"])
                self.market_step_size = Decimal(f["stepSize"])
            elif filter_type in ("MIN_NOTIONAL", "NOTIONAL"):
                self.min_notional = Decimal(
                    f.get("notional", f.get("minNotional", "0.0"))
                )

    def is_ready_for(self, order_type: str) -> bool:
        """Return whether the exchange supplied enough rules for this order type."""
        normalized_type = str(order_type).upper()
        required = (
            self.status == "TRADING"
            and self.tick_size.is_finite()
            and self.tick_size > 0
            and (
                not self._parsed_from_exchange_info
                or (
                    self.min_price.is_finite()
                    and self.min_price > 0
                    and self.max_price.is_finite()
                    and self.max_price >= self.min_price
                )
            )
            and self.step_size.is_finite()
            and self.step_size > 0
            and self.min_qty.is_finite()
            and self.min_qty > 0
            and self.max_qty.is_finite()
            and self.max_qty >= self.min_qty
            and self.min_notional.is_finite()
            and self.min_notional > 0
        )
        if not required:
            return False
        if self._parsed_from_exchange_info:
            if not {"PRICE_FILTER", "LOT_SIZE"}.issubset(self._seen_filter_types):
                return False
            if not ({"MIN_NOTIONAL", "NOTIONAL"} & self._seen_filter_types):
                return False
            if normalized_type == "MARKET" and "MARKET_LOT_SIZE" in self._seen_filter_types:
                if (
                    self.market_step_size is None
                    or self.market_min_qty is None
                    or self.market_max_qty is None
                    or not self.market_step_size.is_finite()
                    or self.market_step_size <= 0
                    or not self.market_min_qty.is_finite()
                    or self.market_min_qty <= 0
                    or not self.market_max_qty.is_finite()
                    or self.market_max_qty < self.market_min_qty
                ):
                    return False
        if self._parsed_from_exchange_info and not self.supported_order_types:
            return False
        return not self.supported_order_types or normalized_type in self.supported_order_types

    def normalize_quantity(self, quantity: Decimal, is_market: bool = False) -> Decimal:
        step = self.market_step_size if is_market and self.market_step_size else self.step_size
        if not step or step == Decimal("0.0"):
            return quantity
        if quantity <= 0:
            return Decimal("0")
        # Round down to nearest step size safely
        steps = int(quantity / step)
        return Decimal(str(steps)) * step

    def normalize_price(self, price: Decimal) -> Decimal:
        if self.tick_size == Decimal("0.0"):
            return price
        if price <= 0:
            return Decimal("0")
        # Banker's rounding can be problematic. We truncate or use quantize.
        # But for exact matching we use int division.
        steps = int(price / self.tick_size)
        return Decimal(str(steps)) * self.tick_size
