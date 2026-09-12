import logging
from decimal import Decimal
from typing import List, Dict
from domain.models import StrategyIntent, TargetExposure, MarketType, utc_now
from datetime import timedelta

logger = logging.getLogger("blessing.engines.meta_allocator")

class MetaAllocator:
    def __init__(self, max_gross_exposure_btc: Decimal = Decimal("2.0")):
        self.max_gross_exposure_btc = max_gross_exposure_btc

    def allocate(self, intents: List[StrategyIntent], symbol: str) -> TargetExposure:
        if not intents:
            return TargetExposure(
                symbol=symbol,
                market_type=MarketType.USDM_FUTURES,
                target_net_delta_qty=Decimal("0.0"),
                target_gross_limit_qty=Decimal("0.0"),
                strategy_attributions={},
                expires_at=utc_now() + timedelta(seconds=60)
            )

        net_delta = Decimal("0.0")
        gross_exposure = Decimal("0.0")
        attributions: Dict[str, Decimal] = {}
        source_intent_ids: List[str] = []
        
        # In a production setup, we apply correlation matrices and risk parity here.
        # For Blessing AI MVP, we resolve conflicts by netting intent deltas weighted by confidence.
        
        for intent in intents:
            # Scale intent by its confidence and opportunity score
            # A brake intent (0.0 delta) naturally contributes 0 to net_delta, but we might want it to reduce gross.
            weight = intent.confidence * intent.opportunity_score
            effective_delta = intent.desired_delta_qty * weight
            
            source_intent_ids.append(intent.intent_id)
            attributions[intent.strategy_id] = (
                attributions.get(intent.strategy_id, Decimal("0.0")) + effective_delta
            )
            net_delta += effective_delta
            gross_exposure += abs(effective_delta)
            
        # Apply portfolio constraints (simplified)
        if gross_exposure > self.max_gross_exposure_btc:
            scale_factor = self.max_gross_exposure_btc / gross_exposure
            net_delta *= scale_factor
            gross_exposure = self.max_gross_exposure_btc
            for k in attributions:
                attributions[k] *= scale_factor
                
        return TargetExposure(
            symbol=symbol,
            market_type=intents[0].market_type if intents else MarketType.USDM_FUTURES,
            target_net_delta_qty=net_delta,
            target_gross_limit_qty=gross_exposure,
            strategy_attributions=attributions,
            expires_at=utc_now() + timedelta(seconds=60),
            source_intent_ids=source_intent_ids,
        )
