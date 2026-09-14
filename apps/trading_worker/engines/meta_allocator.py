import logging
from decimal import Decimal
from typing import Dict, List, Optional
from domain.models import StrategyIntent, PositionSide

logger = logging.getLogger("blessing.engines.meta_allocator")

class MetaAllocator:
    """
    The Meta Allocator handles the continuous risk budget of active strategies.
    It resolves conflicts if strategies disagree, converting multiple intents 
    into a net target portfolio exposure.
    """
    
    def __init__(self, max_portfolio_gross: Decimal = Decimal("5.0")):
        self.max_portfolio_gross = max_portfolio_gross

    def aggregate_intents(self, intents: List[StrategyIntent], current_virtual_positions: Dict[str, Decimal]) -> Dict[str, Decimal]:
        """
        Takes a batch of active intents and current virtual positions, and determines
        the new aggregated net target delta per symbol.
        """
        symbol_deltas: Dict[str, Decimal] = {}
        
        for intent in intents:
            sym = intent.symbol
            if sym not in symbol_deltas:
                symbol_deltas[sym] = Decimal("0.0")
                
            # Weight the requested delta by opportunity score and confidence
            # A score of 0.8 and confidence 0.5 gives a multiplier of 0.4
            weight = intent.opportunity_score * intent.confidence
            
            # Apply the weighted delta
            actual_delta = intent.desired_delta_qty * weight
            
            symbol_deltas[sym] += actual_delta
            
            logger.debug(f"[{sym}] {intent.strategy_id} intent: {intent.desired_delta_qty} * {weight:.2f} = {actual_delta:.4f}")
            
        return symbol_deltas
