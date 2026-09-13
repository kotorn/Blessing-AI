"""Research-only net-economic accounting and causal walk-forward splits.

This module deliberately has no exchange client and no execution side effects.
It accepts already-generated research trades, applies explicit cost inputs, and
keeps out-of-sample partitioning separate from the execution worker.  A
positive sample expectancy here is not launch evidence by itself.
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RESEARCH_DATA_SOURCES = frozenset(
    {
        "BINANCE_PUBLIC_MAINNET_READ_ONLY",
        "BINANCE_PUBLIC_TESTNET_READ_ONLY",
    }
)


def _finite_decimal(value: Any, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a Decimal-compatible number") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return parsed


def _nonnegative_decimal(value: Any, field_name: str) -> Decimal:
    parsed = _finite_decimal(value, field_name)
    if parsed < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return parsed


class EconomicCostModel(BaseModel):
    """Explicit cost assumptions for a research calculation.

    Fee rates are decimal fractions (``0.0002`` means 2 bps). Spread fields
    are quoted full bid/ask bps per leg and are charged at half-spread per
    execution. Slippage fields are one-sided bps per leg. No defaults are
    supplied because an implicit fee schedule would make a positive result
    impossible to audit.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    maker_fee_rate: Decimal
    taker_fee_rate: Decimal

    @field_validator("maker_fee_rate", "taker_fee_rate")
    @classmethod
    def validate_fee_rate(cls, value: Decimal, info) -> Decimal:
        parsed = _nonnegative_decimal(value, info.field_name)
        if parsed > Decimal(1):
            raise ValueError(f"{info.field_name} must not exceed 1")
        return parsed

    def fee_rate(self, maker: bool) -> Decimal:
        return self.maker_fee_rate if maker else self.taker_fee_rate


class BacktestTrade(BaseModel):
    """One completed research trade with pre-cost economics and observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trade_id: str = Field(min_length=1)
    timestamp: datetime
    symbol: str = Field(min_length=2)
    strategy_id: str = Field(min_length=1)
    data_source: str
    regime: str = "UNKNOWN"

    # ``gross_pnl`` is the pre-friction result produced by a separate,
    # causal strategy simulator. This module does not invent signals or PnL.
    gross_pnl: Decimal
    funding_pnl: Decimal

    entry_notional: Decimal
    exit_notional: Decimal
    entry_maker: bool = False
    exit_maker: bool = False
    entry_spread_bps: Decimal
    exit_spread_bps: Decimal
    entry_slippage_bps: Decimal
    exit_slippage_bps: Decimal

    @field_validator("timestamp")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include timezone information")
        return value.astimezone(UTC)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be empty")
        return normalized

    @field_validator("regime")
    @classmethod
    def normalize_regime(cls, value: str) -> str:
        normalized = str(value).strip().upper()
        return normalized or "UNKNOWN"

    @field_validator("data_source")
    @classmethod
    def require_research_source(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in RESEARCH_DATA_SOURCES:
            raise ValueError(
                "data_source must be an explicit Binance public read-only source"
            )
        return normalized

    @field_validator("gross_pnl", "funding_pnl")
    @classmethod
    def require_finite_pnl(cls, value: Decimal, info) -> Decimal:
        return _finite_decimal(value, info.field_name)

    @field_validator(
        "entry_notional",
        "exit_notional",
        "entry_spread_bps",
        "exit_spread_bps",
        "entry_slippage_bps",
        "exit_slippage_bps",
    )
    @classmethod
    def require_nonnegative_cost_inputs(cls, value: Decimal, info) -> Decimal:
        return _nonnegative_decimal(value, info.field_name)

    @model_validator(mode="after")
    def require_positive_turnover(self):
        if self.entry_notional <= 0 or self.exit_notional <= 0:
            raise ValueError("entry_notional and exit_notional must be positive")
        return self


class CostedTrade(BaseModel):
    """A single trade after explicit fee and execution-friction accounting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trade_id: str
    gross_pnl: Decimal
    trading_fees: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    funding_pnl: Decimal
    net_pnl: Decimal


