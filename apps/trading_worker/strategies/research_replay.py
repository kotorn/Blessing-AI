"""Deterministic intents/positions replay for the research-only strategy stack.

This is intentionally separate from ``apps.trading_worker.backtest.replay``
(``DeterministicReplay``), which drives the four *production* engines under
``apps.trading_worker.engines``. Nothing here is imported by that module or by
production execution, and this module never touches it.

Scope: this harness replays an ordered sequence of ``(MarketState,
PriceActionState | None)`` events through one or more of the research
engines and records the resulting intents and running virtual position per
strategy. It is a visibility/regression tool, not a PnL or cost-model
backtest — ``MarketState``/``PriceActionState`` carry no price field today,
and a defensible PnL model needs a price source plus a cost model
(spread/fee/funding). That belongs to a later, separately-scoped phase.
"""

from __future__ import annotations

RESEARCH_ONLY = True

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple, Type

from domain.models import MarketState, PriceActionState, StrategyIntent

from .base import BaseAlphaEngine
from .breakout_confirm import BreakoutConfirmEngine
from .range_fade import RangeFadeEngine
from .shock_momentum import ShockMomentumEngine
from .structural_grid import StructuralGridEngine
from .trend_breakout import TrendBreakoutEngine

STRATEGY_REGISTRY: Dict[str, Type[BaseAlphaEngine]] = {
    "structural_grid": StructuralGridEngine,
    "shock_momentum": ShockMomentumEngine,
    "trend_breakout": TrendBreakoutEngine,
    "range_fade": RangeFadeEngine,
    "breakout_confirm": BreakoutConfirmEngine,
}


@dataclass(frozen=True)
class ResearchReplayRecord:
    timestamp: datetime
    strategy_id: str
    symbol: str
    intent: Optional[StrategyIntent]
    position_after: Decimal


def run_research_replay(
    events: Sequence[Tuple[MarketState, Optional[PriceActionState]]],
    enabled_strategies: Sequence[str],
    symbol: str,
    *,
    engine_kwargs: Optional[Dict[str, dict]] = None,
) -> List[ResearchReplayRecord]:
    """Replay ``events`` through the named research engines for ``symbol``.

    :param events: ordered ``(market_state, price_action_state)`` pairs. The
        price-action state may be ``None`` for events where it isn't
        available; engines that don't use it simply ignore it.
    :param enabled_strategies: names from :data:`STRATEGY_REGISTRY` to run.
    :param symbol: the symbol every engine instance is scoped to.
    :param engine_kwargs: optional per-strategy constructor kwargs (e.g.
        ``{"range_fade": {"max_virtual_position": Decimal("1.0")}}``).
    :raises KeyError: if a name in ``enabled_strategies`` isn't registered.
    """
    unknown = [name for name in enabled_strategies if name not in STRATEGY_REGISTRY]
    if unknown:
        raise KeyError(f"unknown research strategy name(s): {unknown}")

    engine_kwargs = engine_kwargs or {}
    engines: Dict[str, BaseAlphaEngine] = {
        name: STRATEGY_REGISTRY[name](name, symbol, **engine_kwargs.get(name, {}))
        for name in enabled_strategies
    }
    positions: Dict[str, Decimal] = {name: Decimal("0") for name in enabled_strategies}

    records: List[ResearchReplayRecord] = []
    for market_state, pa_state in events:
        for name, engine in engines.items():
            intent = engine.evaluate(market_state, positions[name], pa_state=pa_state)
            if intent is not None:
                positions[name] = positions[name] + intent.desired_delta_qty
            records.append(
                ResearchReplayRecord(
                    timestamp=market_state.timestamp,
                    strategy_id=name,
                    symbol=symbol,
                    intent=intent,
                    position_after=positions[name],
                )
            )
    return records
