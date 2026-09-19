import logging
import math
import collections
from decimal import Decimal
from datetime import datetime, UTC
from typing import Dict, Optional

from domain.models import MarketEvent, PriceActionState

logger = logging.getLogger("blessing.engines.price_action")

class EWMA:
    def __init__(self, alpha: float):
        self.alpha = alpha
        self.value = None

    def update(self, val: float) -> float:
        if self.value is None:
            self.value = val
        else:
            self.value = self.alpha * val + (1.0 - self.alpha) * self.value
        return self.value

class InstrumentTracker:
    def __init__(self):
        # 1-minute tracking (alpha for ~60 updates assuming 1 tick/sec)
        self.price_ema = EWMA(2.0 / (60 + 1))
        self.var_ema = EWMA(2.0 / (60 + 1))
        self.vel_ema = EWMA(2.0 / (60 + 1))
        self.accel_ema = EWMA(2.0 / (60 + 1))
        
        # 1-hour tracking for baseline ATR/Volatility proxy (alpha for ~3600 updates)
        self.long_var_ema = EWMA(2.0 / (3600 + 1))
        
        self.ticks = collections.deque(maxlen=5000)
        
        self.prior_24h_high = Decimal("-inf")
        self.prior_24h_low = Decimal("inf")
        
        self.last_velocity = 0.0

    def process(self, timestamp: datetime, price_d: Decimal) -> Optional[PriceActionState]:
        price = float(price_d)
        self.ticks.append((timestamp, price))
        
        old_prior_high = self.prior_24h_high
        old_prior_low = self.prior_24h_low

        if self.prior_24h_high == Decimal("-inf"):
            self.prior_24h_high = price_d
            self.prior_24h_low = price_d
            old_prior_high = price_d
            old_prior_low = price_d
            
        # Update session highs/lows crudely for the streaming context
        if price_d > self.prior_24h_high:
            self.prior_24h_high = price_d
        if price_d < self.prior_24h_low:
            self.prior_24h_low = price_d
            
        # EWMA Updates
        prev_ema = self.price_ema.value if self.price_ema.value is not None else price
        curr_ema = self.price_ema.update(price)
        
        diff = price - curr_ema
        self.var_ema.update(diff * diff)
        self.long_var_ema.update(diff * diff)
        
        # Velocity as rate of change of EMA
        velocity = (curr_ema - prev_ema) / prev_ema if prev_ema > 0 else 0.0
        # Normalize to % per minute roughly assuming 1 tick/sec -> * 60 * 100
        velocity_pct_min = velocity * 6000.0 
        
        curr_vel = self.vel_ema.update(velocity_pct_min)
        
        # Acceleration as rate of change of velocity
        accel = curr_vel - self.last_velocity
        self.last_velocity = curr_vel
        curr_accel = self.accel_ema.update(accel)
        
        # Standard deviation of price proxy:
        long_stdev = math.sqrt(self.long_var_ema.value) if self.long_var_ema.value else 0.0
        short_stdev = math.sqrt(self.var_ema.value) if self.var_ema.value else 0.0
        
        # Range expansion ratio = short-term volatility / long-term volatility
        range_expansion = (short_stdev / long_stdev) if long_stdev > 0 else 0.0
        
        # Local Swing High/Low over recent ticks (e.g., last 60 events)
        recent_window = list(self.ticks)[-min(len(self.ticks), 60):]
        local_high = Decimal(str(max(p for t, p in recent_window)))
        local_low = Decimal(str(min(p for t, p in recent_window)))
        
        # Liquidity Sweep logic
        is_sweep = False
        is_reclaim = False
        
        if len(self.ticks) > 1:
            prev_price = Decimal(str(self.ticks[-2][1]))
            prior_recent = [Decimal(str(p)) for t, p in recent_window[:-1]]
            prior_local_low = min(prior_recent) if len(prior_recent) >= 2 else old_prior_low
            prior_local_high = max(prior_recent) if len(prior_recent) >= 2 else old_prior_high

            swept_low = (price_d > old_prior_low and prev_price <= old_prior_low) or (
                price_d > prior_local_low and prev_price <= prior_local_low
            )
            swept_high = (price_d < old_prior_high and prev_price >= old_prior_high) or (
                price_d < prior_local_high and prev_price >= prior_local_high
            )

            if swept_low:
                is_sweep = True
                is_reclaim = True
            elif swept_high:
                is_sweep = True
                is_reclaim = False

        state = PriceActionState(
            symbol="", # Overridden by caller
            timestamp=timestamp,
            swing_high=local_high,
            swing_low=local_low,
            prior_24h_high=self.prior_24h_high,
            prior_24h_low=self.prior_24h_low,
            displacement_velocity_pct=Decimal(f"{curr_vel:.4f}"),
            displacement_acceleration=Decimal(f"{curr_accel:.4f}"),
            range_expansion_ratio=Decimal(f"{range_expansion:.4f}"),
            liquidity_swept=is_sweep,
            is_reclaiming=is_reclaim
        )
        return state

class PriceActionEngine:
    def __init__(self, atr_period: int = 14):
        self.atr_period = atr_period
        self.trackers: Dict[str, InstrumentTracker] = {}
        self.last_state: Dict[str, PriceActionState] = {}

    def process_event(self, event: MarketEvent) -> Optional[PriceActionState]:
        sym = event.symbol
        event_timestamp = event.event_time
        if event_timestamp.tzinfo is None:
            logger.warning("Rejecting market event with a naive timestamp for %s", sym)
            return None
        event_timestamp = event_timestamp.astimezone(UTC)

        if sym not in self.trackers:
            self.trackers[sym] = InstrumentTracker()
            
        tracker = self.trackers[sym]
        state = tracker.process(event_timestamp, event.last_price)

        # A single observation has no displacement or prior-tick context. It
        # seeds the tracker, but must not enter the strategy pipeline as a
        # tradeable market state.
        if len(tracker.ticks) < 2:
            return None
        
        if state:
            state = state.model_copy(update={"symbol": sym})
            self.last_state[sym] = state
            
        return state
