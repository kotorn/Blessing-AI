import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.trading_worker.venues.binance.protected_ethusdc_trial import (
    plan_protected_ethusdc_trial,
)
from apps.trading_worker.venues.binance import protected_ethusdc_testnet_runner as trial_runner
from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.main import TradingWorkerApp, WorkerExecutionMode


def rules() -> SymbolTradingRules:
    value = SymbolTradingRules("ETHUSDC")
    value.status = "TRADING"
    value.contract_type = "PERPETUAL"
    value.quote_asset = "USDC"
    value.margin_asset = "USDC"
    value.tick_size = Decimal("0.01")
    value.step_size = Decimal("0.001")
    value.min_qty = Decimal("0.001")
    value.max_qty = Decimal("100")
    value.market_step_size = Decimal("0.001")
    value.market_min_qty = Decimal("0.001")
    value.market_max_qty = Decimal("100")
    value.min_notional = Decimal("5")
    return value


def plan(**overrides):
    now = datetime(2026, 9, 28, tzinfo=timezone.utc)
    values = {
        "rules": rules(),
        "bid": Decimal("2499"),
        "ask": Decimal("2500"),
        "quote_observed_at": now,
        "estimated_roundtrip_cost_usdc": Decimal("0.10"),
        "actual_leverage": 2,
        "now": now,
    }
    values.update(overrides)
    return plan_protected_ethusdc_trial(**values)


def test_protected_ethusdc_trial_plan_stays_below_notional_and_loss_caps():
    result = plan()
    assert result.quantity > 0
    assert result.conservative_notional_usdc <= Decimal("50")
    assert result.planned_loss_usdc <= Decimal("2")
    assert result.stop_trigger < Decimal("2499")
    assert result.target_trigger > Decimal("2500")


def test_protected_ethusdc_trial_rejects_filters_that_require_larger_order():
    restrictive = rules()
    restrictive.min_notional = Decimal("60")
    with pytest.raises(ValueError, match="50 USDC"):
        plan(rules=restrictive)


def test_protected_ethusdc_trial_rejects_stale_quote_missing_cost_and_high_leverage():
    old = datetime(2026, 9, 28, tzinfo=timezone.utc) - timedelta(seconds=6)
    with pytest.raises(ValueError, match="stale"):
        plan(quote_observed_at=old)
    with pytest.raises(ValueError, match="cost evidence"):
        plan(estimated_roundtrip_cost_usdc=Decimal("0"))
    with pytest.raises(ValueError, match="leverage"):
        plan(actual_leverage=11)


def test_protected_ethusdc_trial_rejects_risk_budget_overrun():
    with pytest.raises(ValueError, match="planned loss"):
        plan(estimated_roundtrip_cost_usdc=Decimal("1.6"))


@pytest.mark.asyncio
async def test_testnet_trial_refuses_to_create_worker_without_explicit_approval(monkeypatch):
    monkeypatch.delenv("TESTNET_PROTECTED_ETHUSDC_TRIAL_APPROVED", raising=False)
    monkeypatch.setattr(trial_runner, "TradingWorkerApp", lambda **_kwargs: pytest.fail("Worker started"))
    with pytest.raises(RuntimeError, match="not approved"):
        await trial_runner.run_protected_ethusdc_testnet_trial()


@pytest.mark.asyncio
async def test_testnet_trial_requires_clean_commit_before_worker_creation(monkeypatch):
    monkeypatch.setattr(trial_runner, "_approved_testnet_runtime", lambda: None)
    monkeypatch.setattr(trial_runner, "_current_sha", lambda: (_ for _ in ()).throw(RuntimeError("dirty")))
    monkeypatch.setattr(trial_runner, "TradingWorkerApp", lambda **_kwargs: pytest.fail("Worker started"))
    with pytest.raises(RuntimeError, match="dirty"):
        await trial_runner.run_protected_ethusdc_testnet_trial()


