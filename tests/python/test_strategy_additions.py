from datetime import datetime, UTC
from decimal import Decimal

from apps.trading_worker.strategies import (
    base,
    breakout_confirm,
    range_fade,
    shock_momentum,
    structural_grid,
    trend_breakout,
)
from domain.enums import RegimeType
from domain.models import MarketState, MarketType, PositionSide, PriceActionState


def test_new_research_strategies_are_explicitly_research_only():
    for module in (base, shock_momentum, structural_grid, trend_breakout, range_fade, breakout_confirm):
        assert module.RESEARCH_ONLY is True


def _market_state(symbol="BTCUSDT", regime=RegimeType.R1_RANGE, volatility_zscore="0.5", shock_active=False):
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


def test_range_fade_enters_only_in_calm_mean_reverting_states():
    from apps.trading_worker.strategies.range_fade import RangeFadeEngine

    engine = RangeFadeEngine("range_fade", "BTCUSDT")
    calm = _market_state(regime=RegimeType.R1_RANGE, volatility_zscore="0.5")
    intent = engine.evaluate(calm, Decimal("0"))
    assert intent is not None
    assert intent.strategy_id == "range_fade"
    assert intent.market_type == MarketType.USDM_FUTURES
    assert intent.desired_delta_qty > 0
    assert intent.opportunity_score >= Decimal("0.5")

    stormy = _market_state(regime=RegimeType.R1_RANGE, volatility_zscore="9.0")
    assert engine.evaluate(stormy, Decimal("0")) is None

    trending = _market_state(regime=RegimeType.R3_STRONG_TREND, volatility_zscore="0.2")
    assert engine.evaluate(trending, Decimal("0")) is None

    capped = _market_state(regime=RegimeType.R0_STRONG_MEAN_REVERSION, volatility_zscore="0.1")
    assert engine.evaluate(capped, Decimal("10")) is None

    wrong_symbol = _market_state(symbol="ETHUSDT", regime=RegimeType.R1_RANGE)
    assert engine.evaluate(wrong_symbol, Decimal("0")) is None


def test_breakout_confirm_requires_a_confirmed_streak():
    from apps.trading_worker.strategies.breakout_confirm import BreakoutConfirmEngine

    engine = BreakoutConfirmEngine("breakout_confirm", "BTCUSDT")
    calm = _market_state(regime=RegimeType.R1_RANGE, volatility_zscore="0.2")
    assert engine.evaluate(calm, Decimal("0")) is None

    first = _market_state(regime=RegimeType.R4_BREAKOUT, volatility_zscore="0.4")
    assert engine.evaluate(first, Decimal("0")) is None

    second = _market_state(regime=RegimeType.R4_BREAKOUT, volatility_zscore="0.4")
    intent = engine.evaluate(second, Decimal("0"))
    assert intent is not None
    assert intent.strategy_id == "breakout_confirm"
    assert intent.market_type == MarketType.USDM_FUTURES
    assert intent.desired_delta_qty == Decimal("0.2")
    assert intent.direction.value == "LONG"

    # A shock-flagged breakout never counts as confirmation.
    shocked = BreakoutConfirmEngine("breakout_confirm", "BTCUSDT")
    flagged = _market_state(
        regime=RegimeType.R4_BREAKOUT, volatility_zscore="0.4", shock_active=True
    )
    assert shocked.evaluate(flagged, Decimal("0")) is None
    assert shocked.evaluate(flagged, Decimal("0")) is None

    # A broken streak flattens remaining attributed exposure instead of adding.
    exiting = BreakoutConfirmEngine("breakout_confirm", "BTCUSDT")
    assert exiting.evaluate(first, Decimal("0")) is None
    assert exiting.evaluate(second, Decimal("0")) is not None
    flat = exiting.evaluate(calm, Decimal("0.2"))
    assert flat is not None
    assert flat.desired_delta_qty == Decimal("-0.2")


def _price_action_state(symbol="BTCUSDT", liquidity_swept=False, sweep_side=None):
    return PriceActionState(
        symbol=symbol,
        timestamp=datetime.now(UTC),
        swing_high=Decimal("100"),
        swing_low=Decimal("90"),
        prior_24h_high=Decimal("105"),
        prior_24h_low=Decimal("88"),
        displacement_velocity_pct=Decimal("0.0"),
        displacement_acceleration=Decimal("0.0"),
        range_expansion_ratio=Decimal("1.0"),
        liquidity_swept=liquidity_swept,
        sweep_side=sweep_side,
    )


