import logging
from decimal import Decimal
from datetime import UTC
from typing import Dict, List, Optional
from domain.models import MarketEvent, PriceActionState

logger = logging.getLogger("blessing.engines.price_action")

class PriceActionEngine:
    def __init__(self, atr_period: int = 14):
        self.atr_period = atr_period
        self.price_history: Dict[str, List[Decimal]] = {}
        self.last_state: Dict[str, PriceActionState] = {}
        
    def process_event(self, event: MarketEvent) -> Optional[PriceActionState]:
        sym = event.symbol
        if sym not in self.price_history:
            self.price_history[sym] = []
            
        self.price_history[sym].append(event.last_price)
        # Keep rolling window bounded
        if len(self.price_history[sym]) > 1000:
            self.price_history[sym].pop(0)
            
        # We need a meaningful displacement baseline, simulated here using a simple lookback
        history = self.price_history[sym]
        
        if len(history) < 2:
            return None
            
        current = history[-1]
        previous = history[-2]
        
        # Microstructure calculations
        displacement = current - previous
        velocity_pct = (displacement / previous) * 100 if previous else Decimal("0")
        
        # We define a rolling window for 24h high/low and swing structure
        # In a real environment, we would aggregate bars. For this stream, we use local extrema.
        window = history[-min(len(history), 60):]  # e.g., last 60 ticks/events
        local_high = max(window)
        local_low = min(window)
        
        prior_24h_high = max(history)
        prior_24h_low = min(history)
        
        # Simplistic range expansion ratio
        range_expansion = Decimal("0")
        if prior_24h_high > prior_24h_low:
            range_expansion = (local_high - local_low) / (prior_24h_high - prior_24h_low)
            
        is_sweep = False
        is_reclaim = False
        
        # Liquidity Sweep Logic: Price poked below support (prior_24h_low) but immediately closed back above
        if current > prior_24h_low and previous <= prior_24h_low:
            is_sweep = True
            is_reclaim = True
            
        event_timestamp = event.event_time
        if event_timestamp.tzinfo is None:
            event_timestamp = event_timestamp.replace(tzinfo=UTC)
        else:
            event_timestamp = event_timestamp.astimezone(UTC)

        state = PriceActionState(
            symbol=sym,
            # Historical replay and live lineage must use the exchange event
            # time, not process wall-clock time. This keeps WFO chronology
            # and downstream intents traceable to the observed event.
            timestamp=event_timestamp,
            swing_high=local_high,
            swing_low=local_low,
            prior_24h_high=prior_24h_high,
            prior_24h_low=prior_24h_low,
            displacement_velocity_pct=velocity_pct,
            displacement_acceleration=Decimal("0.0"), # requires 2nd derivative tracking
            range_expansion_ratio=range_expansion,
            liquidity_swept=is_sweep,
            is_reclaiming=is_reclaim
        )
        
        self.last_state[sym] = state
        return state
