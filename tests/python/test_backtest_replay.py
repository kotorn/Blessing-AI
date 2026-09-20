from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from apps.trading_worker.backtest.economic import EconomicCostModel
from apps.trading_worker.backtest.replay import (
    EventWalkForwardConfig,
    HistoricalMarketEvent,
    ReplayExecutionConfig,
    ReplayExecutionError,
    ReplayParameterVariant,
    ReplaySymbolRules,
    ReplayValidationError,
    run_replay,
    run_walk_forward_replay,
    walk_forward_event_splits,
)
from domain.enums import MarketType

START = datetime(2026, 1, 1, tzinfo=UTC)


def _config(*, force_close_at_end: bool = True) -> ReplayExecutionConfig:
    return ReplayExecutionConfig(
        initial_capital=Decimal(1000),
        cost_model=EconomicCostModel(
            maker_fee_rate=Decimal("0.0002"),
            taker_fee_rate=Decimal("0.0005"),
        ),
        market_slippage_bps=Decimal(2),
        funding_interval_sec=3600,
        enabled_strategies=("shock",),
        symbol_rules=(
            ReplaySymbolRules(
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
                step_size=Decimal("0.001"),
                min_quantity=Decimal("0.001"),
                min_notional=Decimal(5),
            ),
        ),
        force_close_at_end=force_close_at_end,
    )


def _event(
    index: int,
    close: str,
    *,
    minutes_after_start: int | None = None,
    funding_rate: str | None = None,
    funding_event: bool = False,
    ask_qty: str = "1",
    bid_qty: str = "1",
) -> HistoricalMarketEvent:
    timestamp = START + timedelta(
        minutes=minutes_after_start if minutes_after_start is not None else index
    )
    close_price = Decimal(close)
    return HistoricalMarketEvent(
        event_id=f"E-{index}",
        event_time=timestamp,
        symbol="BTCUSDT",
        venue="BINANCE_TESTNET",
        market_type=MarketType.USDM_FUTURES,
        open=close_price,
        high=close_price,
        low=close_price,
        close=close_price,
        volume=Decimal(10),
        trade_count=10,
        best_bid=close_price - Decimal("0.1"),
        best_ask=close_price + Decimal("0.1"),
        bid_qty=Decimal(bid_qty),
        ask_qty=Decimal(ask_qty),
        mark_price=close_price,
        funding_rate=Decimal(funding_rate) if funding_rate is not None else None,
        funding_event=funding_event,
        data_source="BINANCE_PUBLIC_TESTNET_READ_ONLY",
    )


def _open_then_close_events(*, funding: bool = True):
    # The existing ShockStrategy is deliberately used as-is.  A positive
    # displacement opens a small long; a later negative displacement creates
    # a smaller signed reduction.  No new alpha is introduced by the replay.
    return [
        _event(0, "100", minutes_after_start=0),
        _event(1, "102", minutes_after_start=1),
        _event(
            2,
            "99",
            minutes_after_start=61,
            funding_rate="0.001" if funding else None,
            funding_event=funding,
        ),
    ]


def test_historical_event_requires_quotes_depth_and_valid_ohlc():
    with pytest.raises(ValidationError, match="OHLC"):
        HistoricalMarketEvent(
            event_id="bad",
            event_time=START,
            symbol="BTCUSDT",
            venue="BINANCE_TESTNET",
            market_type=MarketType.USDM_FUTURES,
            open=Decimal(100),
            high=Decimal(99),
            low=Decimal(98),
            close=Decimal(100),
            volume=Decimal(1),
            trade_count=1,
            best_bid=Decimal(99),
            best_ask=Decimal(101),
            bid_qty=Decimal(1),
            ask_qty=Decimal(1),
            mark_price=Decimal(100),
            data_source="BINANCE_PUBLIC_TESTNET_READ_ONLY",
        )

    with pytest.raises(ValidationError, match="funding_event requires"):
        _event(1, "100", funding_event=True)


def test_replay_runs_existing_pipeline_and_accounts_explicit_costs_and_funding():
    result = run_replay(_open_then_close_events(), _config())

    assert len(result.fills) == 2
    assert len(result.trades) == 1
    assert result.strategy_intents
    assert result.target_exposures
    assert len(result.risk_snapshots) == len(result.execution_decisions)
    assert result.execution_decisions
    assert len(result.equity_curve) == result.event_count
    assert all(fill.decision_id for fill in result.fills)
    assert all(fill.target_exposure_id is not None for fill in result.fills)
    assert result.final_position_qty == Decimal(0)
    assert result.open_position_at_end == Decimal(0)
    assert result.economic_result is not None
    assert result.economic_result.funding_pnl < 0
    assert result.economic_result.trading_fees > 0
    assert result.economic_result.spread_cost > 0
    assert result.economic_result.slippage_cost > 0
    assert result.evidence_status == "RESEARCH_REPLAY_ONLY"
    assert result.launch_eligible is False


