from pathlib import Path


def test_experimental_strategy_stack_is_explicitly_research_only():
    from apps.trading_worker.engines import portfolio_risk
    from apps.trading_worker.strategies import (
        base,
        shock_momentum,
        structural_grid,
        trend_breakout,
    )

    for module in (base, shock_momentum, structural_grid, trend_breakout, portfolio_risk):
        assert module.RESEARCH_ONLY is True


def test_production_bootstrap_does_not_import_research_strategy_stack():
    source = Path("apps/trading_worker/main.py").read_text(encoding="utf-8")

    assert "apps.trading_worker.strategies" not in source
    assert "apps.trading_worker.engines.portfolio_risk" not in source
