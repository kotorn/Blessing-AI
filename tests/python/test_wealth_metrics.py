from datetime import datetime, timezone
from decimal import Decimal
import pytest

from domain.wealth_metrics import (
    DeploymentStage,
    TradeRecord,
    calculate_wealth_metrics,
    evaluate_promotion_gate,
)


def _dt(hour: int) -> datetime:
    return datetime(2026, 9, 23, hour, 0, 0, tzinfo=timezone.utc)


def test_wealth_metrics_empty_trades():
    metrics = calculate_wealth_metrics([])
    assert metrics.total_trades == 0
    assert metrics.win_trades == 0
    assert metrics.net_pnl == Decimal("0.0")
    assert metrics.is_capital_safe is False
    assert metrics.sustainable_growth_score == Decimal("0.0")
    verdict = evaluate_promotion_gate(metrics, DeploymentStage.OBSERVE_ONLY)
    assert verdict.eligible is False
    assert any("status UNKNOWN" in reason for reason in verdict.blocking_reasons)
    assert not any("Rule #0 passed" in criterion for criterion in verdict.passed_criteria)


def test_wealth_metrics_profitable_series():
    trades = [
        TradeRecord(
            trade_id=f"T-{i}",
            symbol="ETHUSDC",
            strategy_id="trend_breakout",
            realized_pnl=Decimal("20.0"),
            commission=Decimal("0.5"),
            funding=Decimal("0.1"),
            slippage_bps=Decimal("2.0"),
            holding_seconds=120.0,
            entry_price=Decimal("2500.0"),
            exit_price=Decimal("2520.0"),
            closed_at=_dt(i),
        )
        for i in range(1, 15)
    ]
    # Add one small loss
    trades.append(
        TradeRecord(
            trade_id="T-15",
            symbol="ETHUSDC",
            strategy_id="trend_breakout",
            realized_pnl=Decimal("-10.0"),
            commission=Decimal("0.5"),
            funding=Decimal("0.0"),
            slippage_bps=Decimal("3.0"),
            holding_seconds=60.0,
            entry_price=Decimal("2500.0"),
            exit_price=Decimal("2490.0"),
            closed_at=_dt(15),
        )
    )

    metrics = calculate_wealth_metrics(trades, initial_capital=Decimal("1000.0"))

    assert metrics.total_trades == 15
    assert metrics.win_trades == 14
    assert metrics.loss_trades == 1
    assert metrics.win_rate_pct > Decimal("90.0")
    assert metrics.profit_factor > Decimal("20.0")
    assert metrics.net_pnl > Decimal("200.0")
    assert metrics.sharpe_ratio > Decimal("1.0")
    assert metrics.sortino_ratio > Decimal("1.0")
    assert metrics.unknown_risk_violations == 0
    assert metrics.is_capital_safe is True
    assert metrics.sustainable_growth_score > Decimal("70.0")


def test_wealth_metrics_rule_zero_violation_blocks_safety():
    trades = [
        TradeRecord(
            trade_id="T-1",
            symbol="ETHUSDC",
            strategy_id="trend_breakout",
            realized_pnl=Decimal("50.0"),
            commission=Decimal("0.5"),
            funding=Decimal("0.0"),
            slippage_bps=Decimal("2.0"),
            holding_seconds=100.0,
            entry_price=Decimal("2500.0"),
            exit_price=Decimal("2550.0"),
            closed_at=_dt(1),
            is_unknown_risk=True,  # Rule #0 violation!
        )
    ]

    metrics = calculate_wealth_metrics(trades, initial_capital=Decimal("1000.0"))
    assert metrics.unknown_risk_violations == 1
    assert metrics.is_capital_safe is False  # Must be False because of unknown risk
    assert metrics.sustainable_growth_score < Decimal("50.0")  # Penalty applied


def test_promotion_gate_evaluation():
    trades = [
        TradeRecord(
            trade_id=f"T-{i}",
            symbol="ETHUSDC",
            strategy_id="trend_breakout",
            realized_pnl=Decimal("15.0"),
            commission=Decimal("0.2"),
            funding=Decimal("0.0"),
            slippage_bps=Decimal("2.0"),
            holding_seconds=100.0,
            entry_price=Decimal("2500.0"),
            exit_price=Decimal("2515.0"),
            closed_at=_dt(1),
        )
        for i in range(12)
    ]

    metrics = calculate_wealth_metrics(trades, initial_capital=Decimal("1000.0"))
    
    # Adequate-looking process-local metrics cannot approve a stage change.
    verdict = evaluate_promotion_gate(metrics, DeploymentStage.OBSERVE_ONLY)
    assert verdict.current_stage == DeploymentStage.OBSERVE_ONLY
    assert verdict.target_stage == DeploymentStage.SHADOW_TRADING
    assert verdict.eligible is False
    assert verdict.passed_criteria == []
    assert any("not authoritative" in reason for reason in verdict.blocking_reasons)
    assert any("process-local and unverified" in reason for reason in verdict.blocking_reasons)
    with pytest.raises(TypeError):
        evaluate_promotion_gate(metrics, DeploymentStage.OBSERVE_ONLY, evidence_authoritative=True)


def test_promotion_gate_blocks_on_rule_zero_breach():
    trades = [
        TradeRecord(
            trade_id=f"T-{i}",
            symbol="ETHUSDC",
            strategy_id="trend_breakout",
            realized_pnl=Decimal("15.0"),
            commission=Decimal("0.2"),
            funding=Decimal("0.0"),
            slippage_bps=Decimal("2.0"),
            holding_seconds=100.0,
            entry_price=Decimal("2500.0"),
            exit_price=Decimal("2515.0"),
            closed_at=_dt(1),
            is_unknown_risk=(i == 5),  # One trade breached Rule #0
        )
        for i in range(15)
    ]

    metrics = calculate_wealth_metrics(trades, initial_capital=Decimal("1000.0"))
    assert metrics.unknown_risk_violations == 1

    verdict = evaluate_promotion_gate(metrics, DeploymentStage.OBSERVE_ONLY)
    assert verdict.eligible is False
    assert any("RULE #0 BREACH" in reason for reason in verdict.blocking_reasons)
