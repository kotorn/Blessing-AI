"""Deterministic, research-only replay of the existing strategy pipeline.

This module is deliberately separated from the Binance adapter.  It consumes
validated, public read-only historical events, runs the existing strategy
engines through the allocator, recovery engine, and risk governor, and
simulates only the resulting market orders against the recorded top of book.
It never imports an exchange client, opens a socket, or authorizes execution.

The output is evidence for research and validation, not a Testnet or Mainnet
launch signal.  A replay can only be considered economically meaningful when
the input dataset includes real quotes, depth, mark prices, and explicit
funding settlement rows; missing information fails closed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from domain.enums import (
    EconomicRiskClass,
    MarketType,
    OrderSide,
    OrderType,
    PositionSide,
    RiskState,
    TimeInForce,
)
from domain.models import (
    ExchangeFill,
    ExecutionDecision,
    MarketEvent,
    OrderIntent,
    RiskSnapshot,
    StrategyIntent,
    TargetExposure,
)

from ..engines.exposure_recovery import ExposureRecoveryEngine
from ..engines.funding_carry import FundingCarryCostInputs, FundingCarryEngine
from ..engines.grid_strategy import GridStrategyEngine
from ..engines.market_state import MarketStateClassifier
from ..engines.meta_allocator import MetaAllocator
from ..engines.price_action import PriceActionEngine
from ..engines.risk_governor import RiskGovernor
from ..engines.shock_strategy import ShockStrategyEngine
from ..engines.trend_strategy import TrendStrategyEngine
from .economic import (
    RESEARCH_DATA_SOURCES,
    BacktestTrade,
    EconomicBacktestResult,
    EconomicCostModel,
    cost_trade,
    evaluate_trades,
)

logger = logging.getLogger("blessing.backtest.replay")

SUPPORTED_STRATEGIES = frozenset({"grid", "trend", "shock", "carry"})
SUPPORTED_VENUES = frozenset({"BINANCE_MAINNET", "BINANCE_TESTNET"})


class ReplayValidationError(ValueError):
    """Raised when the research input cannot support a causal replay."""


class ReplayExecutionError(ValueError):
    """Raised when a simulated order cannot be safely filled or closed."""


def _finite_decimal(value: Any, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be Decimal-compatible") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return parsed


def _positive_decimal(value: Any, field_name: str) -> Decimal:
    parsed = _finite_decimal(value, field_name)
    if parsed <= 0:
        raise ValueError(f"{field_name} must be finite and positive")
    return parsed


def _nonnegative_decimal(value: Any, field_name: str) -> Decimal:
    parsed = _finite_decimal(value, field_name)
    if parsed < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return parsed


class HistoricalMarketEvent(BaseModel):
    """One fully specified causal market observation for replay.

    A close-only candle is intentionally insufficient.  The replay needs the
    actual bid/ask and displayed top-of-book quantities so a market fill can
    be rejected when the recorded liquidity could not support it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    event_time: datetime
    symbol: str = Field(min_length=2)
    venue: str
    market_type: MarketType
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    # Empty one-minute intervals are valid market observations.  They still
    # require real quotes, depth, mark price, and provenance; rejecting them
    # here would make a complete public archive look artificially contiguous.
    trade_count: int = Field(ge=0)
    best_bid: Decimal
    best_ask: Decimal
    bid_qty: Decimal
    ask_qty: Decimal
    mark_price: Decimal
    funding_rate: Decimal | None = None
    funding_event: bool = False
    data_source: str

    @field_validator("event_id")
    @classmethod
    def normalize_event_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("event_id must not be empty")
        return normalized

    @field_validator("event_time")
    @classmethod
    def normalize_event_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("event_time must include timezone information")
        return value.astimezone(UTC)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be empty")
        return normalized

    @field_validator("venue", "data_source")
    @classmethod
    def normalize_nonempty_upper(cls, value: str, info) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be empty")
        return normalized

    @field_validator(
        "open",
        "high",
        "low",
        "close",
        "best_bid",
        "best_ask",
        "bid_qty",
        "ask_qty",
        "mark_price",
    )
    @classmethod
    def require_positive_market_values(cls, value: Decimal, info) -> Decimal:
        return _positive_decimal(value, info.field_name)

    @field_validator("volume")
    @classmethod
    def require_nonnegative_volume(cls, value: Decimal) -> Decimal:
        return _nonnegative_decimal(value, "volume")

    @field_validator("funding_rate")
    @classmethod
    def require_finite_funding_rate(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        return _finite_decimal(value, "funding_rate")

    @model_validator(mode="after")
    def validate_market_observation(self) -> HistoricalMarketEvent:
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("OHLC relationship is invalid")
        if self.high < self.low:
            raise ValueError("high must be greater than or equal to low")
        if self.best_bid > self.best_ask:
            raise ValueError("best_bid must be less than or equal to best_ask")
        if self.data_source not in RESEARCH_DATA_SOURCES:
            raise ValueError("data_source must be an explicit Binance public read-only source")
        if self.venue not in SUPPORTED_VENUES:
            raise ValueError("venue must be BINANCE_MAINNET or BINANCE_TESTNET")
        expected_venue = (
            "BINANCE_TESTNET"
            if self.data_source == "BINANCE_PUBLIC_TESTNET_READ_ONLY"
            else "BINANCE_MAINNET"
        )
        if self.venue != expected_venue:
            raise ValueError("venue does not match data_source")
        if self.market_type != MarketType.USDM_FUTURES:
            raise ValueError("historical replay supports USDⓈ-M futures only")
        if self.funding_event and self.funding_rate is None:
            raise ValueError("funding_event requires an explicit funding_rate")
        if not self.funding_event and self.funding_rate is not None:
            raise ValueError("funding_rate is only accepted on funding_event rows")
        return self

    def to_market_event(self) -> MarketEvent:
        """Convert without introducing a receive-time wall-clock value."""

        return MarketEvent(
            event_id=self.event_id,
            event_time=self.event_time,
            receive_time=self.event_time,
            symbol=self.symbol,
            venue=self.venue,
            market_type=self.market_type,
            last_price=self.close,
            best_bid=self.best_bid,
            best_ask=self.best_ask,
            volume_24h=self.volume,
            mark_price=self.mark_price,
            funding_rate=self.funding_rate,
        )


class ReplaySymbolRules(BaseModel):
    """Exchange rules captured from an explicit exchange-info snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=2)
    tick_size: Decimal
    step_size: Decimal
    min_quantity: Decimal
    max_quantity: Decimal | None = None
    min_notional: Decimal
    max_notional: Decimal | None = None

    @field_validator("symbol")
    @classmethod
    def normalize_rule_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be empty")
        return normalized

    @field_validator("tick_size", "step_size", "min_quantity", "min_notional")
    @classmethod
    def require_positive_rule_values(cls, value: Decimal, info) -> Decimal:
        return _positive_decimal(value, info.field_name)

    @field_validator("max_quantity", "max_notional")
    @classmethod
    def require_optional_positive_rule_values(
        cls, value: Decimal | None, info
    ) -> Decimal | None:
        if value is None:
            return None
        return _positive_decimal(value, info.field_name)

    @model_validator(mode="after")
    def validate_rule_bounds(self) -> ReplaySymbolRules:
        if self.max_quantity is not None and self.max_quantity < self.min_quantity:
            raise ValueError("max_quantity must be greater than or equal to min_quantity")
        if self.max_notional is not None and self.max_notional < self.min_notional:
            raise ValueError("max_notional must be greater than or equal to min_notional")
        return self


class ReplayExecutionConfig(BaseModel):
    """Explicit research execution assumptions and conservative launch caps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    initial_capital: Decimal
    cost_model: EconomicCostModel
    market_slippage_bps: Decimal
    funding_interval_sec: int
    enabled_strategies: tuple[str, ...]
    symbol_rules: tuple[ReplaySymbolRules, ...]
    carry_cost_inputs: FundingCarryCostInputs | None = None

    # These defaults mirror the first-launch Testnet caps.  They are still
    # applied by the research order gate and may only be changed explicitly.
    max_single_order_notional: Decimal = Decimal(100)
    max_total_open_notional: Decimal = Decimal(100)
    max_open_orders: int = 1
    max_active_exposure_chains: int = 1
    max_target_gross_qty: Decimal = Decimal(2)
    max_leverage: Decimal = Decimal(2)
    max_drawdown_pct: Decimal = Decimal(6)
    max_margin_utilization_pct: Decimal = Decimal(70)
    require_funding_events: bool = True
    force_close_at_end: bool = True

    @field_validator("initial_capital")
    @classmethod
    def require_initial_capital(cls, value: Decimal) -> Decimal:
        return _positive_decimal(value, "initial_capital")

    @field_validator("market_slippage_bps", "max_single_order_notional", "max_total_open_notional")
    @classmethod
    def require_nonnegative_config_values(cls, value: Decimal, info) -> Decimal:
        parsed = _nonnegative_decimal(value, info.field_name)
        if info.field_name == "max_single_order_notional" and parsed <= 0:
            raise ValueError("max_single_order_notional must be positive")
        if info.field_name == "max_total_open_notional" and parsed <= 0:
            raise ValueError("max_total_open_notional must be positive")
        return parsed

    @field_validator(
        "max_target_gross_qty", "max_leverage", "max_drawdown_pct", "max_margin_utilization_pct"
    )
    @classmethod
    def require_positive_config_values(cls, value: Decimal, info) -> Decimal:
        return _positive_decimal(value, info.field_name)

    @field_validator("funding_interval_sec")
    @classmethod
    def require_funding_interval(cls, value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("funding_interval_sec must be a positive integer")
        return value

    @field_validator("max_open_orders", "max_active_exposure_chains")
    @classmethod
    def require_positive_limits(cls, value: int, info) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{info.field_name} must be a positive integer")
        return value

    @field_validator("enabled_strategies")
    @classmethod
    def normalize_strategies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(strategy).strip().lower() for strategy in value)
        if not normalized or any(not strategy for strategy in normalized):
            raise ValueError("enabled_strategies must contain at least one strategy")
        if len(set(normalized)) != len(normalized):
            raise ValueError("enabled_strategies must not contain duplicates")
        unsupported = sorted(set(normalized).difference(SUPPORTED_STRATEGIES))
        if unsupported:
            raise ValueError(f"unsupported replay strategies: {', '.join(unsupported)}")
        return normalized

    @model_validator(mode="after")
    def validate_rules(self) -> ReplayExecutionConfig:
        symbols = [rule.symbol for rule in self.symbol_rules]
        if not symbols or len(set(symbols)) != len(symbols):
            raise ValueError("symbol_rules must contain one unique rule set per symbol")
        if self.max_total_open_notional < self.max_single_order_notional:
            raise ValueError("max_total_open_notional must cover one maximum order")
        if "carry" in self.enabled_strategies and self.carry_cost_inputs is None:
            raise ValueError("carry strategy requires explicit carry_cost_inputs")
        return self

    def rules_for(self, symbol: str) -> ReplaySymbolRules:
        normalized = str(symbol).strip().upper()
        for rule in self.symbol_rules:
            if rule.symbol == normalized:
                return rule
        raise ReplayValidationError(f"no captured exchange rules for {normalized}")


class ReplayOrderRecord(BaseModel):
    """Per-order gate result, including rejected orders."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    timestamp: datetime
    decision_id: str
    client_order_id: str | None = None
    risk_class: EconomicRiskClass
    accepted: bool
    reason: str
    exchange_order_id: str | None = None


class ReplayEquityPoint(BaseModel):
    """Event-level mark-to-market account state for audit and drawdown review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    timestamp: datetime
    position_qty: Decimal
    unrealized_pnl: Decimal
    open_funding_pnl: Decimal
    equity: Decimal


class ReplayResult(BaseModel):
    """Research replay output with an explicit non-launch status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_sha256: str
    config_sha256: str
    event_count: int
    start_time: datetime
    end_time: datetime
    strategy_intents: tuple[StrategyIntent, ...]
    target_exposures: tuple[TargetExposure, ...]
    risk_snapshots: tuple[RiskSnapshot, ...]
    execution_decisions: tuple[ExecutionDecision, ...]
    decisions: tuple[ReplayOrderRecord, ...]
    fills: tuple[ExchangeFill, ...]
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[ReplayEquityPoint, ...]
    economic_result: EconomicBacktestResult | None
    final_position_qty: Decimal
    final_equity: Decimal
    open_position_at_end: Decimal
    evidence_status: Literal["RESEARCH_REPLAY_ONLY"] = "RESEARCH_REPLAY_ONLY"
    launch_eligible: Literal[False] = False


class EventWalkForwardConfig(BaseModel):
    """Causal event-time windows for later train-only selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    train_duration_sec: int = Field(gt=0)
    test_duration_sec: int = Field(gt=0)
    purge_duration_sec: int = Field(default=0, ge=0)
    embargo_duration_sec: int = Field(default=0, ge=0)


class EventWalkForwardFold(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fold_index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    train_start_time: datetime
    train_end_time: datetime
    test_start_time: datetime
    test_end_time: datetime


class ReplayParameterVariant(BaseModel):
    """One explicit parameter/configuration candidate for train-only selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    variant_id: str = Field(min_length=1)
    config: ReplayExecutionConfig


class ReplayWalkForwardFoldResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fold_index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    selected_variant_id: str
    train_net_pnl_by_variant: dict[str, Decimal | None]
    selection_artifact_sha256: str
    test_trade_count: int
    test_net_pnl: Decimal | None


class ReplayWalkForwardResult(BaseModel):
    """Actual train-select/test-replay output, still explicitly non-launch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_sha256: str
    variant_ids: tuple[str, ...]
    folds: tuple[ReplayWalkForwardFoldResult, ...]
    oos_trades: tuple[BacktestTrade, ...]
    oos_economic_result: EconomicBacktestResult | None
    evidence_status: Literal["RESEARCH_WALK_FORWARD_ONLY"] = "RESEARCH_WALK_FORWARD_ONLY"
    launch_eligible: Literal[False] = False


@dataclass
class _PositionState:
    signed_qty: Decimal
    reference_entry_price: Decimal
    entry_time: datetime
    entry_notional: Decimal
    entry_spread_bps: Decimal
    entry_slippage_bps: Decimal
    strategy_id: str
    regime: str
    source_intent_ids: list[str]
    funding_pnl: Decimal = Decimal(0)


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_json_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _canonical_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    return value


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        _canonical_json_value(value), sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_event_sequence(events: Sequence[HistoricalMarketEvent]) -> list[HistoricalMarketEvent]:
    if not events:
        raise ReplayValidationError("replay requires at least one historical event")
    normalized = list(events)
    event_ids: set[str] = set()
    first = normalized[0]
    for previous, current in pairwise(normalized):
        if current.event_time <= previous.event_time:
            raise ReplayValidationError("historical events must be strictly chronological")
    for event in normalized:
        if event.event_id in event_ids:
            raise ReplayValidationError(f"duplicate historical event_id: {event.event_id}")
        event_ids.add(event.event_id)
        if event.symbol != first.symbol:
            raise ReplayValidationError("one replay must contain one symbol")
        if event.venue != first.venue or event.market_type != first.market_type:
            raise ReplayValidationError("replay events must use one venue and market type")
        if event.data_source != first.data_source:
            raise ReplayValidationError("replay events must use one data_source")
    return normalized


def walk_forward_event_splits(
    events: Sequence[HistoricalMarketEvent], config: EventWalkForwardConfig
) -> list[EventWalkForwardFold]:
    """Split raw events by event time, preserving purge and embargo gaps."""

    normalized = _validate_event_sequence(events)
    timestamps = [event.event_time for event in normalized]
    folds: list[EventWalkForwardFold] = []
    cursor = 0
    fold_index = 0
    while cursor < len(normalized):
        train_start_time = timestamps[cursor]
        train_end_time = train_start_time + timedelta(seconds=config.train_duration_sec)
        purge_end_time = train_end_time + timedelta(seconds=config.purge_duration_sec)
        test_start = next(
            (index for index, timestamp in enumerate(timestamps) if timestamp >= purge_end_time),
            len(timestamps),
        )
        if test_start >= len(timestamps):
            break
        test_start_time = timestamps[test_start]
        test_end_time = test_start_time + timedelta(seconds=config.test_duration_sec)
        test_end = next(
            (index for index, timestamp in enumerate(timestamps[test_start:], start=test_start)
             if timestamp >= test_end_time),
            len(timestamps),
        )
        if test_end <= test_start:
            break
        train_end = next(
            (index for index, timestamp in enumerate(timestamps) if timestamp >= train_end_time),
            test_start,
        )
        if train_end <= cursor:
            break
        folds.append(
            EventWalkForwardFold(
                fold_index=fold_index,
                train_start=cursor,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                train_start_time=train_start_time,
                train_end_time=train_end_time,
                test_start_time=test_start_time,
                test_end_time=test_end_time,
            )
        )
        fold_index += 1
        embargo_end_time = test_end_time + timedelta(seconds=config.embargo_duration_sec)
        cursor = next(
            (index for index, timestamp in enumerate(timestamps[test_end:], start=test_end)
             if timestamp >= embargo_end_time),
            len(timestamps),
        )
    if not folds:
        raise ReplayValidationError("events are insufficient for one event-time walk-forward fold")
    return folds


class DeterministicReplay:
    """Run a deterministic replay of existing engines with no network access."""

    def __init__(self, config: ReplayExecutionConfig):
        self.config = config
        self._reset_runtime()

    def _reset_runtime(self) -> None:
        """Reset all mutable replay state so one runner is safely reusable."""

        self._clock_time: datetime | None = None
        self._position: _PositionState | None = None
        self._realized_net_pnl = Decimal(0)
        self._peak_equity = self.config.initial_capital
        self._active_exposure_chains = 0
        self._last_funding_at: datetime | None = None
        self._funding_event_seen = False
        self._grid_depth = 0
        self._trade_sequence = 0
        self._fill_sequence = 0

        self.price_action = PriceActionEngine()
        self.market_state = MarketStateClassifier()
        self.grid = GridStrategyEngine()
        self.trend = TrendStrategyEngine()
        self.shock = ShockStrategyEngine()
        self.carry = FundingCarryEngine(cost_inputs=self.config.carry_cost_inputs)
        self.allocator = MetaAllocator(max_gross_exposure_btc=self.config.max_target_gross_qty)
        self.recovery = ExposureRecoveryEngine(clock=self._now)
        self.governor = RiskGovernor(
            max_leverage=self.config.max_leverage,
            max_drawdown_pct=self.config.max_drawdown_pct,
            max_margin_utilization_pct=self.config.max_margin_utilization_pct,
            clock=self._now,
        )

    def _now(self) -> datetime:
        if self._clock_time is None:
            raise ReplayExecutionError("replay clock is not positioned on an event")
        return self._clock_time

    def _current_unrealized(self, mark_price: Decimal) -> Decimal:
        if self._position is None:
            return Decimal(0)
        return (mark_price - self._position.reference_entry_price) * self._position.signed_qty

    def _current_equity(self, mark_price: Decimal) -> Decimal:
        open_funding = self._position.funding_pnl if self._position else Decimal(0)
        return (
            self.config.initial_capital
            + self._realized_net_pnl
            + open_funding
            + self._current_unrealized(mark_price)
        )

    def _equity_point(self, event: HistoricalMarketEvent) -> ReplayEquityPoint:
        return ReplayEquityPoint(
            event_id=event.event_id,
            timestamp=event.event_time,
            position_qty=self._position.signed_qty if self._position else Decimal(0),
            unrealized_pnl=self._current_unrealized(event.mark_price),
            open_funding_pnl=self._position.funding_pnl if self._position else Decimal(0),
            equity=self._current_equity(event.mark_price),
        )

    def _risk_snapshot(self, event: HistoricalMarketEvent) -> RiskSnapshot:
        equity = self._current_equity(event.mark_price)
        self._peak_equity = max(self._peak_equity, equity)
        drawdown = (
            Decimal(100) * (self._peak_equity - equity) / self._peak_equity
            if self._peak_equity > 0
            else Decimal(100)
        )
        signed_qty = self._position.signed_qty if self._position else Decimal(0)
        notional = abs(signed_qty) * event.mark_price
        effective_leverage = notional / equity if equity > 0 else Decimal("Infinity")
        margin_utilization = (
            Decimal(100) * effective_leverage / self.config.max_leverage
            if effective_leverage.is_finite()
            else Decimal(100)
        )
        if equity <= 0:
            risk_state = RiskState.EMERGENCY
        elif self._position is not None:
            # Historical market events do not contain authoritative
            # exchange-position liquidation prices.  This is deliberately
            # UNKNOWN and blocks further risk increase; reductions remain
            # available through RiskGovernor.
            risk_state = RiskState.NO_NEW_RISK
        else:
            risk_state = RiskState.NORMAL
        return RiskSnapshot(
            timestamp=event.event_time,
            portfolio_equity=equity,
            unrealized_pnl=self._current_unrealized(event.mark_price),
            realized_pnl_24h=self._realized_net_pnl,
            margin_utilization_pct=max(Decimal(0), margin_utilization),
            effective_leverage=max(Decimal(0), effective_leverage)
            if effective_leverage.is_finite()
            else Decimal(0),
            current_drawdown_pct=max(Decimal(0), drawdown),
            liquidation_distance_pct=None,
            risk_state=risk_state,
        )

    def _apply_funding(self, event: HistoricalMarketEvent) -> None:
        if self._position is None:
            return
        if self._last_funding_at is None:
            self._last_funding_at = self._position.entry_time
        elapsed = event.event_time - self._last_funding_at
        interval = timedelta(seconds=self.config.funding_interval_sec)
        if self.config.require_funding_events and elapsed >= interval and not event.funding_event:
            raise ReplayValidationError(
                f"missing funding event before {event.event_id} for {event.symbol}"
            )
        if event.funding_event:
            if event.funding_rate is None:
                raise ReplayValidationError(f"funding event {event.event_id} has no rate")
            # The first settlement after entry is calendar-driven and may be
            # less than one configured interval away when entry occurs after
            # the previous settlement.  Once one real settlement is observed,
            # subsequent gaps must match the configured funding cadence.
            if self.config.require_funding_events and self._funding_event_seen and elapsed < interval:
                raise ReplayValidationError(
                    f"funding event {event.event_id} arrived before its configured interval"
                )
            if self.config.require_funding_events and elapsed > interval:
                raise ReplayValidationError(
                    f"funding event gap before {event.event_id} exceeds the configured interval"
                )
            funding_pnl = -self._position.signed_qty * event.mark_price * event.funding_rate
            self._position.funding_pnl += funding_pnl
            self._last_funding_at = event.event_time
            self._funding_event_seen = True

    def _strategy_intents(
        self, event: HistoricalMarketEvent, pa_state: Any, market_state: Any
    ) -> list[StrategyIntent]:
        intents: list[StrategyIntent] = []
        if "grid" in self.config.enabled_strategies:
            intent = self.grid.evaluate(pa_state, market_state, grid_depth=self._grid_depth)
            if intent is not None and intent.desired_delta_qty != 0:
                intents.append(intent)
        if "trend" in self.config.enabled_strategies:
            intent = self.trend.evaluate(pa_state, market_state)
            if intent is not None:
                intents.append(intent)
        if "shock" in self.config.enabled_strategies:
            intent = self.shock.evaluate(pa_state, market_state)
            if intent is not None:
                intents.append(intent)
        if "carry" in self.config.enabled_strategies:
            # The config validator requires the complete explicit carry-cost
            # object before this engine can be enabled.
            intent = self.carry.evaluate(event.to_market_event(), market_state)
            if intent is not None:
                intents.append(intent)
        return intents

    def _target_with_stable_id(self, target: TargetExposure) -> TargetExposure:
        digest = _canonical_hash(
            {
                "symbol": target.symbol,
                "created_at": target.created_at.isoformat(),
                "source_intent_ids": target.source_intent_ids,
            }
        )[:24]
        return target.model_copy(update={"exposure_id": f"EXP-REPLAY-{digest}"})

    @staticmethod
    def _normalise_quantity(quantity: Decimal, step_size: Decimal) -> Decimal:
        units = (quantity / step_size).to_integral_value(rounding=ROUND_DOWN)
        return units * step_size

    def _market_fill_price(self, order: OrderIntent, event: HistoricalMarketEvent) -> Decimal:
        # These fields are required by HistoricalMarketEvent; no synthetic
        # reference price or fixed fallback is permitted.
        if order.side == OrderSide.BUY:
            return event.best_ask * (
                Decimal(1) + self.config.market_slippage_bps / Decimal(10000)
            )
        if order.side == OrderSide.SELL:
            return event.best_bid * (
                Decimal(1) - self.config.market_slippage_bps / Decimal(10000)
            )
        raise ReplayExecutionError("order side is invalid")

    def _order_gate(
        self, order: OrderIntent, decision: ExecutionDecision, event: HistoricalMarketEvent
    ) -> tuple[OrderIntent | None, Decimal | None, str]:
        if order.symbol != event.symbol:
            return None, None, "order symbol does not match event symbol"
        if order.market_type != MarketType.USDM_FUTURES:
            return None, None, "order market type is not USDⓈ-M futures"
        if order.order_type != OrderType.MARKET:
            return None, None, "replay supports only the existing market-order pipeline"
        if order.position_side != PositionSide.BOTH:
            return None, None, "replay does not support hedge-mode position legs"
        if order.price is not None:
            return None, None, "market order must not carry a synthetic price"
        if order.post_only:
            return None, None, "market order cannot be post-only"
        if order.quantity <= 0 or not order.quantity.is_finite():
            return None, None, "order quantity must be finite and positive"

        rules = self.config.rules_for(event.symbol)
        quantity = self._normalise_quantity(order.quantity, rules.step_size)
        if quantity < rules.min_quantity or quantity <= 0:
            return None, None, "normalized quantity is below the captured minimum"
        if rules.max_quantity is not None and quantity > rules.max_quantity:
            return None, None, "normalized quantity exceeds the captured maximum"

        fill_price = self._market_fill_price(order, event)
        notional = quantity * fill_price
        if notional < rules.min_notional:
            return None, None, "simulated order notional is below the captured minimum"
        if rules.max_notional is not None and notional > rules.max_notional:
            return None, None, "simulated order notional exceeds the captured maximum"

        available_depth = event.ask_qty if order.side == OrderSide.BUY else event.bid_qty
        if quantity > available_depth:
            return None, None, "recorded top-of-book depth cannot fill the order"

        signed_order_qty = quantity if order.side == OrderSide.BUY else -quantity
        current_qty = self._position.signed_qty if self._position else Decimal(0)
        is_reducing = (
            current_qty != 0
            and signed_order_qty != 0
            and (current_qty > 0) != (signed_order_qty > 0)
            and quantity <= abs(current_qty)
        )
        if order.reduce_only and not is_reducing:
            return None, None, "reduce-only order does not reduce the authoritative simulated position"
        if not order.reduce_only and current_qty != 0 and (
            (current_qty > 0) != (signed_order_qty > 0)
        ):
            return None, None, "risk-increasing order would cross through zero"
        if decision.risk_class in {
            EconomicRiskClass.NEW_RISK,
            EconomicRiskClass.INCREASE_RISK,
        }:
            if is_reducing:
                return None, None, "risk class conflicts with reducing order semantics"
            if notional > self.config.max_single_order_notional:
                return None, None, "order exceeds the configured replay launch cap"
            if (
                abs(current_qty + signed_order_qty) * event.mark_price
                > self.config.max_total_open_notional
            ):
                return None, None, "total open notional exceeds the configured replay launch cap"
            if current_qty == 0 and self._active_exposure_chains >= self.config.max_active_exposure_chains:
                return None, None, "active exposure-chain limit is reached"
        normalized_order = order.model_copy(update={"quantity": quantity})
        return normalized_order, fill_price, "accepted"

    def _spread_bps(self, event: HistoricalMarketEvent) -> Decimal:
        midpoint = (event.best_bid + event.best_ask) / Decimal(2)
        return (
            (event.best_ask - event.best_bid) / midpoint * Decimal(10000)
            if midpoint > 0
            else Decimal(0)
        )

    def _append_trade_for_reduction(
        self,
        *,
        quantity: Decimal,
        exit_mid: Decimal,
        exit_fill_price: Decimal,
        event: HistoricalMarketEvent,
    ) -> BacktestTrade:
        if self._position is None:
            raise ReplayExecutionError("cannot reduce a flat simulated position")
        position = self._position
        old_abs_qty = abs(position.signed_qty)
        funding_share = position.funding_pnl * quantity / old_abs_qty
        gross_pnl = (exit_mid - position.reference_entry_price) * (
            quantity if position.signed_qty > 0 else -quantity
        )
        entry_notional = position.entry_notional * quantity / old_abs_qty
        self._trade_sequence += 1
        trade = BacktestTrade(
            trade_id=f"REPLAY-TRADE-{self._trade_sequence:06d}",
            timestamp=event.event_time,
            symbol=event.symbol,
            strategy_id=position.strategy_id,
            data_source=event.data_source,
            regime=position.regime,
            # Gross PnL uses reference mid prices.  Explicit spread and
            # slippage observations are then deducted by cost_trade exactly
            # once, keeping the economic result auditable.
            gross_pnl=gross_pnl,
            funding_pnl=funding_share,
            entry_notional=entry_notional,
            exit_notional=quantity * exit_fill_price,
            entry_maker=False,
            exit_maker=False,
            entry_spread_bps=position.entry_spread_bps,
            exit_spread_bps=self._spread_bps(event),
            entry_slippage_bps=position.entry_slippage_bps,
            exit_slippage_bps=self.config.market_slippage_bps,
        )
        self._realized_net_pnl += cost_trade(trade, self.config.cost_model).net_pnl
        return trade

    def _apply_fill(
        self,
        order: OrderIntent,
        fill_price: Decimal,
        event: HistoricalMarketEvent,
        *,
        strategy_id: str,
        regime: str,
        decision_id: str | None = None,
        target_exposure_id: str | None = None,
    ) -> tuple[ExchangeFill, BacktestTrade | None]:
        quantity = order.quantity
        signed_qty = quantity if order.side == OrderSide.BUY else -quantity
        midpoint = (event.best_bid + event.best_ask) / Decimal(2)
        notional = quantity * fill_price
        self._fill_sequence += 1
        exchange_order_id = f"REPLAY-ORDER-{self._fill_sequence:06d}"
        exchange_trade_id = f"REPLAY-FILL-{self._fill_sequence:06d}"
        commission = notional * self.config.cost_model.taker_fee_rate
        trade: BacktestTrade | None = None

        if self._position is None:
            self._position = _PositionState(
                signed_qty=signed_qty,
                reference_entry_price=midpoint,
                entry_time=event.event_time,
                entry_notional=notional,
                entry_spread_bps=self._spread_bps(event),
                entry_slippage_bps=self.config.market_slippage_bps,
                strategy_id=strategy_id,
                regime=regime,
                source_intent_ids=list(order.source_intent_ids),
            )
            self._active_exposure_chains += 1
            self._last_funding_at = event.event_time
            self._funding_event_seen = False
        elif (self._position.signed_qty > 0) == (signed_qty > 0):
            old_qty = abs(self._position.signed_qty)
            new_qty = old_qty + quantity
            self._position.reference_entry_price = (
                self._position.reference_entry_price * old_qty + midpoint * quantity
            ) / new_qty
            previous_notional = self._position.entry_notional
            self._position.entry_notional += notional
            self._position.entry_spread_bps = (
                self._position.entry_spread_bps * previous_notional
                + self._spread_bps(event) * notional
            ) / self._position.entry_notional
            self._position.signed_qty += signed_qty
            self._position.source_intent_ids = list(
                dict.fromkeys(self._position.source_intent_ids + list(order.source_intent_ids))
            )
        else:
            trade = self._append_trade_for_reduction(
                quantity=quantity,
                exit_mid=midpoint,
                exit_fill_price=fill_price,
                event=event,
            )
            old_qty = abs(self._position.signed_qty)
            funding_share = self._position.funding_pnl * quantity / old_qty
            self._position.funding_pnl -= funding_share
            self._position.entry_notional -= self._position.entry_notional * quantity / old_qty
            self._position.signed_qty += signed_qty
            if self._position.signed_qty == 0:
                self._position = None
                self._active_exposure_chains = max(0, self._active_exposure_chains - 1)
                self._last_funding_at = None
                self._funding_event_seen = False

        fill = ExchangeFill(
            exchange_trade_id=exchange_trade_id,
            exchange_order_id=exchange_order_id,
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            position_side=order.position_side,
            quantity=quantity,
            price=fill_price,
            commission=commission,
            commission_asset="USDT",
            realized_pnl=trade.gross_pnl if trade is not None else Decimal(0),
            maker=False,
            event_time=event.event_time,
            transaction_time=event.event_time,
            source="RESEARCH_REPLAY",
            strategy_id=strategy_id,
            decision_id=decision_id,
            target_exposure_id=target_exposure_id,
            source_intent_ids=order.source_intent_ids,
        )
        return fill, trade

    def _force_close(
        self,
        event: HistoricalMarketEvent,
        execution_decisions: list[ExecutionDecision],
        decisions: list[ReplayOrderRecord],
        fills: list[ExchangeFill],
        trades: list[BacktestTrade],
    ) -> None:
        if self._position is None:
            return
        side = OrderSide.SELL if self._position.signed_qty > 0 else OrderSide.BUY
        target_exposure_id = f"REPLAY-END-CLOSE-TARGET-{event.event_id}"
        order = OrderIntent(
            client_order_id=f"REPLAY-END-CLOSE-{event.event_id}",
            symbol=event.symbol,
            market_type=MarketType.USDM_FUTURES,
            side=side,
            position_side=PositionSide.BOTH,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.GTC,
            quantity=abs(self._position.signed_qty),
            reduce_only=True,
            strategy_id=self._position.strategy_id,
            source_intent_ids=self._position.source_intent_ids,
            created_at=event.event_time,
        )
        decision_id = f"REPLAY-END-CLOSE-DECISION-{event.event_id}"
        decision = ExecutionDecision(
            decision_id=decision_id,
            symbol=event.symbol,
            action="REDUCE_POSITION",
            risk_class=EconomicRiskClass.CLOSE,
            orders=[order],
            rational="Explicit end-of-sample flatten for a research replay.",
            net_exposure_delta=-self._position.signed_qty,
            timestamp=event.event_time,
            target_exposure_id=target_exposure_id,
            source_intent_ids=self._position.source_intent_ids,
        )
        execution_decisions.append(decision)
        normalized_order, fill_price, reason = self._order_gate(order, decision, event)
        if normalized_order is None or fill_price is None:
            decisions.append(
                ReplayOrderRecord(
                    event_id=event.event_id,
                    timestamp=event.event_time,
                    decision_id=decision_id,
                    client_order_id=order.client_order_id,
                    risk_class=EconomicRiskClass.CLOSE,
                    accepted=False,
                    reason=f"force close rejected: {reason}",
                )
            )
            raise ReplayExecutionError(f"explicit end-of-sample close failed: {reason}")
        fill, trade = self._apply_fill(
            normalized_order,
            fill_price,
            event,
            strategy_id=order.strategy_id,
            regime="END_OF_SAMPLE",
            decision_id=decision_id,
            target_exposure_id=target_exposure_id,
        )
        fills.append(fill)
        if trade is not None:
            trades.append(trade)
        decisions.append(
            ReplayOrderRecord(
                event_id=event.event_id,
                timestamp=event.event_time,
                decision_id=decision_id,
                client_order_id=order.client_order_id,
                risk_class=EconomicRiskClass.CLOSE,
                accepted=True,
                reason="explicit end-of-sample close filled",
                exchange_order_id=fill.exchange_order_id,
            )
        )

    def run(self, events: Sequence[HistoricalMarketEvent]) -> ReplayResult:
        normalized_events = _validate_event_sequence(events)
        self._reset_runtime()
        if any(event.symbol not in {rule.symbol for rule in self.config.symbol_rules} for event in normalized_events):
            raise ReplayValidationError("every event symbol must have captured exchange rules")
        dataset_sha256 = _canonical_hash([event.model_dump(mode="json") for event in normalized_events])
        config_sha256 = _canonical_hash(self.config)
        strategy_intents: list[StrategyIntent] = []
        target_exposures: list[TargetExposure] = []
        risk_snapshots: list[RiskSnapshot] = []
        execution_decisions: list[ExecutionDecision] = []
        decisions: list[ReplayOrderRecord] = []
        fills: list[ExchangeFill] = []
        trades: list[BacktestTrade] = []
        equity_curve: list[ReplayEquityPoint] = []

        for event in normalized_events:
            self._clock_time = event.event_time
            self._apply_funding(event)
            pa_state = self.price_action.process_event(event.to_market_event())
            if pa_state is None:
                equity_curve.append(self._equity_point(event))
                continue
            market_state = self.market_state.classify(pa_state)
            intents = self._strategy_intents(event, pa_state, market_state)
            if not intents:
                equity_curve.append(self._equity_point(event))
                continue
            strategy_intents.extend(intents)
            target = self._target_with_stable_id(self.allocator.allocate(intents, event.symbol))
            risk_snapshot = self._risk_snapshot(event)
            risk_snapshots.append(risk_snapshot)
            target = self.recovery.process(
                target=target,
                risk=risk_snapshot,
                current_position_qty=self._position.signed_qty if self._position else Decimal(0),
            )
            target_exposures.append(target)
            decision = self.governor.evaluate(
                target,
                risk_snapshot,
                current_position_qty=self._position.signed_qty if self._position else Decimal(0),
            )
            execution_decisions.append(decision)
            strategy_id = "+".join(sorted({intent.strategy_id for intent in intents}))
            regime = getattr(market_state.primary_regime, "value", str(market_state.primary_regime))
            if not decision.orders:
                decisions.append(
                    ReplayOrderRecord(
                        event_id=event.event_id,
                        timestamp=event.event_time,
                        decision_id=decision.decision_id,
                        risk_class=decision.risk_class,
                        accepted=False,
                        reason=decision.rational,
                    )
                )
                equity_curve.append(self._equity_point(event))
                continue
            for order in decision.orders:
                normalized_order, fill_price, reason = self._order_gate(order, decision, event)
                if normalized_order is None or fill_price is None:
                    decisions.append(
                        ReplayOrderRecord(
                            event_id=event.event_id,
                            timestamp=event.event_time,
                            decision_id=decision.decision_id,
                            client_order_id=order.client_order_id,
                            risk_class=decision.risk_class,
                            accepted=False,
                            reason=reason,
                        )
                    )
                    continue
                fill, trade = self._apply_fill(
                    normalized_order,
                    fill_price,
                    event,
                    strategy_id=strategy_id,
                    regime=regime,
                    decision_id=decision.decision_id,
                    target_exposure_id=target.exposure_id,
                )
                fills.append(fill)
                if trade is not None:
                    trades.append(trade)
                if any("grid" in intent.strategy_id.lower() for intent in intents):
                    self._grid_depth += 1
                decisions.append(
                    ReplayOrderRecord(
                        event_id=event.event_id,
                        timestamp=event.event_time,
                        decision_id=decision.decision_id,
                        client_order_id=order.client_order_id,
                        risk_class=decision.risk_class,
                        accepted=True,
                        reason="simulated market fill",
                        exchange_order_id=fill.exchange_order_id,
                    )
                )
            equity_curve.append(self._equity_point(event))

        self._clock_time = normalized_events[-1].event_time
        if self._position is not None and self.config.force_close_at_end:
            # The explicit end-of-sample close is still a risk decision. Keep
            # its contemporaneous risk snapshot in the audit stream so every
            # execution decision has the risk context that authorized or
            # required it.
            risk_snapshots.append(self._risk_snapshot(normalized_events[-1]))
            self._force_close(
                normalized_events[-1],
                execution_decisions,
                decisions,
                fills,
                trades,
            )
            if equity_curve:
                equity_curve[-1] = self._equity_point(normalized_events[-1])

        economic_result = (
            evaluate_trades(
                trades,
                initial_capital=self.config.initial_capital,
                cost_model=self.config.cost_model,
            )
            if trades
            else None
        )
        final_event = normalized_events[-1]
        final_equity = self._current_equity(final_event.mark_price)
        return ReplayResult(
            dataset_sha256=dataset_sha256,
            config_sha256=config_sha256,
            event_count=len(normalized_events),
            start_time=normalized_events[0].event_time,
            end_time=final_event.event_time,
            strategy_intents=tuple(strategy_intents),
            target_exposures=tuple(target_exposures),
            risk_snapshots=tuple(risk_snapshots),
            execution_decisions=tuple(execution_decisions),
            decisions=tuple(decisions),
            fills=tuple(fills),
            trades=tuple(trades),
            equity_curve=tuple(equity_curve),
            economic_result=economic_result,
            final_position_qty=self._position.signed_qty if self._position else Decimal(0),
            final_equity=final_equity,
            open_position_at_end=self._position.signed_qty if self._position else Decimal(0),
        )


def run_replay(
    events: Sequence[HistoricalMarketEvent], config: ReplayExecutionConfig
) -> ReplayResult:
    """Convenience wrapper that creates a fresh isolated replay runner."""

    return DeterministicReplay(config).run(events)


def run_walk_forward_replay(
    events: Sequence[HistoricalMarketEvent],
    variants: Sequence[ReplayParameterVariant],
    window_config: EventWalkForwardConfig,
) -> ReplayWalkForwardResult:
    """Select each fold's variant on train events, then replay untouched OOS events.

    Each train and test window gets a fresh runner.  Purge and embargo are
    expressed in event time by ``walk_forward_event_splits``.  No variant is
    selected from test output, and the selection artifact hash is recomputed
    from the train data/config/scores rather than accepted from a caller.
    """

    normalized_events = _validate_event_sequence(events)
    normalized_variants = list(variants)
    if not normalized_variants:
        raise ReplayValidationError("walk-forward replay requires at least one parameter variant")
    variant_ids = [variant.variant_id for variant in normalized_variants]
    if len(set(variant_ids)) != len(variant_ids):
        raise ReplayValidationError("walk-forward variant IDs must be unique")

    first_config = normalized_variants[0].config
    for variant in normalized_variants[1:]:
        if (
            variant.config.initial_capital != first_config.initial_capital
            or variant.config.cost_model != first_config.cost_model
        ):
            raise ReplayValidationError(
                "walk-forward variants must share initial capital and economic cost model"
            )

    folds = walk_forward_event_splits(normalized_events, window_config)
    fold_results: list[ReplayWalkForwardFoldResult] = []
    oos_trades: list[BacktestTrade] = []
    for fold in folds:
        train_events = normalized_events[fold.train_start : fold.train_end]
        test_events = normalized_events[fold.test_start : fold.test_end]
        train_scores: dict[str, Decimal | None] = {}
        for variant in normalized_variants:
            train_result = run_replay(train_events, variant.config)
            train_scores[variant.variant_id] = (
                train_result.economic_result.net_pnl
                if train_result.economic_result is not None
                else None
            )
        eligible = [
            (variant_id, score)
            for variant_id, score in train_scores.items()
            if score is not None
        ]
        if not eligible:
            raise ReplayValidationError(
                f"walk-forward fold {fold.fold_index} has no train replay economics"
            )
        # Select by descending train net PnL, then variant ID for deterministic
        # tie-breaking. Test output is deliberately not part of selection.
        selected_variant_id = min(
            eligible,
            key=lambda item: (-item[1], item[0]),
        )[0]
        selected_variant = next(
            variant for variant in normalized_variants if variant.variant_id == selected_variant_id
        )
        selection_artifact_sha256 = _canonical_hash(
            {
                "fold_index": fold.fold_index,
                "train_event_ids": [event.event_id for event in train_events],
                "train_dataset_sha256": _canonical_hash(train_events),
                "variant_config_sha256": {
                    variant.variant_id: _canonical_hash(variant.config)
                    for variant in normalized_variants
                },
                "train_net_pnl_by_variant": train_scores,
                "selected_variant_id": selected_variant_id,
            }
        )
        test_result = run_replay(test_events, selected_variant.config)
        test_trades = [
            trade.model_copy(
                update={"trade_id": f"WF{fold.fold_index}-{trade.trade_id}"}
            )
            for trade in test_result.trades
        ]
        oos_trades.extend(test_trades)
        fold_results.append(
            ReplayWalkForwardFoldResult(
                fold_index=fold.fold_index,
                train_start=fold.train_start,
                train_end=fold.train_end,
                test_start=fold.test_start,
                test_end=fold.test_end,
                selected_variant_id=selected_variant_id,
                train_net_pnl_by_variant=train_scores,
                selection_artifact_sha256=selection_artifact_sha256,
                test_trade_count=len(test_trades),
                test_net_pnl=(
                    test_result.economic_result.net_pnl
                    if test_result.economic_result is not None
                    else None
                ),
            )
        )

    oos_economic_result = (
        evaluate_trades(
            oos_trades,
            initial_capital=first_config.initial_capital,
            cost_model=first_config.cost_model,
        )
        if oos_trades
        else None
    )
    return ReplayWalkForwardResult(
        dataset_sha256=_canonical_hash(normalized_events),
        variant_ids=tuple(variant_ids),
        folds=tuple(fold_results),
        oos_trades=tuple(oos_trades),
        oos_economic_result=oos_economic_result,
    )
