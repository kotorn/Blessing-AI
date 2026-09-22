from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

from apps.trading_worker.main import app, TradingWorkerApp, set_worker_engine
from domain.trade_lineage import TradeLineage, OutcomeGrade


@pytest.fixture
def client():
    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    set_worker_engine(worker)
    
    # Pre-populate some lineages
    l1 = TradeLineage.create(
        symbol="ETHUSDC",
        strategy_id="trend_breakout",
        market_state_snapshot={"primary_regime": "R3_STRONG_TREND", "atr_1h": "15.0", "volatility_zscore": "0.5"},
        intent_snapshot={"desired_delta_qty": "1.0", "expected_holding_horizon_sec": 300},
        opportunity_score_snapshot={"expected_edge": "0.0020", "tail_risk_factor": "1.0"},
        target_exposure_snapshot={"target_net_delta_qty": "1.0"},
        risk_decision_snapshot={"action": "SUBMIT_ORDER"},
    )
    l1.notional = Decimal("2500.0")
    l1.total_quantity = Decimal("1.0")
    l1.close_and_evaluate(
        exit_price=Decimal("2520.0"),
        realized_pnl=Decimal("20.0"),
        commission=Decimal("0.5"),
        funding=Decimal("0.0"),
        slippage_bps=Decimal("2.0"),
        holding_seconds=120.0,
    )
    worker.record_closed_trade_lineage(l1)

    # Pre-populate an anomalous lineage that triggers 8D
    l2 = TradeLineage.create(
        symbol="ETHUSDC",
        strategy_id="trend_breakout",
        market_state_snapshot={"primary_regime": "R3_STRONG_TREND", "atr_1h": "15.0", "volatility_zscore": "0.5"},
        intent_snapshot={"desired_delta_qty": "1.0", "expected_holding_horizon_sec": 300},
        opportunity_score_snapshot={"expected_edge": "0.0020", "tail_risk_factor": "1.0"},
        target_exposure_snapshot={"target_net_delta_qty": "1.0"},
        risk_decision_snapshot={"action": "SUBMIT_ORDER"},
    )
    l2.notional = Decimal("2500.0")
    l2.total_quantity = Decimal("1.0")
    l2.close_and_evaluate(
        exit_price=Decimal("2470.0"),
        realized_pnl=Decimal("-30.0"),
        commission=Decimal("0.5"),
        funding=Decimal("0.0"),
        slippage_bps=Decimal("32.0"),  # > 25 bps triggers 8D
        holding_seconds=60.0,
    )
    worker.record_closed_trade_lineage(l2)

    return TestClient(app)


def test_wealth_metrics_endpoint(client):
    res = client.get("/wealth/metrics")
    assert res.status_code == 200
    data = res.json()
    assert "portfolio" in data
    assert "promotion_gate" in data
    assert data["portfolio"]["total_trades"] == 2
    assert "win_rate_pct" in data["portfolio"]
    assert "current_stage" in data["promotion_gate"]


def test_incidents_8d_and_close_endpoint(client):
    res = client.get("/incidents/8d")
    assert res.status_code == 200
    incidents = res.json()
    assert len(incidents) >= 1
    inc = incidents[0]
    assert inc["incident_id"].startswith("8D-")
    assert inc["symbol"] == "ETHUSDC"
    assert inc["d4_root_cause"] is not None

    # Close the incident via API
    close_res = client.post(
        f"/incidents/8d/{inc['incident_id']}/close",
        json={
            "verification": "Shadow tested slippage bounds successfully verified.",
            "prevention": "Level 2 order book depth gate enforced globally.",
            "lessons": "Taker orders without L2 depth gate strictly prohibited.",
            "signoff_agent": "RiskSupervisor",
        },
    )
    assert close_res.status_code == 200
    assert close_res.json()["status"] == "CLOSED"

    # Verify status in active list
    active_res = client.get("/incidents/8d?active_only=true")
    assert active_res.status_code == 200
    active_incidents = active_res.json()
    assert all(i["incident_id"] != inc["incident_id"] for i in active_incidents)


def test_learning_lineages_endpoint(client):
    res = client.get("/learning/lineages")
    assert res.status_code == 200
    lineages = res.json()
    assert len(lineages) == 2
    assert lineages[0]["lineage_id"].startswith("LIN-")
    assert lineages[1]["requires_8d"] is True


def test_learning_pdca_endpoint(client):
    res = client.get("/learning/pdca")
    assert res.status_code == 200
    pdca = res.json()
    assert "trend_breakout" in pdca
    tb = pdca["trend_breakout"]
    assert tb["sample_size"] == 2
    assert "plan_win_rate_pct" in tb
    assert "actual_win_rate_pct" in tb
    assert "drift_detected" in tb
