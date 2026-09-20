"""Fail-closed branch coverage for the shared execution gates.

Exercises the early-exit and validation branches of
``apps/trading_worker/venues/binance/gates.py`` (DecisionExecutionGate and
OrderExecutionGate) that the existing testnet/mainnet safety suites do not
reach.  Pure unit tests: no network, no repository changes, every helper is
local to this file.
"""

import logging
from datetime import datetime, timedelta, timezone, UTC
from decimal import Decimal

import pytest

from apps.trading_worker.venues.binance.config import BinanceEnvironment, environment_label
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.gates import DecisionExecutionGate, OrderExecutionGate
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.models import ConnectionState, ExchangeAccountSnapshot
from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules
from domain.enums import (
    EconomicRiskClass,
    MarketType,
    OrderSide,
    OrderType,
    PositionSide,
    TimeInForce,
)
from domain.models import ExecutionDecision, OrderIntent, utc_now

# --------------------------------------------------------------------------
# Local fakes and helpers (self-contained; not imported from other modules)
# --------------------------------------------------------------------------


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


class RestCallRecorder:
    """Rest client stand-in that records (and forbids) any request."""

    def __init__(self):
        self.calls = []

    async def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("gate check must not issue REST calls")

    async def close(self):
        return None


@pytest.fixture(autouse=True)
def clean_gate_environment(monkeypatch):
    """Keep the environment-sourced gate inputs at their documented defaults.

    The gates read MAX_MARGIN_UTILIZATION_PCT, MAX_MARKET_DATA_AGE_SEC,
    ACCOUNT_SNAPSHOT_MAX_AGE_SEC and MAINNET_LIVE_APPROVED from the process
    environment.  Individual tests opt back in with explicit monkeypatch
    setenv calls so every assertion in this module is deterministic.
    """

    for name in (
        "MAINNET_LIVE_APPROVED",
        "MAX_MARGIN_UTILIZATION_PCT",
        "MAX_MARKET_DATA_AGE_SEC",
        "ACCOUNT_SNAPSHOT_MAX_AGE_SEC",
    ):
        monkeypatch.delenv(name, raising=False)


def make_rules(symbol: str = "BTCUSDT") -> SymbolTradingRules:
    rules = SymbolTradingRules(symbol)
    rules.status = "TRADING"
    rules.supported_order_types = ["LIMIT", "MARKET"]
    rules.tick_size = Decimal("0.1")
    rules.step_size = Decimal("0.001")
    rules.min_qty = Decimal("0.001")
    rules.max_qty = Decimal(100)
    rules.market_step_size = Decimal("0.001")
    rules.market_min_qty = Decimal("0.001")
    rules.market_max_qty = Decimal(100)
    rules.min_notional = Decimal(5)
    return rules


def make_testnet_snapshot(*, age_seconds: float = 0, **overrides) -> ExchangeAccountSnapshot:
    fields = dict(
        wallet_balance=Decimal(100),
        margin_balance=Decimal(100),
        available_balance=Decimal(90),
        unrealized_pnl=Decimal(0),
        total_initial_margin=Decimal(10),
        total_maint_margin=Decimal(5),
        position_initial_margin=Decimal(10),
        total_position_notional=Decimal(0),
        effective_leverage=Decimal(0),
        margin_utilization_pct=Decimal(10),
        min_liquidation_distance_pct=None,
        liquidation_safety="KNOWN",
        exchange_environment="BINANCE_TESTNET",
        valid=True,
        timestamp=utc_now() - timedelta(seconds=age_seconds),
    )
    fields.update(overrides)
    return ExchangeAccountSnapshot(**fields)


