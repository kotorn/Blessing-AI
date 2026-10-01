from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import time
import hashlib
import json
import subprocess

import pytest

from apps.trading_worker.execution_lease import LeaseLostError
from apps.trading_worker.main import TradingWorkerApp
from apps.trading_worker.venues.binance.config import BinanceEnvironment
from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter
from apps.trading_worker.venues.binance.gates import _local_mainnet_risk_gate
from apps.trading_worker.venues.binance.mainnet_risk import LOCAL_LIVE_PILOT_POLICY
from apps.trading_worker.venues.binance.local_pilot_readiness import local_live_pilot_readiness
from apps.trading_worker.venues.binance.mainnet_risk import LOCAL_LIVE_PILOT_POLICY_SHA256
from apps.trading_worker.venues.binance.models import ConnectionState
from domain.enums import MarketType, OrderSide, OrderType, PositionSide, TimeInForce
from domain.models import OrderIntent


def test_pilot_readiness_stays_blocked_without_committed_runtime_evidence():
    readiness = local_live_pilot_readiness()

    assert readiness["status"] == "BLOCKED"
    assert readiness["can_approve"] is False
    assert readiness["can_start"] is False
    assert "LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN" in readiness["blockers"]
    assert "LOCAL_PILOT_CAPABILITY_EVIDENCE_MISSING_OR_STALE" in readiness["blockers"]


@pytest.mark.asyncio
async def test_local_live_arm_requires_server_bound_pilot_campaign(monkeypatch):
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    monkeypatch.delenv("LOCAL_LIVE_PILOT_CAMPAIGN_ID", raising=False)
    monkeypatch.setattr(TradingWorkerApp, "_mainnet_configured", lambda _self: True)

    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    success, message = await worker.arm({
        "executionMode": "LIVE",
        "instruments": ["ETHUSDC"],
        "enforcePreflight": True,
        "releaseApprovalId": "local-approval-12345678-1234-1234-1234-123456789abc",
        "launchPolicy": "STAGED_FIRST_ORDER",
    })

    assert success is False
    assert message == "Local Mainnet LIVE requires a server-bound Research Pilot campaign."
    assert worker.execution_mode.value == "PAPER"
    assert worker.engine_state.value == "DISARMED"
    assert worker.get_state().order_submission_attempts == 0


@pytest.mark.asyncio
async def test_local_runtime_cannot_use_legacy_autonomous_continuation(monkeypatch):
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")
    worker = TradingWorkerApp(symbols=["ETHUSDC"])

    success, message = await worker.continue_autonomous({
        "continuationApprovalId": "continuation-12345678-1234-1234-1234-123456789abc",
        "launchId": "launch-12345678",
        "instruments": ["ETHUSDC"],
        "enforcePreflight": True,
    })

    assert success is False
    assert message == "Legacy autonomous continuation is unavailable on Local; use the active campaign workflow."
    assert worker.execution_mode.value == "PAPER"
    assert worker.engine_state.value == "DISARMED"
    assert worker.get_state().order_submission_attempts == 0


