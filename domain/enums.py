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
    # Seven-state regime vocabulary used by MarketStateClassifier and the
    # strategy engines.  The generic names below remain as compatibility
    # values for older paper/research payloads.
    R0_STRONG_MEAN_REVERSION = "R0_STRONG_MEAN_REVERSION"
    R1_RANGE = "R1_RANGE"
    R2_WEAK_TREND = "R2_WEAK_TREND"
    R3_STRONG_TREND = "R3_STRONG_TREND"
    R4_BREAKOUT = "R4_BREAKOUT"
    R5_VOLATILITY_SHOCK = "R5_VOLATILITY_SHOCK"
    R6_CRISIS = "R6_CRISIS"
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


class EconomicRiskClass(str, Enum):
    """Economic effect of an execution decision, independent of its command."""

    NOOP = "NOOP"
    NEW_RISK = "NEW_RISK"
    INCREASE_RISK = "INCREASE_RISK"
    REDUCE_RISK = "REDUCE_RISK"
    RECOVERY = "RECOVERY"
    CLOSE = "CLOSE"
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