def make_mainnet_snapshot(**overrides) -> ExchangeAccountSnapshot:
    window_start = utc_now().astimezone(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    fields = dict(
        wallet_balance=Decimal(100),
        margin_balance=Decimal(100),
        available_balance=Decimal(90),
        unrealized_pnl=Decimal(0),
        total_initial_margin=Decimal(10),
        total_maint_margin=Decimal(5),
        position_initial_margin=Decimal(10),
        total_position_notional=Decimal(0),
        effective_leverage=Decimal(0),
        margin_utilization_pct=Decimal(10),
        min_liquidation_distance_pct=None,
        liquidation_safety="KNOWN",
        exchange_environment=environment_label(BinanceEnvironment.MAINNET),
        daily_realized_pnl=Decimal(0),
        daily_loss_known=True,
        collateral_asset="USDC",
        risk_currency="USDC",
        daily_loss_asset="USDC",
        daily_pnl_includes_fees=True,
        daily_pnl_includes_funding=True,
        daily_loss_window_start=window_start,
        daily_loss_window_end=window_start + timedelta(days=1),
        configured_leverage=Decimal(10),
        configured_leverage_known=True,
        margin_mode="SINGLE_ASSET_CROSS",
        margin_mode_known=True,
        valid=True,
        timestamp=utc_now(),
    )
    fields.update(overrides)
    return ExchangeAccountSnapshot(**fields)


class _WorkerStub:
    """Healthy worker stand-in for DecisionExecutionGate checks."""

    def __init__(self):
        self.execution_mode = "TESTNET"
        self.engine_state = "ARMED"
        self.kill_switch_active = False
        self.pause_new_risk = False
        self.recovery_only = False
        self.market_data_healthy = True
        self.last_market_event_at = {}
        self.execution_adapter = None

    def is_account_snapshot_ready(self) -> bool:
        return True

    def _testnet_configured(self) -> bool:
        return True

    def _mainnet_configured(self) -> bool:
        return True

    def _enabled_strategies(self) -> set:
        return set()


def make_worker_stub(**overrides) -> _WorkerStub:
    worker = _WorkerStub()
    for name, value in overrides.items():
        setattr(worker, name, value)
    return worker


async def make_gate_adapter(
    env: BinanceEnvironment = BinanceEnvironment.TESTNET,
    snapshot: ExchangeAccountSnapshot | None = None,
):
    symbols = ("ETHUSDC",) if env == BinanceEnvironment.MAINNET else ("BTCUSDT",)
    ledger = InMemoryLedger()
    adapter = BinanceExecutionAdapter(
        api_key="unit-test-key",
        api_secret="unit-test-secret",
        env=env,
        ledger=ledger,
    )
    adapter.state = ConnectionState.READY
    adapter.capabilities.account_request_succeeded = True
    adapter.capabilities.authenticated = True
    adapter.capabilities.trade_authorized = True
    adapter.capabilities.hedge_mode = False
    for symbol in symbols:
        adapter.capabilities.symbol_rules[symbol] = make_rules(symbol)
        adapter.last_market_event_at[symbol] = utc_now()
        adapter.last_market_event_source[symbol] = f"{environment_label(env)}_WS"
        adapter.last_market_event_venue[symbol] = environment_label(env)
        adapter.last_market_event_market_type[symbol] = MarketType.USDM_FUTURES.value
    adapter.user_stream = FakeStream()
    adapter.reconciliation = FakeReconciliation()
    if snapshot is None:
        snapshot = (
            make_mainnet_snapshot()
            if env == BinanceEnvironment.MAINNET
            else make_testnet_snapshot()
        )
    await ledger.set_account_snapshot(snapshot)
    return adapter


def make_limit_intent(**overrides) -> OrderIntent:
    fields = dict(
        client_order_id="UNIT-GATE-1",
        symbol="BTCUSDT",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.001"),
        price=Decimal(10000),
        reduce_only=False,
    )
    fields.update(overrides)
    return OrderIntent(**fields)


def make_executable_decision(*intents, risk_class=EconomicRiskClass.NEW_RISK) -> ExecutionDecision:
    return ExecutionDecision(
        decision_id="UNIT-GATE-DECISION",
        symbol="BTCUSDT",
        action="SUBMIT_ORDER",
        risk_class=risk_class,
        orders=list(intents),
    )


# --------------------------------------------------------------------------
# DecisionExecutionGate
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"execution_mode": "PAPER"}, "not an exchange execution mode"),
        ({"engine_state": "DISARMED"}, "not in an executable armed state"),
        ({"kill_switch_active": True}, "Kill switch is active"),
        ({"execution_adapter": None}, "Execution adapter is unavailable"),
        ({"execution_mode": "LIVE"}, "MAINNET_LIVE_APPROVED is not enabled"),
    ],
    ids=["paper_mode", "disarmed_state", "kill_switch", "missing_adapter", "live_unapproved"],
)
async def test_decision_gate_rejects_non_executable_worker_states(
    monkeypatch, overrides, expected_fragment
):
    adapter = await make_gate_adapter()
    if overrides.get("execution_mode") == "LIVE":
        monkeypatch.delenv("MAINNET_LIVE_APPROVED", raising=False)
    worker = make_worker_stub(**{"execution_adapter": adapter, **overrides})

    result = DecisionExecutionGate(worker).check(
        make_executable_decision(make_limit_intent())
    )

    assert result.allowed is False
    assert expected_fragment in result.reason