def test_worker_readiness_rechecks_artifact_hashes_and_accepts_complete_fixture(tmp_path):
    from apps.trading_worker.venues.binance.local_pilot_readiness import (
        DEPENDENCY_PATHS,
        REQUIRED_CHECKS,
        REQUIRED_REVIEW_DOMAINS,
        SOURCE_PATHS,
        _hash_files,
    )

    root = tmp_path
    for name in SOURCE_PATHS + DEPENDENCY_PATHS + ("infra/postgres/migrations/001_fixture.sql",):
        file_path = root / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("fixture\n", encoding="utf-8")
    (root / ".gitignore").write_text("artifacts/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run([
        "git", "-c", "user.name=Local Test", "-c", "user.email=local-test@example.invalid",
        "commit", "-qm", "readiness fixture",
    ], cwd=root, check=True)
    git_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    now = datetime.now(timezone.utc)
    observed = now.isoformat().replace("+00:00", "Z")
    artifacts = root / "artifacts"
    checks_dir = artifacts / "local-pilot-checks"
    reviews_dir = artifacts / "local-pilot-reviews"
    checks_dir.mkdir(parents=True)
    reviews_dir.mkdir(parents=True)

    trial = {
        "trial_type": "PROTECTED_ETHUSDC_V1", "build_sha": git_sha,
        "environment": "BINANCE_TESTNET", "symbol": "ETHUSDC", "status": "PASS",
        "protection_status": "PROTECTED_VERIFIED", "close_status": "VERIFIED",
        "reconciliation_status": "IN_SYNC", "diff_count": 0, "entry_fill_count": 1,
        "position_after": [], "open_orders_after": [], "open_algo_after": [],
        "entry_client_order_id": "entry-fixture", "close_client_order_id": "close-fixture",
        "stop_client_algo_id": "stop-fixture", "target_client_algo_id": "target-fixture",
    }
    trial_raw = json.dumps(trial, separators=(",", ":")).encode()
    trial_path = artifacts / "testnet-trial-fixture.json"
    trial_path.write_bytes(trial_raw)
    checks = []
    for check_id in REQUIRED_CHECKS:
        output_hash = hashlib.sha256(trial_raw).hexdigest() if check_id == "TESTNET_E2E" else "a" * 64
        record = {
            "id": check_id, "gitSha": git_sha, "status": "PASS", "exitCode": 0,
            "observedAt": observed, "outputSha256": output_hash,
        }
        raw = json.dumps(record, separators=(",", ":")).encode()
        (checks_dir / f"{check_id}.json").write_bytes(raw)
        checks.append({
            "id": check_id, "status": "PASS", "observedAt": observed,
            "resultSha256": hashlib.sha256(raw).hexdigest(),
        })

    reviews = []
    for index, domain in enumerate(REQUIRED_REVIEW_DOMAINS):
        report = {
            "domain": domain, "gitSha": git_sha, "reviewerId": f"reviewer-{index}",
            "status": "PASS", "observedAt": observed,
        }
        raw = json.dumps(report, separators=(",", ":")).encode()
        (reviews_dir / f"{domain}.json").write_bytes(raw)
        reviews.append({
            "domain": domain, "reviewerId": report["reviewerId"], "status": "PASS",
            "observedAt": observed, "reportSha256": hashlib.sha256(raw).hexdigest(),
        })

    capability = {
        "schemaVersion": 1, "gitSha": git_sha,
        "sourceSha256": _hash_files(root, SOURCE_PATHS),
        "dependencySha256": _hash_files(root, DEPENDENCY_PATHS),
        "migrationSha256": _hash_files(root, ("infra/postgres/migrations",)),
        "pilotPolicySha256": hashlib.sha256(
            (root / "config/risk/live_research_pilot.json").read_bytes()
        ).hexdigest(),
        "observedAt": observed, "checks": checks, "reviews": reviews,
    }
    (artifacts / "local-pilot-capability.json").write_text(
        json.dumps(capability, separators=(",", ":")), encoding="utf-8",
    )

    readiness = local_live_pilot_readiness(root, now=now)
    assert readiness["status"] == "BLOCKED"
    assert readiness["can_approve"] is False
    assert readiness["can_start"] is False
    assert readiness["provenance"] == {
        "local_checks": "UNVERIFIED", "reviews": "UNVERIFIED", "testnet": "UNVERIFIED",
    }
    assert "LOCAL_PILOT_REVIEW_PROVENANCE_UNVERIFIED" in readiness["blockers"]
    assert "LOCAL_PILOT_TESTNET_PROVENANCE_UNVERIFIED" in readiness["blockers"]

    (reviews_dir / "ORDER_RISK.json").write_text("tampered", encoding="utf-8")
    blocked = local_live_pilot_readiness(root, now=now)
    assert blocked["status"] == "BLOCKED"
    assert blocked["can_start"] is False
    assert "LOCAL_PILOT_INDEPENDENT_REVIEWS_NOT_VERIFIED" in blocked["blockers"]


@pytest.mark.asyncio
async def test_final_order_fence_rejects_stalled_pilot_lifecycle_monitor(monkeypatch):
    monkeypatch.setattr(
        "apps.trading_worker.venues.binance.execution.local_live_pilot_readiness",
        lambda: {"can_start": True},
    )
    authority = SimpleNamespace(
        _mainnet_launch_session={"policy": "LIVE_RESEARCH_PILOT"},
        local_pilot_monitor_allows_new_risk=lambda: False,
    )
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.MAINNET
    adapter._worker_authority = authority

    with pytest.raises(LeaseLostError, match="lifecycle monitor is stale"):
        await adapter._final_risk_increase_fence(
            SimpleNamespace(risk_class="NEW_RISK"),
            make_intent(),
            object(),
            client_order_id="stalled-monitor-entry",
            reserved_open_orders=0,
            reserved_notional=Decimal("0"),
            allow_emergency_fallback=False,
        )


