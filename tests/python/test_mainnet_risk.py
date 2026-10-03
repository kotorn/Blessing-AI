import hashlib
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.trading_worker.venues.binance.mainnet_risk import (
    MAINNET_RISK_POLICY,
    MAINNET_RISK_POLICY_PATH,
    MAINNET_RISK_POLICY_SHA256,
    MAINNET_RISK_POLICY_VERSION,
    load_mainnet_risk_policy,
    validate_risk_increasing_order,
)
from domain.enums import MarketType, OrderSide, OrderType, PositionSide, TimeInForce
from domain.models import OrderIntent


def make_intent(**updates):
    values = {
        "client_order_id": "RISK-1",
        "symbol": "ETHUSDC",
        "basket_id": "BASKET-1",
        "market_type": MarketType.USDM_FUTURES,
        "side": OrderSide.BUY,
        "position_side": PositionSide.BOTH,
        "order_type": OrderType.MARKET,
        "time_in_force": TimeInForce.GTC,
        "quantity": Decimal("0.4"),
        "stop_loss_price": Decimal(90),
        "take_profit_price": Decimal(130),
        "estimated_fees_usdc": Decimal("0.4"),
        "estimated_funding_usdc": Decimal("0.3"),
        "estimated_slippage_usdc": Decimal("0.3"),
    }
    values.update(updates)
    return OrderIntent(**values)


def validate(intent, **updates):
    values = {
        "entry_price": Decimal(100),
        "basket_headroom_usdc": Decimal(250),
        "daily_loss_headroom_usdc": Decimal(5),
        "current_gross_exposure_usdc": Decimal(0),
        "current_basket_exposure_usdc": Decimal(0),
        "collateral_usdc": Decimal(250),
        "available_balance_usdc": Decimal(250),
        "configured_leverage": Decimal(10),
        "effective_leverage": Decimal(10),
        "active_exposure_chains": 0,
        "same_active_basket": False,
    }
    values.update(updates)
    return validate_risk_increasing_order(intent, **values)


def test_policy_loads_canonical_json_with_expected_hash_version_and_values():
    assert MAINNET_RISK_POLICY_PATH == Path("config/risk/mainnet_local_policy.json").resolve()
    assert MAINNET_RISK_POLICY_VERSION == "local-mainnet-risk-v1"
    assert MAINNET_RISK_POLICY.version == "local-mainnet-risk-v1"
    assert MAINNET_RISK_POLICY.target == "LOCAL"
    assert MAINNET_RISK_POLICY.symbol == "ETHUSDC"
    assert MAINNET_RISK_POLICY.basket_budget_usdc == Decimal(250)
    assert MAINNET_RISK_POLICY.basket_drawdown_usdc == Decimal(125)
    assert MAINNET_RISK_POLICY.daily_loss_usdc == Decimal(5)
    assert MAINNET_RISK_POLICY.collateral_usdc == Decimal(250)
    assert MAINNET_RISK_POLICY.gross_exposure_usdc == Decimal(1000)
    assert MAINNET_RISK_POLICY.first_order_notional_usdc == Decimal(50)
    assert MAINNET_RISK_POLICY.active_exposure_chains == 1
    assert MAINNET_RISK_POLICY.max_leverage == Decimal(10)
    assert MAINNET_RISK_POLICY.risk_reward_risk == Decimal(1)
    assert MAINNET_RISK_POLICY.risk_reward_reward == Decimal(2)
    assert MAINNET_RISK_POLICY.risk_reward_net_of_costs is True
    assert MAINNET_RISK_POLICY.min_reward_to_risk == Decimal(2)
    assert MAINNET_RISK_POLICY_SHA256 == (
        "19c1f38deb4819aaa36c36ea94b3f479ad0e4f7140806f0d5d0106ca80dc75f4"
    )
    assert (
        MAINNET_RISK_POLICY_SHA256
        == hashlib.sha256(MAINNET_RISK_POLICY_PATH.read_bytes()).hexdigest()
    )
    assert load_mainnet_risk_policy() == MAINNET_RISK_POLICY


def test_policy_is_immutable():
    with pytest.raises(FrozenInstanceError):
        MAINNET_RISK_POLICY.daily_loss_usdc = Decimal(6)


def test_buy_order_passes_at_daily_and_rr_boundaries():
    result = validate(make_intent())

    assert result.allowed is True
    assert result.notional_usdc == Decimal("40.0")
    assert result.total_risk_usdc == Decimal("4.0") + Decimal("1.0")
    assert result.net_reward_usdc == Decimal("12.0") - Decimal("1.0")
    assert result.available_risk_usdc == Decimal(5)