@pytest.mark.asyncio
async def test_gates_fail_closed_when_reconciliation_is_not_in_sync():
    adapter = await make_gate_adapter()
    adapter.reconciliation.last_status = "MISMATCH"
    worker = make_worker_stub(execution_adapter=adapter)
    worker.last_market_event_at["BTCUSDT"] = utc_now()

    decision_result = DecisionExecutionGate(worker).check(
        make_executable_decision(make_limit_intent())
    )
    order_result = await OrderExecutionGate(adapter).check(
        make_limit_intent(), EconomicRiskClass.NEW_RISK
    )

    assert decision_result.allowed is False
    assert "reconciliation" in decision_result.reason.lower()
    assert order_result.allowed is False
    assert "reconciliation" in order_result.reason.lower()


@pytest.mark.asyncio
async def test_decision_gate_rejects_when_authoritative_market_sample_missing(monkeypatch):
    adapter = await make_gate_adapter()
    monkeypatch.setattr(adapter, "has_authoritative_market_sample", lambda symbol: False)
    worker = make_worker_stub(execution_adapter=adapter)
    worker.last_market_event_at["BTCUSDT"] = utc_now()

    result = DecisionExecutionGate(worker).check(
        make_executable_decision(make_limit_intent())
    )

    assert result.allowed is False
    assert (
        result.reason
        == "Authoritative BINANCE_TESTNET market sample unavailable for BTCUSDT"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stale_timestamp",
    [
        utc_now() - timedelta(seconds=60),
        datetime(2026, 1, 1, 12, 0, 0),
    ],
    ids=["outdated_tz_aware", "naive_timestamp"],
)
async def test_decision_gate_rejects_stale_or_naive_per_symbol_timestamps(stale_timestamp):
    adapter = await make_gate_adapter()
    worker = make_worker_stub(execution_adapter=adapter)
    worker.last_market_event_at["BTCUSDT"] = stale_timestamp

    result = DecisionExecutionGate(worker).check(
        make_executable_decision(make_limit_intent())
    )

    assert result.allowed is False
    assert result.reason == "Market data stale for BTCUSDT"


# --------------------------------------------------------------------------
# OrderExecutionGate (testnet)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_gate_rejects_risk_increase_when_account_snapshot_check_fails():
    adapter = await make_gate_adapter(snapshot=make_testnet_snapshot(age_seconds=60))
    assert adapter.is_account_snapshot_fresh() is False

    result = await adapter.order_gate.check(make_limit_intent(), EconomicRiskClass.NEW_RISK)

    assert result.allowed is False
    assert "snapshot" in result.reason.lower()


@pytest.mark.asyncio
async def test_order_gate_rejects_adapter_not_ready_for_non_emergency_orders():
    adapter = await make_gate_adapter()
    adapter.state = ConnectionState.DEGRADED

    result = await adapter.order_gate.check(make_limit_intent(), EconomicRiskClass.NEW_RISK)

    assert result.allowed is False
    assert result.reason == "Adapter not READY"


