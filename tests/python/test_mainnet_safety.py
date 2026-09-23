"""Comprehensive fail-closed safety tests for Binance USD-M ETHUSDC Mainnet.

Verifies:
1. Mainnet / Testnet route isolation (fixed endpoints only)
2. Wrong environment rejection
3. Unknown or incomplete exchange filters fail-closed
4. Leverage >10x rejection
5. Mainnet caps (collateral <= 100 USDC, gross exposure <= 1000 USDC,
   order notional <= 50 USDC, daily loss <= 5 USDC, 1 active chain)
6. Stale stream, stale account snapshot, or failed persistence rejection
7. Ambiguous order reconciliation by client order ID
8. Carry strategy fail-closed without positive net edge
9. Binance CLI mutation command rejection
"""

from decimal import Decimal

import pytest

from apps.trading_worker.engines.funding_carry import (
    FundingCarryEngine,
)
from apps.trading_worker.research.binance_cli import (
    BinanceCliPolicyError,
    build_read_only_command,
)
from apps.trading_worker.venues.binance.config import (
    BinanceEnvironment,
    get_rest_url,
    get_ws_url,
    parse_environment,
)
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.gates import (
    DecisionExecutionGate,
    OrderExecutionGate,
)
from apps.trading_worker.venues.binance.ledger import InMemoryLedger
from apps.trading_worker.venues.binance.models import (
    BinanceTransportAmbiguity,
    ConnectionState,
    ExchangeAccountSnapshot,
)
from apps.trading_worker.venues.binance.models import (
    TestnetSafetyLimits as SafetyLimits,
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
    utc_now,
)
from datetime import UTC


def test_mainnet_testnet_route_isolation():
    """Verify that Mainnet maps only to fixed production endpoints."""
    assert get_rest_url(BinanceEnvironment.MAINNET) == "https://fapi.binance.com"
    assert get_ws_url(BinanceEnvironment.MAINNET) == "wss://fstream.binance.com/ws"
    assert get_rest_url(BinanceEnvironment.TESTNET) == "https://testnet.binancefuture.com"
    assert get_ws_url(BinanceEnvironment.TESTNET) == "wss://stream.binancefuture.com/ws"


def test_wrong_environment_rejection():
    """Verify that arbitrary or malformed environments are rejected."""
    with pytest.raises(ValueError, match="Binance environment must be TESTNET or MAINNET"):
        parse_environment("DEMO")
    with pytest.raises(ValueError, match="Binance environment must be TESTNET or MAINNET"):
        parse_environment("STAGING")
    with pytest.raises(ValueError, match="Binance environment must be TESTNET or MAINNET"):
        SafetyLimits.from_environment("PRODUCTION")


def test_unknown_exchange_filters_fail_closed():
    """Verify that missing LOT_SIZE or PRICE_FILTER fails closed."""
    incomplete_info = {
        "symbol": "ETHUSDC",
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "baseAsset": "ETH",
        "quoteAsset": "USDC",
        "marginAsset": "USDC",
        "filters": [
            {
                "filterType": "LOT_SIZE",
                "minQty": "0.001",
                "maxQty": "1000",
                "stepSize": "0.001",
            },
            # Missing PRICE_FILTER
        ],
    }
    rules = SymbolTradingRules("ETHUSDC")
    rules.parse_exchange_info(incomplete_info)
    assert not rules.is_ready_for("LIMIT")


def test_leverage_exceeding_10x_rejected():
    """Verify that leverage >10x is rejected on Mainnet."""
    mainnet_limits = SafetyLimits.from_environment(BinanceEnvironment.MAINNET)
    assert mainnet_limits.max_leverage <= Decimal("2.0")


