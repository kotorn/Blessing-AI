from decimal import Decimal
import pytest

from domain.eight_d import IncidentSeverity, IncidentStatus, IncidentTriggerType
from domain.trade_lineage import TradeLineage, OutcomeGrade
from domain.wealth_metrics import WealthPerformanceMetrics
from apps.learning_engine.why_why_analyzer import WhyWhyAnalyzer
from apps.learning_engine.eight_d_manager import EightDManager
from apps.learning_engine.pdca_evaluator import PDCAEvaluator, StrategyPlan
from apps.learning_engine.capital_scaling import DynamicCapitalAllocator


def _create_sample_lineage(outcome: OutcomeGrade, slippage: Decimal = Decimal("5.0"), pnl: Decimal = Decimal("10.0")) -> TradeLineage:
    l = TradeLineage.create(
        symbol="ETHUSDC",
        strategy_id="trend_breakout",
        market_state_snapshot={"primary_regime": "R3_STRONG_TREND", "atr_1h": "15.0", "volatility_zscore": "0.5"},
        intent_snapshot={"desired_delta_qty": "1.0"},
        opportunity_score_snapshot={"expected_edge": "0.0020", "tail_risk_factor": "1.0"},
        target_exposure_snapshot={"target_net_delta_qty": "1.0"},
        risk_decision_snapshot={"action": "SUBMIT_ORDER"},
    )
    l.notional = Decimal("2500.0")
    l.total_quantity = Decimal("1.0")
    l.entry_price = Decimal("2500.0")
    l.close_and_evaluate(
        exit_price=Decimal("2510.0") if pnl > 0 else Decimal("2480.0"),
        realized_pnl=pnl,
        commission=Decimal("0.5"),
        funding=Decimal("0.0"),
        slippage_bps=slippage,
        holding_seconds=120.0,
    )
    return l


def test_why_why_analyzer_slippage():
    lineage = _create_sample_lineage(OutcomeGrade.EXCESS_SLIPPAGE, slippage=Decimal("35.0"), pnl=Decimal("-15.0"))
    tree = WhyWhyAnalyzer.analyze_lineage(lineage)

    assert len(tree.levels) == 5
    assert "slippage" in tree.levels[0]["question"].lower()
    assert "depth" in tree.root_cause_summary.lower()
    assert len(tree.preventive_insight) > 20


def test_why_why_analyzer_regime_mismatch():
    lineage = TradeLineage.create(
        symbol="BTCUSDT",
        strategy_id="range_fade",
        market_state_snapshot={"primary_regime": "R5_VOLATILITY_SHOCK", "atr_1h": "300.0", "volatility_zscore": "2.8"},
        intent_snapshot={"desired_delta_qty": "0.2"},
        opportunity_score_snapshot={"expected_edge": "0.0015"},
        target_exposure_snapshot={"target_net_delta_qty": "0.2"},
        risk_decision_snapshot={"action": "SUBMIT_ORDER"},
    )
    lineage.notional = Decimal("12000.0")
    lineage.total_quantity = Decimal("0.2")
    lineage.close_and_evaluate(
        exit_price=Decimal("59500.0"),
        realized_pnl=Decimal("-100.0"),
        commission=Decimal("2.0"),
        funding=Decimal("0.0"),
        slippage_bps=Decimal("5.0"),
        holding_seconds=50.0,
    )

    tree = WhyWhyAnalyzer.analyze_lineage(lineage)
    assert len(tree.levels) == 5
    assert "regime" in tree.root_cause_summary.lower() or "latency" in tree.root_cause_summary.lower()


def test_eight_d_manager_incident_lifecycle():
    mgr = EightDManager()
    lineage = _create_sample_lineage(OutcomeGrade.EXCESS_SLIPPAGE, slippage=Decimal("40.0"), pnl=Decimal("-30.0"))

    # 1. Create incident automatically
    incident = mgr.create_incident_from_lineage(lineage)
    assert incident.incident_id.startswith("8D-")
    assert incident.severity == IncidentSeverity.HIGH
    assert incident.status == IncidentStatus.D5_PCA_CHOSEN
    assert incident.d4_root_cause is not None
    assert len(incident.d5_pca) >= 1

    # Check active containment
    assert lineage.symbol in mgr.active_containments
    assert mgr.active_containments[lineage.symbol]["symbol"] == lineage.symbol

    # 2. Advance and close at D8
    success = mgr.advance_and_close(
        incident_id=incident.incident_id,
        verification_evidence="Shadow tests verified 100% orders use POST_ONLY in thin depth.",
        systemic_prevention="Rolled out depth filter to all 13 supported USDⓈ-M instruments.",
        closure_lessons="Taker orders strictly prevented without level 2 depth confirmation.",
        signoff_user_id="operator-test-uid",
    )
    assert success is True
    assert incident.status == IncidentStatus.D8_CLOSED
    assert incident.closed_at is not None
    assert incident.d8_closure["signoff_user_id"] == "operator-test-uid"
    # Symbol active containment should be lifted
    assert lineage.symbol not in mgr.active_containments