@pytest.mark.asyncio
async def test_final_order_fence_rechecks_monitor_after_async_risk_gate(monkeypatch):
    monkeypatch.setattr(
        "apps.trading_worker.venues.binance.execution.local_live_pilot_readiness",
        lambda: {"can_start": True},
    )
    monitor_checks = 0

    def monitor_fresh():
        nonlocal monitor_checks
        monitor_checks += 1
        return monitor_checks == 1

    authority = SimpleNamespace(
        _mainnet_launch_session={"policy": "LIVE_RESEARCH_PILOT"},
        local_pilot_monitor_allows_new_risk=monitor_fresh,
        _evaluate_execution_gate=lambda _decision: (True, ""),
    )
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.env = BinanceEnvironment.MAINNET
    adapter._worker_authority = authority
    adapter.order_gate = SimpleNamespace()

    async def assert_lease(_risk):
        return None

    async def allow_gate():
        adapter._worker_authority.local_pilot_monitor_allows_new_risk = lambda: False
        return SimpleNamespace(allowed=True, prepared="prepared", reason="")

    adapter.order_gate.check = lambda *_args, **_kwargs: allow_gate()
    adapter._assert_execution_lease = assert_lease

    with pytest.raises(LeaseLostError, match="became stale"):
        await adapter._final_risk_increase_fence(
            SimpleNamespace(risk_class="NEW_RISK"),
            make_intent(),
            "prepared",
            client_order_id="monitor-stale-during-gate",
            reserved_open_orders=0,
            reserved_notional=Decimal("0"),
            allow_emergency_fallback=False,
        )
    assert monitor_checks == 1


