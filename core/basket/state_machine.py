"""
Basket State Machine for Blessing AI v0.1
Enforces strict transitions between basket lifecycle states.
Guarantees fail-closed safety and deterministic recovery.
"""

from decimal import Decimal
from typing import Optional, Tuple
import structlog
from core.basket.models import Basket, BasketState, RiskState

logger = structlog.get_logger()


class InvalidStateTransitionError(Exception):
    pass


class BasketStateMachine:
    """
    Validates and executes transitions according to the state diagram:

    [NEW] -> [ACTIVE]
       │         │
       │         ├──> [GRID_EXPANDING] ──> [ACTIVE]
       │         ├──> [PROFITABLE] ──────> [CLOSING] ──> [CLOSED]
       │         ├──> [RECOVERY] ────────> [CLOSING] / [DELEVERAGING]
       │         ├──> [NO_NEW_GRID]
       │         └──> [EMERGENCY_EXIT] ──> [CLOSED]
    """

    ALLOWED_TRANSITIONS = {
        BasketState.NEW: {BasketState.ACTIVE, BasketState.EMERGENCY_EXIT},
        BasketState.ACTIVE: {
            BasketState.GRID_EXPANDING,
            BasketState.PROFITABLE,
            BasketState.RECOVERY,
            BasketState.NO_NEW_GRID,
            BasketState.CLOSING,
            BasketState.EMERGENCY_EXIT,
        },
        BasketState.GRID_EXPANDING: {
            BasketState.ACTIVE,
            BasketState.PROFITABLE,
            BasketState.RECOVERY,
            BasketState.NO_NEW_GRID,
            BasketState.EMERGENCY_EXIT,
        },
        BasketState.PROFITABLE: {
            BasketState.CLOSING,
            BasketState.ACTIVE,  # If price slips back before fill
            BasketState.EMERGENCY_EXIT,
        },
        BasketState.RECOVERY: {
            BasketState.PROFITABLE,
            BasketState.DELEVERAGING,
            BasketState.CLOSING,
            BasketState.EMERGENCY_EXIT,
        },
        BasketState.NO_NEW_GRID: {
            BasketState.RECOVERY,
            BasketState.PROFITABLE,
            BasketState.CLOSING,
            BasketState.DELEVERAGING,
            BasketState.EMERGENCY_EXIT,
        },
        BasketState.DELEVERAGING: {
            BasketState.RECOVERY,
            BasketState.CLOSING,
            BasketState.EMERGENCY_EXIT,
        },
        BasketState.CLOSING: {
            BasketState.CLOSED,
            BasketState.EMERGENCY_EXIT,
        },
        BasketState.EMERGENCY_EXIT: {
            BasketState.CLOSED,
        },
        BasketState.CLOSED: set(),  # Terminal state
    }

    @classmethod
    def can_transition(cls, current_state: BasketState, target_state: BasketState) -> bool:
        return target_state in cls.ALLOWED_TRANSITIONS.get(current_state, set())

    @classmethod
    def transition(
        cls,
        basket: Basket,
        target_state: BasketState,
        reason: str,
        risk_state: RiskState = RiskState.NORMAL,
    ) -> Tuple[bool, str]:
        """
        Attempts to transition basket state with safety invariants.
        """
        current_state = basket.state

        # Invariant 1: Terminal state cannot transition
        if current_state == BasketState.CLOSED:
            return False, "Cannot transition closed basket"

        # Invariant 2: EMERGENCY_EXIT can override any active state
        if target_state == BasketState.EMERGENCY_EXIT:
            basket.state = BasketState.EMERGENCY_EXIT
            logger.warn("basket_emergency_exit", basket_id=basket.basket_id, reason=reason)
            return True, "Emergency exit initiated"

        # Invariant 3: If Risk Governor dictates NO_NEW_GRID or RECOVERY_ONLY, block expansion
        if target_state == BasketState.GRID_EXPANDING:
            if risk_state in [RiskState.NO_NEW_GRID, RiskState.RECOVERY_ONLY, RiskState.DELEVERAGE, RiskState.EMERGENCY]:
                basket.state = BasketState.NO_NEW_GRID
                logger.warn("grid_expansion_blocked_by_risk_governor", basket_id=basket.basket_id, risk_state=risk_state)
                return False, f"Risk governor blocked expansion: {risk_state}"

            if basket.grid_depth >= basket.max_grid_levels:
                basket.state = BasketState.NO_NEW_GRID
                return False, "Max grid levels reached; transitioning to NO_NEW_GRID"

        # Check allowed transitions
        if not cls.can_transition(current_state, target_state):
            err = f"Illegal transition from {current_state} to {target_state}"
            logger.error("invalid_state_transition", error=err, basket_id=basket.basket_id)
            return False, err

        basket.state = target_state
        logger.info(
            "basket_state_changed",
            basket_id=basket.basket_id,
            from_state=current_state,
            to_state=target_state,
            reason=reason,
        )
        return True, "Success"