def test_replay_is_deterministic_for_same_events_and_config():
    events = _open_then_close_events()
    first = run_replay(events, _config())
    second = run_replay(events, _config())

    assert first.model_dump() == second.model_dump()
    assert first.dataset_sha256 == second.dataset_sha256
    assert first.config_sha256 == second.config_sha256
    assert [fill.client_order_id for fill in first.fills] == [
        fill.client_order_id for fill in second.fills
    ]


def test_replay_rejects_insufficient_recorded_depth_without_fallback_price():
    events = [
        _event(0, "100"),
        _event(1, "102", ask_qty="0.01"),
    ]

    result = run_replay(events, _config(force_close_at_end=False))

    assert not result.fills
    assert any(
        "depth" in decision.reason.lower() for decision in result.decisions if not decision.accepted
    )


def test_unknown_liquidation_safety_blocks_risk_increase_after_entry():
    events = [
        _event(0, "100"),
        _event(1, "102"),
        _event(2, "104"),
    ]

    result = run_replay(events, _config(force_close_at_end=False))

    # One initial entry is allowed while flat.  Once a position exists, the
    # replay has no authoritative liquidation price and must fail closed.
    assert len(result.fills) == 1
    assert result.open_position_at_end > 0
    assert any(
        "risk state" in decision.reason.lower() and not decision.accepted
        for decision in result.decisions
    )


def test_missing_funding_settlement_fails_closed():
    events = [
        _event(0, "100"),
        _event(1, "102"),
        _event(2, "99", minutes_after_start=61),
    ]

    with pytest.raises(ReplayValidationError, match="missing funding event"):
        run_replay(events, _config())


def test_explicit_end_of_sample_close_is_required_for_complete_result():
    result = run_replay(
        [_event(0, "100"), _event(1, "102")],
        _config(force_close_at_end=False),
    )

    assert result.open_position_at_end > 0
    assert result.economic_result is None


def test_force_close_uses_recorded_depth_and_fails_closed_if_unfillable():
    events = [
        _event(0, "100"),
        _event(1, "102"),
        _event(2, "104", ask_qty="1", bid_qty="0.01"),
    ]

    with pytest.raises(ReplayExecutionError, match="end-of-sample close"):
        run_replay(events, _config())


def test_event_time_walk_forward_has_purge_and_embargo_gaps():
    events = [_event(index, str(100 + index)) for index in range(12)]
    folds = walk_forward_event_splits(
        events,
        EventWalkForwardConfig(
            train_duration_sec=180,
            test_duration_sec=120,
            purge_duration_sec=60,
            embargo_duration_sec=60,
        ),
    )

    assert folds
    first = folds[0]
    assert first.train_end <= first.test_start
    assert first.train_end_time <= first.test_start_time
    assert first.test_end <= len(events)


def test_walk_forward_replay_selects_on_train_and_evaluates_untouched_oos():
    events = [_event(index, str(100 + index)) for index in range(12)]
    baseline = ReplayParameterVariant(variant_id="baseline", config=_config())
    higher_slippage = ReplayParameterVariant(
        variant_id="higher-slippage",
        config=_config().model_copy(update={"market_slippage_bps": Decimal(4)}),
    )

    result = run_walk_forward_replay(
        events,
        [baseline, higher_slippage],
        EventWalkForwardConfig(
            train_duration_sec=120,
            test_duration_sec=120,
            purge_duration_sec=60,
            embargo_duration_sec=60,
        ),
    )

    assert len(result.folds) >= 2
    assert all(fold.selected_variant_id == "baseline" for fold in result.folds)
    assert all(len(fold.selection_artifact_sha256) == 64 for fold in result.folds)
    assert result.oos_trades
    assert result.oos_economic_result is not None
    assert result.evidence_status == "RESEARCH_WALK_FORWARD_ONLY"
    assert result.launch_eligible is False

    repeat = run_walk_forward_replay(
        events,
        [baseline, higher_slippage],
        EventWalkForwardConfig(
            train_duration_sec=120,
            test_duration_sec=120,
            purge_duration_sec=60,
            embargo_duration_sec=60,
        ),
    )
    assert result.model_dump() == repeat.model_dump()


def test_replay_is_research_only_and_does_not_import_binance_execution():
    source = open("apps/trading_worker/backtest/replay.py", encoding="utf-8").read()

    assert "BinanceExecutionAdapter" not in source
    assert "rest_client" not in source
    assert "websockets" not in source
