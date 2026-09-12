"""
Domain Enums for Blessing AI v0.2
Defines standardized enumerations across strategies, venue adapters, and risk governors.
"""

from enum import Enum


class MarketType(str, Enum):
    SPOT = "SPOT"
    USDM_FUTURES = "USDM_FUTURES"
    COINM_FUTURES = "COINM_FUTURES"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    BOTH = "BOTH"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP = "STOP"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT = "TAKE_PROFIT"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"


class TimeInForce(str, Enum):
    GTC = "GTC"  # Good-Til-Cancelled
    IOC = "IOC"  # Immediate-Or-Cancel
    FOK = "FOK"  # Fill-Or-Kill
    POST_ONLY = "POST_ONLY"  # Maps to GTX on Binance Futures, PO on Spot


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class RegimeType(str, Enum):
    RANGE = "RANGE"
    TREND = "TREND"
    BREAKOUT = "BREAKOUT"
    SHOCK = "SHOCK"
    TRANSITION = "TRANSITION"


class RiskState(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    NO_NEW_RISK = "NO_NEW_RISK"
    RECOVERY_ONLY = "RECOVERY_ONLY"
    DELEVERAGE = "DELEVERAGE"
    LIQUIDATING = "LIQUIDATING"
    EMERGENCY = "EMERGENCY"


class BasketState(str, Enum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    GRID_EXPANDING = "GRID_EXPANDING"
    PROFITABLE = "PROFITABLE"
    DEFENSE = "DEFENSE"
    RECOVERY = "RECOVERY"
    DELEVERAGING = "DELEVERAGING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"


class RecoveryActionType(str, Enum):
    HOLD = "HOLD"
    PARTIAL_HEDGE = "PARTIAL_HEDGE"
    REDUCE_LONG = "REDUCE_LONG"
    HARVEST_HEDGE = "HARVEST_HEDGE"
    PROGRESSIVE_UNWIND = "PROGRESSIVE_UNWIND"
    EMERGENCY_FLATTEN = "EMERGENCY_FLATTEN"
