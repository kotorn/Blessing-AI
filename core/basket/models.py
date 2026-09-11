"""
Domain Models for Blessing AI v0.1
Core domain entities using strict type hints, Decimal precision, and Pydantic validation.
"""

from decimal import Decimal
from enum import Enum
from typing import Dict, List, Optional
from datetime import datetime
from pydantic import BaseModel, Field


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class BasketState(str, Enum):
    NEW = "NEW"
    ACTIVE = "ACTIVE"
    GRID_EXPANDING = "GRID_EXPANDING"
    PROFITABLE = "PROFITABLE"
    RECOVERY = "RECOVERY"
    NO_NEW_GRID = "NO_NEW_GRID"
    DELEVERAGING = "DELEVERAGING"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"


class RiskState(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    NO_NEW_GRID = "NO_NEW_GRID"
    RECOVERY_ONLY = "RECOVERY_ONLY"
    DELEVERAGE = "DELEVERAGE"
    EMERGENCY = "EMERGENCY"


class MarketRegime(str, Enum):
    R0_STRONG_MEAN_REVERSION = "R0_STRONG_MEAN_REVERSION"
    R1_RANGE = "R1_RANGE"
    R2_WEAK_TREND = "R2_WEAK_TREND"
    R3_STRONG_TREND = "R3_STRONG_TREND"
    R4_BREAKOUT = "R4_BREAKOUT"
    R5_VOLATILITY_SHOCK = "R5_VOLATILITY_SHOCK"
    R6_CRISIS = "R6_CRISIS"


class OrderRole(str, Enum):
    GRID_ENTRY = "GRID_ENTRY"
    GRID_STEP = "GRID_STEP"
    BASKET_TP = "BASKET_TP"
    RECOVERY_EXIT = "RECOVERY_EXIT"
    EMERGENCY_SL = "EMERGENCY_SL"


class GridLevel(BaseModel):
    level: int = Field(..., ge=1, le=10)
    target_price: Decimal
    order_size: Decimal
    multiplier: Decimal
    status: str = "PENDING"  # PENDING, SUBMITTED, FILLED, CANCELLED
    client_order_id: Optional[str] = None
    exchange_order_id: Optional[str] = None
    fill_price: Optional[Decimal] = None
    filled_size: Decimal = Decimal("0.0")
    fee_paid: Decimal = Decimal("0.0")
    created_at: datetime
    filled_at: Optional[datetime] = None


class Fill(BaseModel):
    fill_id: str
    client_order_id: str
    exchange_trade_id: str
    basket_id: str
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_asset: str
    is_maker: bool
    executed_at: datetime


class Position(BaseModel):
    venue: str
    symbol: str
    direction: str
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    liquidation_price: Optional[Decimal] = None
    unrealized_pnl: Decimal = Decimal("0.0")
    leverage: Decimal = Decimal("1.0")
    initial_margin: Decimal = Decimal("0.0")
    maintenance_margin: Decimal = Decimal("0.0")
    updated_at: datetime


class RegimeState(BaseModel):
    symbol: str
    timestamp: datetime
    predicted_regime: MarketRegime
    probabilities: Dict[MarketRegime, float]
    entropy: float
    volatility_zscore: float
    atr_percentile: float


class GridSafetyPrediction(BaseModel):
    symbol: str
    timestamp: datetime
    grid_safety_score: float = Field(..., ge=0.0, le=100.0)
    prob_basket_profit: float
    prob_reach_level_2: float
    prob_reach_level_3: float
    prob_reach_level_5: float
    expected_mae_pct: float
    expected_max_equity_dd_pct: float
    expected_grid_depth: float
    expected_recovery_time_hrs: float
    expected_net_basket_return: float
    grid_allowed: bool
    reduced_risk: bool


class Basket(BaseModel):
    """
    First-Class Citizen in Blessing AI.
    Tracks all grid orders, average entry, fees, funding cost, and PnL.
    """
    basket_id: str
    strategy_id: str
    venue: str
    instrument: str
    direction: Direction
    state: BasketState = BasketState.NEW
    grid_depth: int = 0
    max_grid_levels: int = 5
    total_size: Decimal = Decimal("0.0")
    average_entry: Decimal = Decimal("0.0")
    current_mark_price: Decimal = Decimal("0.0")
    target_tp_price: Optional[Decimal] = None
    stop_loss_price: Optional[Decimal] = None
    realized_pnl: Decimal = Decimal("0.0")
    unrealized_pnl: Decimal = Decimal("0.0")
    trading_fees: Decimal = Decimal("0.0")
    funding_accrued: Decimal = Decimal("0.0")  # Positive = received, negative = paid
    slippage_cost: Decimal = Decimal("0.0")
    net_pnl: Decimal = Decimal("0.0")
    grid_levels: List[GridLevel] = Field(default_factory=list)
    fills: List[Fill] = Field(default_factory=list)
    grid_safety_score_entry: Optional[float] = None
    market_regime_entry: Optional[str] = None
    created_at: datetime
    last_updated: datetime
    closed_at: Optional[datetime] = None

    def calculate_net_pnl(self, current_price: Decimal) -> Decimal:
        """
        Net Basket PnL Formula (Section 13):
        Net PnL = Price PnL - Trading Fees + Funding Accrual - Slippage
        """
        if self.total_size == Decimal("0.0"):
            self.unrealized_pnl = Decimal("0.0")
        else:
            if self.direction == Direction.LONG:
                self.unrealized_pnl = (current_price - self.average_entry) * self.total_size
            else:
                self.unrealized_pnl = (self.average_entry - current_price) * self.total_size

        self.net_pnl = (
            self.realized_pnl
            + self.unrealized_pnl
            - self.trading_fees
            + self.funding_accrued
            - self.slippage_cost
        )
        self.current_mark_price = current_price
        return self.net_pnl
