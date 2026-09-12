"""
Core Domain Models for Blessing AI v0.2
Strict financial models using Python Decimal arithmetic and UTC datetime fields.
"""

from decimal import Decimal
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone

try:
    from pydantic import BaseModel, Field, ConfigDict
    PYDANTIC_AVAILABLE = True
except ImportError:
    PYDANTIC_AVAILABLE = False
    # Lightweight fallback shim for environments where pydantic is not yet pip-installed
    class BaseModel:
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
        def model_dump(self):
            return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}
        def dict(self):
            return self.model_dump()
        def __repr__(self):
            return f"{self.__class__.__name__}({self.__dict__})"

    def Field(default=None, default_factory=None):
        if default_factory is not None:
            return default_factory()
        return default

    class ConfigDict:
        def __init__(self, **kwargs):
            pass

from domain.enums import (
    MarketType,
    OrderSide,
    PositionSide,
    OrderType,
    TimeInForce,
    OrderStatus,
    RegimeType,
    RiskState,
    BasketState,
    RecoveryActionType,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Instrument(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    symbol: str
    venue: str
    market_type: MarketType
    base_asset: str
    quote_asset: str
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal
    price_precision: int
    quantity_precision: int
    is_trading_enabled: bool = True
    max_leverage: int = 20


class MarketEvent(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    event_id: str
    event_time: datetime
    receive_time: datetime = Field(default_factory=utc_now)
    symbol: str
    venue: str
    market_type: MarketType
    last_price: Decimal
    best_bid: Decimal
    best_ask: Decimal
    volume_24h: Decimal = Decimal("0.0")
    mark_price: Optional[Decimal] = None
    index_price: Optional[Decimal] = None
    funding_rate: Optional[Decimal] = None


class PriceActionState(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    symbol: str
    timestamp: datetime
    swing_high: Decimal
    swing_low: Decimal
    prior_24h_high: Decimal
    prior_24h_low: Decimal
    displacement_velocity_pct: Decimal  # % change per minute normalized by ATR
    displacement_acceleration: Decimal
    range_expansion_ratio: Decimal      # Current bar range / EMA(ATR, 100)
    liquidity_swept: bool = False
    is_reclaiming: bool = False


class MarketState(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    symbol: str
    timestamp: datetime
    primary_regime: RegimeType
    regime_probabilities: Dict[str, Decimal]
    atr_1h: Decimal
    volatility_zscore: Decimal
    funding_zscore: Decimal = Decimal("0.0")
    shock_active: bool = False


class StrategyIntent(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    intent_id: str
    strategy_id: str
    symbol: str
    market_type: MarketType
    direction: PositionSide
    desired_delta_qty: Decimal          # Signed: positive for Long, negative for Short
    opportunity_score: Decimal          # Bounded [0.00, 1.00]
    confidence: Decimal                 # Bounded [0.00, 1.00]
    expected_holding_horizon_sec: int
    invalidation_price: Optional[Decimal] = None
    evidence: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=utc_now)


class OpportunityScore(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    strategy_id: str
    symbol: str
    raw_score: Decimal
    calibrated_score: Decimal
    expected_edge: Decimal
    regime_fit: Decimal
    tail_risk_factor: Decimal
    confidence: Decimal
    timestamp: datetime = Field(default_factory=utc_now)


class RiskBudget(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    symbol: str
    strategy_id: str
    allocated_margin_usdt: Decimal
    max_drawdown_limit_usdt: Decimal
    leverage_cap: Decimal
    timestamp: datetime = Field(default_factory=utc_now)


class TargetExposure(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    symbol: str
    market_type: MarketType
    target_net_delta_qty: Decimal       # Net economic target
    target_gross_limit_qty: Decimal     # Absolute maximum gross exposure
    strategy_attributions: Dict[str, Decimal]  # Virtual strategy allocations
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime


class GridLevel(BaseModel):
    level: int
    target_price: Decimal
    quantity: Decimal
    is_filled: bool = False
    fill_time: Optional[datetime] = None
    client_order_id: Optional[str] = None


class Basket(BaseModel):
    basket_id: str
    strategy_id: str
    symbol: str
    direction: PositionSide
    state: BasketState = BasketState.NEW
    grid_depth: int = 0
    max_grid_levels: int = 5
    levels: List[GridLevel] = Field(default_factory=list)
    total_quantity: Decimal = Decimal("0.0")
    average_entry_price: Decimal = Decimal("0.0")
    realized_pnl: Decimal = Decimal("0.0")
    unrealized_pnl: Decimal = Decimal("0.0")
    net_fees_paid: Decimal = Decimal("0.0")
    accumulated_funding: Decimal = Decimal("0.0")
    is_closed: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Position(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    symbol: str
    position_side: PositionSide
    quantity: Decimal                   # Magnitude (always positive)
    entry_price: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    liquidation_price: Optional[Decimal] = None
    leverage: Decimal = Decimal("1.0")
    margin_type: str = "CROSSED"        # CROSSED or ISOLATED


class RiskSnapshot(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    timestamp: datetime = Field(default_factory=utc_now)
    portfolio_equity: Decimal
    unrealized_pnl: Decimal
    realized_pnl_24h: Decimal
    margin_utilization_pct: Decimal
    effective_leverage: Decimal
    current_drawdown_pct: Decimal
    liquidation_distance_pct: Decimal
    risk_state: RiskState
    hard_violations: List[str] = Field(default_factory=list)
    soft_violations: List[str] = Field(default_factory=list)


class RecoveryIntent(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    basket_id: str
    target_hedge_ratio: Decimal         # 0.00 to 1.00
    deleveraging_action: RecoveryActionType
    required_delta_adjustment: Decimal
    reason: str
    timestamp: datetime = Field(default_factory=utc_now)


class OrderIntent(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    client_order_id: str
    symbol: str
    market_type: MarketType
    side: OrderSide
    position_side: PositionSide         # Required for Binance Hedge Mode
    order_type: OrderType
    time_in_force: TimeInForce
    quantity: Decimal
    price: Optional[Decimal] = None
    reduce_only: bool = False
    post_only: bool = False
    strategy_id: str = "portfolio"
    created_at: datetime = Field(default_factory=utc_now)


class Fill(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    fill_id: str
    client_order_id: str
    exchange_order_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_asset: str
    timestamp: datetime = Field(default_factory=utc_now)


class ExecutionDecision(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    decision_id: str
    symbol: str
    action: str                         # "SUBMIT_ORDER" | "REDUCE_POSITION" | "NOOP"
    orders: List[OrderIntent] = Field(default_factory=list)
    rational: str = ""
    net_exposure_delta: Decimal = Decimal("0.0")
    timestamp: datetime = Field(default_factory=utc_now)

class ExecutionOrder(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    symbol: str
    side: OrderSide
    quantity: Decimal
    price: Decimal
    order_type: str
    client_order_id: str
    status: str
    timestamp: datetime = Field(default_factory=utc_now)

class ExchangeFill(BaseModel):
    if PYDANTIC_AVAILABLE:
        model_config = ConfigDict(frozen=True)
    exchange_trade_id: str
    exchange_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    position_side: PositionSide
    quantity: Decimal
    price: Decimal
    commission: Decimal
    commission_asset: str
    realized_pnl: Decimal
    maker: bool
    event_time: datetime
    transaction_time: datetime
    source: str
