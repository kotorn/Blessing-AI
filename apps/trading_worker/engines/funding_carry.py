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
        # Funding is an economic input, not a value that can be safely inferred.
        # Without a current exchange funding event there is no carry edge to
        # evaluate, so fail closed and emit no intent.
        funding_rate = event.funding_rate
        if funding_rate is None or not funding_rate.is_finite():
            logger.info("Skipping carry intent for %s: funding rate is unavailable.", event.symbol)
            return None
        
        # 1. Gross Annualized Basis
        gross_annualized_pct = funding_rate * 3 * 365 * 100
        
        # 2. Costs (Taker/Maker fees, round-trip spread, slippage, financing cost)
        # Using typical Binance USD-M metrics:
        # Maker fee: 0.02%, Taker fee: 0.05%
        # Assumed round-trip execution (1 maker, 1 taker) = 0.07%
        # Slippage + Spread impact = ~0.05%
        # Capital financing (opportunity cost) = ~5.0% annually
        round_trip_friction_pct = Decimal("0.12")
        capital_financing_pct = Decimal("5.0")
        
        # We assume 1 trade per week (52 trades/year) to maintain the carry position
        annual_friction_pct = round_trip_friction_pct * Decimal("52.0")
        
        net_annualized_pct = abs(gross_annualized_pct) - annual_friction_pct - capital_financing_pct
        
        if net_annualized_pct < self.min_annualized_yield:
            return None # Insufficient net yield to justify carry after costs
            
        direction = PositionSide.SHORT if funding_rate > 0 else PositionSide.LONG
        
        return StrategyIntent(
            intent_id=f"CARRY-INTENT-{utc_now().timestamp()}",
            strategy_id=self.strategy_id,
            symbol=event.symbol,
            market_type=MarketType.USDM_FUTURES,
            direction=direction,
            desired_delta_qty=Decimal("-0.1") if direction == PositionSide.SHORT else Decimal("0.1"),
            opportunity_score=Decimal("0.8"),
            confidence=Decimal("0.85"),
            expected_holding_horizon_sec=86400 * 7, # 7 days holding horizon expected
            evidence={
                "gross_annualized_pct": str(gross_annualized_pct),
                "net_annualized_pct": str(net_annualized_pct),
                "raw_funding": str(funding_rate)
            }
        )
