from datetime import timedelta
from decimal import Decimal

import pytest

from domain.enums import EconomicRiskClass, MarketType, OrderSide, OrderType, PositionSide, TimeInForce
from domain.models import ExchangeFill, ExchangePosition, ExecutionDecision, ExecutionOrder, OrderIntent, utc_now
from apps.trading_worker.main import (
    ArmRequest,
    EXECUTABLE_ENGINE_STATES,
    TradingWorkerApp,
    WorkerEngineState,
    WorkerExecutionMode,
)
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.manual_testnet import (
    _passive_order,
    _require_current_readonly_evidence,
)
from apps.trading_worker.venues.binance.models import (
    BinanceDefinitiveRejection,
    BinanceAuthenticationError,
    BinanceTransportAmbiguity,
    ConnectionState,
    ExchangeAccountSnapshot,
    TestnetSafetyLimits as SafetyLimits,
)
from apps.trading_worker.venues.binance.reconciliation import (
    BinanceReconciliation,
    build_account_snapshot,
)
from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules
from venues.binance_global.usdm import BinanceGlobalUSDMAdapter


class FakeStream:
    def __init__(self, connected: bool = True):
        self.is_connected = connected

    async def close(self):
        self.is_connected = False


class FakeReconciliation:
    def __init__(self, status: str = "IN_SYNC"):
        self.last_status = status
        self.next_status = status
        self.calls = 0

    async def reconcile(self):
        self.calls += 1
        self.last_status = self.next_status
        return self.last_status


class ScriptedRest:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return await self.handler(method, path, kwargs)

    async def close(self):
        return None


def make_rules(symbol: str = "BTCUSDT") -> SymbolTradingRules:
    rules = SymbolTradingRules(symbol)
    rules.status = "TRADING"
    rules.supported_order_types = ["LIMIT", "MARKET"]
    rules.tick_size = Decimal("0.1")
    rules.step_size = Decimal("0.001")
    rules.min_qty = Decimal("0.001")
    rules.max_qty = Decimal("100")
    rules.market_step_size = Decimal("0.001")
    rules.market_min_qty = Decimal("0.001")
    rules.market_max_qty = Decimal("100")
    rules.min_notional = Decimal("5")
    return rules


def make_snapshot(*, age_seconds: float = 0, liquidation_safety: str = "KNOWN"):
    return ExchangeAccountSnapshot(
        wallet_balance=Decimal("100"),
        margin_balance=Decimal("100"),
        available_balance=Decimal("90"),
        unrealized_pnl=Decimal("0"),
        total_initial_margin=Decimal("10"),
        total_maint_margin=Decimal("5"),
        position_initial_margin=Decimal("10"),
        total_position_notional=Decimal("0"),
        effective_leverage=Decimal("0"),
        margin_utilization_pct=Decimal("10"),
        min_liquidation_distance_pct=None,
        liquidation_safety=liquidation_safety,
        exchange_environment="BINANCE_TESTNET",
        valid=True,
        timestamp=utc_now() - timedelta(seconds=age_seconds),
    )


async def make_adapter(snapshot=None, rest=None):
    ledger = InMemoryLedger()
    adapter = BinanceExecutionAdapter(
        api_key="unit-test-key",
        api_secret="unit-test-secret",
        env=BinanceEnvironment.TESTNET,
        ledger=ledger,
    )
    adapter.state = ConnectionState.READY
    adapter.capabilities.account_request_succeeded = True
    adapter.capabilities.authenticated = True
    adapter.capabilities.hedge_mode = False
    adapter.capabilities.symbol_rules["BTCUSDT"] = make_rules()
    adapter.user_stream = FakeStream()
    adapter.reconciliation = FakeReconciliation()
    if rest is not None:
        adapter.rest_client = rest
    adapter.last_market_event_at["BTCUSDT"] = utc_now()
    await ledger.set_account_snapshot(snapshot or make_snapshot())
    return adapter


async def make_ready_worker(monkeypatch, *, snapshot=None, symbols=None, rest=None):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "unit-test-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "unit-test-secret")
    worker = TradingWorkerApp(symbols=symbols or ["BTCUSDT"])
    worker.execution_mode = WorkerExecutionMode.TESTNET
    worker.engine_state = WorkerEngineState.ARMED
    worker.execution_adapter = await make_adapter(snapshot or make_snapshot(), rest=rest)
    worker.market_data_healthy = True
    worker.last_market_event_at["BTCUSDT"] = utc_now()
    worker._sync_adapter_state()
    return worker


