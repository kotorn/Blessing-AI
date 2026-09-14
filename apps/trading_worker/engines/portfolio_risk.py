"""Research-only portfolio risk prototype.

The production risk authority is ``RiskGovernor``. This module is retained for
offline comparison and must not be imported by the worker bootstrap.
"""

RESEARCH_ONLY = True

import logging
from decimal import Decimal
from typing import Dict, Tuple

logger = logging.getLogger("blessing.engines.risk")

class PortfolioRiskGovernor:
    """
    The Portfolio Risk Governor sits above all strategies and allocators.
    It determines the actual economic exposure based on hard constraints.
    AI/ML/Strategies must never override these constraints.
    """
    
    def __init__(self, max_gross_exposure: Decimal = Decimal("5.0"), max_net_exposure: Decimal = Decimal("3.0")):
        self.max_gross_exposure = max_gross_exposure
        self.max_net_exposure = max_net_exposure
        self.emergency_halt = False

    def check_and_cap_exposure(self, 
                               current_physical_net: Decimal, 
                               current_physical_gross: Decimal, 
                               target_deltas: Dict[str, Decimal]) -> Dict[str, Decimal]:
        """
        Takes the net target deltas from the MetaAllocator and applies physical 
        portfolio risk limits.
        """
        approved_deltas: Dict[str, Decimal] = {}
        
        if self.emergency_halt:
            logger.warning("Emergency halt active. All new exposure rejected.")
            # We can still allow reducing exposure
            for sym, delta in target_deltas.items():
                if (current_physical_net > 0 and delta < 0) or (current_physical_net < 0 and delta > 0):
                    approved_deltas[sym] = delta
            return approved_deltas

        # For simplistic handling, we process one symbol at a time right now
        # A true cross-asset risk engine would calculate the vector sum here.
        
        for sym, delta in target_deltas.items():
            proposed_net = current_physical_net + delta
            proposed_gross = current_physical_gross + abs(delta)
            
            # If the trade reduces gross exposure (e.g. closing a long), always allow it
            if abs(proposed_net) < abs(current_physical_net):
                approved_deltas[sym] = delta
                continue
                
            # If adding exposure, check hard limits
            if proposed_gross > self.max_gross_exposure:
                logger.warning(f"Risk Governor rejecting {delta} for {sym}: Gross exposure limit breached ({proposed_gross} > {self.max_gross_exposure})")
                continue
                
            if abs(proposed_net) > self.max_net_exposure:
                logger.warning(f"Risk Governor rejecting {delta} for {sym}: Net exposure limit breached ({proposed_net} > {self.max_net_exposure})")
                continue
                
            approved_deltas[sym] = delta

        return approved_deltas
