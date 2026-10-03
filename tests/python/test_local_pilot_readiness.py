from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.trading_worker.execution_lease import LeaseLostError
from apps.trading_worker.main import TradingWorkerApp, WorkerEngineState, WorkerExecutionMode
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.gates import DecisionExecutionGate
from apps.trading_worker.venues.binance.local_pilot_readiness import (
    local_live_pilot_readiness,
)
from apps.trading_worker.mainnet_preflight import local_pilot_flat_account_verified


def test_live_pilot_readiness_stays_blocked_until_runtime_evidence_exists(monkeypatch, tmp_path):
    from apps.trading_worker.venues.binance import local_pilot_readiness as module

    # Hermetic: do not depend on whether the checkout running the tests is clean.
    monkeypatch.setattr(module, "_git", lambda root, *args: "a" * 40 if args[0] == "rev-parse" else " M server.ts")
    readiness = local_live_pilot_readiness(tmp_path)

    assert readiness["status"] == "BLOCKED"
    assert readiness["can_approve"] is False
    assert readiness["can_start"] is False
    assert "LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN" in readiness["blockers"]
    assert "LOCAL_PILOT_CAPABILITY_EVIDENCE_MISSING_OR_STALE" in readiness["blockers"]
    assert readiness["implementation_ready"]["status"] in {"FAIL", "NOT_RUN"}
    assert readiness['implementation_ready']['checks'][0] == {
        'id': 'SOURCE_COMMIT', 'status': 'FAIL', 'reason': 'LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN',
    }
    assert all(check["status"] == "NOT_RUN" for check in readiness["implementation_ready"]["checks"][1:])
    assert readiness["approval_ready"]["status"] == "FAIL"
    assert readiness["prepared"]["status"] == "NOT_RUN"


@pytest.mark.parametrize("dirty", [True, False])
def test_ci_attestation_does_not_authorize_local_or_testnet_evidence(monkeypatch, tmp_path, dirty):
    from apps.trading_worker.venues.binance import local_pilot_readiness as module

    calls = []
    sha = "a" * 40
    monkeypatch.setattr(module, "_git", lambda root, *args: sha if args[0] == "rev-parse" else (" M server.ts" if dirty else ""))
    monkeypatch.setattr(module, "_verify", lambda *args: [])

    def verify(root, expected_sha, **kwargs):
        calls.append(expected_sha)
        return {"status": "PASS", "scope": "CI_ONLY", "gitSha": expected_sha}

    monkeypatch.setattr(module, "verify_ci_attestation", verify)
    result = module.local_live_pilot_readiness(tmp_path)
    assert result["ci_attestation"]["status"] == ("NOT_RUN" if dirty else "PASS")
    assert calls == ([] if dirty else [sha])
    assert result["status"] == "BLOCKED"
    assert result["can_approve"] is False
    assert result["can_start"] is False
    assert result["provenance"]["local_checks"] == "UNVERIFIED"
    assert "LOCAL_PILOT_TESTNET_PROVENANCE_UNVERIFIED" in result["blockers"]


def test_ci_attestation_rejects_checkout_changed_during_verification(monkeypatch, tmp_path):
    from apps.trading_worker.venues.binance import local_pilot_readiness as module

    sha = "a" * 40
    changed = False
    monkeypatch.setattr(module, "_git", lambda root, *args: sha if args[0] == "rev-parse" else (" M server.ts" if changed else ""))
    monkeypatch.setattr(module, "_verify", lambda *args: [])

    def verify(*args, **kwargs):
        nonlocal changed
        changed = True
        return {"status": "PASS", "scope": "CI_ONLY", "gitSha": sha}

    monkeypatch.setattr(module, "verify_ci_attestation", verify)
    result = module.local_live_pilot_readiness(tmp_path)
    assert result["ci_attestation"] == {"status": "FAIL", "reason": "CI_ATTESTATION_SOURCE_CHANGED"}
    assert result["can_approve"] is False


def test_first_live_preparation_with_campaign_binding_reports_monitor_not_run(monkeypatch):
    monkeypatch.setenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "pilot-bound-before-first-arm")
    worker = TradingWorkerApp.__new__(TradingWorkerApp)
    worker.execution_mode = WorkerExecutionMode.LIVE
    worker._mainnet_launch_session = None
    worker._pilot_lifecycle_monitor_started_at = None
    worker._pilot_lifecycle_monitor_completed_at = None
    worker._pilot_lifecycle_monitor_last_success_at = None
    worker._pilot_lifecycle_monitor_last_error = None

    assert worker._local_pilot_lifecycle_monitor_state()["status"] == "NOT_RUN"


