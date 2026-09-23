"""Authoritative runtime risk policy for LIVE Binance Mainnet execution.

The policy combines the strictly validated portfolio risk YAML with the
Mainnet safety envelope. Configuration errors are fatal to policy loading so a
LIVE worker can fail closed rather than silently falling back to looser values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path

import yaml
from pydantic import ValidationError

from apps.trading_worker.config.research_config import (
    DEFAULT_CONFIG_DIR,
    RiskGovernorConfig,
)
from apps.trading_worker.venues.binance.models import TestnetSafetyLimits
from domain.enums import RiskState


DEFAULT_RISK_POLICY_PATH = DEFAULT_CONFIG_DIR / "risk" / "portfolio_risk.yaml"


class RiskPolicyError(RuntimeError):
    """Raised when the runtime risk policy cannot be trusted."""


class DrawdownTier(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    NO_NEW_GRID = "NO_NEW_GRID"
    RECOVERY_ONLY = "RECOVERY_ONLY"
    EMERGENCY = "EMERGENCY"


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    version: str
    source_path: Path
    baseline_capital: Decimal
    max_daily_loss: Decimal
    max_leverage: Decimal
    max_margin_utilization_pct: Decimal
    caution_pct: Decimal
    no_new_grid_pct: Decimal
    recovery_only_pct: Decimal
    emergency_stop_pct: Decimal

    def drawdown_pct(self, equity: Decimal) -> Decimal:
        """Return drawdown from starting-capital baseline, never session peak."""

        equity = Decimal(str(equity))
        if not equity.is_finite() or equity < 0:
            raise RiskPolicyError("equity must be a finite non-negative Decimal")
        if self.baseline_capital <= 0:
            raise RiskPolicyError("baseline_capital must be positive")
        drawdown = ((self.baseline_capital - equity) / self.baseline_capital) * Decimal("100")
        return max(Decimal("0"), drawdown)

    def tier_for_drawdown(self, drawdown_pct: Decimal) -> DrawdownTier:
        value = Decimal(str(drawdown_pct))
        if not value.is_finite() or value < 0:
            raise RiskPolicyError("drawdown_pct must be finite and non-negative")
        if value >= self.emergency_stop_pct:
            return DrawdownTier.EMERGENCY
        if value >= self.recovery_only_pct:
            return DrawdownTier.RECOVERY_ONLY
        if value >= self.no_new_grid_pct:
            return DrawdownTier.NO_NEW_GRID
        if value >= self.caution_pct:
            return DrawdownTier.CAUTION
        return DrawdownTier.NORMAL

    def risk_state_for_drawdown(self, drawdown_pct: Decimal) -> RiskState:
        tier = self.tier_for_drawdown(drawdown_pct)
        return {
            DrawdownTier.NORMAL: RiskState.NORMAL,
            DrawdownTier.CAUTION: RiskState.CAUTION,
            DrawdownTier.NO_NEW_GRID: RiskState.NO_NEW_RISK,
            DrawdownTier.RECOVERY_ONLY: RiskState.RECOVERY_ONLY,
            DrawdownTier.EMERGENCY: RiskState.EMERGENCY,
        }[tier]

    def capital_scale_for_drawdown(self, drawdown_pct: Decimal) -> Decimal:
        """Scale risk-increasing capital consistently with the tier contract."""

        tier = self.tier_for_drawdown(drawdown_pct)
        if tier is DrawdownTier.NORMAL:
            return Decimal("1.0")
        if tier is DrawdownTier.CAUTION:
            return Decimal("0.50")
        return Decimal("0.0")


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise RiskPolicyError(f"risk policy file is missing: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        raise RiskPolicyError(f"cannot read risk policy: {type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise RiskPolicyError("risk policy root must be a YAML mapping")
    return payload


def _validate_drawdown_env(expected: Decimal) -> None:
    raw = os.getenv("MAX_DRAWDOWN_PCT")
    if raw is None or not raw.strip():
        return
    try:
        observed = Decimal(raw.strip())
    except (InvalidOperation, ValueError) as exc:
        raise RiskPolicyError("MAX_DRAWDOWN_PCT is not a valid decimal") from exc
    if not observed.is_finite() or observed != expected:
        raise RiskPolicyError(
            f"MAX_DRAWDOWN_PCT={raw!r} does not match policy emergency_stop_pct={expected}"
        )


def load_mainnet_risk_policy(path: Path | str | None = None) -> RiskPolicy:
    """Load the Mainnet runtime policy and fail closed on any inconsistency."""

    policy_path = Path(path) if path is not None else Path(
        os.getenv("RISK_POLICY_PATH", str(DEFAULT_RISK_POLICY_PATH))
    )
    payload = _read_yaml(policy_path)
    try:
        section = RiskGovernorConfig.model_validate(payload).risk_governor
    except ValidationError as exc:
        raise RiskPolicyError(f"risk policy validation failed: {exc}") from exc

    mainnet_limits = TestnetSafetyLimits.from_environment("MAINNET")
    _validate_drawdown_env(section.drawdown_escalation.emergency_stop_pct)

    return RiskPolicy(
        version=section.version,
        source_path=policy_path.resolve(),
        baseline_capital=mainnet_limits.max_collateral,
        max_daily_loss=mainnet_limits.max_daily_loss,
        max_leverage=min(
            section.leverage.max_portfolio_leverage,
            mainnet_limits.max_leverage,
        ),
        max_margin_utilization_pct=section.margin.extreme_hard_limit_pct,
        caution_pct=section.drawdown_escalation.caution_pct,
        no_new_grid_pct=section.drawdown_escalation.no_new_grid_pct,
        recovery_only_pct=section.drawdown_escalation.recovery_only_pct,
        emergency_stop_pct=section.drawdown_escalation.emergency_stop_pct,
    )
