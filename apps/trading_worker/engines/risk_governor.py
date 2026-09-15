import logging
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
from domain.models import TargetExposure, RiskSnapshot, ExecutionDecision, OrderIntent, OrderSide, PositionSide, OrderType, TimeInForce, utc_now
from domain.enums import EconomicRiskClass, RiskState

logger = logging.getLogger("blessing.engines.risk_governor")

class RiskGovernor:
    _RISK_OFF_STATES = frozenset(
        {
            RiskState.NO_NEW_RISK,
            RiskState.RECOVERY_ONLY,
            RiskState.DELEVERAGE,
            RiskState.LIQUIDATING,
            RiskState.EMERGENCY,
        }
    )

    def __init__(
        self,
        max_leverage: Decimal = Decimal("2.0"),
        max_drawdown_pct: Decimal = Decimal("6.0"),
        hedge_mode: bool = False,
        max_margin_utilization_pct: Decimal = Decimal("70.0"),
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.max_leverage = max_leverage
        self.max_drawdown_pct = max_drawdown_pct
        self.max_margin_utilization_pct = max_margin_utilization_pct
        self.hedge_mode = hedge_mode
        self._clock = clock or utc_now
        self._sequence = 0

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise ValueError("RiskGovernor clock must return a datetime")
        if value.tzinfo is None:
            raise ValueError("RiskGovernor clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _next_id(self, prefix: str, timestamp: datetime | None = None) -> str:
        self._sequence += 1
        observed_at = timestamp or self._now()
        token = observed_at.strftime("%Y%m%dT%H%M%S%fZ")
        return f"{prefix}-{token}-{self._sequence:06d}"

    def evaluate(self, target: TargetExposure, risk_snapshot: RiskSnapshot, current_position_qty: Decimal) -> ExecutionDecision:
        """Turn one relative target delta into a gated execution decision.

        ``TargetExposure.target_net_delta_qty`` is intentionally a *relative*
        signed quantity for the current evaluation tick.  It is not an
        absolute target position.  Keeping that meaning here prevents a
        caller from accidentally doubling exposure while trying to reach an
        absolute target.
        """

        now = self._now()
        try:
            required_delta = Decimal(str(target.target_net_delta_qty))
            current_position_qty = Decimal(str(current_position_qty))
        except (InvalidOperation, TypeError, ValueError):
            return self._reject(target, "Target delta or current position is invalid")
        if not required_delta.is_finite() or not current_position_qty.is_finite():
            return self._reject(target, "Target delta or current position is non-finite")

        try:
            target_expiry = target.expires_at
            if target_expiry.tzinfo is None or target_expiry <= now:
                return self._reject(target, "Target exposure is expired or has no timezone")
        except (AttributeError, TypeError):
            return self._reject(target, "Target exposure expiry is invalid")

        # De-risking must remain available during a hard-stop state so the
        # system can flatten rather than becoming trapped with toxic inventory.
        is_reducing = self._is_position_reduction(required_delta, current_position_qty)

        try:
            risk_state = (
                risk_snapshot.risk_state
                if isinstance(risk_snapshot.risk_state, RiskState)
                else RiskState(str(risk_snapshot.risk_state))
            )
        except (AttributeError, TypeError, ValueError):
            # Unknown exchange/risk state can never authorize new exposure,
            # but a known signed reduction remains available for recovery.
            if not is_reducing:
                return self._reject(target, "Risk state is unknown; new risk is blocked")
            risk_state = None

        for field_name in (
            "current_drawdown_pct",
            "effective_leverage",
            "margin_utilization_pct",
        ):
            try:
                value = Decimal(str(getattr(risk_snapshot, field_name)))
            except (AttributeError, InvalidOperation, TypeError, ValueError):
                return self._reject(target, f"Risk snapshot field is invalid: {field_name}")
            if not value.is_finite() or value < 0:
                return self._reject(target, f"Risk snapshot field is invalid: {field_name}")

        # 1. Hard Constraints
        if risk_state in self._RISK_OFF_STATES:
            if not is_reducing:
                return self._reject(
                    target,
                    f"Risk state is {risk_state.value}; new or increased risk is blocked.",
                )
            
        if risk_snapshot.current_drawdown_pct >= self.max_drawdown_pct:
            if not is_reducing:
                return self._reject(target, f"Drawdown ({risk_snapshot.current_drawdown_pct}%) exceeds limit ({self.max_drawdown_pct}%).")
            
        # 2. Leverage Constraint
        if risk_snapshot.effective_leverage >= self.max_leverage:
            if not is_reducing:
                return self._reject(target, f"Leverage ({risk_snapshot.effective_leverage}x) exceeds limit ({self.max_leverage}x). Cannot increase exposure.")

        if risk_snapshot.margin_utilization_pct >= self.max_margin_utilization_pct:
            if not is_reducing:
                return self._reject(
                    target,
                    "Margin utilization "
                    f"({risk_snapshot.margin_utilization_pct}%) exceeds limit "
                    f"({self.max_margin_utilization_pct}%). Cannot increase exposure.",
                )
                
        # 3. Generate a decision for the relative delta.  Crossing zero is
        # deliberately rejected so close and reopen are separate traceable
        # decisions with independent gates.
        if abs(required_delta) < Decimal("0.001"):
            return ExecutionDecision(
                decision_id=self._next_id("DEC", now),
                symbol=target.symbol,
                action="NOOP",
                risk_class=EconomicRiskClass.NOOP,
                rational="Net delta is below minimum threshold.",
                net_exposure_delta=Decimal("0.0"),
                timestamp=now,
                target_exposure_id=target.exposure_id,
                source_intent_ids=target.source_intent_ids,
            )
            
        # 4. Generate Execution Decision
        risk_class = self._classify_risk(required_delta, current_position_qty)
        if risk_class is None:
            return self._reject(
                target,
                "Requested delta would cross through zero and create a new exposure; "
                "close and reopen must be separate decisions.",
            )
        side = OrderSide.BUY if required_delta > 0 else OrderSide.SELL
        if not self.hedge_mode:
            pos_side = PositionSide.BOTH
        elif risk_class in {EconomicRiskClass.REDUCE_RISK, EconomicRiskClass.CLOSE}:
            pos_side = PositionSide.LONG if current_position_qty > 0 else PositionSide.SHORT
        else:
            pos_side = PositionSide.LONG if required_delta > 0 else PositionSide.SHORT
        
        order = OrderIntent(
            client_order_id=self._next_id("B-SYS", now),
            symbol=target.symbol,
            market_type=target.market_type,
            side=side,
            position_side=pos_side,
            order_type=OrderType.MARKET, # Simplifying for now; real system uses LIMIT_MAKER
            time_in_force=TimeInForce.GTC,
            quantity=abs(required_delta),
            reduce_only=risk_class in {EconomicRiskClass.REDUCE_RISK, EconomicRiskClass.CLOSE},
            strategy_id="meta_allocator",
            source_intent_ids=target.source_intent_ids,
            created_at=now,
        )
        
        return ExecutionDecision(
            decision_id=self._next_id("DEC", now),
            symbol=target.symbol,
            action="SUBMIT_ORDER",
            risk_class=risk_class,
            orders=[order],
            rational=f"Approved relative target delta of {required_delta} after risk checks.",
            net_exposure_delta=required_delta,
            timestamp=now,
            target_exposure_id=target.exposure_id,
            source_intent_ids=target.source_intent_ids,
        )

    def _reject(self, target: TargetExposure, reason: str) -> ExecutionDecision:
        logger.warning("Risk Governor REJECTED TargetExposure: %s", reason)
        now = self._now()
        return ExecutionDecision(
            decision_id=self._next_id("DEC", now),
            symbol=target.symbol,
            action="NOOP",
            risk_class=EconomicRiskClass.NOOP,
            rational=reason,
            net_exposure_delta=Decimal("0.0"),
            timestamp=now,
            target_exposure_id=target.exposure_id,
            source_intent_ids=target.source_intent_ids,
        )

    @staticmethod
    def _classify_risk(
        required_delta: Decimal, current_position_qty: Decimal
    ) -> Optional[EconomicRiskClass]:
        if current_position_qty == 0:
            return EconomicRiskClass.NEW_RISK
        if (current_position_qty > 0 and required_delta < 0) or (
            current_position_qty < 0 and required_delta > 0
        ):
            if abs(required_delta) > abs(current_position_qty):
                return None
            if abs(required_delta) == abs(current_position_qty):
                return EconomicRiskClass.CLOSE
            return EconomicRiskClass.REDUCE_RISK
        return EconomicRiskClass.INCREASE_RISK

    @staticmethod
    def _is_position_reduction(required_delta: Decimal, current_position_qty: Decimal) -> bool:
        if current_position_qty > 0:
            return required_delta < 0 and abs(required_delta) <= abs(current_position_qty)
        if current_position_qty < 0:
            return required_delta > 0 and abs(required_delta) <= abs(current_position_qty)
        return False
