"""
Event Schema for Blessing AI v0.1
Defines standardized NATS JetStream event envelopes and payloads.
Ensures uniform serialization, idempotency, and correlation across microservices.
"""

from decimal import Decimal
from typing import Any, Dict, Optional
from datetime import datetime, timezone
import uuid
from pydantic import BaseModel, Field


class BaseEvent(BaseModel):
    """Universal envelope for all NATS JetStream messages."""
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str
    venue: str
    symbol: str
    schema_version: str = "1.0"
    basket_id: Optional[str] = None
    strategy_id: Optional[str] = None
    correlation_id: Optional[str] = None


class MarketTradeEvent(BaseEvent):
    event_type: str = "market.trade"
    price: Decimal
    quantity: Decimal
    trade_id: str
    buyer_is_maker: bool


class MarketOrderbookEvent(BaseEvent):
    event_type: str = "market.book"
    best_bid: Decimal
    best_ask: Decimal
    bid_qty: Decimal
    ask_qty: Decimal
    spread: Decimal
    order_book_imbalance: float


class MarketFundingEvent(BaseEvent):
    event_type: str = "market.funding"
    funding_rate: Decimal
    settle_time: datetime
    mark_price: Decimal
    index_price: Decimal


class StrategyGridSignalEvent(BaseEvent):
    event_type: str = "strategy.grid"
    direction: str
    grid_level: int
    target_price: Decimal
    quantity: Decimal
    regime: str
    grid_safety_score: float


class BasketStateChangedEvent(BaseEvent):
    event_type: str = "basket.state"
    previous_state: str
    new_state: str
    reason: str
    grid_depth: int
    total_size: Decimal
    average_entry: Decimal
    unrealized_pnl: Decimal
    net_pnl: Decimal


class RiskPortfolioSnapshotEvent(BaseEvent):
    event_type: str = "risk.portfolio"
    risk_state: str
    portfolio_equity: Decimal
    portfolio_balance: Decimal
    margin_utilization_pct: Decimal
    effective_leverage: Decimal
    portfolio_drawdown_pct: Decimal
    violations: list[str] = Field(default_factory=list)


class OrderFilledEvent(BaseEvent):
    event_type: str = "order.filled"
    client_order_id: str
    exchange_order_id: str
    side: str
    filled_price: Decimal
    filled_quantity: Decimal
    fee: Decimal
    fee_asset: str
    is_maker: bool
