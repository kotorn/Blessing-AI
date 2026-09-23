from decimal import Decimal
from datetime import UTC, datetime
import pytest
from fastapi.testclient import TestClient

from apps.trading_worker.main import app, TradingWorkerApp, set_worker_engine
from domain.trade_lineage import TradeLineage, OutcomeGrade
from domain.wealth_metrics import TradeRecord


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
    assert data["evidence"]["status"] == "INSUFFICIENT_SAMPLE"
    assert data["evidence"]["authoritative"] is False
    assert data["portfolio"]["is_capital_safe"] is False
    assert data["portfolio"]["capital_safety_status"] == "UNKNOWN"
    assert data["promotion_gate"]["eligible"] is False
    assert data["promotion_gate"]["passed_criteria"] == []
    assert any("not authoritative" in reason for reason in data["promotion_gate"]["blocking_reasons"])


def test_empty_wealth_evidence_is_never_reported_as_safe_or_verified():
    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    set_worker_engine(worker)
    try:
        response = TestClient(app).get("/wealth/metrics")
        assert response.status_code == 200
        data = response.json()
        assert data["evidence"] == {
            "status": "INSUFFICIENT_SAMPLE",
            "source": "PROCESS_MEMORY",
            "sample_size": 0,
            "authoritative": False,
            "reason": (
                "Closed-trade lineage is not yet connected to durable execution history; "
                "worker memory is not authoritative performance evidence."
            ),
        }
        assert data["portfolio"]["is_capital_safe"] is False
        assert data["promotion_gate"]["eligible"] is False
        pdca = TestClient(app).get("/learning/pdca").json()
        assert pdca["trend_breakout"]["evidence_status"] == "INSUFFICIENT_SAMPLE"
        assert pdca["trend_breakout"]["authoritative"] is False
        assert pdca["trend_breakout"]["sample_size"] == 0
        assert pdca["trend_breakout"]["drift_detected"] is None
        assert pdca["trend_breakout"]["drift_severity"] == "UNKNOWN"
        assert pdca["trend_breakout"]["triggers_8d"] is None
    finally:
        set_worker_engine(None)


def test_sufficient_process_local_trade_sample_remains_unverified_and_blocked():
    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.wealth_evaluator.trades = [
        TradeRecord(
            trade_id=f"LOCAL-{i}",
            symbol="ETHUSDC",
            strategy_id="trend_breakout",
            realized_pnl=Decimal("25.0"),
            commission=Decimal("0.1"),
            funding=Decimal("0.0"),
            slippage_bps=Decimal("1.0"),
            holding_seconds=120.0,
            entry_price=Decimal("2500.0"),
            exit_price=Decimal("2525.0"),
            closed_at=datetime(2026, 9, 23, 12, i, tzinfo=UTC),
        )
        for i in range(10)
    ]
    set_worker_engine(worker)
    try:
        data = TestClient(app).get("/wealth/metrics").json()
        assert data["evidence"]["status"] == "PROCESS_LOCAL_UNVERIFIED"
        assert data["evidence"]["sample_size"] == 10
        assert data["evidence"]["authoritative"] is False
        assert data["portfolio"]["capital_safety_status"] == "UNKNOWN"
        assert data["portfolio"]["is_capital_safe"] is False
        assert data["promotion_gate"]["eligible"] is False
        assert data["promotion_gate"]["passed_criteria"] == []
        assert any("process-local and unverified" in reason for reason in data["promotion_gate"]["blocking_reasons"])
    finally:
        set_worker_engine(None)


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
            "signoff_user_id": "operator-test-uid",
        },
    )
    assert close_res.status_code == 200
    assert close_res.json()["status"] == "CLOSED"

    # Verify status in active list
    active_res = client.get("/incidents/8d?active_only=true")
    assert active_res.status_code == 200
    active_incidents = active_res.json()
    assert all(i["incident_id"] != inc["incident_id"] for i in active_incidents)
    closed = client.get('/incidents/8d').json()
    closed_incident = next(i for i in closed if i["incident_id"] == inc["incident_id"])
    assert closed_incident["d8_closure"]["signoff_user_id"] == "operator-test-uid"


def test_close_incident_rejects_blank_evidence(client):
    incident = client.get('/incidents/8d').json()[0]
    response = client.post(
        f"/incidents/8d/{incident['incident_id']}/close",
        json={
            "verification": "   ",
            "prevention": "prevention record",
            "lessons": "closure lessons",
            "signoff_user_id": "operator-test-uid",
        },
    )
    assert response.status_code == 422


def test_worker_image_contains_learning_engine_package():
    from pathlib import Path

    dockerfile = Path(__file__).resolve().parents[2].joinpath('Dockerfile.worker').read_text()
    assert 'COPY apps/learning_engine/ ./apps/learning_engine/' in dockerfile


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
    assert tb["evidence_status"] == "PROCESS_LOCAL_UNVERIFIED"
    assert tb["authoritative"] is False
    assert "plan_win_rate_pct" in tb
    assert "actual_win_rate_pct" in tb
    assert "drift_detected" in tb
