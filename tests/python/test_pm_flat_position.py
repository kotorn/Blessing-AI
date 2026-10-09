"""Portfolio Margin positionRisk returns no ETHUSDC row when the account is flat
(the repo's own real-account fixtures return []). Zero rows must mean flat, not
'identity is not unique', or the 1 s monitor re-degrades the worker forever."""

import asyncio
from decimal import Decimal

import pytest

from apps.trading_worker.venues.binance.execution import BinanceExecutionAdapter as A
from tests.python import _real_path_support as support
from tests.python._real_path_support import Config, Harness


def test_helper_returns_zero_row_only_for_portfolio_margin():
    pm = A._position_rows_for_symbol([], "ETHUSDC", portfolio_margin=True)
    assert len(pm) == 1 and Decimal(pm[0]["positionAmt"]) == 0 and Decimal(pm[0]["unRealizedProfit"]) == 0
    assert A._position_rows_for_symbol([], "ETHUSDC", portfolio_margin=False) == []


def test_helper_keeps_real_rows_and_duplicates():
    row = {"symbol": "ETHUSDC", "positionSide": "BOTH", "positionAmt": "0.019", "unRealizedProfit": "0.1"}
    assert A._position_rows_for_symbol([row], "ETHUSDC", portfolio_margin=True) == [row]
    assert len(A._position_rows_for_symbol([row, dict(row)], "ETHUSDC", portfolio_margin=True)) == 2
    other = {"symbol": "BTCUSDC", "positionSide": "BOTH", "positionAmt": "1"}
    assert A._position_rows_for_symbol([other], "ETHUSDC", portfolio_margin=True)[0]["symbol"] == "ETHUSDC"


@pytest.mark.parametrize("pm", [True])
def test_flat_pm_account_is_flat_and_monitor_does_not_degrade(monkeypatch, pm):
    async def scenario():
        harness = Harness(monkeypatch, Config(portfolio_margin=pm))
        await harness.build()
        harness.exchange.flat_positions_empty = True
        harness.exchange.position_qty = Decimal("0")
        decision = support.make_decision()
        intent = decision.orders[0]
        flat = await harness.adapter._local_mainnet_position_is_flat(intent)
        actions = await harness.adapter.check_and_enforce_pilot_protections(authority=harness.worker)
        return flat, actions

    flat, actions = asyncio.run(scenario())
    assert flat is True
    assert actions["failed_action_count"] == 0, actions
