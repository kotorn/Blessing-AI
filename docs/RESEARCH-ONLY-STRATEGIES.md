# Research-only strategy stack

`apps/trading_worker/strategies/*` and `apps/trading_worker/engines/portfolio_risk.py`
are retained for offline comparison only. They are marked `RESEARCH_ONLY = True`
and are not imported by `apps/trading_worker/main.py`.

The production path is the deterministic pipeline in `apps/trading_worker/engines`:

`MarketEvent -> PriceAction -> MarketState -> StrategyIntent -> MetaAllocator -> Recovery -> RiskGovernor -> ExecutionDecision`

Research modules must not place orders, authorize risk, or become an implicit
replacement for that pipeline. A migration requires a separate reviewed change
and dedicated compatibility tests.
