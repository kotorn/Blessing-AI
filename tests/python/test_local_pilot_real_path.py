"""ONE real Local Live Research Pilot entry attempt through the REAL production chain.

Chain under test (nothing in it is mocked; only the HTTP transport, persistence,
lease, user stream and the ``reconciliation`` object are fakes -- see
``_real_path_support``):

    TradingWorkerApp._clamp_order_notional_if_needed
      -> TradingWorkerApp._evaluate_execution_gate        (DecisionExecutionGate)
      -> TradingWorkerApp.execute_manual_decision
      -> BinanceExecutionAdapter.execute_decision
           OrderExecutionGate #1 (+ cost evidence), durable owner/outbox,
           _final_risk_increase_fence == OrderExecutionGate #2 (+ cost evidence),
           order POST, _protect_local_mainnet_entry (algo placement + read-back)

Goal: measure whether one entry can finish inside the market-data freshness
window (MAX_MARKET_DATA_AGE_SEC, default 3.0 s) when every REST call has a
realistic round-trip latency.  Time is REAL (``asyncio.sleep``); latencies are
chosen >= 20 % away from every measured boundary so the tests stay stable.

Measured boundaries (per-call round trip, both PM modes identical):
  * REST-only market data (no WS frames):  entry first dies at ~1.0 s/call
        ("Market data stale for ETHUSDC" at the fence's final worker gate;
        three sequential cost-evidence hops: clamp, gate #1, fence).
  * WS-fed market data (production-like):  the 3 s window is not what binds;
        the post-fill 5 s protection window dies first at ~0.5 s/call
        (POST + ~9 sequential reads/writes) -> unprotected fill, close-only flow.
        Pre-POST then survives until ~2.5 s/call (account snapshot > 5 s old).

The three production defects this harness originally found (cost-evidence
context keys lost in the clamp and gate, negative fundingInfo floor rejected,
pilot hash binding never matching) are fixed; the tests that isolated them
stay as regression guards and run WITHOUT any bypass.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest

from apps.trading_worker.venues.binance.pilot_bracket import (
    apply_pilot_bracket_to_intent,
    plan_pilot_bracket,
)
from domain.enums import OrderSide
from tests.python import _real_path_support as support
from tests.python._real_path_support import Config, Harness

PM = pytest.mark.parametrize("pm", [False, True], ids=["fapi", "papi"])
COST_ROUTES = {
    False: ["GET /fapi/v1/commissionRate", "GET /fapi/v1/depth", "GET /fapi/v1/fundingRate",
            "GET /fapi/v1/fundingInfo", "GET /fapi/v1/leverageBracket"],
    True: ["GET /papi/v1/um/commissionRate", "GET /fapi/v1/depth", "GET /fapi/v1/fundingRate",
           "GET /fapi/v1/fundingInfo", "GET /papi/v1/um/leverageBracket"],
}


def run(monkeypatch, pm, **cfg):
    return support.run(monkeypatch, portfolio_margin=pm, **cfg)


def assert_clean(result, harness):
    assert result.unhandled == [], "attempt hit a route the fake exchange does not script"
    assert harness.exchange.unhandled == []


# --------------------------------------------------------------------------
# (c) 0 latency, fresh book: the PURE production path (regression guards)
# --------------------------------------------------------------------------
@PM
def test_pure_production_path_posts_an_entry_at_zero_latency(monkeypatch, pm):
    result, harness = run(monkeypatch, pm)
    assert result.order_posted, f"{result.outcome}: {result.reason}"


@PM
def test_defect_clamp_loses_validated_keys_from_risk_context(monkeypatch, pm):
    result, _ = run(monkeypatch, pm)
    assert result.outcome != "PREPLAN_FAILED", result.reason


@PM
def test_defect_cost_evidence_rejects_real_negative_funding_floor(monkeypatch, pm):
    async def scenario():
        harness = Harness(monkeypatch, Config(portfolio_margin=pm))
        await harness.build()
        decision = support.make_decision()
        plan = plan_pilot_bracket(
            rules=harness.adapter.symbol_rules[support.SYMBOL],
            entry_price=Decimal("2610.59"), side=OrderSide.BUY)
        intent = apply_pilot_bracket_to_intent(decision.orders[0], plan, basket_id="pilot-direct")
        return await harness.adapter.get_local_mainnet_cost_evidence(
            intent, {"runtime_target": "LOCAL", "validated_quantity": plan.quantity,
                     "validated_entry_price": plan.entry_price})

    assert asyncio.run(scenario()) is not None


@PM
def test_defect_gate_pilot_hash_binding_can_never_match(monkeypatch, pm):
    result, _ = run(monkeypatch, pm)
    assert result.order_posted, f"{result.outcome}: {result.reason}"


# --------------------------------------------------------------------------
# (a) call inventory + 0-latency success once the three defects are bypassed
# --------------------------------------------------------------------------
@PM
@pytest.mark.parametrize("ws", ["live", "none"])
def test_zero_latency_entry_reaches_protected_with_shims(monkeypatch, pm, ws):
    result, harness = run(monkeypatch, pm, shim=True, ws=ws)
    assert_clean(result, harness)
    assert result.outcome == "PROTECTED", f"{result.outcome}: {result.reason} {result.log[-3:]}"
    assert result.order_posted and result.protection_started and result.protected
    assert result.fail_closed == [] and result.provider_none == []

    # Exactly the three cost-evidence bundles, one per phase; no book/mark REST
    # call because the quote taken at ARM / from the WS stream is still fresh.
    expected = sorted(COST_ROUTES[pm])
    for phase in ("clamp", "gate1", "fence"):
        assert sorted(result.route_counts(phase).elements()) == expected, phase
        assert result.hops(phase) == 1, f"{phase}: the five reads must be gathered (one hop)"
    pre_post = {c.path for c in result.calls if c.phase in {"clamp", "gate1", "fence", "post_fence"}}
    assert "/fapi/v1/ticker/bookTicker" not in pre_post
    assert "/fapi/v1/premiumIndex" not in pre_post

    # Exactly one order POST, issued after the fence (gate #2) and before any algo POST.
    order_path = harness.exchange.order_path
    assert [c.path for c in result.calls if c.method == "POST" and c.path == order_path] == [order_path]
    assert result.phase_calls("post_fence") == 1
    assert result.marks["gate1_end"] <= result.marks["fence_start"] <= result.marks["fence_end"]
    first_algo = min(c.seq for c in result.calls if c.path == harness.exchange.algo_post_path)
    order_seq = next(c.seq for c in result.calls if c.method == "POST" and c.path == order_path)
    assert order_seq < first_algo

    # Post-POST (protection + post-mutation reconcile): algo POST x2 (stop, target),
    # each confirmed by an algo query, then the position/algo read-back.
    post = result.route_counts("post")
    assert post[f"POST {harness.exchange.algo_post_path}"] == 2
    assert post[f"GET {harness.exchange.algo_query_path}"] == 4  # 2 confirms + 2 read-back
    assert post[f"GET {harness.exchange.position_path}"] >= 1


@PM
def test_zero_latency_real_quantity_and_bracket_are_sane(monkeypatch, pm):
    result, harness = run(monkeypatch, pm, shim=True)
    posted = harness.exchange.orders_posted[0]
    assert posted["type"] == "MARKET" and posted["side"] == "BUY"
    notional = Decimal(posted["quantity"]) * Decimal("2610.59")
    assert Decimal("20") <= notional <= Decimal("50")  # real MIN_NOTIONAL 20; pilot cap 50
    assert harness.exchange.algos and all(a["reduceOnly"] == "true" for a in harness.exchange.algos.values())


# --------------------------------------------------------------------------
# (b) latency boundaries
# --------------------------------------------------------------------------
@PM
def test_rest_only_market_data_survives_0p7s_calls(monkeypatch, pm):
    # 3 sequential hops x 0.7 s = 2.1 s of the 3.0 s window (boundary ~1.0 s/call).
    result, harness = run(monkeypatch, pm, shim=True, ws="none", latency=0.7, post_latency=0.0)
    assert_clean(result, harness)
    assert result.outcome == "PROTECTED", f"{result.outcome}: {result.reason}"
    assert result.hops("clamp", "gate1", "fence", "post_fence") == 4  # 3 evidence hops + POST


@PM
def test_rest_only_market_data_fails_at_1p2s_calls_with_market_data_stale(monkeypatch, pm):
    result, harness = run(monkeypatch, pm, shim=True, ws="none", latency=1.2, post_latency=0.0)
    assert_clean(result, harness)
    assert not result.order_posted
    assert result.last_order_block is not None
    assert result.last_order_block["stage"] == "FINAL_FENCE"
    assert "Market data stale for ETHUSDC" in result.last_order_block["reason"]
    # The failure is only visible after all three evidence hops (3 x 1.2 s > 3.0 s)...
    assert result.marks["fence_end"] > 3.0
    # ...and a benign fence abort degrades the adapter, which makes the worker
    # run its kill-switch ("fail closed") workflow.
    assert result.adapter_state == "DEGRADED"
    assert result.fail_closed, "execute_manual_decision must trip _fail_closed_after_autonomous_execution_error"


@PM
def test_ws_fed_market_data_is_not_the_binding_window_at_1p2s_calls(monkeypatch, pm):
    # Same 1.2 s calls that kill the REST-only run: WS frames keep both freshness
    # clocks young, so the 3.0 s window is irrelevant when the stream is alive.
    result, harness = run(monkeypatch, pm, shim=True, ws="live", latency=1.2, post_latency=0.0)
    assert_clean(result, harness)
    assert result.order_posted
    assert result.marks["fence_end"] > 3.0


@PM
def test_post_fill_protection_survives_0p3s_calls(monkeypatch, pm):
    result, harness = run(monkeypatch, pm, shim=True, ws="live", latency=0.0, post_latency=0.3)
    assert_clean(result, harness)
    assert result.outcome == "PROTECTED", f"{result.outcome}: {result.reason}"


@PM
def test_post_fill_protection_window_expires_at_0p7s_calls(monkeypatch, pm):
    # The 5 s deadline is armed in before_order_send (before the POST's own latency)
    # and must cover POST + userTrades + 2x(algo POST + query) + 4 read-back calls
    # (~10 sequential round trips): boundary ~0.5 s/call.
    result, harness = run(monkeypatch, pm, shim=True, ws="live", latency=0.0, post_latency=0.7)
    assert_clean(result, harness)
    assert result.order_posted and result.protection_started
    assert not result.protected and result.outcome == "ORDER_POSTED"
    assert "post-fill protection failed" in result.reason
    assert result.adapter_state == "DEGRADED"
    assert result.fail_closed


# --------------------------------------------------------------------------
# Fragility findings (characterisation; all deterministic, ~0 latency)
# --------------------------------------------------------------------------
@PM
def test_adverse_tick_inside_reserved_buffer_passes_with_exchange_quantity_and_weighted_fill(monkeypatch, pm):
    # The bracket normally lands exactly on the reward floor. Give the synthetic
    # strategy one extra target tick so this path isolates risk-buffer behavior.
    original_plan = support.worker_main.plan_pilot_bracket
    plans = []

    def plan_with_reward_slack(**kwargs):
        plan = original_plan(**kwargs)
        rules = kwargs["rules"]
        plan = replace(
            plan,
            take_profit_price=plan.take_profit_price + rules.tick_size,
            planned_net_reward_usdc=plan.planned_net_reward_usdc + plan.quantity * rules.tick_size,
        )
        plans.append(plan)
        return plan

    monkeypatch.setattr(support.worker_main, "plan_pilot_bracket", plan_with_reward_slack)
    result, harness = run(monkeypatch, pm, shim=True, ask_step_after="clamp", ask_step_ticks=1)
    assert_clean(result, harness)
    assert result.outcome == "PROTECTED", f"{result.outcome}: {result.reason}"
    assert result.order_posted and result.protected

    # Sizing remains exchange-filter normalized at the $40 target; the simulated
    # fill is the actual post-move ask and the same submitted quantity.
    rules = harness.adapter.symbol_rules[support.SYMBOL]
    expected_qty = rules.normalize_quantity(Decimal("40") / Decimal("2610.59"), is_market=True)
    assert Decimal(harness.exchange.orders_posted[0]["quantity"]) == expected_qty
    assert harness.exchange.fill_qty == expected_qty
    assert harness.exchange.fill_price == Decimal("2610.60")
    assert plans
    actual_stop = min(Decimal(row["triggerPrice"]) for row in harness.exchange.algos.values())
    actual_stop_risk = (harness.exchange.fill_price - actual_stop) * harness.exchange.fill_qty
    assert actual_stop_risk + plans[-1].estimated_costs_usdc <= Decimal("2.0")
    assert (harness.exchange.fill_price - plans[-1].entry_price) * harness.exchange.fill_qty <= Decimal("0.20")


@PM
def test_adverse_move_beyond_buffer_is_stopped_by_final_fence_and_kill_switch(monkeypatch, pm):
    # 2,000 ETHUSDC ticks is a $20 entry move (> $0.20 risk reserve at the
    # exchange-filtered quantity). The immutable final fence must still close.
    result, harness = run(monkeypatch, pm, shim=True, ask_step_after="gate1", ask_step_ticks=2000)
    assert_clean(result, harness)
    assert not result.order_posted
    assert result.last_order_block["stage"] == "FINAL_FENCE"
    assert result.adapter_state == "DEGRADED" and result.fail_closed
    planned_qty = Decimal("40") / Decimal("2610.59")
    assert planned_qty * Decimal("20") > Decimal("0.20")

@PM
def test_one_favourable_tick_after_gate1_aborts_with_inputs_changed(monkeypatch, pm):
    result, harness = run(monkeypatch, pm, shim=True, ask_step_after="gate1", ask_step_ticks=-1)
    assert_clean(result, harness)
    assert not result.order_posted
    assert result.last_order_block == {
        "stage": "FINAL_FENCE",
        "reason": "Final order risk inputs changed after preparation; a fresh decision is required",
    }
    assert result.adapter_state == "DEGRADED" and result.fail_closed


@PM
def test_favourable_tick_before_gate1_is_tolerated(monkeypatch, pm):
    result, harness = run(monkeypatch, pm, shim=True, ask_step_after="clamp", ask_step_ticks=-1)
    assert_clean(result, harness)
    assert result.outcome == "PROTECTED"


@PM
def test_account_snapshot_older_than_5s_blocks_entry_although_worker_calls_it_fresh(monkeypatch, pm):
    # derive_local_mainnet_snapshot_risk(max_age_seconds=5) vs the worker's 30 s snapshot limit.
    async def scenario():
        harness = Harness(monkeypatch, Config(portfolio_margin=pm, shim_keys=True, shim_hashes=True,
                                              positive_funding_floor=True, snapshot_age=10.0))
        await harness.build()
        ready = harness.worker.is_account_snapshot_ready()
        return ready, await harness.attempt()

    ready, result = asyncio.run(scenario())
    assert ready is True
    assert not result.order_posted
    assert result.last_order_block == {
        "stage": "ORDER_GATE",
        "reason": "Local Mainnet durable basket-risk context is invalid",
    }
    assert any("_local_mainnet_runtime_evidence" in item or "get_local_mainnet_risk_context" in item
               for item in result.provider_none)


@PM
def test_exchange_clock_ahead_of_local_makes_rest_only_samples_negative_age(monkeypatch, pm):
    # Freshness uses the raw local clock vs the exchange payload timestamp; an
    # exchange clock ahead by 0.8 s yields age < 0 -> fail closed at 0 latency.
    result, harness = run(monkeypatch, pm, shim=True, ws="none", clock_skew_ms=800)
    assert_clean(result, harness)
    assert not result.order_posted
    assert result.outcome == "EXECUTION_BLOCKED"
    assert "Market data stale for ETHUSDC" in result.reason