def test_eight_d_manager_rejects_closure_before_root_cause_and_pca():
    mgr = EightDManager()
    incident = mgr.create_incident_from_lineage(
        _create_sample_lineage(OutcomeGrade.EXCESS_SLIPPAGE, slippage=Decimal("40.0"), pnl=Decimal("-30.0"))
    )
    incident.status = IncidentStatus.D3_CONTAINED
    incident.d4_root_cause = None
    incident.d5_pca = []

    success = mgr.advance_and_close(
        incident_id=incident.incident_id,
        verification_evidence="Verification evidence",
        systemic_prevention="Prevention evidence",
        closure_lessons="Closure lessons",
        signoff_user_id="operator-test-uid",
    )

    assert success is False
    assert incident.status == IncidentStatus.D3_CONTAINED
    assert incident.closed_at is None


def test_pdca_evaluator_drift_detection():
    evaluator = PDCAEvaluator()
    # Create 5 lineages where actual edge decays severely
    lineages = []
    for _ in range(5):
        l = TradeLineage.create(
            symbol="ETHUSDC",
            strategy_id="trend_breakout",
            market_state_snapshot={"primary_regime": "R3_STRONG_TREND", "atr_1h": "10.0"},
            intent_snapshot={"desired_delta_qty": "1.0"},
            opportunity_score_snapshot={"expected_edge": "0.0035"},  # 35 bps expected
            target_exposure_snapshot={"target_net_delta_qty": "1.0"},
            risk_decision_snapshot={"action": "SUBMIT_ORDER"},
        )
        l.notional = Decimal("2500.0")
        l.total_quantity = Decimal("1.0")
        l.close_and_evaluate(
            exit_price=Decimal("2501.0"),
            realized_pnl=Decimal("1.0"),
            commission=Decimal("0.5"),
            funding=Decimal("0.0"),
            slippage_bps=Decimal("8.0"),
            holding_seconds=60.0,
        )
        # Actual edge is ~2 bps vs 35 bps plan -> edge decay > 30 bps
        lineages.append(l)

    result = evaluator.evaluate_strategy("trend_breakout", lineages)
    assert result.drift_detected is True
    assert result.edge_decay_bps > Decimal("20.0")
    assert result.drift_severity == "CRITICAL"
    assert result.triggers_8d is True
    assert any("Clamp strategy allocation" in action for action in result.recommended_actions)


def test_dynamic_capital_allocator_scaling_and_rule_zero():
    allocator = DynamicCapitalAllocator()

    base_metrics = WealthPerformanceMetrics(
        total_trades=50,
        win_trades=32,
        loss_trades=18,
        break_even_trades=0,
        win_rate_pct=Decimal("64.0"),
        payoff_ratio=Decimal("1.8"),
        profit_factor=Decimal("1.9"),
        expectancy_usdt=Decimal("8.5"),
        gross_profit=Decimal("1200.0"),
        gross_loss=Decimal("630.0"),
        net_pnl=Decimal("570.0"),
        total_commission=Decimal("25.0"),
        total_funding=Decimal("5.0"),
        fee_drag_pct=Decimal("2.0"),
        max_drawdown_pct=Decimal("1.5"),
        cagr_pct=Decimal("25.0"),
        sharpe_ratio=Decimal("1.9"),
        sortino_ratio=Decimal("2.4"),
        calmar_ratio=Decimal("16.6"),
        var_95_pct=Decimal("1.0"),
        cvar_95_pct=Decimal("1.5"),
        avg_slippage_bps=Decimal("2.5"),
        unknown_risk_violations=0,
        sustainable_growth_score=Decimal("88.0"),
        is_capital_safe=True,
    )

    # 1. Healthy performance -> Multiplier boosted (> 1.0x)
    verdict = allocator.evaluate_allocation(
        strategy_id="trend_breakout",
        metrics=base_metrics,
        current_drawdown_pct=Decimal("0.8"),
    )
    assert verdict.scaled_allocation_multiplier > Decimal("1.0")
    assert verdict.rule_zero_compliant is True

    # 2. Drawdown elevated -> Dampens allocation
    verdict_dd = allocator.evaluate_allocation(
        strategy_id="trend_breakout",
        metrics=base_metrics,
        current_drawdown_pct=Decimal("4.5"),
    )
    assert verdict_dd.scaled_allocation_multiplier < Decimal("0.6")

    # 3. Rule #0 Breach -> Allocations immediately reduced to 0.0x
    import dataclasses
    breached_metrics = dataclasses.replace(base_metrics, unknown_risk_violations=1)
    verdict_r0 = allocator.evaluate_allocation(
        strategy_id="trend_breakout",
        metrics=breached_metrics,
        current_drawdown_pct=Decimal("0.5"),
    )
    assert verdict_r0.scaled_allocation_multiplier == Decimal("0.0")
    assert verdict_r0.rule_zero_compliant is False
    assert "RULE #0 ENFORCED" in verdict_r0.rationale