def make_limit_intent(
    *,
    client_id: str = "UNIT-1",
    symbol: str = "BTCUSDT",
    quantity: str = "0.001",
    price: str = "10000",
    reduce_only: bool = False,
):
    return OrderIntent(
        client_order_id=client_id,
        symbol=symbol,
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal(quantity),
        price=Decimal(price),
        reduce_only=reduce_only,
    )


def make_decision(risk_class, *intents):
    return ExecutionDecision(
        decision_id="UNIT-DECISION",
        symbol="BTCUSDT",
        action="SUBMIT_ORDER",
        risk_class=risk_class,
        orders=list(intents),
    )


def account_payload():
    return {
        "totalWalletBalance": "100",
        "totalMarginBalance": "100",
        "availableBalance": "90",
        "totalUnrealizedProfit": "0",
        "totalInitialMargin": "10",
        "totalMaintMargin": "5",
        "totalPositionInitialMargin": "10",
    }


def trade_payload():
    return {
        "id": 101,
        "orderId": 7,
        "clientOrderId": "LOCAL-1",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "positionSide": "LONG",
        "qty": "0.001",
        "price": "10000",
        "commission": "0.01",
        "commissionAsset": "USDT",
        "realizedPnl": "0",
        "maker": True,
        "time": 1700000000000,
    }


def test_worker_adapter_state_contract_has_no_attribute_error(monkeypatch):
    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    adapter = BinanceExecutionAdapter(env=BinanceEnvironment.TESTNET)
    adapter.state = ConnectionState.READY
    adapter.capabilities.account_request_succeeded = True
    adapter.capabilities.authenticated = True
    adapter.user_stream = FakeStream()
    adapter.reconciliation = FakeReconciliation()
    worker.execution_adapter = adapter

    worker._sync_adapter_state()
    assert worker.connection_state == "READY"
    assert worker.authenticated is True
    assert adapter.connection_state == ConnectionState.READY

    adapter.state = ConnectionState.DEGRADED
    state = worker.get_state()
    assert state.connection_state == "DEGRADED"
    assert state.connection_status == "DEGRADED"


@pytest.mark.asyncio
async def test_testnet_readiness_does_not_depend_on_engine_armed(monkeypatch):
    worker = await make_ready_worker(monkeypatch)
    worker.engine_state = WorkerEngineState.DISARMED

    capabilities = worker.get_capabilities()

    assert capabilities["testnetExecutionReady"] is True
    assert capabilities["testnetAccountSnapshotReady"] is True
    assert capabilities["testnetMarketDataFresh"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "broken_component",
    ["adapter", "auth", "rules", "stream", "reconciliation", "account", "market", "kill"],
)
async def test_each_testnet_readiness_prerequisite_fails_closed(monkeypatch, broken_component):
    worker = await make_ready_worker(monkeypatch)
    adapter = worker.execution_adapter

    if broken_component == "adapter":
        adapter.state = ConnectionState.DEGRADED
    elif broken_component == "auth":
        adapter.capabilities.authenticated = False
    elif broken_component == "rules":
        adapter.capabilities.symbol_rules.clear()
    elif broken_component == "stream":
        adapter.user_stream.is_connected = False
    elif broken_component == "reconciliation":
        adapter.reconciliation.last_status = "MISMATCH"
    elif broken_component == "account":
        adapter.ledger.account_snapshot = None
    elif broken_component == "market":
        worker.last_market_event_at["BTCUSDT"] = utc_now() - timedelta(seconds=60)
    elif broken_component == "kill":
        worker.kill_switch_active = True

    assert worker.get_capabilities()["testnetExecutionReady"] is False


@pytest.mark.asyncio
async def test_market_data_freshness_is_checked_per_symbol(monkeypatch):
    worker = await make_ready_worker(monkeypatch, symbols=["BTCUSDT", "ETHUSDT"])
    worker.execution_adapter.capabilities.symbol_rules["ETHUSDT"] = make_rules("ETHUSDT")

    assert worker.is_market_data_fresh() is False
    worker.last_market_event_at["ETHUSDT"] = utc_now()
    assert worker.is_market_data_fresh() is True


def test_testnet_limits_have_bounded_defaults_and_safe_invalid_overrides(monkeypatch):
    for name in (
        "TESTNET_ALLOWED_SYMBOLS",
        "TESTNET_MAX_SINGLE_ORDER_NOTIONAL",
        "TESTNET_MAX_TOTAL_OPEN_NOTIONAL",
        "TESTNET_MAX_OPEN_ORDERS",
        "TESTNET_MAX_ACTIVE_EXPOSURE_CHAINS",
    ):
        monkeypatch.setenv(name, "")

    limits = SafetyLimits.from_environment()

    assert limits.allowed_symbols == {"BTCUSDT"}
    assert limits.max_single_order_notional == Decimal("25.0")
    assert limits.max_total_open_notional == Decimal("50.0")
    assert limits.max_open_orders == 1
    assert limits.max_active_exposure_chains == 1

    monkeypatch.setenv("TESTNET_MAX_SINGLE_ORDER_NOTIONAL", "not-a-number")
    assert SafetyLimits.from_environment().max_single_order_notional == Decimal("25.0")