def test_range_fade_uses_sweep_side_when_provided():
    from apps.trading_worker.strategies.range_fade import RangeFadeEngine

    engine = RangeFadeEngine("range_fade", "BTCUSDT")
    calm = _market_state(regime=RegimeType.R1_RANGE, volatility_zscore="0.5")
    pa_state = _price_action_state(liquidity_swept=True, sweep_side=PositionSide.SHORT)

    intent = engine.evaluate(calm, Decimal("0"), pa_state=pa_state)
    assert intent is not None
    assert intent.direction == PositionSide.SHORT
    assert intent.desired_delta_qty < 0


def test_range_fade_falls_back_without_sweep_side():
    from apps.trading_worker.strategies.range_fade import RangeFadeEngine

    engine = RangeFadeEngine("range_fade", "BTCUSDT")
    calm = _market_state(regime=RegimeType.R1_RANGE, volatility_zscore="0.5")

    # No pa_state at all.
    intent = engine.evaluate(calm, Decimal("0"))
    assert intent is not None
    assert intent.direction == PositionSide.LONG
    assert intent.desired_delta_qty > 0

    # pa_state provided but with no sweep signal.
    pa_state = _price_action_state(liquidity_swept=False, sweep_side=None)
    intent = engine.evaluate(calm, Decimal("0"), pa_state=pa_state)
    assert intent is not None
    assert intent.direction == PositionSide.LONG
    assert intent.desired_delta_qty > 0


def test_range_fade_two_positional_args_still_works():
    """Regression pin: the pre-existing two-positional-arg call must keep working."""
    from apps.trading_worker.strategies.range_fade import RangeFadeEngine

    engine = RangeFadeEngine("range_fade", "BTCUSDT")
    calm = _market_state(regime=RegimeType.R1_RANGE, volatility_zscore="0.5")
    intent = engine.evaluate(calm, Decimal("0"))
    assert intent is not None


def test_breakout_confirm_streak_resets_after_flatten():
    from apps.trading_worker.strategies.breakout_confirm import BreakoutConfirmEngine

    engine = BreakoutConfirmEngine("breakout_confirm", "BTCUSDT")
    calm = _market_state(regime=RegimeType.R1_RANGE, volatility_zscore="0.2")
    first = _market_state(regime=RegimeType.R4_BREAKOUT, volatility_zscore="0.4")
    second = _market_state(regime=RegimeType.R4_BREAKOUT, volatility_zscore="0.4")

    assert engine.evaluate(first, Decimal("0")) is None
    intent = engine.evaluate(second, Decimal("0"))
    assert intent is not None
    assert intent.desired_delta_qty == Decimal("0.2")

    # Streak breaks (calm tick) and flattens the attributed position.
    flat = engine.evaluate(calm, Decimal("0.2"))
    assert flat is not None
    assert flat.desired_delta_qty == Decimal("-0.2")

    # A single fresh R4 tick must NOT immediately re-enter: the counter
    # restarted at zero rather than resuming at the prior streak count.
    assert engine.evaluate(first, Decimal("0")) is None


def test_breakout_confirm_high_volatility_does_not_reset_streak():
    from apps.trading_worker.strategies.breakout_confirm import BreakoutConfirmEngine

    engine = BreakoutConfirmEngine("breakout_confirm", "BTCUSDT")
    first = _market_state(regime=RegimeType.R4_BREAKOUT, volatility_zscore="0.4")
    volatile_second = _market_state(regime=RegimeType.R4_BREAKOUT, volatility_zscore="9.0")

    assert engine.evaluate(first, Decimal("0")) is None
    # Second confirmation arrives, but volatility is above the ceiling: no
    # intent is emitted, but the streak count is preserved (not reset),
    # since this event still qualifies as a consecutive R4 observation.
    assert engine.evaluate(volatile_second, Decimal("0")) is None
    calm_confirmation = _market_state(regime=RegimeType.R4_BREAKOUT, volatility_zscore="0.4")
    intent = engine.evaluate(calm_confirmation, Decimal("0"))
    assert intent is not None
