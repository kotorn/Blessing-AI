"""Typed, validated configuration loaders for research/backtest use.

Nothing in this package is imported by the production execution path
(``apps.trading_worker.main``, ``apps.trading_worker.engines``,
``apps.trading_worker.venues``). Wiring a loader here into production
risk/strategy defaults is a separate, explicitly-reviewed change.
"""
