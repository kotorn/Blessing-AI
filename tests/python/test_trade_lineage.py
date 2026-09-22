from datetime import datetime, timezone
from decimal import Decimal
import pytest

from domain.trade_lineage import OutcomeGrade, TradeLineage


def test_trade_lineage_lifecycle():
    lineage = TradeLineage.create(
        symbol="ETHUSDC",
        strategy_id="trend_breakout",
        market_state_snapshot={"primary_regime": "R3_STRONG_TREND", "atr_1h": "15.0", "volatility_zscore": "0.5"},
        intent_snapshot={"desired_delta_qty": "1.0", "expected_holding_horizon_sec": 300},
        opportunity_score_snapshot={"expected_edge": "0.0025", "tail_risk_factor": "1.0"},
        target_exposure_snapshot={"target_net_delta_qty": "1.0"},
        risk_decision_snapshot={"action": "SUBMIT_ORDER", "risk_class": "NEW_RISK"},
        allocation_multiplier=Decimal("1.0"),
    )

    assert lineage.lineage_id.startswith("LIN-")
    assert lineage.expected_edge_bps == Decimal("25.0")
    assert lineage.closed_at is None

    lineage.notional = Decimal("2500.0")
    lineage.total_quantity = Decimal("1.0")
    lineage.entry_price = Decimal("2500.0")

    lineage.record_order({"client_order_id": "ORD-1", "side": "BUY", "quantity": "1.0"})
    lineage.record_fill({"exchange_trade_id": "FILL-1", "price": "2500.0", "quantity": "1.0"})

    # Close with alpha profit
    lineage.close_and_evaluate(
        exit_price=Decimal("2525.0"),
        realized_pnl=Decimal("25.0"),
        commission=Decimal("0.5"),
        funding=Decimal("0.1"),
        slippage_bps=Decimal("2.0"),
        holding_seconds=180.0,
    )

    assert lineage.closed_at is not None
    assert lineage.net_pnl == Decimal("24.6")
    assert lineage.outcome_grade in (OutcomeGrade.ALPHA, OutcomeGrade.ACCEPTABLE_PROFIT)
    assert lineage.requires_8d is False

    d = lineage.to_dict()
    assert d["lineage_id"] == lineage.lineage_id
    assert d["outcome_grade"] == lineage.outcome_grade.value


def test_trade_lineage_excess_slippage_triggers_8d():
    lineage = TradeLineage.create(
        symbol="BTCUSDT",
        strategy_id="shock_momentum",
        market_state_snapshot={"primary_regime": "R4_BREAKOUT", "atr_1h": "500.0", "volatility_zscore": "1.5"},
        intent_snapshot={"desired_delta_qty": "0.1"},
        opportunity_score_snapshot={"expected_edge": "0.0030"},
        target_exposure_snapshot={"target_net_delta_qty": "0.1"},
        risk_decision_snapshot={"action": "SUBMIT_ORDER"},
    )
    lineage.notional = Decimal("6000.0")
    lineage.total_quantity = Decimal("0.1")

    # Close with severe slippage (35 bps) and loss
    lineage.close_and_evaluate(
        exit_price=Decimal("59800.0"),
        realized_pnl=Decimal("-20.0"),
        commission=Decimal("1.0"),
        funding=Decimal("0.0"),
        slippage_bps=Decimal("35.0"),  # > 25 bps triggers 8D
        holding_seconds=45.0,
    )

    assert lineage.outcome_grade == OutcomeGrade.EXCESS_SLIPPAGE
    assert lineage.requires_8d is True
    assert any("Excessive slippage" in item for item in lineage.lessons_learned)


def test_trade_lineage_regime_mismatch_triggers_8d():
    lineage = TradeLineage.create(
        symbol="SOLUSDT",
        strategy_id="range_fade",
        market_state_snapshot={"primary_regime": "R5_VOLATILITY_SHOCK", "atr_1h": "4.0", "volatility_zscore": "3.0"},
        intent_snapshot={"desired_delta_qty": "5.0"},
        opportunity_score_snapshot={"expected_edge": "0.0020"},
        target_exposure_snapshot={"target_net_delta_qty": "5.0"},
        risk_decision_snapshot={"action": "SUBMIT_ORDER"},
    )
    lineage.notional = Decimal("750.0")
    lineage.total_quantity = Decimal("5.0")

    lineage.close_and_evaluate(
        exit_price=Decimal("148.0"),
        realized_pnl=Decimal("-10.0"),
        commission=Decimal("0.2"),
        funding=Decimal("0.0"),
        slippage_bps=Decimal("4.0"),
        holding_seconds=60.0,
    )

    assert lineage.outcome_grade == OutcomeGrade.REGIME_MISMATCH
    assert lineage.requires_8d is True