@pytest.mark.asyncio
async def test_order_gate_margin_utilization_fail_closed_and_env_fallback(monkeypatch):
    negative = await make_gate_adapter(
        snapshot=make_testnet_snapshot(margin_utilization_pct=Decimal(-1))
    )
    # A negative utilization would independently fail the adapter's snapshot
    # freshness precondition (which requires nonnegative utilization); pin the
    # freshness verdict so the negative-utilization branch inside the gate's
    # own margin check is what rejects the order.
    monkeypatch.setattr(negative, "is_account_snapshot_fresh", lambda: True)
    negative_result = await negative.order_gate.check(
        make_limit_intent(), EconomicRiskClass.NEW_RISK
    )
    assert negative_result.allowed is False
    assert "margin" in negative_result.reason.lower()

    # An unparsable MAX_MARGIN_UTILIZATION_PCT must fall back to the default
    # safety limit of 70: 69.9 stays allowed while 70 blocks.
    monkeypatch.setenv("MAX_MARGIN_UTILIZATION_PCT", "not-a-number")
    passing = await make_gate_adapter(
        snapshot=make_testnet_snapshot(margin_utilization_pct=Decimal("69.9"))
    )
    allowed_result = await passing.order_gate.check(
        make_limit_intent(), EconomicRiskClass.NEW_RISK
    )
    assert allowed_result.allowed is True

    blocking = await make_gate_adapter(
        snapshot=make_testnet_snapshot(margin_utilization_pct=Decimal(70))
    )
    blocked_result = await blocking.order_gate.check(
        make_limit_intent(), EconomicRiskClass.NEW_RISK
    )
    assert blocked_result.allowed is False
    assert "margin" in blocked_result.reason.lower()


@pytest.mark.asyncio
async def test_order_gate_rejects_negative_liquidation_distance():
    adapter = await make_gate_adapter(
        snapshot=make_testnet_snapshot(min_liquidation_distance_pct=Decimal(-5))
    )

    result = await adapter.order_gate.check(make_limit_intent(), EconomicRiskClass.NEW_RISK)

    assert result.allowed is False
    assert result.reason == "Liquidation safety is UNKNOWN"


# --------------------------------------------------------------------------
# OrderExecutionGate (mainnet)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"daily_loss_known": False},
        {"daily_loss_asset": "USDT"},
        {"daily_pnl_includes_fees": False},
        {"daily_pnl_includes_funding": False},
    ],
    ids=["loss_unknown", "loss_asset_not_usdc", "fees_excluded", "funding_excluded"],
)
async def test_mainnet_order_gate_rejects_unknown_daily_loss_observations(monkeypatch, mutation):
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    adapter = await make_gate_adapter(
        env=BinanceEnvironment.MAINNET, snapshot=make_mainnet_snapshot(**mutation)
    )

    result = await adapter.order_gate.check(
        make_limit_intent(symbol="ETHUSDC"), EconomicRiskClass.NEW_RISK
    )

    assert result.allowed is False
    assert result.reason == "Mainnet daily loss observation is unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case_id",
    [
        "window_start_not_utc_midnight",
        "window_end_beyond_one_day",
        "window_end_before_start",
        "window_naive_datetimes",
        "window_start_missing",
    ],
)
async def test_mainnet_order_gate_rejects_invalid_or_naive_daily_loss_utc_window(
    monkeypatch, case_id
):
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    midnight = utc_now().astimezone(UTC).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    mutations = {
        "window_start_not_utc_midnight": {
            "daily_loss_window_start": utc_now(),
            "daily_loss_window_end": utc_now() + timedelta(days=1),
        },
        "window_end_beyond_one_day": {
            "daily_loss_window_end": midnight + timedelta(days=2),
        },
        "window_end_before_start": {
            "daily_loss_window_end": midnight - timedelta(hours=1),
        },
        "window_naive_datetimes": {
            "daily_loss_window_start": datetime(2026, 1, 1, 0, 0, 0),
            "daily_loss_window_end": datetime(2026, 1, 2, 0, 0, 0),
        },
        "window_start_missing": {"daily_loss_window_start": None},
    }
    adapter = await make_gate_adapter(
        env=BinanceEnvironment.MAINNET, snapshot=make_mainnet_snapshot(**mutations[case_id])
    )

    result = await adapter.order_gate.check(
        make_limit_intent(symbol="ETHUSDC"), EconomicRiskClass.NEW_RISK
    )

    assert result.allowed is False
    if case_id == "window_naive_datetimes":
        expected = "Mainnet daily loss UTC window is not timezone-aware"
    elif case_id == "window_start_missing":
        expected = "Mainnet daily loss UTC window is unknown"
    else:
        expected = "Mainnet daily loss UTC window is invalid"
    assert result.reason == expected