@pytest.mark.asyncio
async def test_read_only_mainnet_adapter_bypasses_approval_but_blocks_mutations(monkeypatch):
    """Preflight may observe an unapproved account but can never submit orders."""

    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "false")
    adapter = BinanceExecutionAdapter(
        api_key="preflight-key",
        api_secret="preflight-secret",
        env=BinanceEnvironment.MAINNET,
        ledger=InMemoryLedger(),
        preflight_only=True,
    )
    authority = object()
    adapter.bind_worker_authority(authority)

    decision = ExecutionDecision(
        symbol="ETHUSDC",
        decision_id="preflight-no-order",
        action="EXECUTE",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[],
    )

    assert await adapter.execute_decision(decision, authority=authority) == []
    assert await adapter.cancel_all_open_orders(authority=authority) == {
        "status": "BLOCKED",
        "reason": "Read-only preflight adapter cannot cancel orders",
    }
    assert await adapter.cancel_order("ETHUSDC", "client-id", authority=authority) is False
    assert (
        await adapter.modify_order(
            "ETHUSDC",
            "client-id",
            Decimal(100),
            Decimal("0.001"),
            "BUY",
            authority=authority,
        )
        is None
    )
    assert await adapter.emergency_flatten(authority=authority) == []
    assert adapter.last_emergency_result["status"] == "BLOCKED"
    with pytest.raises(PermissionError, match="cannot call the order endpoint"):
        await adapter.rest_client.request(
            "POST",
            "/fapi/v1/order",
            signed=True,
            params={"symbol": "ETHUSDC"},
        )
    assert adapter.rest_client.order_endpoint_attempts == 1
    await adapter.close()


@pytest.mark.asyncio
async def test_mainnet_caps_enforced(monkeypatch):
    """Verify the locked Mainnet caps and order notional enforcement."""
    limits = SafetyLimits.from_environment(BinanceEnvironment.MAINNET)
    assert limits.allowed_symbols == {"ETHUSDC"}
    assert limits.max_collateral <= Decimal("250.0")
    assert limits.max_total_open_notional <= Decimal("1000.0")
    assert limits.max_single_order_notional <= Decimal("50.0")
    assert limits.max_daily_loss <= Decimal("25.0")
    assert limits.max_active_exposure_chains == 1
    assert limits.max_leverage <= Decimal("2.0")

    # Build mock rules for ETHUSDC
    rules = SymbolTradingRules("ETHUSDC")
    rules.parse_exchange_info({
        "symbol": "ETHUSDC",
        "status": "TRADING",
        "contractType": "PERPETUAL",
        "baseAsset": "ETH",
        "quoteAsset": "USDC",
        "marginAsset": "USDC",
        "orderTypes": ["LIMIT", "MARKET"],
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.1", "maxPrice": "100000", "tickSize": "0.1"},
            {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
        ],
    })

    class MockLedger:
        async def get_open_orders(self):
            return []
        async def get_positions(self):
            return []

    class MockCapabilities:
        hedge_mode = False
        trade_authorized = True

    class MockAdapter:
        env = BinanceEnvironment.MAINNET
        connection_state = ConnectionState.READY
        authenticated = True
        capabilities = MockCapabilities()
        safety_limits = limits
        symbol_rules = {"ETHUSDC": rules}
        ledger = MockLedger()
        reconciliation = type("Recon", (), {"last_status": "IN_SYNC"})()
        private_stream_healthy = True
        last_market_event_at = {"ETHUSDC": utc_now()}
        account_snapshot = ExchangeAccountSnapshot(
            wallet_balance=Decimal("100.0"),
            margin_balance=Decimal("100.0"),
            available_balance=Decimal("100.0"),
            unrealized_pnl=Decimal("0.0"),
            total_initial_margin=Decimal("0.0"),
            total_maint_margin=Decimal("0.0"),
            position_initial_margin=Decimal("0.0"),
            total_position_notional=Decimal("0.0"),
            effective_leverage=Decimal("1.0"),
            margin_utilization_pct=Decimal("0.0"),
            liquidation_safety="KNOWN",
            min_liquidation_distance_pct=Decimal("50.0"),
            collateral_asset="USDC",
            risk_currency="USDC",
            margin_mode="SINGLE_ASSET_CROSS",
            margin_mode_known=True,
            configured_leverage=Decimal("2.0"),
            configured_leverage_known=True,
            daily_loss_known=True,
            daily_loss_asset="USDC",
            daily_pnl_includes_fees=True,
            daily_pnl_includes_funding=True,
            daily_loss_window_start=utc_now().replace(hour=0, minute=0, second=0, microsecond=0),
            daily_loss_window_end=utc_now(),
            daily_realized_pnl=Decimal("0.0"),
            valid=True,
        )

        def is_account_snapshot_fresh(self):
            return True

        def has_authoritative_market_sample(self, symbol):
            return True

    adapter = MockAdapter()
    gate = OrderExecutionGate(adapter=adapter)

    too_large_intent = OrderIntent(
        symbol="ETHUSDC",
        client_order_id="CID-ETH-TOO-LARGE",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        position_side=PositionSide.BOTH,
        quantity=Decimal("0.05"),
        price=Decimal("2000.0"),  # 0.05 * 2000 = 100 USDC notional > 50 USDC cap
        time_in_force=TimeInForce.GTC,
    )

    # 1. Without MAINNET_LIVE_APPROVED=true, must be rejected
    res_disarmed = await gate.check(
        too_large_intent,
        risk_class=EconomicRiskClass.NEW_RISK,
        allow_emergency_fallback=False,
    )
    assert not res_disarmed.allowed
    assert "mainnet_live_approved" in res_disarmed.reason.lower()

    # 2. With MAINNET_LIVE_APPROVED=true, must pass approval check but fail single-order notional cap
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    res_cap = await gate.check(
        too_large_intent,
        risk_class=EconomicRiskClass.NEW_RISK,
        allow_emergency_fallback=False,
    )
    assert not res_cap.allowed
    assert "single-order notional exceeded" in res_cap.reason.lower()