def test_manual_trial_requires_matching_readonly_build_evidence(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="read-only Testnet evidence"):
        _require_current_readonly_evidence("build-123")

    evidence_path = tmp_path / "artifacts" / "build-evidence.json"
    evidence_path.parent.mkdir()
    evidence_path.write_text(
        '{"build_sha":"build-123",'
        '"local_non_secret_tests_verified":true,'
        '"readonly_contract_verified":true}',
        encoding="utf-8",
    )

    evidence = _require_current_readonly_evidence("build-123")

    assert evidence.readonly_contract_verified is True


@pytest.mark.asyncio
async def test_no_adapter_testnet_reconcile_is_unknown(monkeypatch):
    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    worker.execution_mode = WorkerExecutionMode.TESTNET

    assert await worker.trigger_reconciliation() == "UNKNOWN"
    assert worker.reconciliation_status == "UNKNOWN"


@pytest.mark.asyncio
async def test_stale_account_snapshot_blocks_risk_increase(monkeypatch):
    worker = await make_ready_worker(monkeypatch, snapshot=make_snapshot(age_seconds=60))
    decision = make_decision(EconomicRiskClass.NEW_RISK, make_limit_intent())

    result = worker.decision_execution_gate.check(decision)

    assert result.allowed is False
    assert "snapshot" in result.reason.lower()


@pytest.mark.asyncio
async def test_decision_gate_requires_explicit_testnet_configuration(monkeypatch):
    worker = await make_ready_worker(monkeypatch)
    monkeypatch.setenv("BINANCE_TESTNET", "false")
    decision = make_decision(EconomicRiskClass.NEW_RISK, make_limit_intent())

    result = worker.decision_execution_gate.check(decision)

    assert result.allowed is False
    assert "configuration" in result.reason.lower()


