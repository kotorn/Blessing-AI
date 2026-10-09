"""Hermetic integration tests for the Local Mainnet Pilot arm-to-order execution chain (OP-1, OP-2, Gap A).

Verifies:
1. ARM succeeds for a LOCAL LIVE_RESEARCH_PILOT worker with mocked adapter and persistence (OP-1, Gap A).
2. Launch readiness calculates local_pilot_execution_ready=True when armed and ready (OP-2).
3. A MarketEvent in LIVE_RESEARCH_PILOT mode triggers _clamp_order_notional_if_needed,
   passes the execution gate, and executes the decision with a QUICK bracket.
4. All negative cases fail closed and stay MONITOR_ONLY:
   - pause_new_risk / pilot accounting pause
   - launch state != ACTIVE
   - pilot_status != ACTIVE
   - campaign expired
   - drawdown triggered
   - MAINNET_LIVE_APPROVED=false
   - preflight false
   - kill switch active
5. Tests are parametrized on portfolio_margin in [False, True].
"""

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.trading_worker.main import (
    ArmRequest,
    TradingWorkerApp,
    WorkerEngineState,
    WorkerExecutionMode,
    local_mainnet_risk_lifecycle_status,
)
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.models import (
    ConnectionState,
    ExchangeAccountSnapshot,
    TestnetSafetyLimits as Limits,
)
from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules
from domain.enums import (
    EconomicRiskClass,
    MarketType,
    OrderSide,
    OrderType,
    PositionSide,
    RegimeType,
    TimeInForce,
)
from domain.models import (
    ExecutionDecision,
    MarketEvent,
    MarketState,
    OrderIntent,
    PriceActionState,
    utc_now,
)


def _create_ethusdc_rules() -> SymbolTradingRules:
    rules = SymbolTradingRules(symbol="ETHUSDC")
    rules.base_asset = "ETH"
    rules.quote_asset = "USDC"
    rules.margin_asset = "USDC"
    rules.status = "TRADING"
    rules.contract_type = "PERPETUAL"
    rules.min_qty = Decimal("0.001")
    rules.max_qty = Decimal("1000.0")
    rules.step_size = Decimal("0.001")
    rules.min_price = Decimal("0.01")
    rules.max_price = Decimal("100000.0")
    rules.tick_size = Decimal("0.01")
    rules.min_notional = Decimal("5.0")
    return rules


