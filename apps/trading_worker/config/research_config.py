"""Typed, validated loader for the research/backtest YAML config tree.

Reads ``config/risk/portfolio_risk.yaml``, ``config/strategy/blessing_default.yaml``,
and ``config/instruments/*.yaml``, validates them strictly (unknown keys, missing
required fields, and non-finite numeric values are all rejected), and produces a
content-addressed fingerprint for reproducibility.

This module is intentionally standalone: nothing in the production execution path
(``apps.trading_worker.main``, ``apps.trading_worker.engines``,
``apps.trading_worker.venues``) imports it, and ``RiskGovernor``'s hardcoded
defaults are not wired to this loader. The hardcoded defaults do not currently
match these YAML values (e.g. ``RiskGovernor.max_drawdown_pct=6.0`` vs.
``emergency_stop_pct=8.0`` here) — wiring this loader into production is a
separate, explicitly-reviewed change, not a side effect of adding the loader.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Optional

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


def _reject_non_finite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("value must be finite (no NaN/Infinity)")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("value must be finite (no NaN/Infinity)")
    return value


FiniteDecimal = Annotated[Decimal, BeforeValidator(_reject_non_finite)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# config/risk/portfolio_risk.yaml
# ---------------------------------------------------------------------------


class LeverageLimits(_StrictModel):
    max_portfolio_leverage: FiniteDecimal = Field(gt=0)
    btc_max_effective_leverage: FiniteDecimal = Field(gt=0)
    eth_max_effective_leverage: FiniteDecimal = Field(gt=0)
    sol_max_effective_leverage: FiniteDecimal = Field(gt=0)
    bnb_max_effective_leverage: FiniteDecimal = Field(gt=0)


class MarginLimits(_StrictModel):
    normal_target_pct: FiniteDecimal = Field(gt=0, le=100)
    stress_threshold_pct: FiniteDecimal = Field(gt=0, le=100)
    extreme_hard_limit_pct: FiniteDecimal = Field(gt=0, le=100)
    min_liquidation_distance_pct: FiniteDecimal = Field(gt=0, le=100)

    @model_validator(mode="after")
    def _check_ordering(self) -> "MarginLimits":
        if not (self.normal_target_pct < self.stress_threshold_pct < self.extreme_hard_limit_pct):
            raise ValueError(
                "margin thresholds must satisfy "
                "normal_target_pct < stress_threshold_pct < extreme_hard_limit_pct"
            )
        return self


class DrawdownEscalation(_StrictModel):
    caution_pct: FiniteDecimal = Field(gt=0, le=100)
    no_new_grid_pct: FiniteDecimal = Field(gt=0, le=100)
    recovery_only_pct: FiniteDecimal = Field(gt=0, le=100)
    emergency_stop_pct: FiniteDecimal = Field(gt=0, le=100)

    @model_validator(mode="after")
    def _check_ordering(self) -> "DrawdownEscalation":
        tiers = (self.caution_pct, self.no_new_grid_pct, self.recovery_only_pct, self.emergency_stop_pct)
        if not (tiers[0] < tiers[1] < tiers[2] < tiers[3]):
            raise ValueError(
                "drawdown escalation tiers must be strictly increasing: "
                "caution_pct < no_new_grid_pct < recovery_only_pct < emergency_stop_pct"
            )
        return self


class FailClosedConfig(_StrictModel):
    max_market_data_staleness_sec: FiniteDecimal = Field(gt=0)
    max_private_ws_heartbeat_sec: FiniteDecimal = Field(gt=0)
    require_reconciliation_on_reconnect: bool
    block_entry_on_db_disconnect: bool


class CorrelationLimits(_StrictModel):
    max_btc_eth_rolling_corr: FiniteDecimal = Field(ge=-1, le=1)
    max_concurrent_crypto_beta_exposure_pct: FiniteDecimal = Field(gt=0, le=100)


class RiskGovernorSection(_StrictModel):
    version: str = Field(min_length=1)
    kill_switch_active: bool
    leverage: LeverageLimits
    margin: MarginLimits
    drawdown_escalation: DrawdownEscalation
    fail_closed: FailClosedConfig
    correlation: CorrelationLimits


class RiskGovernorConfig(_StrictModel):
    risk_governor: RiskGovernorSection


# ---------------------------------------------------------------------------
# config/strategy/blessing_default.yaml
# ---------------------------------------------------------------------------


class GridDefaults(_StrictModel):
    max_levels: int = Field(gt=0)
    base_unit_size_usd: FiniteDecimal = Field(gt=0)
    volume_progression: tuple[FiniteDecimal, ...]
    level_spacing_multipliers: tuple[FiniteDecimal, ...]
    take_profit_target_bps: FiniteDecimal = Field(gt=0)
    trailing_tp_enabled: bool
    trailing_tp_callback_bps: FiniteDecimal = Field(gt=0)

    @model_validator(mode="after")
    def _check_progression_lengths(self) -> "GridDefaults":
        if len(self.volume_progression) != self.max_levels:
            raise ValueError("volume_progression length must equal max_levels")
        if len(self.level_spacing_multipliers) != self.max_levels:
            raise ValueError("level_spacing_multipliers length must equal max_levels")
        if any(v <= 0 for v in self.volume_progression):
            raise ValueError("volume_progression entries must be positive")
        if any(v <= 0 for v in self.level_spacing_multipliers):
            raise ValueError("level_spacing_multipliers entries must be positive")
        return self


class SafetyScoreThresholds(_StrictModel):
    grid_allowed_min: FiniteDecimal = Field(ge=0, le=100)
    reduced_risk_min: FiniteDecimal = Field(ge=0, le=100)
    conservative_min: FiniteDecimal = Field(ge=0, le=100)
    recovery_only_min: FiniteDecimal = Field(ge=0, le=100)
    no_new_grid_below: FiniteDecimal = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _check_ordering(self) -> "SafetyScoreThresholds":
        if not (self.grid_allowed_min > self.reduced_risk_min > self.conservative_min > self.recovery_only_min):
            raise ValueError(
                "safety score thresholds must be strictly decreasing: "
                "grid_allowed_min > reduced_risk_min > conservative_min > recovery_only_min"
            )
        if self.no_new_grid_below > self.recovery_only_min:
            raise ValueError("no_new_grid_below must not exceed recovery_only_min")
        return self


class FundingFilter(_StrictModel):
    max_adverse_funding_rate_8h: FiniteDecimal = Field(ge=0)
    prefer_favorable_funding: bool


class BasisFilter(_StrictModel):
    max_zscore_allowed: FiniteDecimal = Field(gt=0)


class StrategyDefaultsConfig(_StrictModel):
    strategy_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    grid: GridDefaults
    spacing_atr_multipliers: dict[str, Optional[FiniteDecimal]]
    safety_score_thresholds: SafetyScoreThresholds
    funding_filter: FundingFilter
    basis_filter: BasisFilter

    @model_validator(mode="after")
    def _check_spacing_multipliers(self) -> "StrategyDefaultsConfig":
        for regime, multiplier in self.spacing_atr_multipliers.items():
            if multiplier is not None and multiplier <= 0:
                raise ValueError(f"spacing_atr_multipliers[{regime}] must be positive or null")
        return self


# ---------------------------------------------------------------------------
# config/instruments/*.yaml
# ---------------------------------------------------------------------------


class InstrumentConfig(_StrictModel):
    symbol: str = Field(min_length=1)
    venue: str = Field(min_length=1)
    # Deliberately `str`, not `domain.enums.MarketType`: the instrument YAML
    # files use "USDM_PERP" while the domain enum only defines
    # "USDM_FUTURES". This is a known, documented discrepancy, not silently
    # normalized here.
    market_type: str = Field(min_length=1)
    base_asset: str = Field(min_length=1)
    quote_asset: str = Field(min_length=1)
    price_precision: int = Field(ge=0)
    quantity_precision: int = Field(ge=0)
    tick_size: FiniteDecimal = Field(gt=0)
    step_size: FiniteDecimal = Field(gt=0)
    min_notional: FiniteDecimal = Field(gt=0)
    max_effective_leverage: FiniteDecimal = Field(gt=0)
    base_unit_size_usd: FiniteDecimal = Field(gt=0)
    is_active: bool
    primary_research: bool


# ---------------------------------------------------------------------------
# Umbrella config + loader
# ---------------------------------------------------------------------------


class BlessingResearchConfig(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    risk: RiskGovernorConfig
    strategy: StrategyDefaultsConfig
    instruments: tuple[InstrumentConfig, ...]


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"expected a YAML mapping at {path}, got {type(payload).__name__}")
    return payload


def load_research_config(config_dir: Optional[Path] = None) -> BlessingResearchConfig:
    """Load and strictly validate the research config tree rooted at ``config_dir``.

    Defaults to the repository's top-level ``config/`` directory. Raises
    ``pydantic.ValidationError`` on any unknown key, missing required field,
    invalid value, or non-finite number.
    """
    base = Path(config_dir) if config_dir is not None else DEFAULT_CONFIG_DIR

    risk_payload = _load_yaml_mapping(base / "risk" / "portfolio_risk.yaml")
    strategy_payload = _load_yaml_mapping(base / "strategy" / "blessing_default.yaml")

    instruments_dir = base / "instruments"
    instrument_paths = sorted(instruments_dir.glob("*.yaml"))
    if not instrument_paths:
        raise ValueError(f"no instrument config files found under {instruments_dir}")
    instruments = tuple(
        InstrumentConfig.model_validate(_load_yaml_mapping(path)) for path in instrument_paths
    )

    return BlessingResearchConfig(
        risk=RiskGovernorConfig.model_validate(risk_payload),
        strategy=StrategyDefaultsConfig.model_validate(strategy_payload),
        instruments=instruments,
    )


def config_fingerprint(config: BlessingResearchConfig) -> str:
    """Deterministic SHA-256 fingerprint of a validated config's canonical form."""
    payload = json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