def test_sell_order_requires_reverse_direction_and_passes():
    result = validate(
        make_intent(
            side=OrderSide.SELL,
            stop_loss_price=Decimal(110),
            take_profit_price=Decimal(70),
        )
    )
    assert result.allowed is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("estimated_fees_usdc", None),
        ("estimated_funding_usdc", Decimal("-0.01")),
        ("estimated_slippage_usdc", Decimal("NaN")),
        ("stop_loss_price", Decimal("Infinity")),
        ("take_profit_price", Decimal("NaN")),
    ],
)
def test_required_numeric_inputs_must_be_finite_and_costs_nonnegative(field, value):
    if isinstance(value, Decimal) and not value.is_finite():
        with pytest.raises(ValidationError):
            make_intent(**{field: value})
    else:
        assert validate(make_intent(**{field: value})).allowed is False


@pytest.mark.parametrize(
    "side,stop,target",
    [
        (OrderSide.BUY, Decimal(100), Decimal(130)),
        (OrderSide.BUY, Decimal(90), Decimal(90)),
        (OrderSide.SELL, Decimal(70), Decimal(110)),
        (OrderSide.SELL, Decimal(110), Decimal(110)),
    ],
)
def test_directional_stop_and_target_order_is_required(side, stop, target):
    result = validate(make_intent(side=side, stop_loss_price=stop, take_profit_price=target))
    assert result.allowed is False
    assert "requires" in result.reason


def test_basket_id_is_required_for_risk_increasing_order():
    assert "basket_id" in validate(make_intent(basket_id=None)).reason


def test_reduce_only_is_not_a_risk_increasing_input():
    assert validate(make_intent(reduce_only=True)).allowed is False


def test_risk_must_fit_basket_drawdown_and_daily_headroom():
    assert validate(make_intent(), daily_loss_headroom_usdc=Decimal("4.99")).allowed is False
    assert validate(make_intent(), basket_headroom_usdc=Decimal("4.99")).allowed is False
    assert validate(
        make_intent(), basket_headroom_usdc=Decimal(250)
    ).available_risk_usdc == Decimal(5)


def test_basket_drawdown_caps_initial_250_budget_to_125():
    result = validate(
        make_intent(
            quantity=Decimal(7),
            stop_loss_price=Decimal(80),
            take_profit_price=Decimal(160),
        ),
        is_first_risk_increasing_order=False,
        daily_loss_headroom_usdc=Decimal(200),
        policy=replace(MAINNET_RISK_POLICY, daily_loss_usdc=Decimal(200)),
    )
    assert result.allowed is False
    assert result.available_risk_usdc == Decimal(125)


def test_net_reward_must_be_at_least_two_times_total_risk():
    result = validate(make_intent(take_profit_price=Decimal(125)))
    assert result.allowed is False
    assert "1:2" in result.reason


def test_first_order_notional_cap_is_50_usdc():
    intent = make_intent(quantity=Decimal("0.51"), stop_loss_price=Decimal(99))
    result = validate(intent)
    assert result.allowed is False
    assert "50 USDC" in result.reason
    assert validate(intent, is_first_risk_increasing_order=False).allowed is True


@pytest.mark.parametrize(
    "updates,expected",
    [
        ({"current_gross_exposure_usdc": Decimal(961)}, "gross"),
        ({"collateral_usdc": Decimal("250.01")}, "collateral"),
        ({"effective_leverage": Decimal("10.01")}, "leverage"),
    ],
)
def test_account_caps_are_enforced(updates, expected):
    result = validate(make_intent(), **updates)
    assert result.allowed is False
    assert expected in result.reason


def test_projected_leverage_includes_the_new_order_notional():
    result = validate(make_intent(), collateral_usdc=Decimal(1))

    assert result.allowed is False
    assert "projected effective leverage" in result.reason
    assert result.projected_effective_leverage == Decimal(40)


def test_available_balance_must_cover_projected_initial_margin_and_costs():
    result = validate(make_intent(), available_balance_usdc=Decimal(4))

    assert result.allowed is False
    assert "initial margin" in result.reason
    assert result.initial_margin_requirement_usdc == Decimal(4)


def test_basket_budget_and_active_chain_limit_are_enforced():
    assert validate(
        make_intent(), current_basket_exposure_usdc=Decimal("220")
    ).allowed is False
    assert validate(
        make_intent(),
        active_exposure_chains=1,
        same_active_basket=False,
    ).allowed is False
    assert validate(
        make_intent(),
        active_exposure_chains=1,
        same_active_basket=True,
    ).allowed is True


@pytest.mark.parametrize(
    "field",
    [
        "entry_price",
        "basket_headroom_usdc",
        "daily_loss_headroom_usdc",
        "current_gross_exposure_usdc",
        "available_balance_usdc",
        "configured_leverage",
    ],
)
def test_context_fields_must_be_finite(field):
    assert validate(make_intent(), **{field: Decimal("NaN")}).allowed is False