def test_unbound_live_worker_does_not_claim_pilot_monitor_readiness(monkeypatch):
    monkeypatch.delenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", raising=False)
    worker = TradingWorkerApp.__new__(TradingWorkerApp)
    worker.execution_mode = WorkerExecutionMode.LIVE
    worker._mainnet_launch_session = None

    assert worker._local_pilot_lifecycle_monitor_state()["status"] == "NOT_APPLICABLE"


@pytest.mark.asyncio
async def test_disarmed_pilot_flat_account_requires_signed_empty_ledger_and_no_protection():
    class Ledger:
        positions = []
        open_orders = []

        async def get_positions(self):
            return self.positions

        async def get_open_orders(self):
            return self.open_orders

    class Protections:
        active = []

        async def list_active_protections(self, venue, symbol):
            assert venue == "binance_mainnet"
            assert symbol == "ETHUSDC"
            return self.active

    ledger = Ledger()
    protections = Protections()
    worker = SimpleNamespace(
        engine_state=WorkerEngineState.DISARMED,
        execution_adapter=None,
        persistence=SimpleNamespace(
            repository=SimpleNamespace(algo_protections=protections)
        ),
    )
    adapter = SimpleNamespace(
        preflight_only=True,
        account_snapshot=SimpleNamespace(
            valid=True,
            exchange_environment="BINANCE_MAINNET",
            total_position_notional=Decimal("0"),
        ),
        reconciliation=SimpleNamespace(last_status="IN_SYNC"),
        ledger=ledger,
    )

    assert await local_pilot_flat_account_verified(worker, adapter) is True

    ledger.open_orders = [SimpleNamespace()]
    assert await local_pilot_flat_account_verified(worker, adapter) is False
    ledger.open_orders = []
    protections.active = [{"entry_client_order_id": "unknown"}]
    assert await local_pilot_flat_account_verified(worker, adapter) is False


@pytest.mark.asyncio
async def test_pilot_bound_adapter_rejects_new_risk_before_order_gate(monkeypatch):
    monkeypatch.setattr(
        "apps.trading_worker.venues.binance.execution.local_live_pilot_readiness",
        lambda: {"can_start": False},
    )
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.MAINNET
    adapter.preflight_only = False
    adapter.require_testnet_protection = False
    authority = SimpleNamespace(_mainnet_launch_session={"policy": "LIVE_RESEARCH_PILOT"})
    adapter._worker_authority = authority
    decision = SimpleNamespace(risk_class="NEW_RISK", action="SUBMIT", decision_id="test")

    result = await adapter._execute_decision(decision, authority=authority)

    assert result == []


@pytest.mark.asyncio
async def test_pilot_final_send_fence_rejects_even_if_earlier_checks_were_bypassed(monkeypatch):
    monkeypatch.setattr(
        "apps.trading_worker.venues.binance.execution.local_live_pilot_readiness",
        lambda: {"can_start": False},
    )
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter._worker_authority = SimpleNamespace(_mainnet_launch_session={"policy": "LIVE_RESEARCH_PILOT"})

    with pytest.raises(LeaseLostError, match="capability gate is not ready"):
        await adapter._final_risk_increase_fence(
            SimpleNamespace(), SimpleNamespace(), object(), client_order_id="entry",
            reserved_open_orders=0, reserved_notional=0, allow_emergency_fallback=False,
        )