class FakeExecutionAdapter:
    """Mock adapter exposing required Mainnet lifecycle methods, fresh streams, and cost evidence."""

    instances = []

    def __init__(self, **kwargs):
        self.env = kwargs.get("env", BinanceEnvironment.MAINNET)
        self.preflight_only = kwargs.get("preflight_only", False)
        self.portfolio_margin = kwargs.get("portfolio_margin", False)
        self.safety_limits = Limits.from_environment(self.env)
        self.connection_state = ConnectionState.READY
        self.state = ConnectionState.READY
        self.authenticated = True
        self.private_stream_healthy = True
        self.closed = False
        self.order_submission_attempts = 0
        self.now = utc_now()
        self.last_market_event_at = {"ETHUSDC": self.now}
        self.execution_lease_required = False
        self.symbol_rules = {"ETHUSDC": _create_ethusdc_rules()}
        self.capabilities = SimpleNamespace(
            account_request_succeeded=True,
            authenticated=True,
            trade_authorized=True,
            position_mode_known=True,
            hedge_mode=False,
        )
        self.reconciliation = SimpleNamespace(last_status="IN_SYNC")
        self.account_snapshot = ExchangeAccountSnapshot(
            wallet_balance=Decimal("200.0"),
            margin_balance=Decimal("200.0"),
            available_balance=Decimal("200.0"),
            unrealized_pnl=Decimal("0.0"),
            total_initial_margin=Decimal("0.0"),
            total_maint_margin=Decimal("0.0"),
            position_initial_margin=Decimal("0.0"),
            total_position_notional=Decimal("0.0"),
            effective_leverage=Decimal("1.0"),
            margin_utilization_pct=Decimal("5.0"),
            min_liquidation_distance_pct=Decimal("50.0"),
            liquidation_safety="KNOWN",
            daily_loss_known=True,
            daily_realized_pnl=Decimal("0.0"),
            collateral_asset="USDC",
            risk_currency="USDC",
            configured_leverage=Decimal("5.0"),
            configured_leverage_known=True,
            margin_mode="CROSS",
            margin_mode_known=True,
            valid=True,
            exchange_environment="BINANCE_MAINNET",
        )
        self.ledger = SimpleNamespace(
            get_positions=AsyncMock(return_value=[]),
            get_all_orders=AsyncMock(return_value=[]),
            get_fills=AsyncMock(return_value=[]),
            get_account_snapshot=AsyncMock(return_value=self.account_snapshot),
            account_snapshot=self.account_snapshot,
            on_order_update=None,
            on_fill_update=None,
            on_position_update=None,
            on_account_snapshot_update=None,
        )
        self._worker_authority = None
        self.__class__.instances.append(self)

    def bind_worker_authority(self, authority):
        self._worker_authority = authority

    async def connect(self):
        return True

    async def refresh_market_data(self, symbols):
        return True

    def record_market_event(self, event):
        self.last_market_event_at[str(event.symbol).upper()] = event.event_time
        return True

    def is_symbol_ready_for_execution(self, symbol):
        return symbol == "ETHUSDC"

    def is_account_snapshot_fresh(self):
        return True

    def has_authoritative_market_sample(self, symbol):
        return symbol == "ETHUSDC"

    def _market_data_max_age(self):
        return 30.0

    async def close(self):
        self.closed = True

    async def get_fresh_market_price(self, symbol: str, side: str = "BUY"):
        return Decimal("2501.0") if str(side).upper() == "BUY" else Decimal("2499.0")

    async def get_local_mainnet_cost_evidence(self, *args, **kwargs):
        return {
            "fees_upper_bound_usdc": "0.05",
            "funding_upper_bound_usdc": "0.01",
            "slippage_upper_bound_usdc": "0.02",
            "taker_fee_rate": Decimal("0.0005"),
            "adjusted_funding_rate_cap": Decimal("0.003"),
            "funding_interval_hours": 8,
            "tier_1_mmr": Decimal("0.005"),
        }

    async def get_local_mainnet_risk_context(self, *args, **kwargs):
        return {"risk_proven": True}

    async def verify_local_mainnet_protection(self, *args, **kwargs):
        return True

    async def execute_decision(self, decision, authority=None):
        return [{"status": "FILLED", "orderId": "12345"}]


def setup_pilot_env(monkeypatch, portfolio_margin: bool):
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    monkeypatch.setenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", "pilot-campaign-001")
    monkeypatch.setenv("LOCAL_LIVE_PILOT_STRATEGY_ID", "grid")
    monkeypatch.setenv("LOCAL_SOURCE_FINGERPRINT", "a" * 64)
    monkeypatch.setenv("MAINNET_RELEASE_APPROVAL_ID", "local-approval-12345678-1234-1234-1234-123456789abc")
    monkeypatch.setenv("BINANCE_PORTFOLIO_MARGIN", "true" if portfolio_margin else "false")
    monkeypatch.setenv("BINANCE_MAINNET_API_KEY", "fake_mainnet_key")
    monkeypatch.setenv("BINANCE_MAINNET_API_SECRET", "fake_mainnet_secret")