@pytest.mark.asyncio
async def test_protection_store_failure_is_not_reported_as_successful_monitor_cycle(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("LOCAL_RUNTIME_TARGET", "LOCAL")

    class Protections:
        async def list_active_protections(self, **_kwargs):
            raise OSError("database read failed")

    class Persistence:
        repository = SimpleNamespace(algo_protections=Protections())

        async def get_mainnet_launch_session(self, _launch_id):
            return {
                "launch_id": "launch-monitor-test",
                "policy": "LIVE_RESEARCH_PILOT",
                "pilot_campaign_id": "campaign-monitor-test",
                "runtime_target": "LOCAL",
                "symbol": "ETHUSDC",
                "pilot_status": "ACTIVE",
                "pilot_peak_pnl_usdc": "0",
                "pilot_net_pnl_usdc": "0",
                "pilot_max_drawdown_usdc": "5",
            }

    worker = SimpleNamespace(
        _mainnet_launch_id="launch-monitor-test",
        _mainnet_launch_session={
            "launch_id": "launch-monitor-test",
            "policy": "LIVE_RESEARCH_PILOT",
            "pilot_campaign_id": "campaign-monitor-test",
        },
        persistence=Persistence(),
        pause_new_risk=False,
    )
    adapter = BinanceExecutionAdapter.__new__(BinanceExecutionAdapter)
    adapter.state = ConnectionState.READY
    adapter.reconciliation = SimpleNamespace(last_status="IN_SYNC")
    adapter._is_local_mainnet_runtime = lambda: True

    result = await adapter.check_and_enforce_pilot_protections(worker)

    assert result["failed_action_count"] == 1
    assert worker.pause_new_risk is True
    assert adapter.reconciliation.last_status == "UNKNOWN"


class NormalizedRules:
    @staticmethod
    def normalize_price(value):
        return Decimal(str(value)).quantize(Decimal("0.1"))

    @staticmethod
    def normalize_quantity(value):
        return Decimal(str(value))


def make_adapter(**overrides):
    values = {
        "env": BinanceEnvironment.MAINNET,
        "connection_state": ConnectionState.READY,
        "authenticated": True,
        "capabilities": SimpleNamespace(trade_authorized=True),
        "private_stream_healthy": True,
        "reconciliation": SimpleNamespace(last_status="IN_SYNC"),
        "symbol_rules": {"ETHUSDC": NormalizedRules()},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_intent():
    return OrderIntent(
        client_order_id="local-risk-1",
        symbol="ETHUSDC",
        basket_id="basket-1",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.4"),
        stop_loss_price=Decimal("90"),
        take_profit_price=Decimal("130"),
        management_mode="QUICK",
        estimated_fees_usdc=Decimal("0.4"),
        estimated_funding_usdc=Decimal("0.3"),
        estimated_slippage_usdc=Decimal("0.3"),
    )


def make_risk_context():
    source_hash = "a" * 64
    dependency_hash = "b" * 64
    migration_hash = "c" * 64
    strategy_hash = "d" * 64
    return {
        "observed_at": datetime.now(timezone.utc),
        "runtime_target": "LOCAL",
        "database_provider": "POSTGRES_LOCAL",
        "database_identity_verified": True,
        "persistence_durable": True,
        "lease_held": True,
        "kill_switch_active": False,
        "market_data_fresh": True,
        "account_snapshot_fresh": True,
        "reconciliation_status": "IN_SYNC",
        "source_hash": source_hash,
        "dependency_hash": dependency_hash,
        "migration_hash": migration_hash,
        "basket_id": "basket-1",
        "basket_headroom_usdc": Decimal("250"),
        "daily_loss_headroom_usdc": Decimal("5"),
        "current_gross_exposure_usdc": Decimal("0"),
        "current_basket_exposure_usdc": Decimal("0"),
        "collateral_usdc": Decimal("250"),
        "available_balance_usdc": Decimal("250"),
        "configured_leverage": Decimal("10"),
        "effective_leverage": Decimal("10"),
        "active_exposure_chains": 0,
        "same_active_basket": False,
        "is_first_risk_increasing_order": True,
        "live_research_pilot": {
            "approval_verified": True,
            "approval_role": "trading_admin",
            "binding_verified": True,
            "runtime_target": "LOCAL",
            "symbol": "ETHUSDC",
            "status": "ACTIVE",
            "management_mode": "QUICK",
            "source_hash": source_hash,
            "dependency_hash": dependency_hash,
            "migration_hash": migration_hash,
            "strategy_hash": strategy_hash,
            "risk_policy_hash": LOCAL_LIVE_PILOT_POLICY_SHA256,
            "campaign_expires_at": datetime.now(timezone.utc) + timedelta(days=1),
            "limits": {
                "max_position_notional_usdc": Decimal("50"),
                "max_order_notional_usdc": Decimal("50"),
                "max_total_exposure_usdc": Decimal("50"),
                "max_position_stop_risk_usdc": Decimal("2"),
                "campaign_drawdown_usdc": Decimal("5"),
                "max_leverage": Decimal("10"),
            },
            "drawdown_triggered": False,
            "current_position_notional_usdc": Decimal("0"),
            "current_total_exposure_usdc": Decimal("0"),
            "current_net_pnl_usdc": Decimal("0"),
            "peak_net_pnl_usdc": Decimal("0"),
            "configured_leverage": Decimal("10"),
            "effective_leverage": Decimal("0"),
            "active_exposure_chains": 0,
            "active_position_count": 0,
        },
    }


def make_cost_evidence(intent, *, observed_at=None, **overrides):
    monotonic_completed = time.monotonic()
    monotonic_started = monotonic_completed - 0.001
    expected_params = {
        "commission": {"symbol": "ETHUSDC"},
        "depth": {"symbol": "ETHUSDC", "limit": 1000},
        "funding": {"symbol": "ETHUSDC", "limit": 3},
        "funding_info": {},
        "leverage_brackets": {"symbol": "ETHUSDC"},
    }
    expected_signed = {
        "commission": True, "depth": False, "funding": False,
        "funding_info": False, "leverage_brackets": True,
    }
    endpoint_observation = {
        "route": "/fapi/test",
        "method": "GET",
        "started_at": datetime.now(timezone.utc) - timedelta(milliseconds=1),
        "completed_at": datetime.now(timezone.utc),
        "started_monotonic": monotonic_started,
        "duration_ms": 1.0,
        "completed_monotonic": monotonic_completed,
        "source_timestamp": None,
    }
    evidence = {
        "source": "BINANCE_FAPI_COMMISSION_FUNDING_DEPTH",
        "observed_at": observed_at or datetime.now(timezone.utc),
        "symbol": intent.symbol,
        "client_order_id": intent.client_order_id,
        "quantity": intent.quantity,
        "side": "BUY",
        "fees_upper_bound_usdc": Decimal("0.1"),
        "funding_upper_bound_usdc": Decimal("0.1"),
        "slippage_upper_bound_usdc": Decimal("0.1"),
        "request_observations": {
            name: {
                **endpoint_observation,
                "evidence_key": name,
                "route": route,
                "signed": expected_signed[name],
                "params": expected_params[name],
            }
            for name, route in (("commission", "/fapi/v1/commissionRate"), ("depth", "/fapi/v1/depth"), ("funding", "/fapi/v1/fundingRate"), ("funding_info", "/fapi/v1/fundingInfo"), ("leverage_brackets", "/fapi/v1/leverageBracket"))
        },
        "depth_levels_requested": 1000,
        "depth_bid_levels_received": 1,
        "depth_ask_levels_received": 1,
        "funding_interval_hours": Decimal("1"),
        "funding_events_assumed": 25,
        "cost_horizon_seconds": 86400,
        "cost_horizon_source": "LOCAL_LIVE_PILOT_POLICY",
        "runtime_target": "LOCAL",
        "venue": "BINANCE_MAINNET",
    }
    observation_overrides = overrides.pop("request_observations", None)
    if isinstance(observation_overrides, dict):
        for name, item in observation_overrides.items():
            if name in evidence["request_observations"]:
                evidence["request_observations"][name].update(item)
    evidence.update(overrides)
    return evidence


@pytest.mark.asyncio
async def test_local_mainnet_risk_is_blocked_without_durable_basket_context(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    result = await _local_mainnet_risk_gate(
        make_adapter(), make_intent(), entry_price=Decimal("100"), quantity=Decimal("0.4")
    )

    assert result.allowed is False
    assert "basket-risk context is unavailable" in result.reason


@pytest.mark.asyncio
async def test_pre_entry_gate_does_not_require_or_call_post_fill_protection(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    intent = make_intent().model_copy(update={"quantity": Decimal("0.15")})
    calls = {"context": 0, "costs": 0, "protection": 0}

    async def risk_context(_intent, _execution):
        calls["context"] += 1
        return make_risk_context()

    async def cost_evidence(_intent, _context):
        calls["costs"] += 1
        return make_cost_evidence(intent)

    async def protection_verifier(*_args, **_kwargs):
        calls["protection"] += 1
        raise AssertionError("post-fill verification cannot be a pre-entry condition")

    result = await _local_mainnet_risk_gate(
        make_adapter(
            get_local_mainnet_risk_context=risk_context,
            get_local_mainnet_cost_evidence=cost_evidence,
            verify_local_mainnet_protection=protection_verifier,
        ),
        intent,
        entry_price=Decimal("100"),
        quantity=Decimal("0.15"),
    )

    assert result is None
    assert calls == {"context": 1, "costs": 1, "protection": 0}


@pytest.mark.asyncio
async def test_pilot_uses_hashed_reward_risk_floor_without_inheriting_scale_up_floor(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    assert LOCAL_LIVE_PILOT_POLICY["min_reward_to_risk"] == "0.125"
    assert len(LOCAL_LIVE_PILOT_POLICY_SHA256) == 64
    intent = make_intent().model_copy(update={"quantity": Decimal("0.15"), "take_profit_price": Decimal("105")})

    async def risk_context(_intent, _execution):
        return make_risk_context()

    async def cost_evidence(_intent, _context):
        return make_cost_evidence(intent)

    accepted = await _local_mainnet_risk_gate(
        make_adapter(
            get_local_mainnet_risk_context=risk_context,
            get_local_mainnet_cost_evidence=cost_evidence,
        ),
        intent,
        entry_price=Decimal("100"),
        quantity=Decimal("0.15"),
    )
    assert accepted is None  # net reward / total risk is below the scale-up 2R floor.

    larger_risk = make_intent().model_copy(
        update={"quantity": Decimal("0.19"), "take_profit_price": Decimal("102.9")}
    )

    async def larger_cost_evidence(_intent, _context):
        return make_cost_evidence(larger_risk)

    blocked = await _local_mainnet_risk_gate(
        make_adapter(
            get_local_mainnet_risk_context=risk_context,
            get_local_mainnet_cost_evidence=larger_cost_evidence,
        ),
        larger_risk,
        entry_price=Decimal("100"),
        quantity=Decimal("0.19"),
    )
    assert blocked is not None and blocked.allowed is False
    assert "required 1:0.125 risk/reward floor" in blocked.reason


@pytest.mark.asyncio
async def test_pilot_caps_and_expiry_block_new_risk(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    intent = make_intent().model_copy(update={"quantity": Decimal("0.15")})

    async def evaluate(pilot_overrides):
        context = make_risk_context()
        context["live_research_pilot"].update(pilot_overrides)

        async def risk_context(_intent, _execution):
            return context

        async def cost_evidence(_intent, _context):
            return make_cost_evidence(intent)

        return await _local_mainnet_risk_gate(
            make_adapter(
                get_local_mainnet_risk_context=risk_context,
                get_local_mainnet_cost_evidence=cost_evidence,
            ),
            intent,
            entry_price=Decimal("100"),
            quantity=Decimal("0.15"),
        )

    over_exposure = await evaluate({"current_total_exposure_usdc": Decimal("45")})
    assert over_exposure is not None and "notional" in over_exposure.reason
    expired = await evaluate({"campaign_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    assert expired is not None and "expired" in expired.reason
    drawdown = await evaluate({"current_net_pnl_usdc": Decimal("-5")})
    assert drawdown is not None and "drawdown" in drawdown.reason
    headroom = await evaluate({"current_net_pnl_usdc": Decimal("-4.9")})
    assert headroom is not None and "drawdown" in headroom.reason
    already_open = await evaluate({"active_position_count": 1})
    assert already_open is not None and "active" in already_open.reason


@pytest.mark.asyncio
async def test_strategy_cost_estimates_never_count_as_exchange_cost_evidence(monkeypatch):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    intent = make_intent()

    async def risk_context(_intent, _execution):
        return {
            **make_risk_context(),
            "verified_fees_usdc": intent.estimated_fees_usdc,
            "verified_funding_usdc": intent.estimated_funding_usdc,
            "verified_slippage_usdc": intent.estimated_slippage_usdc,
            "cost_evidence_status": "NOT_AVAILABLE",
        }

    result = await _local_mainnet_risk_gate(
        make_adapter(get_local_mainnet_risk_context=risk_context),
        intent,
        entry_price=Decimal("100"),
        quantity=Decimal("0.4"),
    )

    assert result.allowed is False
    assert "commission/funding/depth cost evidence is unavailable" in result.reason


@pytest.mark.parametrize(
    "overrides",
    [
        {"stop_loss_price": Decimal("110")},
        {"take_profit_price": Decimal("95")},
        {"stop_loss_price": Decimal("90.01")},
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_unnormalized_pre_entry_bracket_is_rejected_before_context(
    monkeypatch, overrides
):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    calls = 0

    async def risk_context(_intent, _execution):
        nonlocal calls
        calls += 1
        return make_risk_context()

    intent = make_intent().model_copy(update=overrides)
    result = await _local_mainnet_risk_gate(
        make_adapter(get_local_mainnet_risk_context=risk_context),
        intent,
        entry_price=Decimal("100"),
        quantity=Decimal("0.4"),
    )

    assert result.allowed is False
    assert "pre-entry stop/target plan" in result.reason
    assert calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"observed_at": datetime.now(timezone.utc) - timedelta(seconds=6)},
        {"client_order_id": "different-entry"},
        {"quantity": Decimal("0.5")},
        {"slippage_upper_bound_usdc": Decimal("-0.01")},
        {
            "request_observations": {
                name: {"duration_ms": 5001}
                for name in ("commission", "depth", "funding", "funding_info", "leverage_brackets")
            }
        },
        {"request_observations": {"commission": {"route": "/fapi/v1/depth"}}},
        {"depth_bid_levels_received": True},
        {"depth_ask_levels_received": True},
        {"cost_horizon_seconds": 3600},
        {"funding_events_assumed": 24},
        {"runtime_target": "CLOUD_RUN"},
        {"venue": "BINANCE_TESTNET"},
    ],
)
@pytest.mark.asyncio
async def test_exchange_cost_evidence_must_be_fresh_complete_and_bound_to_intent(
    monkeypatch, overrides
):
    monkeypatch.setenv("LOCAL_ONLY", "true")
    monkeypatch.setenv("MAINNET_LIVE_APPROVED", "true")
    intent = make_intent()

    async def risk_context(_intent, _execution):
        return make_risk_context()

    async def cost_evidence(_intent, _context):
        return make_cost_evidence(intent, **overrides)

    result = await _local_mainnet_risk_gate(
        make_adapter(
            get_local_mainnet_risk_context=risk_context,
            get_local_mainnet_cost_evidence=cost_evidence,
        ),
        intent,
        entry_price=Decimal("100"),
        quantity=Decimal("0.4"),
    )

    assert result.allowed is False
    assert "cost evidence is stale, incomplete, or mismatched" in result.reason
