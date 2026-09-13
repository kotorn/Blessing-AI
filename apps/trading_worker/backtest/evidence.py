"""Evidence-only evaluation for net-economic walk-forward research.

The evaluator consumes externally generated, timestamped trades and explicit
cost inputs.  It never generates signals, optimizes parameters, contacts an
exchange, or authorizes execution.  A passing research-quality result is still
not Testnet or Live evidence.
"""

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .economic import (
    BacktestTrade,
    EconomicBacktestResult,
    EconomicCostModel,
    WalkForwardConfig,
    WalkForwardFold,
    evaluate_trades,
    walk_forward_splits,
)


def _finite_decimal(value: object, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be Decimal-compatible") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return parsed


class ParameterVariantResult(BaseModel):
    """Precomputed OOS result for one neighboring parameter variant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variant_id: str = Field(min_length=1)
    oos_net_return_pct: Decimal
    oos_average_net_pnl: Decimal
    max_drawdown_pct: Decimal
    oos_trade_count: int = Field(gt=0)
    adjacent_to_baseline: bool = True

    @field_validator("oos_net_return_pct", "oos_average_net_pnl", "max_drawdown_pct")
    @classmethod
    def require_finite_metrics(cls, value: Decimal, info) -> Decimal:
        parsed = _finite_decimal(value, info.field_name)
        if info.field_name == "max_drawdown_pct" and parsed < 0:
            raise ValueError("max_drawdown_pct must be non-negative")
        return parsed


class WalkForwardFoldEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fold_index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    oos_trade_count: int
    oos_net_pnl: Decimal
    oos_net_return_pct: Decimal
    positive_net_expectancy: bool


class WalkForwardEvidence(BaseModel):
    """Auditable OOS summary with an explicit non-launch status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: str
    symbol: str
    data_source: str
    initial_capital: Decimal
    fold_count: int
    oos_trade_count: int
    oos_net_pnl: Decimal
    oos_net_return_pct: Decimal
    oos_average_net_pnl: Decimal
    positive_oos_expectancy: bool
    positive_fold_count: int
    positive_fold_ratio: Decimal
    max_oos_drawdown_pct: Decimal
    observed_regimes: list[str]
    unknown_oos_trade_count: int
    required_regimes: list[str]
    regime_coverage_passed: bool
    parameter_plateau_passed: bool
    research_quality_passed: bool
    folds: list[WalkForwardFoldEvidence]
    parameter_variants: list[ParameterVariantResult]
    evidence_status: Literal["RESEARCH_ONLY"] = "RESEARCH_ONLY"
    launch_eligible: Literal[False] = False


def parameter_plateau_passes(
    variants: Sequence[ParameterVariantResult],
    *,
    min_variants: int = 3,
    min_positive_ratio: Decimal = Decimal("0.67"),
    max_return_spread_pct: Decimal = Decimal("5"),
) -> bool:
    """Require several adjacent OOS variants with similar positive results."""

    if min_variants < 1:
        raise ValueError("min_variants must be positive")
    positive_ratio = _finite_decimal(min_positive_ratio, "min_positive_ratio")
    spread_limit = _finite_decimal(max_return_spread_pct, "max_return_spread_pct")
    if not Decimal("0") < positive_ratio <= Decimal("1"):
        raise ValueError("min_positive_ratio must be in (0, 1]")
    if spread_limit < 0:
        raise ValueError("max_return_spread_pct must be non-negative")

    adjacent = [variant for variant in variants if variant.adjacent_to_baseline]
    if len(adjacent) < min_variants:
        return False
    positive = [variant for variant in adjacent if variant.oos_average_net_pnl > 0]
    if Decimal(len(positive)) / Decimal(len(adjacent)) < positive_ratio:
        return False
    returns = [variant.oos_net_return_pct for variant in adjacent]
    return max(returns) - min(returns) <= spread_limit


def evaluate_walk_forward_evidence(
    trades: Sequence[BacktestTrade],
    *,
    config: WalkForwardConfig,
    initial_capital: Decimal,
    cost_model: EconomicCostModel,
    required_regimes: Sequence[str],
    parameter_variants: Sequence[ParameterVariantResult],
    min_folds: int = 3,
    min_positive_fold_ratio: Decimal = Decimal("0.67"),
    min_plateau_variants: int = 3,
    max_plateau_return_spread_pct: Decimal = Decimal("5"),
) -> WalkForwardEvidence:
    """Evaluate only chronological test windows from externally simulated trades."""

    if min_folds < 1:
        raise ValueError("min_folds must be positive")
    positive_ratio_limit = _finite_decimal(
        min_positive_fold_ratio, "min_positive_fold_ratio"
    )
    if not Decimal("0") < positive_ratio_limit <= Decimal("1"):
        raise ValueError("min_positive_fold_ratio must be in (0, 1]")
    normalized_regimes = list(dict.fromkeys(str(regime).strip().upper() for regime in required_regimes))
    if not normalized_regimes or any(not regime for regime in normalized_regimes):
        raise ValueError("required_regimes must contain at least one named regime")
    if not trades:
        raise ValueError("at least one research trade is required")

    strategy_ids = {trade.strategy_id for trade in trades}
    symbols = {trade.symbol for trade in trades}
    sources = {trade.data_source for trade in trades}
    if len(strategy_ids) != 1 or len(symbols) != 1 or len(sources) != 1:
        raise ValueError("walk-forward evidence requires one strategy, symbol, and data source")
    strategy_id = next(iter(strategy_ids))
    symbol = next(iter(symbols))
    folds: list[WalkForwardFold] = walk_forward_splits(trades, config)
    if len(folds) < min_folds:
        raise ValueError(f"at least {min_folds} walk-forward folds are required")

    oos_trades: list[BacktestTrade] = []
    fold_evidence: list[WalkForwardFoldEvidence] = []
    for fold in folds:
        fold_trades = list(trades[fold.test_start : fold.test_end])
        if not fold_trades:
            raise ValueError(f"walk-forward fold {fold.fold_index} has no OOS trades")
        fold_result: EconomicBacktestResult = evaluate_trades(
            fold_trades,
            initial_capital=initial_capital,
            cost_model=cost_model,
        )
        oos_trades.extend(fold_trades)
        fold_evidence.append(
            WalkForwardFoldEvidence(
                fold_index=fold.fold_index,
                train_start=fold.train_start,
                train_end=fold.train_end,
                test_start=fold.test_start,
                test_end=fold.test_end,
                oos_trade_count=fold_result.trade_count,
                oos_net_pnl=fold_result.net_pnl,
                oos_net_return_pct=fold_result.net_return_pct,
                positive_net_expectancy=fold_result.positive_net_expectancy,
            )
        )

    oos_result = evaluate_trades(
        oos_trades,
        initial_capital=initial_capital,
        cost_model=cost_model,
    )
    positive_fold_count = sum(
        1 for fold in fold_evidence if fold.positive_net_expectancy
    )
    positive_fold_ratio = Decimal(positive_fold_count) / Decimal(len(fold_evidence))
    observed_regimes = sorted(
        {trade.regime for trade in oos_trades if trade.regime != "UNKNOWN"}
    )
    unknown_oos_trade_count = sum(
        1 for trade in oos_trades if trade.regime == "UNKNOWN"
    )
    regime_coverage_passed = set(normalized_regimes).issubset(observed_regimes)
    plateau_passed = parameter_plateau_passes(
        parameter_variants,
        min_variants=min_plateau_variants,
        max_return_spread_pct=max_plateau_return_spread_pct,
    )
    research_quality_passed = bool(
        oos_result.positive_net_expectancy
        and positive_fold_ratio >= positive_ratio_limit
        and unknown_oos_trade_count == 0
        and regime_coverage_passed
        and plateau_passed
    )

    return WalkForwardEvidence(
        strategy_id=strategy_id,
        symbol=symbol,
        data_source=oos_result.data_source,
        initial_capital=oos_result.initial_capital,
        fold_count=len(fold_evidence),
        oos_trade_count=oos_result.trade_count,
        oos_net_pnl=oos_result.net_pnl,
        oos_net_return_pct=oos_result.net_return_pct,
        oos_average_net_pnl=oos_result.average_net_pnl,
        positive_oos_expectancy=oos_result.positive_net_expectancy,
        positive_fold_count=positive_fold_count,
        positive_fold_ratio=positive_fold_ratio,
        max_oos_drawdown_pct=oos_result.max_drawdown_pct,
        observed_regimes=observed_regimes,
        unknown_oos_trade_count=unknown_oos_trade_count,
        required_regimes=normalized_regimes,
        regime_coverage_passed=regime_coverage_passed,
        parameter_plateau_passed=plateau_passed,
        research_quality_passed=research_quality_passed,
        folds=fold_evidence,
        parameter_variants=list(parameter_variants),
    )