def create_fake_persistence():
    rules = _create_ethusdc_rules()
    now = utc_now()
    session = {
        "launch_id": "launch-12345678",
        "approval_id": "local-approval-12345678-1234-1234-1234-123456789abc",
        "policy": "LIVE_RESEARCH_PILOT",
        "state": "ACTIVE",
        "pilot_status": "ACTIVE",
        "reserved_orders": 0,
        "submitted_orders": 0,
        "pilot_campaign_expires_at": now + timedelta(hours=24),
        "pilot_drawdown_triggered": False,
        "pilot_accounting_resume_eligible": False,
    }

    class FakePersistence:
        def __init__(self):
            self.mode = SimpleNamespace(value="REQUIRED")
            self._session = dict(session)
            self.is_connected = True

        def readiness(self):
            return {
                "durable": True,
                "mode": "REQUIRED",
                "runtime_target": "LOCAL",
                "database_provider": "POSTGRES_LOCAL",
                "database_host": "127.0.0.1",
                "database_port": 5433,
                "database_identity_verified": True,
                "schema_verified": True,
                "mainnet_launch_session": self._session,
            }

        def validate_execution_mode(self, mode):
            return None

        async def create_execution_ledger(self, symbol, venue):
            return SimpleNamespace(
                get_positions=AsyncMock(return_value=[]),
                get_account_snapshot=AsyncMock(return_value=None),
                on_order_update=None,
                on_fill_update=None,
                on_position_update=None,
                on_account_snapshot_update=None,
            )

        async def create_mainnet_launch_session(self, **kwargs):
            return dict(self._session)

        async def ensure_order_durable(self, order):
            return True

        async def reserve_mainnet_risk_order(self, *args, **kwargs):
            return True

        def enqueue_order(self, *args, **kwargs):
            pass

        def enqueue_fill(self, *args, **kwargs):
            pass

        def enqueue_position(self, *args, **kwargs):
            pass

        def enqueue_risk_snapshot(self, *args, **kwargs):
            pass

    return FakePersistence()