@pytest.mark.asyncio
async def test_failed_testnet_arm_does_not_mutate_existing_paper_runtime(monkeypatch):
    monkeypatch.delenv("BINANCE_TESTNET", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_TESTNET_API_SECRET", raising=False)

    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    worker.active_configuration = {
        "executionMode": "PAPER",
        "instruments": ["BTCUSDT"],
    }
    worker.execution_mode = WorkerExecutionMode.PAPER
    worker.engine_state = WorkerEngineState.ARMED

    armed, reason = await worker.arm(
        ArmRequest(
            executionMode="TESTNET",
            instruments=["BTCUSDT"],
            strategies={"grid": True},
        )
    )

    assert armed is False
    assert "Configuration Preflight Failed" in reason
    assert worker.execution_mode == WorkerExecutionMode.PAPER
    assert worker.engine_state == WorkerEngineState.ARMED
    assert worker.active_configuration["executionMode"] == "PAPER"


@pytest.mark.asyncio
async def test_stale_account_snapshot_does_not_prevent_reduction_fallback(monkeypatch):
    worker = await make_ready_worker(monkeypatch, snapshot=make_snapshot(age_seconds=60))
    decision = make_decision(
        EconomicRiskClass.CLOSE,
        make_limit_intent(reduce_only=True),
    )

    result = worker.decision_execution_gate.check(decision)

    assert result.allowed is True


@pytest.mark.asyncio
async def test_unknown_liquidation_safety_blocks_risk_increase(monkeypatch):
    worker = await make_ready_worker(
        monkeypatch, snapshot=make_snapshot(liquidation_safety="UNKNOWN")
    )
    decision = make_decision(EconomicRiskClass.INCREASE_RISK, make_limit_intent())

    result = worker.decision_execution_gate.check(decision)

    assert result.allowed is False
    assert "liquidation" in result.reason.lower()


@pytest.mark.asyncio
async def test_zero_liquidation_distance_blocks_risk_increase(monkeypatch):
    worker = await make_ready_worker(monkeypatch)
    worker.execution_adapter.ledger.account_snapshot.min_liquidation_distance_pct = Decimal("0")
    decision = make_decision(EconomicRiskClass.INCREASE_RISK, make_limit_intent())

    result = worker.decision_execution_gate.check(decision)

    assert result.allowed is False
    assert "liquidation" in result.reason.lower()


@pytest.mark.asyncio
async def test_pause_and_recovery_only_use_economic_risk_class(monkeypatch):
    worker = await make_ready_worker(monkeypatch)
    worker.pause_new_risk = True
    worker.engine_state = WorkerEngineState.PAUSED_NEW_RISK

    for risk_class in (EconomicRiskClass.NEW_RISK, EconomicRiskClass.INCREASE_RISK):
        assert not worker.decision_execution_gate.check(
            make_decision(risk_class, make_limit_intent())
        ).allowed
    for risk_class in (
        EconomicRiskClass.REDUCE_RISK,
        EconomicRiskClass.RECOVERY,
        EconomicRiskClass.CLOSE,
        EconomicRiskClass.EMERGENCY,
    ):
        assert worker.decision_execution_gate.check(
            make_decision(risk_class, make_limit_intent(reduce_only=True))
        ).allowed

    worker.pause_new_risk = False
    worker.recovery_only = True
    worker.engine_state = WorkerEngineState.RECOVERY_ONLY
    assert not worker.decision_execution_gate.check(
        make_decision(EconomicRiskClass.NEW_RISK, make_limit_intent())
    ).allowed
    assert worker.decision_execution_gate.check(
        make_decision(EconomicRiskClass.CLOSE, make_limit_intent(reduce_only=True))
    ).allowed


@pytest.mark.asyncio
async def test_recovery_orders_are_reduce_only_at_the_last_gate(monkeypatch):
    worker = await make_ready_worker(monkeypatch)
    decision = make_decision(EconomicRiskClass.RECOVERY, make_limit_intent())

    result = await worker.execution_adapter.order_gate.check(
        decision.orders[0], decision.risk_class
    )

    assert result.allowed is False
    assert "reduceonly" in result.reason.lower()


@pytest.mark.asyncio
async def test_reduce_only_side_must_reduce_signed_position():
    adapter = await make_adapter()
    await adapter.ledger.upsert_position(
        ExchangePosition(
            symbol="BTCUSDT",
            position_side=PositionSide.BOTH,
            quantity=Decimal("0.001"),
            entry_price=Decimal("10000"),
            mark_price=Decimal("10000"),
        )
    )
    intent = make_limit_intent(reduce_only=True).model_copy(
        update={"side": OrderSide.BUY}
    )

    result = await adapter.order_gate.check(intent, EconomicRiskClass.CLOSE)

    assert result.allowed is False
    assert "reduceonly" in result.reason.lower()


@pytest.mark.asyncio
async def test_emergency_reduction_can_use_fallback_when_stream_market_data_is_stale(monkeypatch):
    worker = await make_ready_worker(monkeypatch, snapshot=make_snapshot(age_seconds=60))
    worker.market_data_healthy = False
    worker.last_market_event_at["BTCUSDT"] = utc_now() - timedelta(seconds=60)
    decision = make_decision(
        EconomicRiskClass.EMERGENCY,
        make_limit_intent(reduce_only=True),
    )

    result = worker.decision_execution_gate.check(decision)

    assert result.allowed is True


def test_paused_and_recovery_states_continue_processing_reductions():
    assert EXECUTABLE_ENGINE_STATES == {
        WorkerEngineState.ARMED,
        WorkerEngineState.PAUSED_NEW_RISK,
        WorkerEngineState.RECOVERY_ONLY,
    }
    assert WorkerEngineState.EMERGENCY not in EXECUTABLE_ENGINE_STATES


@pytest.mark.asyncio
async def test_kill_switch_state_precedes_pause_and_recovery_controls():
    worker = TradingWorkerApp(symbols=["BTCUSDT"])
    worker.active_configuration = {"instruments": ["BTCUSDT"]}

    result = await worker.set_kill_switch(True)

    assert result["status"] == "CONFIRMED"
    assert worker.kill_switch_active is True
    assert worker.engine_state == WorkerEngineState.EMERGENCY

    await worker.set_pause_new_risk(True)
    await worker.set_recovery_only(True)

    assert worker.engine_state == WorkerEngineState.EMERGENCY


@pytest.mark.asyncio
async def test_market_order_without_fresh_price_is_rejected(monkeypatch):
    async def handler(method, path, kwargs):
        return {}

    adapter = await make_adapter(rest=ScriptedRest(handler))
    intent = OrderIntent(
        client_order_id="MARKET-NO-PRICE",
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.001"),
    )

    result = await adapter.order_gate.check(intent, EconomicRiskClass.NEW_RISK)

    assert result.allowed is False
    assert "market price" in result.reason.lower()


@pytest.mark.asyncio
async def test_testnet_single_order_cap_is_enforced(monkeypatch):
    adapter = await make_adapter()
    result = await adapter.order_gate.check(
        make_limit_intent(quantity="0.001", price="26000"),
        EconomicRiskClass.NEW_RISK,
    )

    assert result.allowed is False
    assert "single-order" in result.reason.lower()


@pytest.mark.asyncio
async def test_multiple_orders_are_gated_individually():
    post_calls = []

    async def handler(method, path, kwargs):
        if method == "POST" and path == "/fapi/v1/order":
            post_calls.append(kwargs["params"])
            return {
                "orderId": len(post_calls),
                "clientOrderId": kwargs["params"]["newClientOrderId"],
                "status": "NEW",
                "symbol": "BTCUSDT",
                "price": kwargs["params"]["price"],
                "origQty": kwargs["params"]["quantity"],
            }
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    decision = make_decision(
        EconomicRiskClass.NEW_RISK,
        make_limit_intent(client_id="ORDER-1", price="20000"),
        make_limit_intent(client_id="ORDER-2", price="20000"),
    )

    executed = await adapter.execute_decision(decision)

    assert len(executed) == 1
    assert len(post_calls) == 1


@pytest.mark.asyncio
async def test_execution_lineage_survives_order_and_fill_events():
    async def handler(method, path, kwargs):
        if method == "POST" and path == "/fapi/v1/order":
            return {
                "orderId": 12,
                "clientOrderId": kwargs["params"]["newClientOrderId"],
                "status": "NEW",
                "symbol": "BTCUSDT",
                "price": kwargs["params"]["price"],
                "origQty": kwargs["params"]["quantity"],
            }
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    intent = make_limit_intent(client_id="TRACE-ORDER").model_copy(
        update={"strategy_id": "grid", "source_intent_ids": ["INT-1"]}
    )
    decision = ExecutionDecision(
        decision_id="DEC-1",
        symbol="BTCUSDT",
        action="SUBMIT_ORDER",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[intent],
        target_exposure_id="EXP-1",
        source_intent_ids=["INT-1"],
    )

    executed = await adapter.execute_decision(decision)

    assert len(executed) == 1
    assert executed[0].strategy_id == "grid"
    assert executed[0].decision_id == "DEC-1"
    assert executed[0].target_exposure_id == "EXP-1"
    assert executed[0].source_intent_ids == ["INT-1"]

    await adapter._on_ws_event(
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": 1700000000000,
            "o": {
                "s": "BTCUSDT",
                "c": "TRACE-ORDER",
                "X": "FILLED",
                "i": "12",
                "x": "TRADE",
                "S": "BUY",
                "ps": "BOTH",
                "q": "0.001",
                "p": "10000",
                "l": "0.001",
                "L": "10000",
                "n": "0.01",
                "N": "USDT",
                "rp": "0",
                "m": True,
                "t": "99",
                "T": 1700000000000,
            },
        }
    )

    assert len(adapter.ledger.fills) == 1
    assert adapter.ledger.fills[0].decision_id == "DEC-1"
    assert adapter.ledger.fills[0].target_exposure_id == "EXP-1"
    assert adapter.ledger.fills[0].source_intent_ids == ["INT-1"]


