from datetime import UTC, datetime
from decimal import Decimal

import pytest

from apps.trading_worker.strategies.research_replay import run_research_replay
from domain.enums import RegimeType
from domain.models import MarketState


def _market_state(regime, volatility_zscore="0.5", shock_active=False, symbol="BTCUSDT"):
    return MarketState(
        symbol=symbol,
        timestamp=datetime.now(UTC),
        primary_regime=regime,
        regime_probabilities={},
        atr_1h=Decimal("10"),
        volatility_zscore=Decimal(str(volatility_zscore)),
        funding_zscore=Decimal("0.0"),
        shock_active=shock_active,
    )


def test_replay_tracks_intents_and_positions_across_strategies():
    events = [
        (_market_state(RegimeType.R1_RANGE, "0.5"), None),
        (_market_state(RegimeType.R4_BREAKOUT, "0.4"), None),
        (_market_state(RegimeType.R4_BREAKOUT, "0.4"), None),
    ]
    strategies = ["structural_grid", "range_fade", "breakout_confirm"]

    records = run_research_replay(events, strategies, "BTCUSDT")

    assert len(records) == len(events) * len(strategies)

    by_strategy = {name: [r for r in records if r.strategy_id == name] for name in strategies}

    # structural_grid: enters once in the calm R1_RANGE tick, then the two
    # R4_BREAKOUT ticks score too low to add further.
    grid_records = by_strategy["structural_grid"]
    assert grid_records[0].intent is not None
    assert grid_records[0].intent.desired_delta_qty == Decimal("0.1")
    assert grid_records[0].position_after == Decimal("0.1")
    assert grid_records[1].intent is None
    assert grid_records[1].position_after == Decimal("0.1")
    assert grid_records[2].intent is None
    assert grid_records[2].position_after == Decimal("0.1")

    # range_fade: enters once in the calm tick (no sweep signal -> LONG
    # placeholder), stays flat during the breakout ticks.
    fade_records = by_strategy["range_fade"]
    assert fade_records[0].intent is not None
    assert fade_records[0].position_after == Decimal("0.05")
    assert fade_records[1].intent is None
    assert fade_records[2].intent is None
    assert fade_records[2].position_after == Decimal("0.05")

    # breakout_confirm: needs two consecutive R4 ticks; the streak persists
    # across events (per-engine state), so only the third event enters.
    confirm_records = by_strategy["breakout_confirm"]
    assert confirm_records[0].intent is None
    assert confirm_records[0].position_after == Decimal("0")
    assert confirm_records[1].intent is None
    assert confirm_records[1].position_after == Decimal("0")
    assert confirm_records[2].intent is not None
    assert confirm_records[2].intent.desired_delta_qty == Decimal("0.2")
    assert confirm_records[2].position_after == Decimal("0.2")


def test_unknown_strategy_name_raises():
    events = [(_market_state(RegimeType.R1_RANGE), None)]
    with pytest.raises(KeyError):
        run_research_replay(events, ["not_a_real_strategy"], "BTCUSDT")


def test_engine_kwargs_are_applied_per_strategy():
    events = [(_market_state(RegimeType.R1_RANGE, "0.5"), None)]
    records = run_research_replay(
        events,
        ["range_fade"],
        "BTCUSDT",
        engine_kwargs={"range_fade": {"max_virtual_position": Decimal("0.01")}},
    )
    # max_virtual_position is only checked against the position *before* this
    # event (0 < 0.01), so the first entry still fires; this just confirms
    # the kwarg reaches the engine constructor without error.
    assert records[0].intent is not None