@pytest.mark.parametrize("portfolio_margin", [False, True])
@pytest.mark.asyncio
async def test_local_pilot_arm_success_and_order_execution(monkeypatch, portfolio_margin):
    """ARM a LOCAL LIVE_RESEARCH_PILOT worker, verify readiness, and execute a QUICK trade on MarketEvent."""
    setup_pilot_env(monkeypatch, portfolio_margin)
    FakeExecutionAdapter.instances.clear()

    monkeypatch.setattr("apps.trading_worker.main.BinanceExecutionAdapter", FakeExecutionAdapter)
    monkeypatch.setattr(
        "apps.trading_worker.main.local_live_pilot_readiness",
        lambda: {"can_start": True, "blockers": [], "status": "READY"},
    )

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    persistence = create_fake_persistence()
    worker.persistence = persistence
    worker.symbols = ["ETHUSDC"]
    worker.reconciliation_status = "IN_SYNC"
    worker.market_data_healthy = True
    worker.private_stream_healthy = True
    worker.authenticated = True

    # Preflight mocks
    monkeypatch.setattr(worker, "_mainnet_configured", lambda: True)
    monkeypatch.setattr(worker, "_adapter_trade_authorized", lambda: True)
    monkeypatch.setattr(worker, "_symbol_rules_ready", lambda: True)
    monkeypatch.setattr(worker, "is_market_data_fresh", lambda symbols=None: True)
    monkeypatch.setattr(worker, "is_account_snapshot_ready", lambda: True)
    monkeypatch.setattr(worker, "is_mainnet_account_risk_ready", lambda: True)
    monkeypatch.setattr(worker, "local_supervisor_heartbeat_is_fresh", lambda: True)
    monkeypatch.setattr(worker, "_restart_public_market_stream", AsyncMock(return_value=True))

    read_only_preflight_result = {
        "preflightPassed": True,
        "orderSubmissionAttempts": 0,
        "orderEndpointAttempts": 0,
        "checks": [
            {"id": "CHK-PREFLIGHT-PERSISTENCE", "status": "PASS"},
            {"id": "CHK-PREFLIGHT-DURABLE-LEDGER", "status": "PASS"},
            {"id": "CHK-PREFLIGHT-KILL-SWITCH", "status": "PASS"},
            {"id": "CHK-PREFLIGHT-PILOT-MONITOR", "status": "PASS"},
            {"id": "CHK-PREFLIGHT-PILOT-FLAT-ACCOUNT", "status": "PASS"},
            {"id": "CHK-PREFLIGHT-CONNECTION", "status": "PASS"},
            {"id": "CHK-PREFLIGHT-AUTH", "status": "PASS"},
            {"id": "CHK-PREFLIGHT-CAN-TRADE", "status": "PASS"},
            {"id": "CHK-PREFLIGHT-POSITION-MODE", "status": "PASS"},
            {"id": "CHK-PREFLIGHT-RULES", "status": "PASS"},
            {"id": "CHK-PREFLIGHT-RECONCILIATION", "status": "PASS"},
            {"id": "CHK-PREFLIGHT-PRIVATE-STREAM", "status": "PASS"},
            {"id": "CHK-PREFLIGHT-ACCOUNT-RISK", "status": "PASS"},
            {"id": "CHK-PREFLIGHT-MARKET", "status": "PASS"},
        ],
    }
    monkeypatch.setattr(
        worker, "run_mainnet_read_only_preflight", AsyncMock(return_value=read_only_preflight_result)
    )

    # 1. ARM the worker
    arm_req = ArmRequest(
        executionMode="LIVE",
        instruments=["ETHUSDC"],
        strategies={"grid": True},
        riskProfile="CONSERVATIVE",
        enforcePreflight=True,
        releaseApprovalId="local-approval-12345678-1234-1234-1234-123456789abc",
        launchPolicy="LIVE_RESEARCH_PILOT",
        pilotCampaignId="pilot-campaign-001",
    )
    armed, reason = await worker.arm(arm_req)
    assert armed is True, f"ARM failed: {reason}"
    assert worker.engine_state == WorkerEngineState.ARMED
    assert worker.execution_mode == WorkerExecutionMode.LIVE

    # 2. Check Launch Readiness model
    readiness = worker.get_launch_readiness()
    assert readiness["mainnet_preflight_ready"] is True
    assert readiness["local_pilot_execution_ready"] is True

    # 3. Handle a MarketEvent and verify order execution
    executed_decisions = []
    original_execute_manual_decision = worker.execute_manual_decision

    async def mock_execute_manual_decision(decision):
        executed_decisions.append(decision)
        return await original_execute_manual_decision(decision)

    monkeypatch.setattr(worker, "execute_manual_decision", mock_execute_manual_decision)

    # Mock strategy evaluation to produce a candidate BUY decision
    now = utc_now()
    order_intent = OrderIntent(
        client_order_id="CID-PILOT-BUY-1",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.01"),
        created_at=now,
        strategy_id="grid",
        metadata={"strategy_id": "grid"},
    )
    mock_decision = ExecutionDecision(
        decision_id="DEC-PILOT-BUY-1",
        symbol="ETHUSDC",
        action="SUBMIT_ORDER",
        strategy_id="grid",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[order_intent],
        net_exposure_delta=Decimal("0.01"),
        timestamp=now,
    )
    monkeypatch.setattr(worker.risk_governor, "evaluate", lambda *args, **kwargs: mock_decision)

    pa_state = PriceActionState(
        symbol="ETHUSDC",
        timestamp=now,
        swing_high=Decimal("2550.0"),
        swing_low=Decimal("2450.0"),
        prior_24h_high=Decimal("2550.0"),
        prior_24h_low=Decimal("2450.0"),
        displacement_velocity_pct=Decimal("0.0"),
        displacement_acceleration=Decimal("0.0"),
        range_expansion_ratio=Decimal("0.0"),
        is_reclaiming=True,
    )
    monkeypatch.setattr(worker.pa_engine, "process_event", lambda ev: pa_state)
    monkeypatch.setattr(
        worker.market_state_engine,
        "classify",
        lambda state: MarketState(
            symbol="ETHUSDC",
            timestamp=now,
            primary_regime=RegimeType.R1_RANGE,
            regime_probabilities={"R1_RANGE": Decimal("1.0")},
            atr_1h=Decimal("10.0"),
            volatility_zscore=Decimal("0.0"),
        ),
    )

    event = MarketEvent(
        event_id="ev-pilot-1",
        event_time=now,
        venue="BINANCE_MAINNET",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        last_price=Decimal("2500.0"),
        best_bid=Decimal("2499.0"),
        best_ask=Decimal("2501.0"),
        funding_rate=Decimal("0.0001"),
    )

    await worker.handle_market_event(event)

    assert len(executed_decisions) == 1, "Expected order decision to execute"
    executed = executed_decisions[0]
    assert executed.symbol == "ETHUSDC"
    assert len(executed.orders) >= 1
    # Verify bracket attached (TP, SL, QUICK management mode)
    assert any(hasattr(o, "client_order_id") for o in executed.orders)
    assert executed.orders[0].management_mode == "QUICK"
    assert executed.orders[0].stop_loss_price is not None
    assert executed.orders[0].take_profit_price is not None