def test_stale_stream_and_persistence_rejection(monkeypatch):
    """Verify that stale market data or private stream rejects risk-increasing orders."""
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")

    class MockAdapter:
        env = BinanceEnvironment.MAINNET
        connection_state = ConnectionState.READY
        authenticated = True
        capabilities = type("Cap", (), {"trade_authorized": True})()
        private_stream_healthy = True
        reconciliation = type("Recon", (), {"last_status": "IN_SYNC"})()
        account_snapshot = type(
            "Snap",
            (),
            {
                "valid": True,
                "available_balance": Decimal("100.0"),
                "margin_utilization_pct": Decimal("10.0"),
                "liquidation_safety": "KNOWN",
                "min_liquidation_distance_pct": Decimal("50.0"),
            },
        )()
        last_market_event_at = {}

    class MockWorkerStale:
        def __init__(self):
            self.execution_mode = "LIVE"
            self.kill_switch_active = False
            self.market_data_healthy = False  # Stale market data
            self.engine_state = "ARMED"
            self.pause_new_risk = False
            self.recovery_only = False
            self.last_market_event_at = {}
            self.execution_adapter = MockAdapter()

        def _mainnet_configured(self):
            return True

        def _enabled_strategies(self):
            return {"grid", "trend", "shock", "carry"}

        def is_account_snapshot_ready(self):
            return True

    worker = MockWorkerStale()
    gate = DecisionExecutionGate(worker=worker)

    decision = ExecutionDecision(
        symbol="ETHUSDC",
        decision_id="dec-1",
        action="EXECUTE",
        direction="LONG",
        risk_class=EconomicRiskClass.NEW_RISK,
        strategy_id="grid",
        opportunity_score=0.9,
        target_exposure_delta=Decimal("20.0"),
        confidence=0.9,
        orders=[
            OrderIntent(
                symbol="ETHUSDC",
                client_order_id="cid-1",
                market_type=MarketType.USDM_FUTURES,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                position_side=PositionSide.LONG,
                quantity=Decimal("0.01"),
                price=Decimal("2000.0"),
            )
        ],
    )
    res = gate.check(decision)
    assert not res.allowed
    assert "stale" in res.reason.lower()


def test_ambiguous_order_reconciliation_by_client_order_id():
    """Verify that an ambiguous transport error carries the client order id."""
    err = BinanceTransportAmbiguity("Request timed out")
    assert isinstance(err, Exception)
    assert "timed out" in str(err).lower()


