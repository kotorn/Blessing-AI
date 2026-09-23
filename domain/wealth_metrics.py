"""
Wealth Performance and Risk-Adjusted Return Metrics for Blessing AI.
Strict financial calculation using Python Decimal and standard quant metrics.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from typing import Any, List, Optional, Sequence


class DeploymentStage(str, Enum):
    OBSERVE_ONLY = "OBSERVE_ONLY"
    SHADOW_TRADING = "SHADOW_TRADING"
    STAGED_FIRST_ORDER = "STAGED_FIRST_ORDER"
    SMALL_LIVE = "SMALL_LIVE"
    CONSTRAINED_AUTONOMOUS = "CONSTRAINED_AUTONOMOUS"
    PORTFOLIO_AUTONOMOUS = "PORTFOLIO_AUTONOMOUS"


@dataclass(frozen=True, slots=True)
class TradeRecord:
    trade_id: str
    symbol: str
    strategy_id: str
    realized_pnl: Decimal
    commission: Decimal
    funding: Decimal
    slippage_bps: Decimal
    holding_seconds: float
    entry_price: Decimal
    exit_price: Decimal
    closed_at: datetime
    is_unknown_risk: bool = False
    notional: Decimal = Decimal("0.0")

    @property
    def net_pnl(self) -> Decimal:
        """Net PnL after deducting exchange commissions and funding costs."""
        return self.realized_pnl - self.commission + self.funding


@dataclass(frozen=True, slots=True)
class WealthPerformanceMetrics:
    total_trades: int
    win_trades: int
    loss_trades: int
    break_even_trades: int
    win_rate_pct: Decimal
    payoff_ratio: Decimal
    profit_factor: Decimal
    expectancy_usdt: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    net_pnl: Decimal
    total_commission: Decimal
    total_funding: Decimal
    fee_drag_pct: Decimal
    max_drawdown_pct: Decimal
    cagr_pct: Decimal
    sharpe_ratio: Decimal
    sortino_ratio: Decimal
    calmar_ratio: Decimal
    var_95_pct: Decimal
    cvar_95_pct: Decimal
    avg_slippage_bps: Decimal
    unknown_risk_violations: int
    sustainable_growth_score: Decimal  # Composite 0 - 100
    is_capital_safe: bool


@dataclass(frozen=True, slots=True)
class PromotionGateVerdict:
    current_stage: DeploymentStage
    target_stage: DeploymentStage
    eligible: bool
    passed_criteria: List[str]
    blocking_reasons: List[str]
    metrics_snapshot: WealthPerformanceMetrics


def _quantize(val: Decimal, precision: str = "0.0001") -> Decimal:
    return val.quantize(Decimal(precision), rounding=ROUND_HALF_UP)


def calculate_wealth_metrics(
    trades: Sequence[TradeRecord],
    initial_capital: Decimal = Decimal("1000.0"),
    period_days: int = 365,
    risk_free_rate: Decimal = Decimal("0.02"),
) -> WealthPerformanceMetrics:
    """
    Calculate full risk-adjusted metrics from a sequence of TradeRecords.
    Adheres strictly to Capital Preservation and Sustainable Wealth Growth invariants.
    """
    if not trades:
        return WealthPerformanceMetrics(
            total_trades=0,
            win_trades=0,
            loss_trades=0,
            break_even_trades=0,
            win_rate_pct=Decimal("0.0"),
            payoff_ratio=Decimal("0.0"),
            profit_factor=Decimal("0.0"),
            expectancy_usdt=Decimal("0.0"),
            gross_profit=Decimal("0.0"),
            gross_loss=Decimal("0.0"),
            net_pnl=Decimal("0.0"),
            total_commission=Decimal("0.0"),
            total_funding=Decimal("0.0"),
            fee_drag_pct=Decimal("0.0"),
            max_drawdown_pct=Decimal("0.0"),
            cagr_pct=Decimal("0.0"),
            sharpe_ratio=Decimal("0.0"),
            sortino_ratio=Decimal("0.0"),
            calmar_ratio=Decimal("0.0"),
            var_95_pct=Decimal("0.0"),
            cvar_95_pct=Decimal("0.0"),
            avg_slippage_bps=Decimal("0.0"),
            unknown_risk_violations=0,
            sustainable_growth_score=Decimal("0.0"),
            # Absence of observations cannot establish capital safety.
            is_capital_safe=False,
        )

    total_trades = len(trades)
    win_trades = 0
    loss_trades = 0
    break_even_trades = 0
    gross_profit = Decimal("0.0")
    gross_loss = Decimal("0.0")
    net_pnl_total = Decimal("0.0")
    total_commission = Decimal("0.0")
    total_funding = Decimal("0.0")
    total_slippage = Decimal("0.0")
    unknown_risk_violations = 0

    net_pnls: List[Decimal] = []

    for t in trades:
        net = t.net_pnl
        net_pnls.append(net)
        net_pnl_total += net
        total_commission += t.commission
        total_funding += t.funding
        total_slippage += t.slippage_bps
        if t.is_unknown_risk:
            unknown_risk_violations += 1

        if net > Decimal("0.0"):
            win_trades += 1
            gross_profit += net
        elif net < Decimal("0.0"):
            loss_trades += 1
            gross_loss += abs(net)
        else:
            break_even_trades += 1

    # Win Rate & Payoff
    win_rate = Decimal(win_trades) / Decimal(total_trades)
    win_rate_pct = _quantize(win_rate * Decimal("100"), "0.01")

    avg_win = (gross_profit / Decimal(win_trades)) if win_trades > 0 else Decimal("0.0")
    avg_loss = (gross_loss / Decimal(loss_trades)) if loss_trades > 0 else Decimal("0.0")

    if avg_loss > Decimal("0.0"):
        payoff_ratio = _quantize(avg_win / avg_loss, "0.01")
    else:
        payoff_ratio = _quantize(Decimal("100.0") if avg_win > 0 else Decimal("0.0"), "0.01")

    # Profit Factor
    if gross_loss > Decimal("0.0"):
        profit_factor = _quantize(gross_profit / gross_loss, "0.01")
    else:
        profit_factor = _quantize(Decimal("100.0") if gross_profit > 0 else Decimal("0.0"), "0.01")

    # Expectancy
    loss_rate = Decimal(loss_trades) / Decimal(total_trades)
    expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)
    expectancy_usdt = _quantize(expectancy, "0.01")

    # Fee drag
    if gross_profit > Decimal("0.0"):
        fee_drag_pct = _quantize((total_commission / gross_profit) * Decimal("100"), "0.01")
    else:
        fee_drag_pct = Decimal("0.0")

    avg_slippage_bps = _quantize(total_slippage / Decimal(total_trades), "0.01")

    # Equity curve and Maximum Drawdown
    equity = initial_capital
    peak = initial_capital
    max_dd_val = Decimal("0.0")
    max_dd_pct = Decimal("0.0")

    returns_pct: List[float] = []

    for net in net_pnls:
        prior_equity = equity
        equity += net
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd_val:
            max_dd_val = dd
        if peak > Decimal("0.0"):
            dd_pct = (dd / peak) * Decimal("100.0")
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
        if prior_equity > Decimal("0.0"):
            returns_pct.append(float(net / prior_equity))
        else:
            returns_pct.append(0.0)

    max_drawdown_pct = _quantize(max_dd_pct, "0.01")

    # CAGR
    effective_days = max(1, period_days)
    years = float(effective_days) / 365.0
    ending_capital = max(Decimal("0.01"), initial_capital + net_pnl_total)
    total_return_ratio = float(ending_capital / initial_capital)
    if total_return_ratio > 0 and years > 0:
        cagr_val = (total_return_ratio ** (1.0 / years)) - 1.0
        cagr_pct = _quantize(Decimal(str(cagr_val * 100.0)), "0.01")
    else:
        cagr_pct = Decimal("-100.0")

    # Sharpe & Sortino Ratios (Annualized assuming daily or periodic intervals)
    n = len(returns_pct)
    mean_ret = sum(returns_pct) / n if n > 0 else 0.0
    rf_daily = float(risk_free_rate) / 365.0

    # Variance and downside variance
    variance = sum((r - mean_ret) ** 2 for r in returns_pct) / max(1, n - 1)
    std_dev = math.sqrt(variance) if variance > 0 else 0.0

    downside_variance = sum(min(0.0, r - rf_daily) ** 2 for r in returns_pct) / max(1, n - 1)
    downside_std_dev = math.sqrt(downside_variance) if downside_variance > 0 else 0.0

    annual_factor = math.sqrt(365.0)

    if std_dev > 1e-9:
        sharpe_val = ((mean_ret - rf_daily) / std_dev) * annual_factor
        sharpe_ratio = _quantize(Decimal(str(sharpe_val)), "0.01")
    else:
        sharpe_ratio = Decimal("0.0")

    if downside_std_dev > 1e-9:
        sortino_val = ((mean_ret - rf_daily) / downside_std_dev) * annual_factor
        sortino_ratio = _quantize(Decimal(str(sortino_val)), "0.01")
    else:
        sortino_ratio = Decimal("0.0")

    # Calmar Ratio: CAGR % / Max Drawdown %
    if max_drawdown_pct > Decimal("0.0"):
        calmar_ratio = _quantize(cagr_pct / max_drawdown_pct, "0.01")
    else:
        calmar_ratio = _quantize(Decimal("10.0") if cagr_pct > 0 else Decimal("0.0"), "0.01")

    # VaR 95% and CVaR 95% (Historical percentile on returns)
    sorted_returns = sorted(returns_pct)
    cutoff_idx = max(0, int(math.floor(0.05 * len(sorted_returns))))
    var_95_raw = abs(sorted_returns[cutoff_idx]) if sorted_returns else 0.0
    var_95_pct = _quantize(Decimal(str(var_95_raw * 100.0)), "0.01")

    tail_losses = [abs(r) for r in sorted_returns[: cutoff_idx + 1] if r < 0]
    if tail_losses:
        cvar_raw = sum(tail_losses) / len(tail_losses)
        cvar_95_pct = _quantize(Decimal(str(cvar_raw * 100.0)), "0.01")
    else:
        cvar_95_pct = var_95_pct

    # Sustainable Growth Score (0 to 100)
    # Composite weighing Sharpe, Sortino, Drawdown health, Expectancy, and Zero Unknown Risk
    score = Decimal("50.0")
    if sharpe_ratio > Decimal("1.5"):
        score += Decimal("15.0")
    elif sharpe_ratio > Decimal("1.0"):
        score += Decimal("10.0")
    elif sharpe_ratio < Decimal("0.0"):
        score -= Decimal("20.0")

    if sortino_ratio > Decimal("2.0"):
        score += Decimal("10.0")

    if max_drawdown_pct < Decimal("5.0"):
        score += Decimal("15.0")
    elif max_drawdown_pct > Decimal("10.0"):
        score -= Decimal("25.0")

    if expectancy_usdt > Decimal("0.0"):
        score += Decimal("10.0")
    else:
        score -= Decimal("15.0")

    if unknown_risk_violations > 0:
        # Rule #0 penalty: Unknown Risk completely invalidates sustainable growth
        score -= Decimal("50.0")

    sustainable_growth_score = max(Decimal("0.0"), min(Decimal("100.0"), _quantize(score, "0.1")))

    is_capital_safe = (
        max_drawdown_pct <= Decimal("10.0")
        and unknown_risk_violations == 0
        and expectancy_usdt >= Decimal("0.0")
    )

    return WealthPerformanceMetrics(
        total_trades=total_trades,
        win_trades=win_trades,
        loss_trades=loss_trades,
        break_even_trades=break_even_trades,
        win_rate_pct=win_rate_pct,
        payoff_ratio=payoff_ratio,
        profit_factor=profit_factor,
        expectancy_usdt=expectancy_usdt,
        gross_profit=_quantize(gross_profit, "0.01"),
        gross_loss=_quantize(gross_loss, "0.01"),
        net_pnl=_quantize(net_pnl_total, "0.01"),
        total_commission=_quantize(total_commission, "0.01"),
        total_funding=_quantize(total_funding, "0.01"),
        fee_drag_pct=fee_drag_pct,
        max_drawdown_pct=max_drawdown_pct,
        cagr_pct=cagr_pct,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        calmar_ratio=calmar_ratio,
        var_95_pct=var_95_pct,
        cvar_95_pct=cvar_95_pct,
        avg_slippage_bps=avg_slippage_bps,
        unknown_risk_violations=unknown_risk_violations,
        sustainable_growth_score=sustainable_growth_score,
        is_capital_safe=is_capital_safe,
    )


# Promotion Criteria Configs for Progressive Mainnet Deployment
PROMOTION_REQUIREMENTS = {
    DeploymentStage.SHADOW_TRADING: {
        "min_trades": 10,
        "max_drawdown_pct": Decimal("5.0"),
        "min_profit_factor": Decimal("1.0"),
        "allow_unknown_risk": False,
    },
    DeploymentStage.STAGED_FIRST_ORDER: {
        "min_trades": 25,
        "max_drawdown_pct": Decimal("4.0"),
        "min_profit_factor": Decimal("1.1"),
        "min_sharpe": Decimal("0.8"),
        "allow_unknown_risk": False,
    },
    DeploymentStage.SMALL_LIVE: {
        "min_trades": 50,
        "max_drawdown_pct": Decimal("3.5"),
        "min_profit_factor": Decimal("1.25"),
        "min_sharpe": Decimal("1.0"),
        "min_win_rate_pct": Decimal("50.0"),
        "allow_unknown_risk": False,
    },
    DeploymentStage.CONSTRAINED_AUTONOMOUS: {
        "min_trades": 100,
        "max_drawdown_pct": Decimal("3.0"),
        "min_profit_factor": Decimal("1.4"),
        "min_sharpe": Decimal("1.3"),
        "min_win_rate_pct": Decimal("52.0"),
        "min_growth_score": Decimal("70.0"),
        "allow_unknown_risk": False,
    },
    DeploymentStage.PORTFOLIO_AUTONOMOUS: {
        "min_trades": 250,
        "max_drawdown_pct": Decimal("2.5"),
        "min_profit_factor": Decimal("1.6"),
        "min_sharpe": Decimal("1.6"),
        "min_sortino": Decimal("2.0"),
        "min_win_rate_pct": Decimal("55.0"),
        "min_growth_score": Decimal("85.0"),
        "allow_unknown_risk": False,
    },
}

STAGE_SEQUENCE = [
    DeploymentStage.OBSERVE_ONLY,
    DeploymentStage.SHADOW_TRADING,
    DeploymentStage.STAGED_FIRST_ORDER,
    DeploymentStage.SMALL_LIVE,
    DeploymentStage.CONSTRAINED_AUTONOMOUS,
    DeploymentStage.PORTFOLIO_AUTONOMOUS,
]


def evaluate_promotion_gate(
    metrics: WealthPerformanceMetrics,
    current_stage: DeploymentStage,
) -> PromotionGateVerdict:
    """
    Fail closed while promotion evidence is process-local and non-authoritative.

    Promotion must remain blocked until durable execution lineage is wired into
    this gate. A caller-supplied boolean is intentionally not accepted as proof.
    """
    try:
        curr_idx = STAGE_SEQUENCE.index(current_stage)
    except ValueError:
        return PromotionGateVerdict(
            current_stage=current_stage,
            target_stage=current_stage,
            eligible=False,
            passed_criteria=[],
            blocking_reasons=[f"Invalid current stage: {current_stage}"],
            metrics_snapshot=metrics,
        )

    if curr_idx >= len(STAGE_SEQUENCE) - 1:
        return PromotionGateVerdict(
            current_stage=current_stage,
            target_stage=current_stage,
            eligible=False,
            passed_criteria=[],
            blocking_reasons=[
                "No higher deployment stage exists; this gate does not certify current-stage safety."
            ],
            metrics_snapshot=metrics,
        )

    target_stage = STAGE_SEQUENCE[curr_idx + 1]
    reqs = PROMOTION_REQUIREMENTS.get(target_stage, {})
    min_trades = reqs.get("min_trades", 0)
    blocking = [
        "Promotion blocked: performance evidence is not authoritative. "
        "Process-local metrics cannot approve a stage change."
    ]
    if metrics.total_trades == 0:
        blocking.append(
            "Rule #0 status UNKNOWN: no closed-trade evidence is available. "
            "Promotion remains blocked until risk can be evaluated."
        )
    elif metrics.unknown_risk_violations > 0:
        blocking.append(
            "RULE #0 BREACH observed in the available sample: "
            f"{metrics.unknown_risk_violations} unknown risk event(s)."
        )
    else:
        blocking.append(
            "Rule #0 status UNKNOWN: zero observed events in non-authoritative "
            "process memory cannot establish a pass."
        )

    if metrics.total_trades < min_trades:
        blocking.append(
            f"Insufficient trade sample size: {metrics.total_trades} < {min_trades}"
        )
    else:
        blocking.append(
            f"Trade sample count {metrics.total_trades} is process-local and unverified."
        )

    return PromotionGateVerdict(
        current_stage=current_stage,
        target_stage=target_stage,
        eligible=False,
        passed_criteria=[],
        blocking_reasons=blocking,
        metrics_snapshot=metrics,
    )
