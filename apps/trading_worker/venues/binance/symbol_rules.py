import logging
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any, Dict, Optional

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
        self.max_notional = Decimal("0.0")
        self.min_notional_apply_to_market = True
        self.max_notional_apply_to_market = True
        self.percent_price_multiplier_up: Optional[Decimal] = None
        self.percent_price_multiplier_down: Optional[Decimal] = None
        self.percent_price_bid_multiplier_up: Optional[Decimal] = None
        self.percent_price_bid_multiplier_down: Optional[Decimal] = None
        self.percent_price_ask_multiplier_up: Optional[Decimal] = None
        self.percent_price_ask_multiplier_down: Optional[Decimal] = None
        self.supported_order_types = []
        self._parsed_from_exchange_info = False
        self._seen_filter_types: set[str] = set()
        self._min_notional_constraints: list[tuple[Decimal, bool]] = []
        self._max_notional_constraints: list[tuple[Decimal, bool]] = []

    @property
    def parsed_from_exchange_info(self) -> bool:
        return self._parsed_from_exchange_info

    @property
    def has_percent_price_filter(self) -> bool:
        return bool(
            {"PERCENT_PRICE", "PERCENT_PRICE_BY_SIDE"} & self._seen_filter_types
        )

    def parse_exchange_info(self, symbol_data: Dict[str, Any]):
        if not isinstance(symbol_data, dict):
            raise ValueError("Exchange symbol information must be an object")
        self.status = str(symbol_data.get("status", "UNKNOWN")).upper()
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
        self.max_notional = Decimal("0.0")
        self.min_notional_apply_to_market = True
        self.max_notional_apply_to_market = True
        self.percent_price_multiplier_up = None
        self.percent_price_multiplier_down = None
        self.percent_price_bid_multiplier_up = None
        self.percent_price_bid_multiplier_down = None
        self.percent_price_ask_multiplier_up = None
        self.percent_price_ask_multiplier_down = None
        self._min_notional_constraints = []
        self._max_notional_constraints = []

        filters = symbol_data.get("filters", [])
        if not isinstance(filters, list):
            raise ValueError("Exchange symbol filters must be a list")

        def decimal_filter_value(filter_data: Dict[str, Any], *names: str) -> Decimal:
            raw = next(
                (filter_data[name] for name in names if filter_data.get(name) not in (None, "")),
                None,
            )
            if raw is None:
                raise ValueError(
                    f"Exchange {filter_data.get('filterType', 'UNKNOWN')} filter is missing "
                    + "/".join(names)
                )
            try:
                value = Decimal(str(raw))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Exchange {filter_data.get('filterType', 'UNKNOWN')} filter has invalid "
                    + "/".join(names)
                ) from exc
            if not value.is_finite():
                raise ValueError(
                    f"Exchange {filter_data.get('filterType', 'UNKNOWN')} filter has non-finite "
                    + "/".join(names)
                )
            return value

        def bool_filter_value(
            filter_data: Dict[str, Any], name: str, default: bool = True
        ) -> bool:
            raw = filter_data.get(name)
            if raw in (None, ""):
                return default
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, (int, float)):
                return raw != 0
            if isinstance(raw, str):
                normalized = raw.strip().lower()
                if normalized in {"1", "true", "yes", "on"}:
                    return True
                if normalized in {"0", "false", "no", "off"}:
                    return False
            raise ValueError(
                f"Exchange {filter_data.get('filterType', 'UNKNOWN')} filter has invalid {name}"
            )

        def add_min_notional(value: Decimal, applies_to_market: bool) -> None:
            if value <= 0:
                raise ValueError("Exchange minimum notional must be positive")
            self._min_notional_constraints.append((value, applies_to_market))
            self.min_notional = max(self.min_notional, value)
            self.min_notional_apply_to_market = any(
                applies for _, applies in self._min_notional_constraints
            )

        def add_max_notional(value: Decimal, applies_to_market: bool) -> None:
            if value < 0:
                raise ValueError("Exchange maximum notional must be non-negative")
            if value == 0:
                return
            self._max_notional_constraints.append((value, applies_to_market))
            positive_maxima = [item[0] for item in self._max_notional_constraints]
            self.max_notional = min(positive_maxima)
            self.max_notional_apply_to_market = all(
                applies for _, applies in self._max_notional_constraints
            )
        
        for f in filters:
            if not isinstance(f, dict):
                raise ValueError("Exchange symbol filter must be an object")
            filter_type = str(f.get("filterType", "")).upper()
            self._seen_filter_types.add(filter_type)
            if filter_type == "PRICE_FILTER":
                self.min_price = decimal_filter_value(f, "minPrice")
                self.max_price = decimal_filter_value(f, "maxPrice")
                self.tick_size = decimal_filter_value(f, "tickSize")
            elif filter_type == "LOT_SIZE":
                self.min_qty = decimal_filter_value(f, "minQty")
                self.max_qty = decimal_filter_value(f, "maxQty")
                self.step_size = decimal_filter_value(f, "stepSize")
            elif filter_type == "MARKET_LOT_SIZE":
                self.market_min_qty = decimal_filter_value(f, "minQty")
                self.market_max_qty = decimal_filter_value(f, "maxQty")
                self.market_step_size = decimal_filter_value(f, "stepSize")
            elif filter_type == "MIN_NOTIONAL":
                add_min_notional(
                    decimal_filter_value(f, "notional", "minNotional"),
                    bool_filter_value(f, "applyToMarket"),
                )
            elif filter_type == "NOTIONAL":
                add_min_notional(
                    decimal_filter_value(f, "minNotional", "notional"),
                    bool_filter_value(f, "applyMinToMarket"),
                )
                if f.get("maxNotional") not in (None, ""):
                    add_max_notional(
                        decimal_filter_value(f, "maxNotional"),
                        bool_filter_value(f, "applyMaxToMarket"),
                    )
                else:
                    # A declared NOTIONAL filter is not complete without its
                    # maximum side. Do not silently reinterpret malformed
                    # exchange metadata as an unlimited order rule.
                    raise ValueError(
                        "Exchange NOTIONAL filter is missing maxNotional"
                    )
            elif filter_type == "PERCENT_PRICE":
                self.percent_price_multiplier_up = decimal_filter_value(
                    f, "multiplierUp"
                )
                self.percent_price_multiplier_down = decimal_filter_value(
                    f, "multiplierDown"
                )
            elif filter_type == "PERCENT_PRICE_BY_SIDE":
                self.percent_price_bid_multiplier_up = decimal_filter_value(
                    f, "bidMultiplierUp"
                )
                self.percent_price_bid_multiplier_down = decimal_filter_value(
                    f, "bidMultiplierDown"
                )
                self.percent_price_ask_multiplier_up = decimal_filter_value(
                    f, "askMultiplierUp"
                )
                self.percent_price_ask_multiplier_down = decimal_filter_value(
                    f, "askMultiplierDown"
                )

    @staticmethod
    def _valid_multiplier(value: Optional[Decimal]) -> bool:
        return value is not None and value.is_finite() and value > 0

    def _percent_price_rules_ready(self) -> bool:
        if "PERCENT_PRICE" in self._seen_filter_types and not (
            self._valid_multiplier(self.percent_price_multiplier_up)
            and self._valid_multiplier(self.percent_price_multiplier_down)
            and self.percent_price_multiplier_up >= self.percent_price_multiplier_down
        ):
            return False
        if "PERCENT_PRICE_BY_SIDE" in self._seen_filter_types and not (
            self._valid_multiplier(self.percent_price_bid_multiplier_up)
            and self._valid_multiplier(self.percent_price_bid_multiplier_down)
            and self._valid_multiplier(self.percent_price_ask_multiplier_up)
            and self._valid_multiplier(self.percent_price_ask_multiplier_down)
            and self.percent_price_bid_multiplier_up >= self.percent_price_bid_multiplier_down
            and self.percent_price_ask_multiplier_up >= self.percent_price_ask_multiplier_down
        ):
            return False
        return True

    def min_notional_for(self, order_type: str) -> Decimal:
        if str(order_type).upper() != "MARKET" or not self._min_notional_constraints:
            return self.min_notional
        applicable = [value for value, applies in self._min_notional_constraints if applies]
        return max(applicable, default=Decimal("0"))

    def max_notional_for(self, order_type: str) -> Decimal:
        if str(order_type).upper() != "MARKET" or not self._max_notional_constraints:
            return self.max_notional
        applicable = [value for value, applies in self._max_notional_constraints if applies]
        return min(applicable, default=Decimal("0"))

    def validate_percent_price(
        self,
        price: Decimal,
        side: str,
        reference_price: Optional[Decimal],
    ) -> tuple[bool, str]:
        """Validate mark/reference-price bands supplied by Binance filters."""

        if not ("PERCENT_PRICE" in self._seen_filter_types or "PERCENT_PRICE_BY_SIDE" in self._seen_filter_types):
            return True, ""
        if not self._percent_price_rules_ready():
            return False, "percent-price filter data is incomplete"
        if reference_price is None or not reference_price.is_finite() or reference_price <= 0:
            return False, "percent-price reference is unavailable"
        normalized_side = str(side).upper()
        if normalized_side not in {"BUY", "SELL"}:
            return False, "percent-price side is invalid"

        lower = Decimal("0")
        upper: Optional[Decimal] = None

        def intersect(multiplier_down: Decimal, multiplier_up: Decimal) -> None:
            nonlocal lower, upper
            lower = max(lower, reference_price * multiplier_down)
            candidate_upper = reference_price * multiplier_up
            upper = candidate_upper if upper is None else min(upper, candidate_upper)

        if "PERCENT_PRICE" in self._seen_filter_types:
            intersect(self.percent_price_multiplier_down, self.percent_price_multiplier_up)
        if "PERCENT_PRICE_BY_SIDE" in self._seen_filter_types:
            if normalized_side == "BUY":
                intersect(
                    self.percent_price_bid_multiplier_down,
                    self.percent_price_bid_multiplier_up,
                )
            else:
                intersect(
                    self.percent_price_ask_multiplier_down,
                    self.percent_price_ask_multiplier_up,
                )
        if price < lower or (upper is not None and price > upper):
            return False, f"price is outside Binance percent-price band [{lower}, {upper}]"
        return True, ""

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
            if not self._percent_price_rules_ready():
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

    def normalize_price(self, price: Decimal, *, round_up: bool = False) -> Decimal:
        if self.tick_size == Decimal("0.0"):
            return price
        if price <= 0:
            return Decimal("0")
        # A BUY price is floored so it cannot cross above the requested level;
        # a SELL price is ceiled so normalization cannot silently move it
        # below the requested level. Both results remain on the exchange tick.
        steps = int(price / self.tick_size)
        if round_up and Decimal(str(steps)) * self.tick_size < price:
            steps += 1
        return Decimal(str(steps)) * self.tick_size