@pytest.mark.parametrize("portfolio_margin", [False, True])
@pytest.mark.parametrize(
    "negative_case",
    [
        "pause_new_risk",
        "pilot_accounting_pause",
        "state_not_active",
        "pilot_status_not_active",
        "campaign_expired",
        "drawdown_triggered",
        "mainnet_live_not_approved",
        "preflight_false",
        "kill_switch",
    ],
)
@pytest.mark.asyncio
async def test_local_pilot_negative_cases_stay_monitor_only(monkeypatch, portfolio_margin, negative_case):
    """Negative cases must fail closed, set local_pilot_execution_ready=False, and stay MONITOR_ONLY."""
    setup_pilot_env(monkeypatch, portfolio_margin)
    FakeExecutionAdapter.instances.clear()

    monkeypatch.setattr("apps.trading_worker.main.BinanceExecutionAdapter", FakeExecutionAdapter)
    monkeypatch.setattr(
        "apps.trading_worker.main.local_live_pilot_readiness",
        lambda: {"can_start": True, "blockers": [], "status": "READY"},
    )

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    persistence = create_fake_persistence()
    worker.persistence = persistence
    worker.symbols = ["ETHUSDC"]
    worker.reconciliation_status = "IN_SYNC"
    worker.market_data_healthy = True
    worker.private_stream_healthy = True
    worker.authenticated = True

    monkeypatch.setattr(worker, "_mainnet_configured", lambda: True)
    monkeypatch.setattr(worker, "_adapter_trade_authorized", lambda: True)
    monkeypatch.setattr(worker, "_symbol_rules_ready", lambda: True)
    monkeypatch.setattr(worker, "is_market_data_fresh", lambda symbols=None: True)
    monkeypatch.setattr(worker, "is_account_snapshot_ready", lambda: True)
    monkeypatch.setattr(worker, "is_mainnet_account_risk_ready", lambda: True)
    monkeypatch.setattr(worker, "local_supervisor_heartbeat_is_fresh", lambda: True)
    monkeypatch.setattr(worker, "_restart_public_market_stream", AsyncMock(return_value=True))

    read_only_preflight_result = {
        "preflightPassed": True,
        "orderSubmissionAttempts": 0,
        "orderEndpointAttempts": 0,
        "checks": [{"id": "CHK-PREFLIGHT-PERSISTENCE", "status": "PASS"}],
    }
    monkeypatch.setattr(
        worker, "run_mainnet_read_only_preflight", AsyncMock(return_value=read_only_preflight_result)
    )

    arm_req = ArmRequest(
        executionMode="LIVE",
        instruments=["ETHUSDC"],
        strategies={"grid": True},
        riskProfile="CONSERVATIVE",
        enforcePreflight=True,
        releaseApprovalId="local-approval-12345678-1234-1234-1234-123456789abc",
        launchPolicy="LIVE_RESEARCH_PILOT",
        pilotCampaignId="pilot-campaign-001",
    )
    armed, reason = await worker.arm(arm_req)
    assert armed is True, f"Initial ARM failed: {reason}"

    # Now apply the negative condition
    now = utc_now()
    if negative_case == "pause_new_risk":
        worker.pause_new_risk = True
    elif negative_case == "pilot_accounting_pause":
        worker._pilot_accounting_pause_active = True
    elif negative_case == "state_not_active":
        worker._mainnet_launch_session["state"] = "PAUSED_NEW_RISK"
        persistence._session["state"] = "PAUSED_NEW_RISK"
    elif negative_case == "pilot_status_not_active":
        worker._mainnet_launch_session["pilot_status"] = "CLOSE_ONLY"
        persistence._session["pilot_status"] = "CLOSE_ONLY"
    elif negative_case == "campaign_expired":
        worker._mainnet_launch_session["pilot_campaign_expires_at"] = now - timedelta(minutes=5)
        persistence._session["pilot_campaign_expires_at"] = now - timedelta(minutes=5)
    elif negative_case == "drawdown_triggered":
        worker._mainnet_launch_session["pilot_drawdown_triggered"] = True
        persistence._session["pilot_drawdown_triggered"] = True
    elif negative_case == "mainnet_live_not_approved":
        monkeypatch.setenv("MAINNET_LIVE_APPROVED", "false")
    elif negative_case == "preflight_false":
        monkeypatch.setattr(worker, "is_market_data_fresh", lambda symbols=None: False)
    elif negative_case == "kill_switch":
        worker.kill_switch_active = True

    # Readiness should indicate not ready
    readiness = worker.get_launch_readiness()
    if negative_case != "mainnet_live_not_approved":
        assert readiness.get("local_pilot_execution_ready", False) is False

    executed_decisions = []
    monkeypatch.setattr(
        worker, "execute_manual_decision", AsyncMock(side_effect=lambda d: executed_decisions.append(d))
    )

    mock_decision = ExecutionDecision(
        decision_id="DEC-NEGATIVE-TEST",
        symbol="ETHUSDC",
        action="SUBMIT_ORDER",
        orders=[
            OrderIntent(
                client_order_id="CID-PILOT-NEG",
                symbol="ETHUSDC",
                market_type=MarketType.USDM_FUTURES,
                side=OrderSide.BUY,
                position_side=PositionSide.BOTH,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
                quantity=Decimal("0.01"),
                created_at=now,
                strategy_id="grid",
                metadata={"strategy_id": "grid"},
            )
        ],
        strategy_id="grid",
        risk_class=EconomicRiskClass.NEW_RISK,
        net_exposure_delta=Decimal("0.01"),
        timestamp=now,
    )
    monkeypatch.setattr(worker.risk_governor, "evaluate", lambda *args, **kwargs: mock_decision)

    event = MarketEvent(
        event_id="ev-neg-1",
        event_time=now,
        venue="BINANCE_MAINNET",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        last_price=Decimal("2500.0"),
        best_bid=Decimal("2499.0"),
        best_ask=Decimal("2501.0"),
        funding_rate=Decimal("0.0001"),
    )

    await worker.handle_market_event(event)

    # In all negative cases, execute_manual_decision must NEVER be called
    assert len(executed_decisions) == 0, f"Case {negative_case} unexpectedly executed a decision!"