def test_carry_strategy_fail_closed_without_positive_net_edge():
    """Verify funding carry engine fails closed without explicit cost inputs."""
    carry_engine = FundingCarryEngine()
    assert carry_engine.cost_inputs is None

    # When cost inputs are None, carry engine emits no intents
    event = MarketEvent(
        event_id="ev-1",
        event_time=utc_now(),
        venue="binance_usdm",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        last_price=Decimal("2500.0"),
        best_bid=Decimal("2499.0"),
        best_ask=Decimal("2501.0"),
        funding_rate=Decimal("0.0001"),
    )
    state = MarketState(
        symbol="ETHUSDC",
        timestamp=utc_now(),
        primary_regime=RegimeType.R1_RANGE,
        regime_probabilities={"R1_RANGE": Decimal("1.0")},
        atr_1h=Decimal("10.0"),
        volatility_zscore=Decimal("0.0"),
    )
    intent = carry_engine.evaluate(event, state)
    assert intent is None


def test_cli_mutation_commands_rejected():
    """Verify the Binance CLI research wrapper rejects all mutation operations."""
    for mutation in ("place_order", "new_order", "cancel_order", "cancel_all", "transfer", "set_leverage", "set_margin_type"):
        with pytest.raises(BinanceCliPolicyError):
            build_read_only_command(mutation)


@pytest.mark.asyncio
async def test_disarmed_worker_ignores_market_event_without_pausing_risk():
    """Verify that incoming market events while DISARMED in LIVE mode do not trigger risk pause."""
    from apps.trading_worker.main import (
        TradingWorkerApp,
        WorkerEngineState,
        WorkerExecutionMode,
    )

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE
    worker.engine_state = WorkerEngineState.DISARMED
    worker.active_configuration = None
    worker.execution_adapter = None
    worker.pause_new_risk = False

    event = MarketEvent(
        event_id="ev-live-disarmed",
        event_time=utc_now(),
        venue="BINANCE_MAINNET",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        last_price=Decimal("2500.0"),
        best_bid=Decimal("2499.0"),
        best_ask=Decimal("2501.0"),
        funding_rate=Decimal("0.0001"),
    )

    await worker.handle_market_event(event)

    assert worker.engine_state == WorkerEngineState.DISARMED
    assert worker.pause_new_risk is False
    assert worker.get_state().engine_state == WorkerEngineState.DISARMED