@pytest.mark.asyncio
async def test_private_pilot_fill_persists_realized_pnl_and_fee_idempotency_keys(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")

    class Repository:
        def __init__(self):
            self.events = []

        async def append_local_live_pilot_event(self, **event):
            self.events.append(event)
            return {
                "pilot_drawdown_triggered": False,
                "pilot_status": "ACTIVE",
                "state": "PAUSED_NEW_RISK",
                "pilot_accounting_resume_eligible": True,
            }

    repository = Repository()
    app = TradingWorkerApp.__new__(TradingWorkerApp)
    app.execution_mode = WorkerExecutionMode.LIVE
    app._mainnet_launch_session = {
        "launch_id": "launch-pilot-test",
        "policy": "LIVE_RESEARCH_PILOT",
        "pilot_campaign_id": "pilot-test-campaign",
    }
    app.persistence = SimpleNamespace(
        repository=repository,
        is_connected=True,
    )
    app.pause_new_risk = False
    app.engine_state = WorkerEngineState.ARMED
    app._pilot_accounting_pause_active = True
    fill = SimpleNamespace(
        symbol="ETHUSDC",
        commission_asset="USDC",
        transaction_time=1_780_000_000_000,
        exchange_trade_id="trade-42",
        exchange_order_id="order-7",
        client_order_id="client-7",
        commission=Decimal("0.03"),
        realized_pnl=Decimal("0.50"),
        quantity=Decimal("0.1"),
        price=Decimal(2500),
        side=SimpleNamespace(value="BUY"),
        position_side=SimpleNamespace(value="BOTH"),
    )

    assert await app._persist_local_live_pilot_fill(fill) is True
    assert app.pause_new_risk is True
    assert app.engine_state == WorkerEngineState.PAUSED_NEW_RISK
    assert [(event["event_type"], event["event_key"], event["net_pnl_delta_usdc"])
            for event in repository.events] == [
        ("FEE", "FEE:trade-42", Decimal("-0.03")),
        ("FILL", "FILL:trade-42", Decimal("0.50")),
    ]
    assert all(event["source"] == "BINANCE" for event in repository.events)
    assert all(event["payload"]["run_id"] == "launch-pilot-test" for event in repository.events)

    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    app.kill_switch_active = False
    app._mainnet_configured = lambda: True
    app._enabled_strategies = lambda: {"grid"}
    app.execution_adapter = SimpleNamespace(
        connection_state="READY",
        authenticated=True,
        capabilities=SimpleNamespace(trade_authorized=True),
        private_stream_healthy=True,
        reconciliation=SimpleNamespace(last_status="IN_SYNC"),
    )
    app.decision_execution_gate = DecisionExecutionGate(app)
    decision = SimpleNamespace(
        risk_class="NEW_RISK",
        action="EXECUTE",
        orders=[SimpleNamespace(strategy_id="grid", symbol="ETHUSDC")],
    )
    allowed, reason = app._evaluate_execution_gate(decision)
    assert allowed is False
    assert reason == "Paused new risk"


@pytest.mark.asyncio
async def test_pilot_fresh_mark_resumes_only_its_accounting_pause():
    event_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    events = []

    class Repository:
        async def append_local_live_pilot_event(self, **event):
            events.append(event)
            assert event["event_type"] == "MARK"
            return {
                "pilot_drawdown_triggered": False,
                "pilot_accounting_resumed": True,
            }

    app = TradingWorkerApp.__new__(TradingWorkerApp)
    app._mainnet_launch_session = {
        "launch_id": "launch-pilot-test",
        "policy": "LIVE_RESEARCH_PILOT",
        "pilot_campaign_id": "pilot-test-campaign",
    }
    app.persistence = SimpleNamespace(repository=Repository(), is_connected=True)
    app.execution_adapter = SimpleNamespace(reconciliation=SimpleNamespace(last_status="IN_SYNC"))
    app.pause_new_risk = True
    app.engine_state = WorkerEngineState.PAUSED_NEW_RISK
    app._pilot_accounting_pause_active = True
    app.kill_switch_active = False
    app.reconciliation_status = "IN_SYNC"
    app._refresh_engine_state = lambda: setattr(app, "engine_state", WorkerEngineState.ARMED)

    assert await app._persist_local_live_pilot_mark(
        "ETHUSDC", f"{event_ms}:ETHUSDC:BOTH", Decimal("0.10"), event_ms,
        snapshot_scope="ETHUSDC_SIGNED_POSITION_RISK_REQUEST_WINDOW_START",
    ) is True
    assert events[0]["payload"]["snapshot_scope"] == (
        "ETHUSDC_SIGNED_POSITION_RISK_REQUEST_WINDOW_START"
    )
    assert app.pause_new_risk is False
    assert app._pilot_accounting_pause_active is False
    assert app.engine_state == WorkerEngineState.ARMED


@pytest.mark.asyncio
async def test_pilot_fresh_mark_does_not_resume_while_exchange_reconciliation_is_unknown():
    event_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    class Repository:
        async def append_local_live_pilot_event(self, **_event):
            return {"pilot_drawdown_triggered": False, "pilot_accounting_resumed": True}

    app = TradingWorkerApp.__new__(TradingWorkerApp)
    app._mainnet_launch_session = {
        "launch_id": "launch-pilot-test",
        "policy": "LIVE_RESEARCH_PILOT",
        "pilot_campaign_id": "pilot-test-campaign",
    }
    app.persistence = SimpleNamespace(repository=Repository(), is_connected=True)
    app.execution_adapter = SimpleNamespace(reconciliation=SimpleNamespace(last_status="UNKNOWN"))
    app.pause_new_risk = True
    app.engine_state = WorkerEngineState.PAUSED_NEW_RISK
    app._pilot_accounting_pause_active = True
    app.kill_switch_active = False
    app.reconciliation_status = "IN_SYNC"

    assert await app._persist_local_live_pilot_mark(
        "ETHUSDC", f"{event_ms}:ETHUSDC:BOTH", Decimal("0.10"), event_ms
    ) is True
    assert app.pause_new_risk is True
    assert app._pilot_accounting_pause_active is True
    assert app.engine_state == WorkerEngineState.PAUSED_NEW_RISK


@pytest.mark.asyncio
async def test_local_pilot_accounting_api_requires_fresh_reconciled_consistent_components(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")

    class Persistence:
        is_connected = True

        def __init__(self, snapshot):
            self.snapshot = snapshot

        async def get_local_live_pilot_accounting(self, launch_id):
            assert launch_id == "launch-pilot-test"
            return self.snapshot

    async def readback(snapshot, *, reconciliation="IN_SYNC"):
        app = TradingWorkerApp.__new__(TradingWorkerApp)
        app.execution_mode = WorkerExecutionMode.LIVE
        app._mainnet_launch_session = {
            "launch_id": "launch-pilot-test",
            "pilot_campaign_id": "pilot-test-campaign",
            "policy": "LIVE_RESEARCH_PILOT",
            "runtime_target": "LOCAL",
        }
        app.persistence = Persistence(snapshot)
        app.execution_adapter = SimpleNamespace(
            env=BinanceEnvironment.MAINNET,
            private_stream_healthy=True,
            reconciliation=SimpleNamespace(last_status=reconciliation),
        )
        app.reconciliation_status = reconciliation
        app.authenticated = True
        return await app.get_local_live_pilot_accounting()

    base = {
        "pilot_net_pnl_usdc": Decimal("1.25"),
        "pilot_peak_pnl_usdc": Decimal("1.50"),
        "pilot_last_account_snapshot_at": datetime.now(timezone.utc),
        "realized_pnl_usdc": Decimal("1.00"),
        "unrealized_pnl_usdc": Decimal("0.30"),
        "fees_usdc": Decimal("0.10"),
        "funding_usdc": Decimal("0.05"),
        "last_financial_event_id": 4,
        "last_mark_event_id": 5,
        "last_event_at": datetime.now(timezone.utc),
    }
    verified = await readback(base)
    assert verified["status"] == "VERIFIED"
    assert verified["net_pnl_usdc"] == "1.25"
    assert verified["drawdown_usdc"] == "0.25"
    assert verified["slippage_usdc"] == "UNKNOWN"
    stale = await readback({
        **base,
        "pilot_last_account_snapshot_at": datetime.now(timezone.utc) - timedelta(seconds=6),
    })
    assert stale["status"] == "STALE"
    assert stale["net_pnl_usdc"] == "UNKNOWN"

    mismatch = await readback({**base, "pilot_net_pnl_usdc": Decimal("1.26")})
    assert mismatch["status"] == "MISMATCH"
    assert mismatch["net_pnl_usdc"] == "UNKNOWN"

    unreconciled = await readback(base, reconciliation="UNKNOWN")
    assert unreconciled["status"] == "RECONCILIATION_UNKNOWN"
    assert unreconciled["net_pnl_usdc"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_confirmed_pilot_order_sets_a_recoverable_pause_not_a_terminal_campaign():
    async def mark_submitted(_launch_id, _client_order_id):
        return True

    async def read_session(_launch_id):
        return {
            "launch_id": "launch-pilot-test",
            "policy": "LIVE_RESEARCH_PILOT",
            "state": "ACTIVE",
            "pilot_status": "ACTIVE",
        }

    app = TradingWorkerApp.__new__(TradingWorkerApp)
    app.execution_mode = WorkerExecutionMode.LIVE
    app._mainnet_launch_id = "launch-pilot-test"
    app._mainnet_launch_session = {
        "launch_id": "launch-pilot-test",
        "policy": "LIVE_RESEARCH_PILOT",
        "state": "ACTIVE",
        "pilot_status": "ACTIVE",
    }
    app.persistence = SimpleNamespace(
        mark_mainnet_risk_order_submitted=mark_submitted,
        get_mainnet_launch_session=read_session,
    )
    app.pause_new_risk = False
    app._pilot_accounting_pause_active = False
    app.engine_state = WorkerEngineState.ARMED
    app._refresh_engine_state = lambda: setattr(
        app, "engine_state",
        WorkerEngineState.PAUSED_NEW_RISK if app.pause_new_risk else WorkerEngineState.ARMED,
    )

    await app._on_order_submission_result(
        SimpleNamespace(risk_class="NEW_RISK", client_order_id="entry-pilot-1"),
        "CONFIRMED",
    )

    assert app.pause_new_risk is True
    assert app._pilot_accounting_pause_active is True
    assert app._mainnet_launch_session["pilot_status"] == "ACTIVE"
    assert app._mainnet_launch_session["state"] == "ACTIVE"


@pytest.mark.asyncio
async def test_private_pilot_fill_unknown_fee_asset_fails_closed(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    app = TradingWorkerApp.__new__(TradingWorkerApp)
    app.execution_mode = WorkerExecutionMode.LIVE
    app._mainnet_launch_session = {
        "launch_id": "launch-pilot-test",
        "policy": "LIVE_RESEARCH_PILOT",
        "pilot_campaign_id": "pilot-test-campaign",
    }
    app.persistence = SimpleNamespace(
        repository=SimpleNamespace(append_local_live_pilot_event=lambda **_: None),
        is_connected=True,
    )
    app.pause_new_risk = False
    app.engine_state = WorkerEngineState.ARMED
    fill = SimpleNamespace(symbol="ETHUSDC", commission_asset="BNB")

    assert await app._persist_local_live_pilot_fill(fill) is False
    assert app.pause_new_risk is True
    assert app.engine_state == WorkerEngineState.DEGRADED


@pytest.mark.asyncio
async def test_private_position_mark_is_durable_and_triggers_risk_pause():
    event_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    class Repository:
        async def append_local_live_pilot_event(self, **event):
            assert event["event_type"] == "MARK"
            assert event["event_key"] == f"MARK:{event_ms}:ETHUSDC:BOTH"
            assert event["payload"]["unrealized_pnl_usdc"] == "-5.25"
            assert event["payload"]["snapshot_scope"] == "ETHUSDC_POSITION_ACCOUNT_UPDATE"
            return {"pilot_drawdown_triggered": True}

    app = TradingWorkerApp.__new__(TradingWorkerApp)
    app._mainnet_launch_session = {
        "launch_id": "launch-pilot-test",
        "policy": "LIVE_RESEARCH_PILOT",
        "pilot_campaign_id": "pilot-test-campaign",
    }
    app.persistence = SimpleNamespace(repository=Repository(), is_connected=True)
    app.pause_new_risk = False
    app.engine_state = WorkerEngineState.ARMED

    assert await app._persist_local_live_pilot_mark(
        "ETHUSDC", f"{event_ms}:ETHUSDC:BOTH", Decimal("-5.25"), event_ms
    ) is True
    assert app.pause_new_risk is True
    assert app.engine_state == WorkerEngineState.PAUSED_NEW_RISK


@pytest.mark.asyncio
async def test_mainnet_private_fill_event_routes_into_pilot_accounting_callback(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")

    class Ledger:
        def __init__(self):
            self.order = SimpleNamespace(
                client_order_id="client-9", status="NEW", exchange_order_id=None,
                strategy_id="quick", decision_id="decision-1", target_exposure_id="exposure-1",
                source_intent_ids=["intent-1"],
            )
            self.fills = []

        async def get_order_by_client_id(self, client_order_id):
            return self.order if client_order_id == self.order.client_order_id else None

        async def upsert_order(self, order):
            self.order = order

        async def set_account_snapshot(self, snapshot):
            self.account_snapshot = snapshot

        async def append_fill(self, fill):
            self.fills.append(fill)

    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.MAINNET
    adapter.ledger = Ledger()
    adapter.last_order_event_at = {}
    adapter.reconciliation = SimpleNamespace(last_status="IN_SYNC", last_diffs=[])
    received = []

    async def write_fill(fill):
        received.append(fill)
        return True

    adapter.on_local_live_pilot_fill = write_fill
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    await adapter._on_ws_event({
        "e": "ORDER_TRADE_UPDATE",
        "E": now_ms,
        "o": {
            "s": "ETHUSDC", "c": "client-9", "X": "PARTIALLY_FILLED",
            "i": 555, "x": "TRADE", "t": 77, "l": "0.1", "L": "2500",
            "n": "0.01", "N": "USDC", "rp": "0", "m": False, "T": now_ms,
            "S": "BUY", "ps": "BOTH",
        },
    })

    assert len(adapter.ledger.fills) == 1
    assert received == adapter.ledger.fills
    assert received[0].exchange_trade_id == "77"
    assert received[0].commission == Decimal("0.01")


@pytest.mark.asyncio
async def test_funding_trigger_reconciles_signed_income_to_active_durable_owner(monkeypatch):
    from apps.trading_worker.venues.binance.config import BinanceEnvironment

    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    now = datetime.now(timezone.utc)
    launch_started = now.replace(microsecond=0)
    first_fill = now.replace(microsecond=0)
    income_ms = int(now.timestamp() * 1000)
    appended = []

    class Owners:
        async def list_protections(self, venue, symbol):
            assert venue == "binance_mainnet"
            assert symbol == "ETHUSDC"
            return [{
                "mainnet_launch_id": "launch-pilot-test",
                "entry_client_order_id": "entry-11",
                "filled_quantity": Decimal("0.02"),
                "first_fill_at": first_fill,
                "closed_at": None,
            }]

    class Repository:
        algo_protections = Owners()

        async def append_local_live_pilot_event(self, **event):
            appended.append(event)
            return {"pilot_drawdown_triggered": False}

    class Rest:
        async def request(self, method, path, **kwargs):
            assert (method, path, kwargs["signed"]) == ("GET", "/fapi/v1/income", True)
            assert kwargs["params"]["incomeType"] == "FUNDING_FEE"
            assert kwargs["params"]["symbol"] == "ETHUSDC"
            return [{
                "symbol": "ETHUSDC", "incomeType": "FUNDING_FEE", "asset": "USDC",
                "income": "-0.01250000", "time": income_ms, "tranId": 90001,
            }]

    app = TradingWorkerApp.__new__(TradingWorkerApp)
    app.execution_mode = WorkerExecutionMode.LIVE
    app._mainnet_launch_session = {
        "launch_id": "launch-pilot-test",
        "pilot_campaign_id": "pilot-test-campaign",
        "policy": "LIVE_RESEARCH_PILOT",
        "created_at": launch_started,
    }
    app.persistence = SimpleNamespace(repository=Repository(), is_connected=True)
    app.execution_adapter = SimpleNamespace(
        env=BinanceEnvironment.MAINNET,
        reconciliation=SimpleNamespace(_income_path="/fapi/v1/income"),
        rest_client=Rest(),
    )
    app.pause_new_risk = False
    app.engine_state = WorkerEngineState.ARMED

    assert await app._reconcile_local_live_pilot_funding("ETHUSDC", income_ms) is True
    assert len(appended) == 1
    assert appended[0]["event_type"] == "FUNDING"
    assert appended[0]["event_key"] == "FUNDING:90001"
    assert appended[0]["net_pnl_delta_usdc"] == Decimal("-0.01250000")
    assert appended[0]["payload"]["owner_entry_client_order_id"] == "entry-11"


@pytest.mark.asyncio
async def test_funding_account_update_dispatches_symbol_and_event_time_to_reconciler(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")

    class Ledger:
        async def set_account_snapshot(self, _snapshot):
            return None

        async def get_positions(self):
            return []

        async def upsert_position(self, _position):
            return None

    class Reconciliation:
        last_status = "IN_SYNC"
        last_diffs = []

    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.MAINNET
    adapter.ledger = Ledger()
    adapter.reconciliation = Reconciliation()
    adapter.on_local_live_pilot_mark = None
    callback_values = []
    mark_reconciliation_states = []

    async def reconcile(symbol, event_time):
        callback_values.append((symbol, event_time))
        return True

    async def persist_mark(*_args):
        mark_reconciliation_states.append(adapter.reconciliation.last_status)
        return True

    adapter.on_local_live_pilot_funding_reconcile = reconcile
    adapter.on_local_live_pilot_mark = persist_mark
    event_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    await adapter._on_ws_event({
        "e": "ACCOUNT_UPDATE",
        "E": event_ms,
        "a": {
            "m": "FUNDING_FEE", "S": "ETHUSDC", "B": [],
            "P": [{"s": "ETHUSDC", "ps": "BOTH", "pa": "0.1", "ep": "100", "up": "0.2"}],
        },
    })

    assert callback_values == [("ETHUSDC", event_ms)]
    assert mark_reconciliation_states == ["UNKNOWN"]
    assert adapter.reconciliation.last_status == "UNKNOWN"
