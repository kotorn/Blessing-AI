"""A retry after an aborted pilot attempt must reuse the launch's basket.

The launch session binds its basket once (COALESCE on reserve/bind) and the
order risk context rejects any other basket. A fresh random basket per
decision therefore turned one aborted attempt into a dead campaign.
"""

import re
from decimal import Decimal

import pytest

from apps.trading_worker.main import TradingWorkerApp, WorkerExecutionMode
from domain.enums import (
    EconomicRiskClass,
    MarketType,
    OrderSide,
    OrderType,
    PositionSide,
    TimeInForce,
)
from domain.models import ExecutionDecision, OrderIntent, utc_now
from tests.python.test_local_pilot_arm_to_order import FakeExecutionAdapter

BASKET_RE = re.compile(r"^pilot-[0-9a-f]{16}$")


def _worker(session: dict | None) -> TradingWorkerApp:
    worker = TradingWorkerApp(symbols=["ETHUSDC"])
    worker.execution_mode = WorkerExecutionMode.LIVE
    worker.execution_adapter = FakeExecutionAdapter()
    worker._mainnet_launch_session = session
    return worker


def _decision(index: int) -> ExecutionDecision:
    now = utc_now()
    order = OrderIntent(
        client_order_id=f"CID-RETRY-{index}",
        symbol="ETHUSDC",
        market_type=MarketType.USDM_FUTURES,
        side=OrderSide.BUY,
        position_side=PositionSide.BOTH,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.GTC,
        quantity=Decimal("0.01"),
        created_at=now,
        strategy_id="grid",
        metadata={"strategy_id": "grid"},
    )
    return ExecutionDecision(
        decision_id=f"DEC-RETRY-{index}",
        symbol="ETHUSDC",
        action="SUBMIT_ORDER",
        strategy_id="grid",
        risk_class=EconomicRiskClass.NEW_RISK,
        orders=[order],
        net_exposure_delta=Decimal("0.01"),
        timestamp=now,
    )


@pytest.mark.asyncio
async def test_second_attempt_reuses_the_launch_derived_basket():
    session = {"launch_id": "launch-12345678", "policy": "LIVE_RESEARCH_PILOT"}
    worker = _worker(session)
    first = await worker._clamp_order_notional_if_needed(_decision(1), Decimal("2500"))
    second = await worker._clamp_order_notional_if_needed(_decision(2), Decimal("2500"))
    basket_1 = first.orders[0].basket_id
    basket_2 = second.orders[0].basket_id
    assert basket_1 and BASKET_RE.fullmatch(basket_1)
    assert basket_1 == basket_2


@pytest.mark.asyncio
async def test_baskets_differ_between_launches():
    a = _worker({"launch_id": "launch-aaaaaaaa", "policy": "LIVE_RESEARCH_PILOT"})
    b = _worker({"launch_id": "launch-bbbbbbbb", "policy": "LIVE_RESEARCH_PILOT"})
    da = await a._clamp_order_notional_if_needed(_decision(1), Decimal("2500"))
    db = await b._clamp_order_notional_if_needed(_decision(1), Decimal("2500"))
    assert da.orders[0].basket_id != db.orders[0].basket_id


@pytest.mark.asyncio
async def test_an_already_bound_basket_wins_over_the_derived_one():
    session = {
        "launch_id": "launch-12345678",
        "policy": "LIVE_RESEARCH_PILOT",
        "basket_id": "pilot-existing-bound-basket",
    }
    worker = _worker(session)
    out = await worker._clamp_order_notional_if_needed(_decision(1), Decimal("2500"))
    assert out.orders[0].basket_id == "pilot-existing-bound-basket"
