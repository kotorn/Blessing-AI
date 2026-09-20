import copy
import math
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from apps.trading_worker.config.research_config import (
    DEFAULT_CONFIG_DIR,
    config_fingerprint,
    load_research_config,
)

REPO_CONFIG_DIR = DEFAULT_CONFIG_DIR


def test_loads_the_real_config_tree():
    config = load_research_config(REPO_CONFIG_DIR)
    assert config.risk.risk_governor.leverage.max_portfolio_leverage == Decimal("2.0")
    assert config.strategy.grid.max_levels == 5
    assert len(config.instruments) >= 1
    symbols = {instrument.symbol for instrument in config.instruments}
    assert "BTCUSDT" in symbols


def test_spacing_multipliers_allow_null():
    config = load_research_config(REPO_CONFIG_DIR)
    assert config.strategy.spacing_atr_multipliers["R4_BREAKOUT"] is None
    assert config.strategy.spacing_atr_multipliers["R1_RANGE"] == Decimal("0.90")


def test_config_fingerprint_is_stable_and_sensitive():
    config_a = load_research_config(REPO_CONFIG_DIR)
    config_b = load_research_config(REPO_CONFIG_DIR)
    fp_a = config_fingerprint(config_a)
    fp_b = config_fingerprint(config_b)
    assert fp_a == fp_b
    assert len(fp_a) == 64

    mutated = config_a.model_copy(
        update={
            "risk": config_a.risk.model_copy(
                update={
                    "risk_governor": config_a.risk.risk_governor.model_copy(
                        update={
                            "leverage": config_a.risk.risk_governor.leverage.model_copy(
                                update={"max_portfolio_leverage": Decimal("3.0")}
                            )
                        }
                    )
                }
            )
        }
    )
    assert config_fingerprint(mutated) != fp_a


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")


def _copy_config_tree(tmp_path: Path) -> Path:
    dest = tmp_path / "config"
    for relative in (
        "risk/portfolio_risk.yaml",
        "strategy/blessing_default.yaml",
    ):
        src = REPO_CONFIG_DIR / relative
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    instruments_src = REPO_CONFIG_DIR / "instruments"
    for src in instruments_src.glob("*.yaml"):
        target = dest / "instruments" / src.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def test_rejects_unknown_key_in_risk_config(tmp_path):
    config_dir = _copy_config_tree(tmp_path)
    payload = yaml.safe_load((config_dir / "risk" / "portfolio_risk.yaml").read_text(encoding="utf-8"))
    payload["risk_governor"]["unexpected_field"] = "oops"
    _write_yaml(config_dir / "risk" / "portfolio_risk.yaml", payload)
    with pytest.raises(ValidationError):
        load_research_config(config_dir)


def test_rejects_unknown_key_in_strategy_config(tmp_path):
    config_dir = _copy_config_tree(tmp_path)
    payload = yaml.safe_load((config_dir / "strategy" / "blessing_default.yaml").read_text(encoding="utf-8"))
    payload["unexpected_field"] = "oops"
    _write_yaml(config_dir / "strategy" / "blessing_default.yaml", payload)
    with pytest.raises(ValidationError):
        load_research_config(config_dir)


def test_rejects_unknown_key_in_instrument_config(tmp_path):
    config_dir = _copy_config_tree(tmp_path)
    instrument_path = next((config_dir / "instruments").glob("*.yaml"))
    payload = yaml.safe_load(instrument_path.read_text(encoding="utf-8"))
    payload["unexpected_field"] = "oops"
    _write_yaml(instrument_path, payload)
    with pytest.raises(ValidationError):
        load_research_config(config_dir)


def test_rejects_missing_required_field(tmp_path):
    config_dir = _copy_config_tree(tmp_path)
    payload = yaml.safe_load((config_dir / "risk" / "portfolio_risk.yaml").read_text(encoding="utf-8"))
    del payload["risk_governor"]["margin"]["stress_threshold_pct"]
    _write_yaml(config_dir / "risk" / "portfolio_risk.yaml", payload)
    with pytest.raises(ValidationError):
        load_research_config(config_dir)


def test_rejects_non_finite_float(tmp_path):
    config_dir = _copy_config_tree(tmp_path)
    payload = yaml.safe_load((config_dir / "risk" / "portfolio_risk.yaml").read_text(encoding="utf-8"))
    payload["risk_governor"]["leverage"]["max_portfolio_leverage"] = math.nan
    _write_yaml(config_dir / "risk" / "portfolio_risk.yaml", payload)
    with pytest.raises(ValidationError):
        load_research_config(config_dir)


def test_rejects_infinite_float(tmp_path):
    config_dir = _copy_config_tree(tmp_path)
    payload = yaml.safe_load((config_dir / "risk" / "portfolio_risk.yaml").read_text(encoding="utf-8"))
    payload["risk_governor"]["leverage"]["max_portfolio_leverage"] = math.inf
    _write_yaml(config_dir / "risk" / "portfolio_risk.yaml", payload)
    with pytest.raises(ValidationError):
        load_research_config(config_dir)


def test_rejects_volume_progression_length_mismatch(tmp_path):
    config_dir = _copy_config_tree(tmp_path)
    payload = yaml.safe_load((config_dir / "strategy" / "blessing_default.yaml").read_text(encoding="utf-8"))
    payload["grid"]["volume_progression"] = [1.0, 1.0]
    _write_yaml(config_dir / "strategy" / "blessing_default.yaml", payload)
    with pytest.raises(ValidationError):
        load_research_config(config_dir)


def test_rejects_out_of_order_drawdown_tiers(tmp_path):
    config_dir = _copy_config_tree(tmp_path)
    payload = yaml.safe_load((config_dir / "risk" / "portfolio_risk.yaml").read_text(encoding="utf-8"))
    payload["risk_governor"]["drawdown_escalation"]["emergency_stop_pct"] = 1.0
    _write_yaml(config_dir / "risk" / "portfolio_risk.yaml", payload)
    with pytest.raises(ValidationError):
        load_research_config(config_dir)
