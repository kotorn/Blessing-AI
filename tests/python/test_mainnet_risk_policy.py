from decimal import Decimal

import pytest
import yaml

from apps.learning_engine.capital_scaling import DynamicCapitalAllocator
from apps.trading_worker.config.risk_policy import (
    DrawdownTier,
    RiskPolicyError,
    load_mainnet_risk_policy,
)
from apps.trading_worker.venues.binance.models import TestnetSafetyLimits
from domain.enums import RiskState
from domain.wealth_metrics import calculate_wealth_metrics


def _clear_mainnet_risk_env(monkeypatch):
    for name in (
        "MAINNET_MAX_COLLATERAL",
        "MAINNET_MAX_DAILY_LOSS",
        "MAINNET_MAX_LEVERAGE",
        "MAX_DRAWDOWN_PCT",
        "RISK_POLICY_PATH",
    ):
        monkeypatch.delenv(name, raising=False)


def test_mainnet_policy_matches_live_contract(monkeypatch):
    _clear_mainnet_risk_env(monkeypatch)
    policy = load_mainnet_risk_policy()

    assert policy.baseline_capital == Decimal("250")
    assert policy.max_daily_loss == Decimal("25")
    assert policy.max_leverage == Decimal("2.0")
    assert policy.caution_pct == Decimal("5.0")
    assert policy.no_new_grid_pct == Decimal("10.0")
    assert policy.recovery_only_pct == Decimal("15.0")
    assert policy.emergency_stop_pct == Decimal("20.0")


@pytest.mark.parametrize(
    ("drawdown", "tier", "risk_state", "capital_scale"),
    [
        ("4.999", DrawdownTier.NORMAL, RiskState.NORMAL, "1.0"),
        ("5", DrawdownTier.CAUTION, RiskState.CAUTION, "0.50"),
        ("9.999", DrawdownTier.CAUTION, RiskState.CAUTION, "0.50"),
        ("10", DrawdownTier.NO_NEW_GRID, RiskState.NO_NEW_RISK, "0.0"),
        ("14.999", DrawdownTier.NO_NEW_GRID, RiskState.NO_NEW_RISK, "0.0"),
        ("15", DrawdownTier.RECOVERY_ONLY, RiskState.RECOVERY_ONLY, "0.0"),
        ("19.999", DrawdownTier.RECOVERY_ONLY, RiskState.RECOVERY_ONLY, "0.0"),
        ("20", DrawdownTier.EMERGENCY, RiskState.EMERGENCY, "0.0"),
    ],
)
def test_drawdown_tier_boundaries(monkeypatch, drawdown, tier, risk_state, capital_scale):
    _clear_mainnet_risk_env(monkeypatch)
    policy = load_mainnet_risk_policy()
    value = Decimal(drawdown)

    assert policy.tier_for_drawdown(value) is tier
    assert policy.risk_state_for_drawdown(value) is risk_state
    assert policy.capital_scale_for_drawdown(value) == Decimal(capital_scale)


def test_drawdown_is_measured_from_starting_capital(monkeypatch):
    _clear_mainnet_risk_env(monkeypatch)
    policy = load_mainnet_risk_policy()

    assert policy.drawdown_pct(Decimal("237.5")) == Decimal("5.00")
    assert policy.drawdown_pct(Decimal("225")) == Decimal("10.0")
    assert policy.drawdown_pct(Decimal("212.5")) == Decimal("15.00")
    assert policy.drawdown_pct(Decimal("200")) == Decimal("20.0")
    assert policy.drawdown_pct(Decimal("275")) == Decimal("0")


def test_missing_policy_fails_closed(tmp_path, monkeypatch):
    _clear_mainnet_risk_env(monkeypatch)
    with pytest.raises(RiskPolicyError, match="missing"):
        load_mainnet_risk_policy(tmp_path / "missing.yaml")


def test_invalid_tier_order_fails_closed(tmp_path, monkeypatch):
    _clear_mainnet_risk_env(monkeypatch)
    policy = load_mainnet_risk_policy()
    payload = yaml.safe_load(policy.source_path.read_text(encoding="utf-8"))
    payload["risk_governor"]["drawdown_escalation"]["emergency_stop_pct"] = 14
    invalid_path = tmp_path / "invalid-risk.yaml"
    invalid_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises(RiskPolicyError, match="validation failed"):
        load_mainnet_risk_policy(invalid_path)


def test_drawdown_env_must_match_policy(monkeypatch):
    _clear_mainnet_risk_env(monkeypatch)
    monkeypatch.setenv("MAX_DRAWDOWN_PCT", "6")
    with pytest.raises(RiskPolicyError, match="does not match"):
        load_mainnet_risk_policy()


def test_mainnet_env_cannot_widen_leverage_or_daily_loss(monkeypatch):
    _clear_mainnet_risk_env(monkeypatch)
    monkeypatch.setenv("MAINNET_MAX_LEVERAGE", "10")
    monkeypatch.setenv("MAINNET_MAX_DAILY_LOSS", "100")

    limits = TestnetSafetyLimits.from_environment("MAINNET")
    assert limits.max_leverage == Decimal("2")
    assert limits.max_daily_loss == Decimal("25")


def test_capital_scaling_matches_drawdown_contract():
    allocator = DynamicCapitalAllocator()
    metrics = calculate_wealth_metrics([])

    before_caution = allocator.evaluate_allocation("s", metrics, Decimal("4.999"))
    caution = allocator.evaluate_allocation("s", metrics, Decimal("5"))
    no_new_grid = allocator.evaluate_allocation("s", metrics, Decimal("10"))
    recovery_only = allocator.evaluate_allocation("s", metrics, Decimal("15"))
    emergency = allocator.evaluate_allocation("s", metrics, Decimal("20"))

    assert before_caution.drawdown_factor == Decimal("1.0")
    assert caution.drawdown_factor == Decimal("0.50")
    assert no_new_grid.drawdown_factor == Decimal("0.0")
    assert recovery_only.drawdown_factor == Decimal("0.0")
    assert emergency.drawdown_factor == Decimal("0.0")