class EconomicBacktestResult(BaseModel):
    """Auditable aggregate metrics; never an execution-readiness signal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data_source: str
    initial_capital: Decimal
    trade_count: int
    gross_pnl: Decimal
    trading_fees: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    funding_pnl: Decimal
    net_pnl: Decimal
    net_return_pct: Decimal
    average_net_pnl: Decimal
    positive_net_expectancy: bool
    max_drawdown_pct: Decimal
    equity_curve: list[Decimal]

    # Explicitly prevents a research result from being mistaken for launch
    # evidence, even if net_pnl and sample expectancy are positive.
    evidence_status: Literal["RESEARCH_CALCULATION_ONLY"] = (
        "RESEARCH_CALCULATION_ONLY"
    )
    launch_eligible: Literal[False] = False


def cost_trade(trade: BacktestTrade, cost_model: EconomicCostModel) -> CostedTrade:
    """Apply explicit costs to one already-simulated trade."""

    entry_fee = trade.entry_notional * cost_model.fee_rate(trade.entry_maker)
    exit_fee = trade.exit_notional * cost_model.fee_rate(trade.exit_maker)
    trading_fees = entry_fee + exit_fee

    # A quoted full spread is split across the two sides of each execution.
    spread_cost = (
        trade.entry_notional * trade.entry_spread_bps / Decimal(20000)
        + trade.exit_notional * trade.exit_spread_bps / Decimal(20000)
    )
    slippage_cost = (
        trade.entry_notional * trade.entry_slippage_bps / Decimal(10000)
        + trade.exit_notional * trade.exit_slippage_bps / Decimal(10000)
    )
    net_pnl = (
        trade.gross_pnl
        - trading_fees
        - spread_cost
        - slippage_cost
        + trade.funding_pnl
    )
    return CostedTrade(
        trade_id=trade.trade_id,
        gross_pnl=trade.gross_pnl,
        trading_fees=trading_fees,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost,
        funding_pnl=trade.funding_pnl,
        net_pnl=net_pnl,
    )


def evaluate_trades(
    trades: Sequence[BacktestTrade],
    *,
    initial_capital: Decimal,
    cost_model: EconomicCostModel,
) -> EconomicBacktestResult:
    """Calculate net economics from chronological, provenance-checked trades."""

    capital = _finite_decimal(initial_capital, "initial_capital")
    if capital <= 0:
        raise ValueError("initial_capital must be positive")
    if not trades:
        raise ValueError("at least one research trade is required")

    normalized_trades = list(trades)
    source = normalized_trades[0].data_source
    previous_timestamp: datetime | None = None
    trade_ids: set[str] = set()
    for trade in normalized_trades:
        if trade.data_source != source:
            raise ValueError("all trades must use one consistent data_source")
        if trade.trade_id in trade_ids:
            raise ValueError(f"duplicate research trade_id: {trade.trade_id}")
        trade_ids.add(trade.trade_id)
        if previous_timestamp is not None and trade.timestamp <= previous_timestamp:
            raise ValueError("trades must be strictly ordered chronologically")
        previous_timestamp = trade.timestamp

    costed = [cost_trade(trade, cost_model) for trade in normalized_trades]
    gross_pnl = sum((trade.gross_pnl for trade in costed), Decimal(0))
    trading_fees = sum((trade.trading_fees for trade in costed), Decimal(0))
    spread_cost = sum((trade.spread_cost for trade in costed), Decimal(0))
    slippage_cost = sum((trade.slippage_cost for trade in costed), Decimal(0))
    funding_pnl = sum((trade.funding_pnl for trade in costed), Decimal(0))
    net_pnl = sum((trade.net_pnl for trade in costed), Decimal(0))

    equity = capital
    peak = capital
    max_drawdown_pct = Decimal(0)
    equity_curve: list[Decimal] = []
    for trade in costed:
        equity += trade.net_pnl
        peak = max(peak, equity)
        drawdown_pct = Decimal(100) * (peak - equity) / peak if peak > 0 else Decimal(100)
        max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)
        equity_curve.append(equity)

    trade_count = len(costed)
    average_net_pnl = net_pnl / Decimal(trade_count)
    return EconomicBacktestResult(
        data_source=source,
        initial_capital=capital,
        trade_count=trade_count,
        gross_pnl=gross_pnl,
        trading_fees=trading_fees,
        spread_cost=spread_cost,
        slippage_cost=slippage_cost,
        funding_pnl=funding_pnl,
        net_pnl=net_pnl,
        net_return_pct=Decimal(100) * net_pnl / capital,
        average_net_pnl=average_net_pnl,
        positive_net_expectancy=average_net_pnl > 0,
        max_drawdown_pct=max_drawdown_pct,
        equity_curve=equity_curve,
    )


class WalkForwardConfig(BaseModel):
    """Fixed rolling, causal train/test windows with purge and embargo gaps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    train_size: int = Field(gt=0)
    test_size: int = Field(gt=0)
    purge_size: int = Field(default=0, ge=0)
    embargo_size: int = Field(default=0, ge=0)


class WalkForwardFold(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fold_index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int


def walk_forward_splits(
    records: Sequence[Any], config: WalkForwardConfig
) -> list[WalkForwardFold]:
    """Return non-overlapping chronological folds without lookahead leakage.

    ``records`` must expose timezone-aware ``timestamp`` values in ascending
    order. The purge gap separates train from test; the embargo gap separates
    one test window from the next rolling train window.
    """

    if not records:
        raise ValueError("walk-forward records must not be empty")
    timestamps: list[datetime] = []
    for record in records:
        timestamp = getattr(record, "timestamp", None)
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise ValueError("walk-forward records require timezone-aware timestamps")
        timestamps.append(timestamp.astimezone(UTC))
    if any(right <= left for left, right in pairwise(timestamps)):
        raise ValueError("walk-forward records must be strictly ordered chronologically")

    folds: list[WalkForwardFold] = []
    cursor = 0
    fold_index = 0
    while True:
        train_start = cursor
        train_end = train_start + config.train_size
        test_start = train_end + config.purge_size
        test_end = test_start + config.test_size
        if test_end > len(records):
            break
        folds.append(
            WalkForwardFold(
                fold_index=fold_index,
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
            )
        )
        fold_index += 1
        cursor = test_end + config.embargo_size

    if not folds:
        raise ValueError("records are insufficient for one walk-forward fold")
    return folds
