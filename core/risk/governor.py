"""
Portfolio Risk Governor for Blessing AI v0.1
Autonomous capital preservation engine positioned strictly ABOVE all Strategies and ML models.
Rule: AI and Strategy layers have ZERO authority to override the Risk Governor.
"""

from decimal import Decimal
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone
import structlog
from pydantic import BaseModel, Field

from core.basket.models import RiskState, MarketRegime

logger = structlog.get_logger()


class RiskLimits(BaseModel):
    max_portfolio_leverage: Decimal = Decimal("2.0")
    margin_utilization_normal_pct: Decimal = Decimal("20.0")
    margin_utilization_stress_pct: Decimal = Decimal("30.0")
    margin_utilization_extreme_pct: Decimal = Decimal("40.0")
    min_liquidation_distance_pct: Decimal = Decimal("25.0")
    drawdown_caution_pct: Decimal = Decimal("2.0")
    drawdown_no_new_grid_pct: Decimal = Decimal("4.0")
    drawdown_recovery_only_pct: Decimal = Decimal("6.0")
    drawdown_emergency_pct: Decimal = Decimal("8.0")
    max_staleness_seconds: float = 3.0
    basis_shock_zscore_threshold: float = 2.5
    high_correlation_threshold: float = 0.85


class RiskEvaluation(BaseModel):
    risk_state: RiskState
    hard_rule_violations: List[str] = Field(default_factory=list)
    soft_rule_warnings: List[str] = Field(default_factory=list)
    can_open_new_basket: bool = True
    can_expand_grid: bool = True
    requires_deleveraging: bool = False
    requires_emergency_flatten: bool = False
    details: Dict[str, str] = Field(default_factory=dict)


class PortfolioRiskGovernor:
    """
    Evaluates account-wide exposure, margin, drawdowns, correlation, and connectivity.
    Implements the fail-closed doctrine.
    """

    def __init__(self, limits: Optional[RiskLimits] = None):
        self.limits = limits or RiskLimits()
        self.kill_switch_active = False

    def evaluate(
        self,
        portfolio_equity: Decimal,
        portfolio_balance: Decimal,
        used_margin: Decimal,
        total_notional_exposure: Decimal,
        liquidation_distance_pct: Optional[Decimal],
        last_market_data_ts: datetime,
        last_private_ws_ts: datetime,
        basis_zscore: float,
        btc_eth_correlation: float,
        concurrent_long_exposure_pct: float,
        grid_safety_score: Optional[float] = None,
    ) -> RiskEvaluation:
        violations: List[str] = []
        warnings: List[str] = []
        now = datetime.now(timezone.utc)

        # 0. Manual Kill Switch Check
        if self.kill_switch_active:
            violations.append("MANUAL_KILL_SWITCH_ACTIVE")
            return RiskEvaluation(
                risk_state=RiskState.EMERGENCY,
                hard_rule_violations=violations,
                can_open_new_basket=False,
                can_expand_grid=False,
                requires_emergency_flatten=True,
            )

        # 1. Connectivity & Stale Data (Fail-Closed)
        market_data_age = (now - last_market_data_ts).total_seconds()
        private_ws_age = (now - last_private_ws_ts).total_seconds()
        if market_data_age > self.limits.max_staleness_seconds:
            violations.append(f"STALE_MARKET_DATA ({market_data_age:.1f}s > {self.limits.max_staleness_seconds}s)")
        if private_ws_age > self.limits.max_staleness_seconds:
            violations.append(f"STALE_PRIVATE_WEBSOCKET ({private_ws_age:.1f}s > {self.limits.max_staleness_seconds}s)")

        # 2. Margin Utilization
        margin_utilization_pct = (used_margin / portfolio_equity * Decimal("100")) if portfolio_equity > 0 else Decimal("100")
        if margin_utilization_pct >= self.limits.margin_utilization_extreme_pct:
            violations.append(f"MARGIN_UTILIZATION_EXTREME ({margin_utilization_pct:.1f}% >= {self.limits.margin_utilization_extreme_pct}%)")
        elif margin_utilization_pct >= self.limits.margin_utilization_stress_pct:
            warnings.append(f"MARGIN_UTILIZATION_STRESS ({margin_utilization_pct:.1f}%)")

        # 3. Effective Leverage
        effective_leverage = (total_notional_exposure / portfolio_equity) if portfolio_equity > 0 else Decimal("99")
        if effective_leverage > self.limits.max_portfolio_leverage:
            violations.append(f"MAX_LEVERAGE_BREACH ({effective_leverage:.2f}x > {self.limits.max_portfolio_leverage}x)")

        # 4. Liquidation Distance
        if liquidation_distance_pct is not None and liquidation_distance_pct < self.limits.min_liquidation_distance_pct:
            violations.append(f"LIQUIDATION_DISTANCE_CRITICAL ({liquidation_distance_pct:.1f}% < {self.limits.min_liquidation_distance_pct}%)")

        # 5. Portfolio Drawdown (From Balance or High-Water Mark)
        dd_pct = ((portfolio_balance - portfolio_equity) / portfolio_balance * Decimal("100")) if portfolio_balance > 0 else Decimal("0")
        if dd_pct < Decimal("0.0"):
            dd_pct = Decimal("0.0")

        # 6. Basis Shock & Correlation Soft Rules
        if abs(basis_zscore) > self.limits.basis_shock_zscore_threshold:
            warnings.append(f"BASIS_SHOCK_DETECTED (Z-score {basis_zscore:.2f})")

        if btc_eth_correlation > self.limits.high_correlation_threshold and concurrent_long_exposure_pct > 60.0:
            warnings.append(f"HIGH_CRYPTO_BETA_CONCENTRATION (Corr {btc_eth_correlation:.2f}, Long Exposure {concurrent_long_exposure_pct:.1f}%)")

        # Determine Risk State
        state = RiskState.NORMAL
        can_open = True
        can_expand = True
        requires_deleveraging = False
        requires_emergency = False

        if dd_pct >= self.limits.drawdown_emergency_pct or len(violations) > 0:
            state = RiskState.EMERGENCY
            can_open = False
            can_expand = False
            requires_emergency = True
        elif dd_pct >= self.limits.drawdown_recovery_only_pct:
            state = RiskState.RECOVERY_ONLY
            can_open = False
            can_expand = False
        elif dd_pct >= self.limits.drawdown_no_new_grid_pct or margin_utilization_pct >= self.limits.margin_utilization_stress_pct:
            state = RiskState.NO_NEW_GRID
            can_open = False
            can_expand = False
        elif dd_pct >= self.limits.drawdown_caution_pct or len(warnings) > 0:
            state = RiskState.CAUTION
            can_open = True
            can_expand = True
            # AI Grid Safety Score gates new baskets during CAUTION
            if grid_safety_score is not None and grid_safety_score < 65.0:
                can_open = False

        return RiskEvaluation(
            risk_state=state,
            hard_rule_violations=violations,
            soft_rule_warnings=warnings,
            can_open_new_basket=can_open,
            can_expand_grid=can_expand,
            requires_deleveraging=requires_deleveraging,
            requires_emergency_flatten=requires_emergency,
            details={
                "margin_utilization_pct": f"{margin_utilization_pct:.2f}%",
                "effective_leverage": f"{effective_leverage:.2f}x",
                "portfolio_drawdown_pct": f"{dd_pct:.2f}%",
            },
        )
