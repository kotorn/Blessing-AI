"""
Domain Interfaces for Blessing AI v0.2
Pure abstract contracts. Strategies and Risk Governors must never import exchange SDKs.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Callable, Coroutine
from decimal import Decimal
from domain.models import (
    StrategyIntent,
    MarketState,
    PriceActionState,
    Basket,
    Position,
    RiskBudget,
    TargetExposure,
    RiskSnapshot,
    RecoveryIntent,
    OrderIntent,
    Instrument,
)


class StrategyEngine(ABC):
    """
    Common Strategy Interface.
    Every alpha engine (Grid, Trend, Shock, Carry) evaluates state and emits StrategyIntent.
    They NEVER directly submit orders.
    """

    @property
    @abstractmethod
    def strategy_id(self) -> str:
        pass

    @abstractmethod
    def evaluate(
        self,
        symbol: str,
        market_state: MarketState,
        price_action: PriceActionState,
        active_baskets: Dict[str, Basket],
        current_positions: Dict[str, Position],
        context: Optional[Dict[str, Any]] = None,
    ) -> StrategyIntent:
        """Evaluate market inputs and return strategic desired delta and score."""
        pass


class VenueAdapter(ABC):
    """
    Abstract Exchange Adapter Interface.
    Decouples all internal algorithms from venue-specific REST & WebSocket protocols.
    """

    @abstractmethod
    async def connect(self) -> None:
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        pass

    @abstractmethod
    async def get_instrument(self, symbol: str) -> Instrument:
        pass

    @abstractmethod
    async def get_positions(self) -> List[Position]:
        pass

    @abstractmethod
    async def submit_order(self, order_intent: OrderIntent) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def cancel_order(self, symbol: str, client_order_id: str) -> bool:
        pass

    @abstractmethod
    async def emergency_flatten(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        pass


class RiskGovernor(ABC):
    """
    Portfolio Risk Governor Interface.
    Has supreme veto authority over all strategies and ML models.
    """

    @abstractmethod
    def evaluate_portfolio_risk(
        self,
        equity: Decimal,
        positions: Dict[str, Position],
        intents: List[StrategyIntent],
        context: Optional[Dict[str, Any]] = None,
    ) -> RiskSnapshot:
        pass

    @abstractmethod
    def enforce_limits(
        self,
        target_exposure: TargetExposure,
        current_snapshot: RiskSnapshot,
    ) -> TargetExposure:
        pass


class ExposureRecoveryEngine(ABC):
    """
    Exposure Recovery Engine Interface.
    Decides when and how to deleverage, partially hedge, or harvest profits.
    """

    @abstractmethod
    def evaluate_basket(
        self,
        basket: Basket,
        market_state: MarketState,
        price_action: PriceActionState,
        risk_snapshot: RiskSnapshot,
    ) -> Optional[RecoveryIntent]:
        pass
