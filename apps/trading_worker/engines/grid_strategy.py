import logging
from decimal import Decimal, InvalidOperation
from typing import Optional
from domain.models import MarketState, PriceActionState, StrategyIntent, PositionSide, MarketType, utc_now
from domain.enums import RegimeType

logger = logging.getLogger("blessing.engines.grid_strategy")


class GridStrategyEngine:
    """Deterministic structural-grid intent producer.

    This engine never places orders. It emits a bounded intent and applies
    the grid brake before the allocator or execution pipeline sees it.
    """

    _RISK_OFF_REGIMES = frozenset(
        {
            RegimeType.R2_WEAK_TREND,
            RegimeType.R3_STRONG_TREND,
            RegimeType.R4_BREAKOUT,
            RegimeType.R5_VOLATILITY_SHOCK,
            RegimeType.R6_CRISIS,
            # Compatibility values used by older paper/research payloads.
            RegimeType.TREND,
            RegimeType.BREAKOUT,
            RegimeType.SHOCK,
            RegimeType.TRANSITION,
        }
    )

    def __init__(self, strategy_id: str = "Structural Grid", max_grid_levels: int = 5):
        if not isinstance(max_grid_levels, int) or isinstance(max_grid_levels, bool):
            raise ValueError("max_grid_levels must be an integer")
        if max_grid_levels < 1:
            raise ValueError("max_grid_levels must be positive")
        self.strategy_id = strategy_id
        self.max_grid_levels = max_grid_levels

    def _brake_intent(self, pa_state: PriceActionState, reason: str) -> StrategyIntent:
        return StrategyIntent(
            intent_id=f"GRID-BRAKE-{utc_now().timestamp()}",
            strategy_id=self.strategy_id,
            symbol=pa_state.symbol,
            market_type=MarketType.USDM_FUTURES,
            direction=PositionSide.BOTH,
            desired_delta_qty=Decimal("0.0"),
            opportunity_score=Decimal("0.0"),
            confidence=Decimal("1.0"),
            expected_holding_horizon_sec=0,
            evidence={"brake_reason": reason},
            timestamp=pa_state.timestamp,
        )

    def observed_depth(
        self,
        *,
        position_qty: Decimal,
        open_grid_orders: int,
        filled_grid_orders: int,
    ) -> int:
        """Return a conservative depth observed from authoritative lineage.

        A non-flat position with no identifiable grid lineage is treated as
        fully capped: the worker must not add grid risk on top of inventory
        owned by another strategy or an unknown exchange event.  Historical
        grid fills do not keep depth alive after the symbol is flat and has no
        working grid order.
        """

        try:
            quantity = Decimal(str(position_qty))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("position_qty must be Decimal-compatible") from exc
        if not quantity.is_finite() or quantity < 0:
            raise ValueError("position_qty must be finite and non-negative")
        if (
            not isinstance(open_grid_orders, int)
            or isinstance(open_grid_orders, bool)
            or open_grid_orders < 0
            or not isinstance(filled_grid_orders, int)
            or isinstance(filled_grid_orders, bool)
            or filled_grid_orders < 0
        ):
            raise ValueError("grid order counts must be non-negative integers")
        if quantity == 0 and open_grid_orders == 0:
            return 0
        if quantity > 0 and open_grid_orders + filled_grid_orders == 0:
            return self.max_grid_levels
        return min(self.max_grid_levels, open_grid_orders + filled_grid_orders)

    def evaluate(
        self,
        pa_state: PriceActionState,
        market_state: MarketState,
        grid_depth: int = 0,
    ) -> Optional[StrategyIntent]:
        regime = market_state.primary_regime
        regime_name = getattr(regime, "name", str(regime))

        if not isinstance(grid_depth, int) or isinstance(grid_depth, bool) or grid_depth < 0:
            return self._brake_intent(pa_state, "Invalid grid depth")

        # Do not expand a grid in a directional, breakout, transition, or
        # shock regime. shock_active is authoritative even if a classifier
        # has not yet changed the primary regime.
        if getattr(market_state, "shock_active", False) or regime in self._RISK_OFF_REGIMES:
            return self._brake_intent(
                pa_state,
                "Grid expansion disabled in regime "
                f"{regime_name}",
            )

        if grid_depth >= self.max_grid_levels:
            return self._brake_intent(
                pa_state,
                f"Maximum grid depth {self.max_grid_levels} reached",
            )
            
        # Standard Grid Opportunity
        base_confidence = Decimal("0.8")
        opportunity_score = Decimal("0.5")
        
        if regime == RegimeType.R0_STRONG_MEAN_REVERSION:
            opportunity_score = Decimal("0.9")
            base_confidence = Decimal("0.9")
            
        direction = PositionSide.LONG if pa_state.is_reclaiming else PositionSide.BOTH
        # Reclaiming is the only entry condition. A non-reclaiming event
        # therefore cannot add to adverse inventory. Remaining levels reduce
        # the next delta; this is bounded deceleration, never martingale.
        if direction == PositionSide.LONG:
            remaining_levels = self.max_grid_levels - grid_depth
            delta = Decimal("0.1") * Decimal(remaining_levels) / Decimal(self.max_grid_levels)
        else:
            delta = Decimal("0.0")
        
        return StrategyIntent(
            intent_id=f"GRID-INTENT-{utc_now().timestamp()}",
            strategy_id=self.strategy_id,
            symbol=pa_state.symbol,
            market_type=MarketType.USDM_FUTURES,
            direction=direction,
            desired_delta_qty=delta,
            opportunity_score=opportunity_score,
            confidence=base_confidence,
            expected_holding_horizon_sec=3600,
            evidence={
                "regime": regime_name,
                "atr": str(market_state.atr_1h),
                "grid_depth": grid_depth,
                "max_grid_levels": self.max_grid_levels,
            },
            timestamp=pa_state.timestamp,
        )