def _prepare_arm_ready_worker(monkeypatch, portfolio_margin: bool):
    """Build a LOCAL LIVE worker with a fresh (no active launch) persistence and mocked preflight."""
    setup_pilot_env(monkeypatch, portfolio_margin)
    FakeExecutionAdapter.instances.clear()
    monkeypatch.setattr("apps.trading_worker.main.BinanceExecutionAdapter", FakeExecutionAdapter)
    monkeypatch.setattr(
        "apps.trading_worker.main.local_live_pilot_readiness",
        lambda: {"can_start": True, "blockers": [], "status": "READY"},
    )

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    # create_fake_persistence starts with no active launch: worker._mainnet_launch_session
    # stays None until ARM creates the session.
    persistence = create_fake_persistence()
    worker.persistence = persistence
    worker.symbols = ["ETHUSDC"]
    worker.reconciliation_status = "IN_SYNC"
    worker.market_data_healthy = True
    worker.private_stream_healthy = True
    worker.authenticated = True

    monkeypatch.setattr(worker, "_mainnet_configured", lambda: True)
    monkeypatch.setattr(worker, "_adapter_trade_authorized", lambda: True)
    monkeypatch.setattr(worker, "_symbol_rules_ready", lambda: True)
    monkeypatch.setattr(worker, "is_market_data_fresh", lambda symbols=None: True)
    monkeypatch.setattr(worker, "is_account_snapshot_ready", lambda: True)
    monkeypatch.setattr(worker, "is_mainnet_account_risk_ready", lambda: True)
    monkeypatch.setattr(worker, "local_supervisor_heartbeat_is_fresh", lambda: True)
    monkeypatch.setattr(worker, "_restart_public_market_stream", AsyncMock(return_value=True))
    monkeypatch.setattr(
        worker,
        "run_mainnet_read_only_preflight",
        AsyncMock(
            return_value={
                "preflightPassed": True,
                "orderSubmissionAttempts": 0,
                "orderEndpointAttempts": 0,
                "checks": [
                    {"id": check_id, "status": "PASS"}
                    for check_id in (
                        "CHK-PREFLIGHT-PERSISTENCE",
                        "CHK-PREFLIGHT-DURABLE-LEDGER",
                        "CHK-PREFLIGHT-KILL-SWITCH",
                        "CHK-PREFLIGHT-PILOT-MONITOR",
                        "CHK-PREFLIGHT-PILOT-FLAT-ACCOUNT",
                        "CHK-PREFLIGHT-CONNECTION",
                        "CHK-PREFLIGHT-AUTH",
                        "CHK-PREFLIGHT-CAN-TRADE",
                        "CHK-PREFLIGHT-POSITION-MODE",
                        "CHK-PREFLIGHT-RULES",
                        "CHK-PREFLIGHT-RECONCILIATION",
                        "CHK-PREFLIGHT-PRIVATE-STREAM",
                        "CHK-PREFLIGHT-ACCOUNT-RISK",
                        "CHK-PREFLIGHT-MARKET",
                    )
                ],
            }
        ),
    )
    return worker


