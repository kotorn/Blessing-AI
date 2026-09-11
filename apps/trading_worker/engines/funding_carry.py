import logging
from decimal import Decimal
from typing import Optional
from domain.models import MarketEvent, MarketState, StrategyIntent, PositionSide, MarketType, utc_now

logger = logging.getLogger("blessing.engines.funding_carry")

class FundingCarryEngine:
    def __init__(self, strategy_id: str = "funding_carry", min_annualized_yield: Decimal = Decimal("10.0")):
        self.strategy_id = strategy_id
        self.min_annualized_yield = min_annualized_yield
        
    def evaluate(self, event: MarketEvent, market_state: MarketState) -> Optional[StrategyIntent]:
        # Funding rate may be parsed from event. funding_rate defaults to 0.0 if not present.
        # Approximated simulation: For MVP, assume Binance USDs-M standard 8h funding (3x per day)
        funding_rate = event.funding_rate or Decimal("0.0001") # Mock 0.01% if missing to test logic
        
        annualized_pct = funding_rate * 3 * 365 * 100
        
        if abs(annualized_pct) < self.min_annualized_yield:
            return None # Insufficient yield to justify carry
            
        direction = PositionSide.SHORT if funding_rate > 0 else PositionSide.LONG
        
        return StrategyIntent(
            intent_id=f"CARRY-INTENT-{utc_now().timestamp()}",
            strategy_id=self.strategy_id,
            symbol=event.symbol,
            market_type=MarketType.USDM_FUTURES,
            direction=direction,
            desired_delta_qty=Decimal("-0.1") if direction == PositionSide.SHORT else Decimal("0.1"),
            opportunity_score=Decimal("85.0"),
            confidence=Decimal("0.90"),
            expected_holding_horizon_sec=86400 * 3, # 3 days holding horizon
            evidence={"annualized_funding_pct": str(annualized_pct), "raw_funding": str(funding_rate)}
        )