@pytest.mark.asyncio
async def test_order_amendment_that_increases_notional_is_capped():
    put_calls = []

    async def handler(method, path, kwargs):
        if method == "PUT":
            put_calls.append(kwargs["params"])
            raise AssertionError("A capped amendment must not reach Binance")
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    await adapter.ledger.upsert_order(
        ExecutionOrder(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.001"),
            price=Decimal("10000"),
            order_type="LIMIT",
            client_order_id="AMEND-1",
            status="NEW",
            exchange_order_id="13",
        )
    )

    amended = await adapter.modify_order(
        "BTCUSDT", "AMEND-1", Decimal("10000"), Decimal("0.003"), "BUY"
    )

    assert amended is None
    assert put_calls == []


@pytest.mark.asyncio
async def test_lower_notional_entry_amendment_preserves_entry_semantics():
    put_calls = []

    async def handler(method, path, kwargs):
        if method == "PUT" and path == "/fapi/v1/order":
            put_calls.append(kwargs["params"])
            return {
                "orderId": 14,
                "clientOrderId": "AMEND-2",
                "status": "NEW",
                "symbol": "BTCUSDT",
                "price": kwargs["params"]["price"],
                "origQty": kwargs["params"]["quantity"],
            }
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    await adapter.ledger.upsert_order(
        ExecutionOrder(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.001"),
            price=Decimal("10000"),
            order_type="LIMIT",
            client_order_id="AMEND-2",
            status="NEW",
            exchange_order_id="14",
            reduce_only=False,
        )
    )

    amended = await adapter.modify_order(
        "BTCUSDT", "AMEND-2", Decimal("9000"), Decimal("0.001"), "BUY"
    )

    assert amended is not None
    assert amended.price == Decimal("9000")
    assert amended.quantity == Decimal("0.001")
    assert amended.reduce_only is False
    assert put_calls and "reduceOnly" not in put_calls[0]