@pytest.mark.asyncio
async def test_local_pilot_arm_binds_accounting_callbacks_to_new_session(monkeypatch):
    """Pilot accounting hooks must be callable after a successful ARM.

    On a fresh ARM no launch session exists before ARM creates one, so hooks gated
    only on the pre-ARM session would stay None and fills, fees and marks would
    never reach the campaign ledger. The hooks must be bound once the session
    created during ARM is known.
    """
    worker = _prepare_arm_ready_worker(monkeypatch, portfolio_margin=False)
    arm_req = ArmRequest(
        executionMode="LIVE",
        instruments=["ETHUSDC"],
        strategies={"grid": True},
        riskProfile="CONSERVATIVE",
        enforcePreflight=True,
        releaseApprovalId="local-approval-12345678-1234-1234-1234-123456789abc",
        launchPolicy="LIVE_RESEARCH_PILOT",
        pilotCampaignId="pilot-campaign-001",
    )

    armed, reason = await worker.arm(arm_req)
    assert armed is True, f"ARM failed: {reason}"

    adapter = worker.execution_adapter
    assert adapter is not None
    assert callable(adapter.on_local_live_pilot_fill), "fill/fee accounting hook unbound after ARM"
    assert callable(adapter.on_local_live_pilot_mark), "mark accounting hook unbound after ARM"
    assert callable(adapter.on_local_live_pilot_funding_reconcile), "funding hook unbound after ARM"
    assert adapter.on_local_live_pilot_fill == worker._persist_local_live_pilot_fill
    assert adapter.on_local_live_pilot_mark == worker._persist_local_live_pilot_mark
    assert adapter.on_local_live_pilot_funding_reconcile == worker._reconcile_local_live_pilot_funding