@pytest.mark.asyncio
async def test_protected_trial_worker_path_records_exchange_readback_shape(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(trial_runner, "_approved_testnet_runtime", lambda: None)
    monkeypatch.setattr(trial_runner, "_current_sha", lambda: "a" * 40)
    calls = []

    async def signed_request(method, route, **_kwargs):
        calls.append((method, route))
        if route.endswith("positionRisk"):
            return [{"symbol": "ETHUSDC", "positionAmt": "0", "positionSide": "BOTH", "leverage": "2"}]
        if route.endswith("commissionRate"):
            return {"takerCommissionRate": "0.0004"}
        return []

    adapter = SimpleNamespace(
        env=BinanceEnvironment.TESTNET,
        rest_client=SimpleNamespace(request=signed_request),
        _position_risk_path="/fapi/v2/positionRisk",
        _open_algo_orders_path="/fapi/v1/openAlgoOrders",
        get_best_bid_ask=AsyncMock(return_value=(Decimal("2499"), Decimal("2500"))),
        last_market_event_at={"ETHUSDC": datetime.now(timezone.utc)},
        symbol_rules={"ETHUSDC": rules()},
        safety_limits=SimpleNamespace(max_single_order_notional=Decimal("50")),
        last_testnet_protection={},
        ledger=SimpleNamespace(get_fills=AsyncMock(return_value=[
            SimpleNamespace(client_order_id="unused", symbol="ETHUSDC")
        ])),
    )

    class FakeWorker:
        def __init__(self, symbols):
            assert symbols == ["ETHUSDC"]
            self.execution_adapter = adapter
            self.persistence = SimpleNamespace(repository=SimpleNamespace(
                algo_protections=SimpleNamespace(list_active_protections=AsyncMock(return_value=[]))
            ))

        async def arm(self, request):
            assert request.executionMode == "TESTNET"
            return True, ""

        async def execute_protected_testnet_decision(self, decision):
            entry_id = decision.orders[0].client_order_id
            adapter.last_testnet_protection = {
                "status": "PROTECTED", "stop_client_algo_id": "stop-1",
                "take_profit_client_algo_id": "target-1",
            }
            adapter.ledger.get_fills = AsyncMock(return_value=[
                SimpleNamespace(client_order_id=entry_id, symbol="ETHUSDC")
            ])
            return [SimpleNamespace(client_order_id=entry_id)]

        async def close_protected_ethusdc_testnet_trial(self, entry_id):
            return {
                "close_status": "VERIFIED", "close_client_order_id": "close-1",
                "close_order_type": "MARKET", "close_order_reduce_only": True,
                "protection_at_close": {
                    "status": "PROTECTED", "observed_at": "2026-10-03T00:00:00.000Z",
                    "close_submission_at": "2026-10-03T00:00:00.000Z",
                    "stop": {"algo_id": "101", "client_algo_id": "stop-1",
                             "order_type": "STOP_MARKET", "status": "NEW",
                             "close_position": True, "reduce_only": False},
                    "target": {"algo_id": "102", "client_algo_id": "target-1",
                               "order_type": "TAKE_PROFIT_MARKET", "status": "NEW",
                               "close_position": True, "reduce_only": False},
                },
                "position_after": [], "open_orders_after": [], "open_algo_after": [],
                "reconciliation_status": "IN_SYNC", "diff_count": 0,
            }

        async def disarm(self):
            calls.append(("DISARM", ""))

    monkeypatch.setattr(trial_runner, "TradingWorkerApp", FakeWorker)
    result = await trial_runner.run_protected_ethusdc_testnet_trial()
    assert result["status"] == "PASS"
    assert result["entry_fill_count"] == 1
    assert result["close_client_order_id"] == "close-1"
    assert ("DISARM", "") in calls
    saved = list((tmp_path / "artifacts").glob("testnet-trial-*.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text())["status"] == "PASS"


@pytest.mark.asyncio
async def test_trial_close_persists_close_pending_before_one_reduction_and_verifies_readback():
    timeline = []
    claimed_reason = "protected_ethusdc_testnet_trial_close:close-1:CLAIMED"
    owner = {
        "entry_client_order_id": "entry-1", "state": "PROTECTED",
        "environment": "TESTNET", "venue": "binance_testnet", "symbol": "ETHUSDC",
        "entry_side": "BUY", "position_side": "BOTH", "requested_quantity": Decimal("0.01"),
        "filled_quantity": Decimal("0.01"),
        "stop_client_algo_id": "stop-1", "take_profit_client_algo_id": "target-1",
        "stop_algo_id": "101", "take_profit_algo_id": "102",
    }

    async def persist(record):
        timeline.append(record["state"])
        return True

    async def claim(**values):
        timeline.append("CLOSE_PENDING")
        return {**owner, "state": "CLOSE_PENDING", "state_reason": values["state_reason"]}

    async def mark_submitting(**values):
        timeline.append("SUBMITTING")
        return {**owner, "state": "CLOSE_PENDING", "state_reason": values["submitting_reason"]}

    async def close_owned(record, *, authority):
        assert record["state"] == "CLOSE_PENDING"
        assert record["state_reason"] == claimed_reason
        assert authority is worker
        marked = await authority.claim_testnet_trial_close_submission(record, "close-1")
        assert marked["state_reason"].endswith(":SUBMITTING")
        assert timeline == ["CLOSE_PENDING", "SUBMITTING"]
        return [SimpleNamespace(client_order_id="close-1", exchange_order_id="9001")]

    async def request(_method, path, **_kwargs):
        if path.endswith("positionRisk"):
            return [{"symbol": "ETHUSDC", "positionAmt": "0"}]
        return []

    adapter = SimpleNamespace(
        env=BinanceEnvironment.TESTNET,
        last_emergency_result={
            "status": "CONFIRMED",
            "protection_at_close": {
                "status": "PROTECTED", "observed_at": "2026-10-03T00:00:00.000Z",
                "close_submission_at": "2026-10-03T00:00:00.000Z",
                "stop": {"algo_id": "101", "client_algo_id": "stop-1",
                         "order_type": "STOP_MARKET", "status": "NEW",
                         "close_position": True, "reduce_only": False},
                "target": {"algo_id": "102", "client_algo_id": "target-1",
                           "order_type": "TAKE_PROFIT_MARKET", "status": "NEW",
                           "close_position": True, "reduce_only": False},
            },
        },
        testnet_trial_close_client_order_id=lambda _entry: "close-1",
        close_owned_testnet_trial=close_owned,
        query_order=AsyncMock(return_value={
            "status": "FILLED", "orderId": 9001, "clientOrderId": "close-1", "symbol": "ETHUSDC",
            "side": "SELL", "positionSide": "BOTH", "reduceOnly": True,
            "type": "MARKET", "origQty": "0.01", "executedQty": "0.01",
        }),
        _cancel_local_mainnet_owned_algos=AsyncMock(return_value=True),
        rest_client=SimpleNamespace(request=request),
        _position_risk_path="/fapi/v2/positionRisk",
        _open_algo_orders_path="/fapi/v1/openAlgoOrders",
        reconciliation=SimpleNamespace(reconcile=AsyncMock(return_value="IN_SYNC"), last_diffs=[]),
    )
    worker = TradingWorkerApp.__new__(TradingWorkerApp)
    worker.execution_mode = WorkerExecutionMode.TESTNET
    worker.execution_adapter = adapter
    worker.persistence = SimpleNamespace(repository=SimpleNamespace(
        algo_protections=SimpleNamespace(
            list_active_protections=AsyncMock(return_value=[owner]),
            claim_testnet_protection_close=claim,
            mark_testnet_protection_close_submitting=mark_submitting,
        )
    ))
    worker.pause_new_risk = False
    worker._refresh_engine_state = lambda: None
    worker._persist_testnet_protection_update = persist

    result = await worker.close_protected_ethusdc_testnet_trial("entry-1")
    assert result["close_status"] == "VERIFIED"
    assert timeline == ["CLOSE_PENDING", "SUBMITTING", "CLOSED"]
    assert worker.pause_new_risk is True
    adapter._cancel_local_mainnet_owned_algos.assert_awaited_once()


@pytest.mark.asyncio
async def test_ambiguous_testnet_close_stays_close_pending_without_algo_cancel():
    owner = {
        "entry_client_order_id": "entry-1", "state": "PROTECTED",
        "environment": "TESTNET", "venue": "binance_testnet", "symbol": "ETHUSDC",
        "entry_side": "BUY", "position_side": "BOTH", "requested_quantity": Decimal("0.01"),
        "filled_quantity": Decimal("0.01"),
        "stop_client_algo_id": "stop-1", "take_profit_client_algo_id": "target-1",
        "stop_algo_id": "101", "take_profit_algo_id": "102",
    }
    persisted = []

    async def claim(**values):
        persisted.append(values["state_reason"])
        return {**owner, "state": "CLOSE_PENDING", "state_reason": values["state_reason"]}

    adapter = SimpleNamespace(
        env=BinanceEnvironment.TESTNET,
        last_emergency_result={"status": "UNKNOWN"},
        testnet_trial_close_client_order_id=lambda _entry: "close-1",
        _cancel_local_mainnet_owned_algos=AsyncMock(return_value=True),
        close_owned_testnet_trial=AsyncMock(return_value=[]),
    )
    worker = TradingWorkerApp.__new__(TradingWorkerApp)
    worker.execution_mode = WorkerExecutionMode.TESTNET
    worker.execution_adapter = adapter
    worker.persistence = SimpleNamespace(repository=SimpleNamespace(
        algo_protections=SimpleNamespace(list_active_protections=AsyncMock(return_value=[owner]))
    ))
    worker.pause_new_risk = False
    worker._refresh_engine_state = lambda: None
    worker._persist_testnet_protection_update = AsyncMock(return_value=True)
    worker.persistence.repository.algo_protections.claim_testnet_protection_close = claim

    with pytest.raises(RuntimeError, match="outcome is unknown"):
        await worker.close_protected_ethusdc_testnet_trial("entry-1")
    assert persisted == ["protected_ethusdc_testnet_trial_close:close-1:CLAIMED"]
    adapter._cancel_local_mainnet_owned_algos.assert_not_awaited()


@pytest.mark.asyncio
async def test_owned_testnet_close_refuses_quantity_or_direction_mismatch_before_send():
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.TESTNET
    adapter._worker_authorized = lambda _authority: True
    adapter._mutation_lock = asyncio.Lock()
    adapter.last_emergency_result = {}
    adapter.query_order = AsyncMock(side_effect=[None, {
        "symbol": "ETHUSDC", "orderId": 77, "clientOrderId": "entry-1", "side": "BUY",
        "positionSide": "BOTH", "origQty": "0.01", "executedQty": "0.01", "status": "FILLED",
    }])
    adapter.rest_client = SimpleNamespace(portfolio_margin=False, request=AsyncMock(return_value=[{
        "symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "-0.01",
    }]))
    fill = SimpleNamespace(
        client_order_id="entry-1", exchange_order_id="77", symbol="ETHUSDC",
        side="BUY", position_side="BOTH", quantity=Decimal("0.01"),
    )
    local_entry = SimpleNamespace(
        client_order_id="entry-1", exchange_order_id="77", symbol="ETHUSDC", side="BUY",
    )
    adapter.ledger = SimpleNamespace(
        get_order_by_client_id=AsyncMock(return_value=local_entry),
        get_fills=AsyncMock(return_value=[fill]), replace_positions=AsyncMock(),
    )
    adapter.reconciliation = SimpleNamespace(_recover_order_fills=AsyncMock())
    adapter._execute_decision = AsyncMock(side_effect=AssertionError("close must not be sent"))
    owner = {
        "environment": "TESTNET", "venue": "binance_testnet", "symbol": "ETHUSDC",
        "entry_client_order_id": "entry-1", "state": "CLOSE_PENDING",
        "entry_side": "BUY", "position_side": "BOTH", "requested_quantity": Decimal("0.01"),
        "filled_quantity": Decimal("0.01"),
        "state_reason": "protected_ethusdc_testnet_trial_close:BAI-TC-"
        + hashlib.sha256(b"entry-1").hexdigest()[:16] + ":CLAIMED",
    }

    orders = await adapter.close_owned_testnet_trial(owner, authority=object())

    assert orders == []
    assert adapter.last_emergency_result["status"] == "UNKNOWN"
    assert adapter.last_emergency_result["reason"] == "trial_position_quantity_or_direction_mismatch"
    adapter._execute_decision.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("protection_state", ["PROTECTED", "AMBIGUOUS"])
async def test_owned_testnet_close_requires_fresh_open_protection_before_submission(protection_state):
    events = []
    entry_id = "entry-protected"
    close_id = BinanceExecutionAdapter.testnet_trial_close_client_order_id(entry_id)
    owner = {
        "environment": "TESTNET", "venue": "binance_testnet", "symbol": "ETHUSDC",
        "entry_client_order_id": entry_id, "state": "CLOSE_PENDING", "entry_side": "BUY",
        "position_side": "BOTH", "requested_quantity": Decimal("0.01"),
        "filled_quantity": Decimal("0.01"), "stop_trigger_price": Decimal("2400"),
        "take_profit_trigger_price": Decimal("2600"), "stop_algo_id": "101",
        "take_profit_algo_id": "102", "stop_client_algo_id": "stop-1",
        "take_profit_client_algo_id": "target-1",
        "state_reason": f"protected_ethusdc_testnet_trial_close:{close_id}:CLAIMED",
    }
    entry_exchange = {
        "symbol": "ETHUSDC", "orderId": 77, "clientOrderId": entry_id, "side": "BUY",
        "positionSide": "BOTH", "origQty": "0.01", "executedQty": "0.01", "status": "FILLED",
    }
    close_exchange = {
        "symbol": "ETHUSDC", "orderId": 88, "clientOrderId": close_id, "side": "SELL",
        "positionSide": "BOTH", "origQty": "0.01", "executedQty": "0.01", "status": "FILLED",
        "type": "MARKET", "reduceOnly": "true",
    }
    fill = SimpleNamespace(
        client_order_id=entry_id, exchange_order_id="77", symbol="ETHUSDC", side="BUY",
        position_side="BOTH", quantity=Decimal("0.01"), price=Decimal("2500"),
    )
    local_entry = SimpleNamespace(
        client_order_id=entry_id, exchange_order_id="77", symbol="ETHUSDC", side="BUY",
    )
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.TESTNET
    adapter._worker_authorized = lambda _authority: True
    adapter._mutation_lock = asyncio.Lock()
    adapter.last_emergency_result = {}
    adapter.query_order = AsyncMock(side_effect=[None, entry_exchange, close_exchange])
    adapter.ledger = SimpleNamespace(
        get_order_by_client_id=AsyncMock(return_value=local_entry),
        get_fills=AsyncMock(return_value=[fill]), replace_positions=AsyncMock(),
    )
    adapter.reconciliation = SimpleNamespace(
        _recover_order_fills=AsyncMock(), reconcile=AsyncMock(return_value="IN_SYNC"), last_diffs=[],
    )
    adapter.rest_client = SimpleNamespace(
        portfolio_margin=False,
        request=AsyncMock(side_effect=[
            [{"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0.01"}],
            [{"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0"}],
        ]),
    )
    proof = {
        "stop_order_id": 101, "stop_client_order_id": "stop-1", "stop_order_type": "STOP_MARKET",
        "stop_status": "NEW", "stop_close_position": True, "stop_reduce_only": False,
        "take_profit_order_id": 102, "take_profit_client_order_id": "target-1",
        "take_profit_order_type": "TAKE_PROFIT_MARKET", "take_profit_status": "NEW",
        "take_profit_close_position": True, "take_profit_reduce_only": False,
        "position_side": "BOTH", "close_position": True, "reduce_only": False,
    }

    async def read_protection(intent, *, entry_client_order_id):
        events.append("protection_readback")
        assert entry_client_order_id == entry_id
        assert intent.stop_algo_id == 101 and intent.take_profit_algo_id == 102
        assert intent.stop_trigger == Decimal("2400")
        return SimpleNamespace(
            state=protection_state, evidence=proof if protection_state == "PROTECTED" else None,
            reasons=() if protection_state == "PROTECTED" else ("stop_not_open",),
        )

    adapter.read_back_algo_protection = AsyncMock(side_effect=read_protection)

    async def claim_submission(_owner, observed_close_id):
        events.append("submission_claim")
        assert observed_close_id == close_id
        return {**owner, "state_reason": f"protected_ethusdc_testnet_trial_close:{close_id}:SUBMITTING"}

    authority = SimpleNamespace(claim_testnet_trial_close_submission=AsyncMock(side_effect=claim_submission))

    async def execute(decision, *, before_mutation, **_kwargs):
        events.append("close_pre_send")
        await before_mutation()
        events.append("close_submission")
        assert decision.orders[0].reduce_only is True
        return [SimpleNamespace(client_order_id=close_id, exchange_order_id="88")]

    adapter._execute_decision = AsyncMock(side_effect=execute)

    orders = await adapter.close_owned_testnet_trial(owner, authority=authority)

    if protection_state == "PROTECTED":
        assert len(orders) == 1, adapter.last_emergency_result
        assert events == ["submission_claim", "close_pre_send", "protection_readback", "close_submission"]
        assert adapter.last_emergency_result["protection_at_close"]["stop"]["status"] == "NEW"
        assert adapter.last_emergency_result["protection_at_close"]["target"]["status"] == "NEW"
    else:
        assert orders == []
        assert events == ["submission_claim", "close_pre_send", "protection_readback"]
        assert adapter.last_emergency_result["status"] == "UNKNOWN"
        assert adapter.last_emergency_result["reason"] == "trial_protection_not_confirmed_before_close"
        authority.claim_testnet_trial_close_submission.assert_awaited_once()
        adapter._execute_decision.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_owned_testnet_trial_with_fill_sized_reduce_only_bracket_shape():
    """D4 requirement: exercise fill-sized reduce-only close shape while conditional orders are open."""
    entry_id = "entry-reduce-only-1"
    close_id = "close-reduce-only-1"
    owner = {
        "entry_client_order_id": entry_id, "state": "CLOSE_PENDING",
        "state_reason": f"protected_ethusdc_testnet_trial_close:{close_id}:CLAIMED",
        "environment": "TESTNET", "venue": "binance_testnet", "symbol": "ETHUSDC",
        "entry_side": "BUY", "position_side": "BOTH",
        "requested_quantity": Decimal("0.02"), "filled_quantity": Decimal("0.02"),
        "stop_client_algo_id": "stop-ro-1", "take_profit_client_algo_id": "target-ro-1",
        "stop_algo_id": "201", "take_profit_algo_id": "202",
        "stop_trigger_price": Decimal("2400"), "take_profit_trigger_price": Decimal("2600"),
    }
    entry_exchange = {
        "clientOrderId": entry_id, "orderId": 881, "symbol": "ETHUSDC", "side": "BUY",
        "positionSide": "BOTH", "origQty": "0.02", "executedQty": "0.02", "status": "FILLED",
    }
    close_exchange = {
        "clientOrderId": close_id, "orderId": 882, "symbol": "ETHUSDC", "side": "SELL",
        "positionSide": "BOTH", "origQty": "0.02", "executedQty": "0.02", "status": "FILLED",
        "type": "MARKET", "reduceOnly": "true",
    }
    fill = SimpleNamespace(
        client_order_id=entry_id, exchange_order_id="881", symbol="ETHUSDC", side="BUY",
        position_side="BOTH", quantity=Decimal("0.02"), price=Decimal("2500"),
    )
    local_entry = SimpleNamespace(
        client_order_id=entry_id, exchange_order_id="881", symbol="ETHUSDC", side="BUY",
    )
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.TESTNET
    adapter._worker_authorized = lambda _authority: True
    adapter._mutation_lock = asyncio.Lock()
    adapter.last_emergency_result = {}
    adapter.testnet_trial_close_client_order_id = lambda _e: close_id
    adapter.query_order = AsyncMock(side_effect=[None, entry_exchange, close_exchange])
    adapter.ledger = SimpleNamespace(
        get_order_by_client_id=AsyncMock(return_value=local_entry),
        get_fills=AsyncMock(return_value=[fill]), replace_positions=AsyncMock(),
    )
    adapter.reconciliation = SimpleNamespace(
        _recover_order_fills=AsyncMock(), reconcile=AsyncMock(return_value="IN_SYNC"), last_diffs=[],
    )
    adapter.rest_client = SimpleNamespace(
        portfolio_margin=False,
        request=AsyncMock(side_effect=[
            [{"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0.02"}],
            [{"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0"}],
        ]),
    )
    # The bracket shape used on Portfolio Margin / Pilot Bracket: closePosition=False, reduceOnly=True
    proof = {
        "stop_order_id": 201, "stop_client_order_id": "stop-ro-1", "stop_order_type": "STOP_MARKET",
        "stop_status": "NEW", "stop_close_position": False, "stop_reduce_only": True,
        "take_profit_order_id": 202, "take_profit_client_order_id": "target-ro-1",
        "take_profit_order_type": "TAKE_PROFIT_MARKET", "take_profit_status": "NEW",
        "take_profit_close_position": False, "take_profit_reduce_only": True,
        "position_side": "BOTH", "close_position": False, "reduce_only": True,
    }

    async def read_protection(intent, *, entry_client_order_id):
        return SimpleNamespace(state="PROTECTED", evidence=proof, reasons=())

    adapter.read_back_algo_protection = AsyncMock(side_effect=read_protection)

    async def claim_submission(_owner, observed_close_id):
        assert observed_close_id == close_id
        return {**owner, "state_reason": f"protected_ethusdc_testnet_trial_close:{close_id}:SUBMITTING"}

    authority = SimpleNamespace(claim_testnet_trial_close_submission=AsyncMock(side_effect=claim_submission))

    async def execute(decision, *, before_mutation, **_kwargs):
        await before_mutation()
        assert decision.orders[0].reduce_only is True
        assert decision.orders[0].quantity == Decimal("0.02")
        return [SimpleNamespace(client_order_id=close_id, exchange_order_id="882")]

    adapter._execute_decision = AsyncMock(side_effect=execute)

    orders = await adapter.close_owned_testnet_trial(owner, authority=authority)

    assert len(orders) == 1
    assert adapter.last_emergency_result["status"] == "CONFIRMED"
    assert adapter.last_emergency_result["protection_at_close"]["stop"]["close_position"] is False
    assert adapter.last_emergency_result["protection_at_close"]["stop"]["reduce_only"] is True
    assert adapter.last_emergency_result["protection_at_close"]["target"]["close_position"] is False
    assert adapter.last_emergency_result["protection_at_close"]["target"]["reduce_only"] is True