@pytest.mark.asyncio
async def test_transport_ambiguity_never_blindly_resubmits():
    calls = []

    async def handler(method, path, kwargs):
        calls.append((method, path))
        if method == "POST":
            raise BinanceTransportAmbiguity("timeout")
        if method == "GET" and path == "/fapi/v1/order":
            raise BinanceDefinitiveRejection(-2013, "Order does not exist")
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    adapter.reconciliation.next_status = "MISMATCH"

    await adapter.execute_decision(
        make_decision(EconomicRiskClass.NEW_RISK, make_limit_intent())
    )

    assert [method for method, _ in calls].count("POST") == 1
    assert adapter.state == ConnectionState.DEGRADED
    assert adapter.reconciliation.calls == 1


@pytest.mark.asyncio
async def test_ambiguity_recovery_requires_authoritative_reconciliation():
    async def handler(method, path, kwargs):
        if method == "POST":
            raise BinanceTransportAmbiguity("timeout")
        if method == "GET" and path == "/fapi/v1/order":
            return {
                "orderId": 12,
                "clientOrderId": "UNIT-1",
                "status": "NEW",
                "symbol": "BTCUSDT",
                "price": "10000",
                "origQty": "0.001",
            }
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    adapter.reconciliation.next_status = "MISMATCH"

    recovered = await adapter.execute_decision(
        make_decision(EconomicRiskClass.NEW_RISK, make_limit_intent())
    )

    assert len(recovered) == 0
    assert adapter.state == ConnectionState.DEGRADED
    assert adapter.reconciliation.calls == 1


@pytest.mark.asyncio
async def test_cancel_ambiguity_returns_success_only_after_query_and_reconciliation():
    async def handler(method, path, kwargs):
        if method == "DELETE":
            raise BinanceTransportAmbiguity("timeout")
        if method == "GET" and path == "/fapi/v1/order":
            return {
                "orderId": 12,
                "clientOrderId": "UNIT-1",
                "status": "CANCELED",
                "symbol": "BTCUSDT",
                "price": "10000",
                "origQty": "0.001",
            }
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    await adapter.ledger.upsert_order(
        ExecutionOrder(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.001"),
            price=Decimal("10000"),
            order_type="LIMIT",
            client_order_id="UNIT-1",
            status="NEW",
            exchange_order_id="12",
        )
    )

    assert await adapter.cancel_order("BTCUSDT", "UNIT-1") is True
    assert adapter.state == ConnectionState.READY
    assert adapter.reconciliation.calls == 1


@pytest.mark.asyncio
async def test_authentication_failure_degrades_adapter_and_clears_truth():
    async def handler(method, path, kwargs):
        raise BinanceAuthenticationError(-2015, "Invalid API-key, IP, or permissions")

    adapter = await make_adapter(rest=ScriptedRest(handler))
    executed = await adapter.execute_decision(
        make_decision(EconomicRiskClass.NEW_RISK, make_limit_intent())
    )

    assert executed == []
    assert adapter.authenticated is False
    assert adapter.state == ConnectionState.DEGRADED


@pytest.mark.asyncio
async def test_one_way_order_requires_both_position_side():
    adapter = await make_adapter()
    intent = make_limit_intent().model_copy(update={"position_side": PositionSide.LONG})

    result = await adapter.order_gate.check(intent, EconomicRiskClass.NEW_RISK)

    assert result.allowed is False
    assert "one-way" in result.reason.lower()


@pytest.mark.asyncio
async def test_total_open_notional_cap_includes_existing_position():
    adapter = await make_adapter()
    await adapter.ledger.upsert_position(
        ExchangePosition(
            symbol="BTCUSDT",
            position_side=PositionSide.BOTH,
            quantity=Decimal("0.0016"),
            entry_price=Decimal("20000"),
            mark_price=Decimal("20000"),
        )
    )

    result = await adapter.order_gate.check(
        make_limit_intent(price="20000"),
        EconomicRiskClass.NEW_RISK,
    )

    assert result.allowed is False
    assert "total open" in result.reason.lower()


@pytest.mark.asyncio
async def test_kill_switch_stays_active_when_exchange_is_unreachable(monkeypatch):
    async def handler(method, path, kwargs):
        raise BinanceTransportAmbiguity("exchange unavailable")

    worker = await make_ready_worker(monkeypatch, rest=ScriptedRest(handler))

    result = await worker.set_kill_switch(True)

    assert result["status"] == "UNKNOWN"
    assert worker.kill_switch_active is True
    assert worker.engine_state == WorkerEngineState.EMERGENCY


