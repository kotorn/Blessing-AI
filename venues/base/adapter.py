"""
VenueAdapter Abstract Base Interface
Defines the strict decoupled abstraction layer between Strategy/Risk engines
and any exchange venue (Binance Global, Binance TH, Bybit, OKX, MT5, Pepperstone, OANDA).
"""

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Callable, Coroutine, Dict, List, Optional
from pydantic import BaseModel
from datetime import datetime


class SymbolInfo(BaseModel):
    symbol: str
    venue: str
    market_type: str  # SPOT, USDM_PERP, COINM_PERP
    base_asset: str
    quote_asset: str
    contract_size: Decimal
    price_precision: int
    quantity_precision: int
    tick_size: Decimal
    step_size: Decimal
    min_notional: Decimal
    max_leverage: int


class BalanceInfo(BaseModel):
    asset: str
    wallet_balance: Decimal
    available_balance: Decimal
    unrealized_pnl: Decimal
    margin_balance: Decimal


class PositionInfo(BaseModel):
    symbol: str
    direction: str  # LONG, SHORT, FLAT
    quantity: Decimal
    entry_price: Decimal
    mark_price: Decimal
    liquidation_price: Optional[Decimal]
    unrealized_pnl: Decimal
    leverage: Decimal
    margin_type: str
    isolated_margin: Decimal
    updated_at: datetime


class OrderRequest(BaseModel):
    client_order_id: str
    symbol: str
    side: str  # BUY, SELL
    order_type: str  # LIMIT, MARKET, STOP_MARKET
    quantity: Decimal
    price: Optional[Decimal] = None
    time_in_force: str = "GTC"
    reduce_only: bool = False
    post_only: bool = False


class OrderResponse(BaseModel):
    client_order_id: str
    exchange_order_id: str
    symbol: str
    status: str  # NEW, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED
    price: Optional[Decimal]
    quantity: Decimal
    filled_quantity: Decimal
    avg_fill_price: Optional[Decimal]
    fee: Decimal = Decimal("0.0")
    fee_asset: Optional[str] = None
    transact_time: datetime


class FundingInfo(BaseModel):
    symbol: str
    funding_rate: Decimal
    funding_time: datetime
    mark_price: Decimal
    index_price: Decimal
    estimated_settle_rate: Optional[Decimal] = None


class VenueAdapter(ABC):
    """
    Abstract Exchange Adapter.
    Strategy & Risk Governors ONLY program against this interface.
    Never import exchange-specific SDKs into Strategy or Basket modules.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Initialize HTTP sessions and WebSocket connections with authentication."""
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully disconnect all connections and active subscriptions."""
        pass

    @abstractmethod
    async def subscribe_trades(self, symbol: str, callback: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        """Stream real-time public trade events."""
        pass

    @abstractmethod
    async def subscribe_orderbook(self, symbol: str, depth: int, callback: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        """Stream level-2 orderbook updates."""
        pass

    @abstractmethod
    async def subscribe_mark_price(self, symbol: str, callback: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        """Stream real-time mark price updates (derivatives only)."""
        pass

    @abstractmethod
    async def subscribe_account(self, callback: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        """Stream private user data / balance / margin events."""
        pass

    @abstractmethod
    async def subscribe_orders(self, callback: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]) -> None:
        """Stream private execution reports / order fill updates."""
        pass

    @abstractmethod
    async def get_symbol_info(self, symbol: str) -> SymbolInfo:
        """Fetch normalized symbol metadata, tick size, step size, limits."""
        pass

    @abstractmethod
    async def get_balance(self, asset: str) -> BalanceInfo:
        """Query asset balance."""
        pass

    @abstractmethod
    async def get_position(self, symbol: str) -> PositionInfo:
        """Query position for a single instrument."""
        pass

    @abstractmethod
    async def get_positions(self) -> List[PositionInfo]:
        """Query all open positions on the account."""
        pass

    @abstractmethod
    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderResponse]:
        """Query currently active resting orders."""
        pass

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Submit a deterministic order with unique client_order_id."""
        pass

    @abstractmethod
    async def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        """Cancel a resting order."""
        pass

    @abstractmethod
    async def amend_order(self, symbol: str, client_order_id: str, new_price: Decimal, new_quantity: Decimal) -> OrderResponse:
        """Amend resting order price or quantity."""
        pass

    @abstractmethod
    async def emergency_flatten(self, symbol: Optional[str] = None) -> List[OrderResponse]:
        """Emergency circuit breaker: Cancel open orders and market close positions immediately."""
        pass


class DerivativesVenueAdapter(VenueAdapter):
    """Extended adapter interface for USDⓈ-M Futures, Perpetual Swaps, and Derivatives."""

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> FundingInfo:
        """Fetch current funding rate and settlement countdown."""
        pass

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> Decimal:
        """Fetch current index and mark price."""
        pass

    @abstractmethod
    async def get_index_price(self, symbol: str) -> Decimal:
        """Fetch spot index benchmark price."""
        pass

    @abstractmethod
    async def get_open_interest(self, symbol: str) -> Dict[str, Any]:
        """Fetch aggregated Open Interest in USD and contracts."""
        pass

    @abstractmethod
    async def get_liquidation_information(self, symbol: str) -> Dict[str, Any]:
        """Compute exact distance to liquidation from exchange margin state."""
        pass

    @abstractmethod
    async def get_margin_information(self) -> Dict[str, Any]:
        """Fetch total initial, maintenance, and used margin."""
        pass
