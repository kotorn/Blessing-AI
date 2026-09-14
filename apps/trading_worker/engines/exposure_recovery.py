import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Dict, Optional, Tuple
from domain.models import TargetExposure, RiskSnapshot, utc_now
from domain.enums import RecoveryActionType

logger = logging.getLogger("blessing.engines.exposure_recovery")

class RecoveryState:
    def __init__(self, clock: Callable[[], datetime]):
        self.is_active: bool = False
        self.action_type: RecoveryActionType = RecoveryActionType.HOLD
        self.hedge_ratio: Decimal = Decimal("0.0")
        self.last_update = clock()
        self.locked_toxicity_level: Decimal = Decimal("0.0")
        
    def reset(self):
        self.is_active = False
        self.action_type = RecoveryActionType.HOLD
        self.hedge_ratio = Decimal("0.0")
        self.locked_toxicity_level = Decimal("0.0")

class ExposureRecoveryEngine:
    def __init__(
        self,
        drawdown_trigger_pct: Decimal = Decimal("2.0"),
        max_hedge_ratio: Decimal = Decimal("0.8"),
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.drawdown_trigger_pct = drawdown_trigger_pct
        self.max_hedge_ratio = max_hedge_ratio
        self.states: Dict[str, RecoveryState] = {}
        self._clock = clock or utc_now

    def _get_or_create_state(self, symbol: str) -> RecoveryState:
        if symbol not in self.states:
            self.states[symbol] = RecoveryState(self._clock)
        return self.states[symbol]

    def _determine_action(self, state: RecoveryState, risk: RiskSnapshot, current_qty: Decimal, target_delta: Decimal) -> Tuple[RecoveryActionType, Decimal]:
        """
        Always compare: 'Open opposite hedge' versus 'Reduce existing exposure' before increasing gross positions.
        """
        # If drawdown is catastrophic, emergency flatten
        if risk.current_drawdown_pct >= self.drawdown_trigger_pct * Decimal("2.5"):
            return RecoveryActionType.EMERGENCY_FLATTEN, Decimal("-1.0")

        # If drawdown is severe, progressive unwind (reducing existing exposure)
        if risk.current_drawdown_pct >= self.drawdown_trigger_pct * Decimal("1.5"):
            return RecoveryActionType.PROGRESSIVE_UNWIND, Decimal("-0.25")

        # If just over the threshold, dynamic hedge or hold
        # In this MVP, if we want to hedge, we check if target_delta is already hedging
        is_long = current_qty > 0
        is_short = current_qty < 0
        
        # If strategy wants to reduce exposure, allow it (harvesting or natural reduction)
        if (is_long and target_delta < 0) or (is_short and target_delta > 0):
            return RecoveryActionType.HARVEST_HEDGE, target_delta
            
        # Otherwise, mandate a partial hedge or hold
        # Prefer reducing existing exposure over opening counter-exposure (gross expansion)
        # But for dynamic state, we will apply a partial hedge constraint
        return RecoveryActionType.PARTIAL_HEDGE, Decimal("0.3")

    def process(self, target: TargetExposure, risk: RiskSnapshot, current_position_qty: Decimal, volatility_factor: Decimal = Decimal("1.0")) -> TargetExposure:
        symbol = target.symbol
        state = self._get_or_create_state(symbol)
        
        is_long = current_position_qty > 0
        is_short = current_position_qty < 0
        abs_qty = abs(current_position_qty)
        
        is_toxic = risk.current_drawdown_pct >= self.drawdown_trigger_pct
        
        # State transitions
        if not is_toxic and not state.is_active:
            return target
            
        if not is_toxic and state.is_active:
            # Recovery is complete, progressively re-risk
            logger.info("Recovery complete for %s. Resetting state.", symbol)
            state.reset()
            return target
            
        if is_toxic and not state.is_active:
            logger.warning("EXPOSURE RECOVERY TRIGGERED for %s (Drawdown: %s%%)", symbol, risk.current_drawdown_pct)
            state.is_active = True
            state.locked_toxicity_level = risk.current_drawdown_pct
            
        state.last_update = self._clock()

        # A recovery state must never manufacture exposure.  With no existing
        # position there is nothing to hedge or unwind, so every strategy
        # target is treated as NEW_RISK and suppressed until drawdown recovers.
        if abs_qty == 0:
            state.action_type = RecoveryActionType.HOLD
            logger.info(
                "Recovery is blocking new exposure for flat symbol %s until drawdown recovers",
                symbol,
            )
            return TargetExposure(
                symbol=target.symbol,
                market_type=target.market_type,
                target_net_delta_qty=Decimal("0.0"),
                target_gross_limit_qty=target.target_gross_limit_qty,
                strategy_attributions=target.strategy_attributions,
                created_at=target.created_at,
                expires_at=target.expires_at,
                exposure_id=target.exposure_id,
                source_intent_ids=target.source_intent_ids,
            )
        
        # Grid Brake: ALWAYS prevent adding to toxic inventory
        new_target_delta = target.target_net_delta_qty
        if is_long and new_target_delta > 0:
            logger.info("Grid Brake: Blocking long exposure increase during recovery.")
            new_target_delta = Decimal("0.0")
        elif is_short and new_target_delta < 0:
            logger.info("Grid Brake: Blocking short exposure increase during recovery.")
            new_target_delta = Decimal("0.0")

        # Determine next action
        action, intensity = self._determine_action(state, risk, current_position_qty, new_target_delta)
        state.action_type = action

        if action == RecoveryActionType.EMERGENCY_FLATTEN:
            logger.warning("EMERGENCY FLATTEN for %s", symbol)
            new_target_delta = -current_position_qty
            
        elif action == RecoveryActionType.PROGRESSIVE_UNWIND:
            # Reduce existing position by a percentage intensity
            reduction = abs_qty * abs(intensity)
            new_target_delta = -reduction if is_long else reduction
            logger.warning("Progressive Unwind: Forcing %s exposure reduction (Delta: %s)", intensity * 100, new_target_delta)
            
        elif action == RecoveryActionType.PARTIAL_HEDGE:
            # Calculate required hedge delta based on volatility
            required_hedge_ratio = min(self.max_hedge_ratio, abs(intensity) * volatility_factor)
            required_hedge_qty = abs_qty * required_hedge_ratio
            
            # Since we prefer reducing over hedging, if we are long, target_delta should be negative
            # If target_delta is already more negative than required, we keep it
            hedge_delta = -required_hedge_qty if is_long else required_hedge_qty
            
            if (is_long and new_target_delta > hedge_delta) or (is_short and new_target_delta < hedge_delta):
                logger.info("Applying Dynamic Counter-Exposure / Hedge (Ratio: %s, Delta: %s)", required_hedge_ratio, hedge_delta)
                new_target_delta = hedge_delta
                
        elif action == RecoveryActionType.HARVEST_HEDGE:
            # Keep the strategy's desired reduction
            pass

        return TargetExposure(
            symbol=target.symbol,
            market_type=target.market_type,
            target_net_delta_qty=new_target_delta,
            target_gross_limit_qty=target.target_gross_limit_qty,
            strategy_attributions=target.strategy_attributions,
            created_at=target.created_at,
            expires_at=target.expires_at,
            exposure_id=target.exposure_id,
            source_intent_ids=target.source_intent_ids,
        )