def test_mainnet_mutable_adapters_are_rejected():
    with pytest.raises(ValueError):
        BinanceExecutionAdapter(env=BinanceEnvironment.MAINNET)
    with pytest.raises(ValueError):
        BinanceGlobalUSDMAdapter("key", "secret", testnet=False)


def test_account_snapshot_uses_position_risk_and_real_liquidation_distance():
    snapshot = build_account_snapshot(
        account_payload(),
        [
            {
                "symbol": "BTCUSDT",
                "positionSide": "BOTH",
                "positionAmt": "1",
                "markPrice": "100",
                "liquidationPrice": "90",
                "notional": "-100",
            },
            {
                "symbol": "ETHUSDT",
                "positionSide": "BOTH",
                "positionAmt": "-2",
                "markPrice": "100",
                "liquidationPrice": "120",
                "notional": "200",
            },
        ],
    )

    assert snapshot.total_position_notional == Decimal("300")
    assert snapshot.effective_leverage == Decimal("3")
    assert snapshot.margin_utilization_pct == Decimal("10")
    assert snapshot.min_liquidation_distance_pct == Decimal("10")
    assert snapshot.liquidation_safety == "KNOWN"


def test_missing_mark_price_invalidates_active_account_snapshot():
    with pytest.raises(ValueError, match="markPrice"):
        build_account_snapshot(
            account_payload(),
            [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "LONG",
                    "positionAmt": "1",
                    "liquidationPrice": "90",
                }
            ],
        )


def test_unavailable_liquidation_price_is_unknown_not_invented():
    snapshot = build_account_snapshot(
        account_payload(),
        [
            {
                "symbol": "BTCUSDT",
                "positionSide": "LONG",
                "positionAmt": "1",
                "markPrice": "100",
                "liquidationPrice": "0",
            }
        ],
    )

    assert snapshot.liquidation_safety == "UNKNOWN"
    assert snapshot.min_liquidation_distance_pct is None


def test_exchange_symbol_rules_require_real_filter_data_for_each_order_type():
    rules = SymbolTradingRules("BTCUSDT")
    rules.parse_exchange_info(
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "orderTypes": ["LIMIT", "MARKET"],
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "0.1", "maxPrice": "1000000", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "50", "stepSize": "0.001"},
                {"filterType": "NOTIONAL", "minNotional": "5"},
            ],
        }
    )

    assert rules.is_ready_for("LIMIT") is True
    assert rules.is_ready_for("MARKET") is True
    assert rules.normalize_price(Decimal("10000.19")) == Decimal("10000.1")

    missing_notional = SymbolTradingRules("BTCUSDT")
    missing_notional.parse_exchange_info(
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "orderTypes": ["LIMIT"],
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "0.1", "maxPrice": "1000000", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
            ],
        }
    )
    assert missing_notional.is_ready_for("LIMIT") is False


@pytest.mark.asyncio
async def test_manual_trial_does_not_raise_cap_for_exchange_minimum():
    adapter = await make_adapter()
    adapter.symbol_rules["BTCUSDT"].min_notional = Decimal("50")

    with pytest.raises(RuntimeError, match="25 USDT Testnet cap"):
        _passive_order(adapter, "BTCUSDT", Decimal("10000"), Decimal("10001"))


@pytest.mark.asyncio
async def test_filled_order_recovery_recovers_canonical_fill_and_reaches_in_sync():
    async def handler(method, path, kwargs):
        if path == "/fapi/v2/positionRisk":
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "LONG",
                    "positionAmt": "0.001",
                    "entryPrice": "10000",
                    "markPrice": "10000",
                    "liquidationPrice": "9000",
                    "notional": "10",
                }
            ]
        if path == "/fapi/v1/openOrders":
            return []
        if path == "/fapi/v2/account":
            return account_payload()
        if path == "/fapi/v1/order":
            return {"orderId": 7, "status": "FILLED", "symbol": "BTCUSDT"}
        if path == "/fapi/v1/userTrades":
            return [trade_payload()]
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    ledger = InMemoryLedger()
    await ledger.upsert_order(
        ExecutionOrder(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.001"),
            price=Decimal("10000"),
            order_type="LIMIT",
            client_order_id="LOCAL-1",
            status="NEW",
            exchange_order_id="7",
            position_side=PositionSide.LONG,
        )
    )
    await ledger.replace_positions([])
    reconciliation = BinanceReconciliation(ScriptedRest(handler), ledger)

    status = await reconciliation.reconcile()

    assert status == "IN_SYNC"
    assert reconciliation.last_status == "IN_SYNC"
    assert len(ledger.fills) == 1
    assert isinstance(ledger.fills[0], ExchangeFill)
    assert ledger.fills[0].exchange_trade_id == "101"
    assert ledger.fills[0].client_order_id == "LOCAL-1"
    assert (await ledger.get_open_orders()) == []
    await ledger.append_fill(ledger.fills[0])
    assert len(ledger.fills) == 1


