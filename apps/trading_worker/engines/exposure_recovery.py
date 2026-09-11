import logging
from decimal import Decimal
from domain.models import TargetExposure, RiskSnapshot, utc_now
from domain.enums import RecoveryActionType

logger = logging.getLogger("blessing.engines.exposure_recovery")

class ExposureRecoveryEngine:
    def __init__(self, drawdown_trigger_pct: Decimal = Decimal("2.5")):
        self.drawdown_trigger_pct = drawdown_trigger_pct

    def process(self, target: TargetExposure, risk: RiskSnapshot, current_position_qty: Decimal) -> TargetExposure:
        is_long = current_position_qty > 0
        is_short = current_position_qty < 0
        
        # Determine if inventory is toxic based on drawdown constraint
        is_toxic = risk.current_drawdown_pct >= self.drawdown_trigger_pct
        
        if not is_toxic:
            return target
            
        logger.warning("EXPOSURE RECOVERY TRIGGERED for %s (Drawdown: %s%%)", target.symbol, risk.current_drawdown_pct)
        
        new_target_delta = target.target_net_delta_qty
        
        # 1. Grid Brake: Prevent adding to toxic inventory
        if is_long and new_target_delta > 0:
            logger.info("Grid Brake: Blocking long exposure increase during recovery.")
            new_target_delta = Decimal("0.0")
        elif is_short and new_target_delta < 0:
            logger.info("Grid Brake: Blocking short exposure increase during recovery.")
            new_target_delta = Decimal("0.0")
            
        # 2. Progressive De-risking: Force reduction over hedging if toxicity is severe
        if risk.current_drawdown_pct > self.drawdown_trigger_pct * Decimal("1.5"):
            reduction = abs(current_position_qty) * Decimal("0.1")
            new_target_delta = -reduction if is_long else reduction
            logger.warning("Severe Toxicity: Forcing 10%% exposure reduction (Delta: %s)", new_target_delta)
            
        return TargetExposure(
            symbol=target.symbol,
            market_type=target.market_type,
            target_net_delta_qty=new_target_delta,
            target_gross_limit_qty=target.target_gross_limit_qty,
            strategy_attributions=target.strategy_attributions,
            created_at=target.created_at,
            expires_at=target.expires_at
        )
