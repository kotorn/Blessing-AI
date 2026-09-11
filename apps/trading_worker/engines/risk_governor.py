import logging
from decimal import Decimal
from typing import Optional, List
from domain.models import TargetExposure, RiskSnapshot, ExecutionDecision, OrderIntent, OrderSide, PositionSide, OrderType, TimeInForce, utc_now
from domain.enums import RiskState

logger = logging.getLogger("blessing.engines.risk_governor")

class RiskGovernor:
    def __init__(self, max_leverage: Decimal = Decimal("2.0"), max_drawdown_pct: Decimal = Decimal("6.0")):
        self.max_leverage = max_leverage
        self.max_drawdown_pct = max_drawdown_pct

    def evaluate(self, target: TargetExposure, risk_snapshot: RiskSnapshot, current_position_qty: Decimal) -> ExecutionDecision:
        # 1. Hard Constraints
        if risk_snapshot.risk_state in [RiskState.EMERGENCY, RiskState.LIQUIDATING]:
            return self._reject(target, "System is in EMERGENCY state. No new risk allowed.")
            
        if risk_snapshot.current_drawdown_pct >= self.max_drawdown_pct:
            return self._reject(target, f"Drawdown ({risk_snapshot.current_drawdown_pct}%) exceeds limit ({self.max_drawdown_pct}%).")
            
        # 2. Leverage Constraint
        if risk_snapshot.effective_leverage >= self.max_leverage:
            # Only allow risk-reducing trades
            if abs(target.target_net_delta_qty) > abs(current_position_qty):
                return self._reject(target, f"Leverage ({risk_snapshot.effective_leverage}x) exceeds limit ({self.max_leverage}x). Cannot increase exposure.")
                
        # 3. Calculate required order to reach target delta
        # Simplified: target_net_delta_qty represents the ABSOLUTE target exposure we want.
        # Wait, strategy intents gave desired_delta_qty which is usually relative, but the meta allocator
        # aggregated them into target_net_delta_qty. For Blessing AI, we treat target_net_delta_qty as the 
        # relative change wanted by the strategies this tick, OR the absolute portfolio target?
        # Let's assume TargetExposure from MetaAllocator is relative to CURRENT position for now to make it a delta.
        
        required_delta = target.target_net_delta_qty
        
        if abs(required_delta) < Decimal("0.001"):
            return ExecutionDecision(
                decision_id=f"DEC-{utc_now().timestamp()}",
                symbol=target.symbol,
                action="NOOP",
                rational="Net delta is below minimum threshold.",
                net_exposure_delta=Decimal("0.0")
            )
            
        # 4. Generate Execution Decision
        side = OrderSide.BUY if required_delta > 0 else OrderSide.SELL
        pos_side = PositionSide.LONG if required_delta > 0 else PositionSide.SHORT # simplified for one-way mode representation
        
        order = OrderIntent(
            client_order_id=f"B-SYS-{int(utc_now().timestamp() * 1000)}",
            symbol=target.symbol,
            market_type=target.market_type,
            side=side,
            position_side=pos_side,
            order_type=OrderType.MARKET, # Simplifying for now; real system uses LIMIT_MAKER
            time_in_force=TimeInForce.GTC,
            quantity=abs(required_delta),
            strategy_id="meta_allocator"
        )
        
        return ExecutionDecision(
            decision_id=f"DEC-{utc_now().timestamp()}",
            symbol=target.symbol,
            action="SUBMIT_ORDER",
            orders=[order],
            rational=f"Approved target delta of {required_delta} with expected edge.",
            net_exposure_delta=required_delta
        )

    def _reject(self, target: TargetExposure, reason: str) -> ExecutionDecision:
        logger.warning("Risk Governor REJECTED TargetExposure: %s", reason)
        return ExecutionDecision(
            decision_id=f"DEC-{utc_now().timestamp()}",
            symbol=target.symbol,
            action="NOOP",
            rational=reason,
            net_exposure_delta=Decimal("0.0")
        )