@pytest.mark.asyncio
async def test_recent_trade_recovery_falls_back_to_client_order_id_for_lineage():
    async def handler(method, path, kwargs):
        if path == "/fapi/v1/userTrades":
            return [trade_payload()]
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    ledger = InMemoryLedger()
    await ledger.upsert_order(
        ExecutionOrder(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.001"),
            price=Decimal("10000"),
            client_order_id="LOCAL-1",
            status="FILLED",
            exchange_order_id="999",
            strategy_id="grid",
            decision_id="DEC-1",
        )
    )
    reconciliation = BinanceReconciliation(ScriptedRest(handler), ledger)

    await reconciliation._recover_recent_trades({"BTCUSDT"})

    assert len(ledger.fills) == 1
    assert ledger.fills[0].client_order_id == "LOCAL-1"
    assert ledger.fills[0].strategy_id == "grid"
    assert ledger.fills[0].decision_id == "DEC-1"


@pytest.mark.asyncio
async def test_fill_recovery_failure_prevents_in_sync():
    async def handler(method, path, kwargs):
        if path == "/fapi/v2/positionRisk":
            return []
        if path == "/fapi/v1/openOrders":
            return []
        if path == "/fapi/v2/account":
            return account_payload()
        if path == "/fapi/v1/order":
            return {"orderId": 7, "status": "FILLED", "symbol": "BTCUSDT"}
        if path == "/fapi/v1/userTrades":
            return []
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    ledger = InMemoryLedger()
    await ledger.upsert_order(
        ExecutionOrder(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.001"),
            price=Decimal("10000"),
            client_order_id="LOCAL-1",
            status="NEW",
            exchange_order_id="7",
        )
    )
    await ledger.replace_positions([])
    reconciliation = BinanceReconciliation(ScriptedRest(handler), ledger)

    status = await reconciliation.reconcile()

    assert status == "MISMATCH"
    assert any(diff.code == "FILL_RECOVERY_FAILED" for diff in reconciliation.last_diffs)


@pytest.mark.asyncio
async def test_bootstrap_marks_ledger_initialized_only_after_verification():
    calls = []

    async def handler(method, path, kwargs):
        calls.append(path)
        if path == "/fapi/v2/positionRisk":
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "0",
                }
            ]
        if path == "/fapi/v1/openOrders":
            return []
        if path == "/fapi/v2/account":
            payload = account_payload()
            payload.pop("totalMaintMargin")
            return payload
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    ledger = InMemoryLedger()
    reconciliation = BinanceReconciliation(ScriptedRest(handler), ledger)

    assert await reconciliation.bootstrap() is False
    assert await ledger.is_initialized() is False
    assert reconciliation.last_status == "UNKNOWN"
    assert calls == ["/fapi/v2/positionRisk", "/fapi/v1/openOrders", "/fapi/v2/account"]


@pytest.mark.asyncio
async def test_bootstrap_detects_existing_local_position_mismatch_before_sync():
    async def handler(method, path, kwargs):
        if path == "/fapi/v2/positionRisk":
            return [
                {
                    "symbol": "BTCUSDT",
                    "positionSide": "BOTH",
                    "positionAmt": "0.002",
                    "entryPrice": "10000",
                    "markPrice": "10000",
                    "liquidationPrice": "9000",
                    "notional": "20",
                }
            ]
        if path == "/fapi/v1/openOrders":
            return []
        if path == "/fapi/v2/account":
            return account_payload()
        if path == "/fapi/v1/userTrades":
            return []
        raise AssertionError(f"Unexpected REST call: {method} {path}")

    ledger = InMemoryLedger()
    await ledger.replace_positions(
        [
            ExchangePosition(
                symbol="BTCUSDT",
                position_side=PositionSide.BOTH,
                quantity=Decimal("0.001"),
                entry_price=Decimal("10000"),
                mark_price=Decimal("10000"),
            )
        ],
        mark_initialized=False,
    )
    reconciliation = BinanceReconciliation(ScriptedRest(handler), ledger)

    assert await reconciliation.bootstrap() is False
    assert reconciliation.last_status == "MISMATCH"
    assert any(
        diff.code == "POSITION_QTY_MISMATCH" for diff in reconciliation.last_diffs
    )
    assert await ledger.is_initialized() is False
