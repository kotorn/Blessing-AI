"""Experimental strategy stack retained for research only.

Production execution uses the engines under ``apps.trading_worker.engines``.
"""

RESEARCH_ONLY = True

import abc
import logging
from decimal import Decimal
from typing import Dict, List, Optional
from domain.models import MarketState, StrategyIntent

logger = logging.getLogger("blessing.strategies.base")

class BaseAlphaEngine(abc.ABC):
    """
    Base contract for all Alpha Engines (Grid, Trend, Shock, Funding).
    Strategies must never place exchange orders directly.
    They produce StrategyIntents based on MarketState.
    """
    
    def __init__(self, strategy_id: str, symbol: str):
        self.strategy_id = strategy_id
        self.symbol = symbol

    @abc.abstractmethod
    def evaluate(self, state: MarketState, current_position: Decimal) -> Optional[StrategyIntent]:
        """
        Evaluate the current market state and return a target intent.
        
        :param state: The current deterministic market state
        :param current_position: The strategy's currently attributed virtual position (not the portfolio total)
        """
        pass
