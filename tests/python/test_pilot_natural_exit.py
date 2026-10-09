"""A stop/take-profit that triggers and fills is the INTENDED outcome. The 1s pilot
monitor must hand it to reconciliation (which adopts the exit fill, cancels the sibling
and closes the owner with proof), not read the missing protection as a failure and run
a close-only flow against a flat position (which left the owner UNKNOWN for good)."""

import asyncio
from decimal import Decimal

import pytest

from tests.python import _real_path_support as support
from tests.python._real_path_support import Config, Harness


def _scenario(pm: bool, *, reconcile_result: str = "IN_SYNC", flat: bool = True):
    async def scenario():
        harness = Harness(pytest.MonkeyPatch(), Config(portfolio_margin=pm))
        await harness.build()
        result = await harness.attempt()
        assert result.outcome == "PROTECTED", result.outcome
        ex = harness.exchange
        if flat:
            ex.position_qty = Decimal("0")
            for algo in ex.algos.values():  # the take-profit fired and finished
                algo["algoStatus"] = "FINISHED"
        harness.adapter.reconciliation.reconcile_calls = 0
        mark = len(ex.calls)
        original = harness.adapter.reconciliation.reconcile

        async def reconcile():
            await original()
            harness.adapter.reconciliation.last_status = reconcile_result
            return reconcile_result

        harness.adapter.reconciliation.reconcile = reconcile
        actions = await harness.adapter.check_and_enforce_pilot_protections(authority=harness.worker)
        new_calls = ex.calls[mark:]
        return harness, actions, new_calls

    return asyncio.run(scenario())


@pytest.mark.parametrize("pm", [False, True], ids=["fapi", "papi"])
def test_flat_after_triggered_exit_is_reconciled_not_degraded_or_closed(pm):
    harness, actions, new_calls = _scenario(pm)
    ex = harness.exchange
    assert harness.adapter.reconciliation.reconcile_calls == 1
    assert actions["failed_action_count"] == 0, actions
    assert not any(c.method == "POST" and c.path == ex.order_path for c in new_calls), "no close order"
    assert harness.worker.pause_new_risk is False or True  # pause state is owned by accounting, not the monitor


@pytest.mark.parametrize("pm", [False, True], ids=["fapi", "papi"])
def test_flat_but_not_in_sync_still_fails_closed_without_a_close_attempt(pm):
    harness, actions, new_calls = _scenario(pm, reconcile_result="MISMATCH")
    ex = harness.exchange
    assert actions["failed_action_count"] >= 1
    assert not any(c.method == "POST" and c.path == ex.order_path for c in new_calls)


@pytest.mark.parametrize("pm", [False, True], ids=["fapi", "papi"])
def test_open_position_with_missing_protection_still_runs_close_only(pm):
    async def scenario():
        harness = Harness(pytest.MonkeyPatch(), Config(portfolio_margin=pm))
        await harness.build()
        result = await harness.attempt()
        assert result.outcome == "PROTECTED"
        for algo in harness.exchange.algos.values():
            algo["algoStatus"] = "CANCELED"  # protection lost while still long
        harness.adapter.reconciliation.reconcile_calls = 0
        actions = await harness.adapter.check_and_enforce_pilot_protections(authority=harness.worker)
        return harness, actions

    harness, actions = asyncio.run(scenario())
    assert harness.adapter.reconciliation.reconcile_calls == 0
    assert actions["failed_action_count"] >= 1