@pytest.mark.asyncio
async def test_mainnet_daily_loss_boundary_just_under_cap_passes_just_over_blocks(
    monkeypatch, caplog
):
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    adapter = await make_gate_adapter(
        env=BinanceEnvironment.MAINNET,
        snapshot=make_mainnet_snapshot(
            daily_realized_pnl=Decimal(-4), unrealized_pnl=Decimal("-0.99")
        ),
    )
    intent = make_limit_intent(symbol="ETHUSDC", quantity="0.05", price="100")

    with caplog.at_level(logging.ERROR, logger="blessing.binance.gates"):
        under_result = await adapter.order_gate.check(intent, EconomicRiskClass.NEW_RISK)
        assert under_result.allowed is True
        assert "daily_loss_cap_breached" not in caplog.text

        adapter.ledger.account_snapshot = make_mainnet_snapshot(
            daily_realized_pnl=Decimal(-4), unrealized_pnl=Decimal("-1.01")
        )
        over_result = await adapter.order_gate.check(intent, EconomicRiskClass.NEW_RISK)
        assert over_result.allowed is False
        assert over_result.reason == "Mainnet daily loss cap exceeded"
        assert "monitor_event=daily_loss_cap_breached" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ({"margin_balance": Decimal(0)}, "Mainnet collateral is invalid"),
        ({"collateral_asset": "USDT"}, "Mainnet USDC collateral is not verified"),
        ({"margin_mode_known": False}, "Mainnet margin mode is unknown or unsupported"),
        ({"wallet_balance": Decimal(300)}, "Mainnet collateral cap exceeded"),
        ({"effective_leverage": Decimal(12)}, "Mainnet effective leverage cap exceeded"),
        (
            {"configured_leverage_known": False},
            "Mainnet configured ETHUSDC leverage cap exceeded or unknown",
        ),
    ],
    ids=[
        "zero_collateral",
        "non_usdc_collateral",
        "margin_mode_unknown",
        "wallet_over_cap",
        "effective_leverage_over_cap",
        "configured_leverage_unknown",
    ],
)
async def test_mainnet_order_gate_rejects_invalid_collateral_leverage_or_margin_mode(
    monkeypatch, mutation, expected_reason
):
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    adapter = await make_gate_adapter(
        env=BinanceEnvironment.MAINNET, snapshot=make_mainnet_snapshot(**mutation)
    )
    recorder = RestCallRecorder()
    adapter.rest_client = recorder

    result = await adapter.order_gate.check(
        make_limit_intent(symbol="ETHUSDC"), EconomicRiskClass.NEW_RISK
    )

    assert result.allowed is False
    assert result.reason == expected_reason
    assert adapter.state == ConnectionState.READY
    assert recorder.calls == []


# --------------------------------------------------------------------------
# DecisionExecutionGate strategy policy
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_gate_unknown_strategy_id_and_alias_resolution():
    adapter = await make_gate_adapter()
    worker = make_worker_stub(execution_adapter=adapter)
    worker.last_market_event_at["BTCUSDT"] = utc_now()

    unknown_result = DecisionExecutionGate(worker).check(
        make_executable_decision(make_limit_intent(strategy_id="martingale"))
    )
    assert unknown_result.allowed is False
    assert unknown_result.reason == "Unknown or unsupported strategy 'martingale'"

    worker._enabled_strategies = lambda: {"grid", "trend", "shock", "carry"}
    alias_result = DecisionExecutionGate(worker).check(
        make_executable_decision(make_limit_intent(strategy_id="Funding Carry"))
    )
    assert alias_result.allowed is True