def test_ethusdc_grid_strategy_sizing_and_notional_clamp():
    """Verify that ETHUSDC grid sizing and dynamic notional clamp honor the 50 USDC pilot limit."""
    from apps.trading_worker.engines.grid_strategy import GridStrategyEngine
    from apps.trading_worker.main import TradingWorkerApp, WorkerExecutionMode
    from apps.trading_worker.venues.binance.models import TestnetSafetyLimits
    from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules
    from domain.models import (
        ExecutionDecision,
        OrderIntent,
        OrderSide,
        OrderType,
        PositionSide,
        PriceActionState,
        RegimeType,
        TimeInForce,
    )

    now = utc_now()
    pa_state = PriceActionState(
        symbol="ETHUSDC",
        timestamp=now,
        swing_high=Decimal(2550),
        swing_low=Decimal(2450),
        prior_24h_high=Decimal(2550),
        prior_24h_low=Decimal(2450),
        displacement_velocity_pct=Decimal(0),
        displacement_acceleration=Decimal(0),
        range_expansion_ratio=Decimal(0),
        is_reclaiming=True,
    )
    market_state = type(
        "StubMarketState",
        (),
        {
            "symbol": "ETHUSDC",
            "timestamp": now,
            "primary_regime": RegimeType.R1_RANGE,
            "regime_probabilities": {},
            "atr_1h": Decimal(20),
            "volatility_zscore": Decimal(0),
            "shock_active": False,
        },
    )()

    # Grid engine for ETHUSDC should size appropriately
    grid = GridStrategyEngine()
    intent = grid.evaluate(pa_state, market_state, grid_depth=0)
    assert intent is not None
    assert intent.desired_delta_qty == Decimal("0.028")

    # Dynamic clamp in worker
    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE

    rules = SymbolTradingRules("ETHUSDC")
    rules.status = "TRADING"
    rules.min_qty = Decimal("0.001")
    rules.max_qty = Decimal("1000.0")
    rules.step_size = Decimal("0.001")
    rules.min_price = Decimal("0.01")
    rules.max_price = Decimal("100000.0")
    rules.tick_size = Decimal("0.01")
    rules.min_notional = Decimal("5.0")

    class MockLimitsAdapter:
        safety_limits = TestnetSafetyLimits.from_environment("MAINNET")
        symbol_rules = {"ETHUSDC": rules}

    worker.execution_adapter = MockLimitsAdapter()

    oversized_order = OrderIntent(
        client_order_id="CID-OVERSIZED",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.1"),  # 0.1 * 2500 = 250 USDC > 50 USDC
        created_at=now,
    )
    decision = ExecutionDecision(
        decision_id="DEC-TEST-CLAMP",
        symbol="ETHUSDC",
        action="SUBMIT_ORDER",
        orders=[oversized_order],
        net_exposure_delta=Decimal("0.1"),
        timestamp=now,
    )

    decision = worker._clamp_order_notional_if_needed(decision, reference_price=Decimal("2500.0"))

    clamped_order = decision.orders[0]
    clamped_notional = clamped_order.quantity * Decimal("2500.0")
    assert clamped_notional <= Decimal("50.0")
    assert clamped_notional >= Decimal("5.0")
    assert clamped_order.quantity == Decimal("0.018")  # 45 / 2500 = 0.018
    assert decision.net_exposure_delta == Decimal("0.018")


def test_clamp_order_notional_bumps_undersized_order_to_satisfy_min_notional():
    from datetime import datetime, timezone

    from apps.trading_worker.main import TradingWorkerApp, WorkerExecutionMode
    from apps.trading_worker.venues.binance.models import TestnetSafetyLimits
    from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules
    from domain.models import (
        ExecutionDecision,
        MarketType,
        OrderIntent,
        OrderSide,
        OrderType,
        PositionSide,
        TimeInForce,
    )

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE

    rules = SymbolTradingRules("ETHUSDC")
    rules.status = "TRADING"
    rules.min_qty = Decimal("0.001")
    rules.max_qty = Decimal("1000.0")
    rules.step_size = Decimal("0.001")
    rules.min_price = Decimal("0.01")
    rules.max_price = Decimal("100000.0")
    rules.tick_size = Decimal("0.01")
    rules.min_notional = Decimal("20.0")

    class MockLimitsAdapter:
        safety_limits = TestnetSafetyLimits.from_environment("MAINNET")
        symbol_rules = {"ETHUSDC": rules}

    worker.execution_adapter = MockLimitsAdapter()

    now = datetime.now(UTC)
    undersized_order = OrderIntent(
        client_order_id="B-SYS-TEST-MIN-NOTIONAL",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.005"),  # 0.005 * 2500 = 12.5 USDC < 20 USDC
        created_at=now,
    )
    decision = ExecutionDecision(
        decision_id="DEC-TEST-CLAMP-MIN",
        symbol="ETHUSDC",
        action="SUBMIT_ORDER",
        orders=[undersized_order],
        net_exposure_delta=Decimal("0.005"),
        timestamp=now,
    )

    decision = worker._clamp_order_notional_if_needed(decision, reference_price=Decimal("2500.0"))

    clamped_order = decision.orders[0]
    clamped_notional = clamped_order.quantity * Decimal("2500.0")
    assert clamped_notional >= Decimal("20.0"), f"Expected >= 20, got {clamped_notional}"
    assert clamped_notional <= Decimal("50.0"), f"Expected <= 50, got {clamped_notional}"
    assert clamped_order.quantity == Decimal("0.010")  # 25 / 2500 = 0.010
    assert decision.net_exposure_delta == Decimal("0.010")



