"""Exchange-normalized bracket producer for the Local Live Research Pilot.

Derives order sizing, stop loss, take profit, and management mode for the
ETHUSDC QUICK pilot strictly adhering to config/risk/live_research_pilot.json.
This module is pure calculation and does not submit orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from domain.enums import OrderSide, PositionSide
from domain.models import OrderIntent
from .symbol_rules import SymbolTradingRules

DEFAULT_NOTIONAL_CAP_USDC = Decimal("50.0")
DEFAULT_ENTRY_TARGET_NOTIONAL_USDC = Decimal("40.0")
DEFAULT_MAX_STOP_RISK_USDC = Decimal("2.0")
DEFAULT_EXECUTION_RISK_BUFFER_USDC = Decimal("0.20")
DEFAULT_MIN_NET_REWARD_USDC = Decimal("0.25")
DEFAULT_MIN_REWARD_TO_RISK = Decimal("0.125")
DEFAULT_CAMPAIGN_DRAWDOWN_CAP_USDC = Decimal("5.0")


@dataclass(frozen=True, slots=True)
class PilotBracketPlan:
    """Immutable sizing and bracket plan for one pilot order."""

    symbol: str
    quantity: Decimal
    entry_price: Decimal
    notional_usdc: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal
    management_mode: str
    estimated_fees_usdc: Decimal
    estimated_funding_usdc: Decimal
    estimated_slippage_usdc: Decimal
    estimated_costs_usdc: Decimal
    planned_stop_risk_usdc: Decimal
    total_risk_usdc: Decimal
    planned_net_reward_usdc: Decimal
    side: OrderSide
    position_side: PositionSide = PositionSide.BOTH


def _decimal(value: Any, name: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be Decimal-compatible") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    if positive and parsed <= 0:
        raise ValueError(f"{name} must be positive")
    if nonnegative and parsed < 0:
        raise ValueError(f"{name} must be nonnegative")
    return parsed


def plan_pilot_bracket(
    *,
    rules: SymbolTradingRules,
    entry_price: Decimal,
    side: OrderSide,
    estimated_fees_usdc: Decimal | None = None,
    estimated_funding_usdc: Decimal | None = None,
    estimated_slippage_usdc: Decimal | None = None,
    notional_cap_usdc: Decimal = DEFAULT_NOTIONAL_CAP_USDC,
    entry_target_notional_usdc: Decimal = DEFAULT_ENTRY_TARGET_NOTIONAL_USDC,
    max_stop_risk_usdc: Decimal = DEFAULT_MAX_STOP_RISK_USDC,
    execution_risk_buffer_usdc: Decimal = DEFAULT_EXECUTION_RISK_BUFFER_USDC,
    min_net_reward_usdc: Decimal = DEFAULT_MIN_NET_REWARD_USDC,
    min_reward_to_risk: Decimal = DEFAULT_MIN_REWARD_TO_RISK,
    campaign_drawdown_headroom_usdc: Decimal = DEFAULT_CAMPAIGN_DRAWDOWN_CAP_USDC,
) -> PilotBracketPlan:
    """Calculate exchange-normalized quantity, stop loss and take profit for the pilot.

    Fails closed if the geometry cannot satisfy the notional cap, stop-risk cap,
    or reward floor within exchange-normalized tick and step boundaries.
    """
    if rules is None or rules.symbol != "ETHUSDC" or not rules.is_ready_for("MARKET"):
        raise ValueError("ETHUSDC exchange rules are not ready for MARKET")
    if not rules.is_usdc_perpetual():
        raise ValueError("ETHUSDC rules must be for a USDC perpetual contract")

    entry = _decimal(entry_price, "entry_price", positive=True)
    notional_cap = _decimal(notional_cap_usdc, "notional_cap_usdc", positive=True)
    entry_target = _decimal(entry_target_notional_usdc, "entry_target_notional_usdc", positive=True)
    if entry_target > notional_cap:
        raise ValueError("entry_target_notional_usdc cannot exceed the hard notional cap")
    max_stop_risk = _decimal(max_stop_risk_usdc, "max_stop_risk_usdc", positive=True)
    execution_buffer = _decimal(
        execution_risk_buffer_usdc, "execution_risk_buffer_usdc", nonnegative=True
    )
    min_net_reward = _decimal(min_net_reward_usdc, "min_net_reward_usdc", positive=True)
    min_r2r = _decimal(min_reward_to_risk, "min_reward_to_risk", nonnegative=True)
    dd_headroom = _decimal(campaign_drawdown_headroom_usdc, "campaign_drawdown_headroom_usdc", positive=True)

    if side not in {OrderSide.BUY, OrderSide.SELL}:
        raise ValueError("side must be OrderSide.BUY or OrderSide.SELL")

    # 1. Quantity sizing: strictly capped at notional_cap (50 USDC)
    raw_qty = entry_target / entry
    quantity = rules.normalize_quantity(raw_qty, is_market=True)
    while quantity * entry > notional_cap:
        quantity -= rules.step_size
        quantity = rules.normalize_quantity(quantity, is_market=True)

    min_qty = rules.market_min_qty if rules.market_min_qty is not None else rules.min_qty
    max_qty = rules.market_max_qty if rules.market_max_qty is not None else rules.max_qty
    min_notional = rules.min_notional_for("MARKET")
    notional = quantity * entry

    if quantity <= 0 or quantity < min_qty or quantity > max_qty:
        raise ValueError(f"Quantity {quantity} violates symbol lot size [{min_qty}, {max_qty}]")
    if notional < min_notional:
        raise ValueError(f"Notional {notional} is below symbol min notional {min_notional}")
    if notional > notional_cap:
        raise ValueError(f"Notional {notional} exceeds pilot cap {notional_cap}")

    # 2. Costs calculation
    # Default fees: 0.05% taker commission each way (0.1% roundtrip)
    fees = (
        _decimal(estimated_fees_usdc, "estimated_fees_usdc", nonnegative=True)
        if estimated_fees_usdc is not None
        else notional * Decimal("0.0005") * Decimal("2")
    )
    # Default funding: 8h fundingInterval, 4 events in 24h horizon, 0.3% cap
    funding = (
        _decimal(estimated_funding_usdc, "estimated_funding_usdc", nonnegative=True)
        if estimated_funding_usdc is not None
        else notional * Decimal("0.003") * Decimal("4")
    )
    # Default slippage: 0.05 USDC conservative buffer
    slippage = (
        _decimal(estimated_slippage_usdc, "estimated_slippage_usdc", nonnegative=True)
        if estimated_slippage_usdc is not None
        else Decimal("0.05")
    )
    costs = fees + funding + slippage

    if costs >= max_stop_risk:
        raise ValueError(f"Estimated costs {costs} exceed or equal the planned stop risk cap {max_stop_risk}")

    # 3. Stop loss: total risk = stop_loss_risk + costs <= min(max_stop_risk, dd_headroom)
    allowed_stop_risk = min(max_stop_risk, dd_headroom) - costs - execution_buffer
    if allowed_stop_risk <= 0:
        raise ValueError("No risk budget remaining after transaction costs")
    stop_distance = allowed_stop_risk / quantity

    if side == OrderSide.BUY:
        raw_stop = entry - stop_distance
        stop_price = rules.normalize_price(raw_stop)
        # Verify tick rounding didn't push risk above cap
        while (entry - stop_price) * quantity > allowed_stop_risk:
            stop_price += rules.tick_size
            stop_price = rules.normalize_price(stop_price)
        if stop_price >= entry or stop_price <= 0:
            raise ValueError(f"Normalized BUY stop price {stop_price} is invalid against entry {entry}")
        actual_stop_risk = (entry - stop_price) * quantity
    else:  # SELL
        raw_stop = entry + stop_distance
        stop_price = rules.normalize_price(raw_stop, round_up=True)
        while (stop_price - entry) * quantity > allowed_stop_risk:
            stop_price -= rules.tick_size
            stop_price = rules.normalize_price(stop_price)
        if stop_price <= entry or stop_price <= 0:
            raise ValueError(f"Normalized SELL stop price {stop_price} is invalid against entry {entry}")
        actual_stop_risk = (stop_price - entry) * quantity

    total_risk = actual_stop_risk + costs
    if (
        total_risk + execution_buffer > max_stop_risk
        or total_risk + execution_buffer > dd_headroom
    ):
        raise ValueError(f"Calculated total risk {total_risk} exceeds limit")

    # 4. Take profit: net reward = gross_reward - costs >= max(min_net_reward, min_r2r * total_risk)
    required_net_reward = max(min_net_reward, min_r2r * total_risk)
    required_gross_reward = required_net_reward + costs
    target_distance = required_gross_reward / quantity

    if side == OrderSide.BUY:
        raw_target = entry + target_distance
        target_price = rules.normalize_price(raw_target, round_up=True)
        while (target_price - entry) * quantity - costs < required_net_reward:
            target_price += rules.tick_size
            target_price = rules.normalize_price(target_price)
        if target_price <= entry:
            raise ValueError(f"Normalized BUY target price {target_price} is below or equal to entry {entry}")
        actual_gross_reward = (target_price - entry) * quantity
    else:  # SELL
        raw_target = entry - target_distance
        target_price = rules.normalize_price(raw_target)
        while (entry - target_price) * quantity - costs < required_net_reward:
            target_price -= rules.tick_size
            target_price = rules.normalize_price(target_price)
        if target_price >= entry or target_price <= 0:
            raise ValueError(f"Normalized SELL target price {target_price} is above or equal to entry {entry}")
        actual_gross_reward = (entry - target_price) * quantity

    actual_net_reward = actual_gross_reward - costs
    if actual_net_reward < min_net_reward:
        raise ValueError(f"Actual net reward {actual_net_reward} is below required floor {min_net_reward}")

    return PilotBracketPlan(
        symbol="ETHUSDC",
        quantity=quantity,
        entry_price=entry,
        notional_usdc=notional,
        stop_loss_price=stop_price,
        take_profit_price=target_price,
        management_mode="QUICK",
        estimated_fees_usdc=fees,
        estimated_funding_usdc=funding,
        estimated_slippage_usdc=slippage,
        estimated_costs_usdc=costs,
        planned_stop_risk_usdc=actual_stop_risk,
        total_risk_usdc=total_risk,
        planned_net_reward_usdc=actual_net_reward,
        side=side,
        position_side=PositionSide.BOTH,
    )


def apply_pilot_bracket_to_intent(
    intent: OrderIntent,
    plan: PilotBracketPlan,
    *,
    basket_id: str | None = None,
) -> OrderIntent:
    """Return a model copy of intent with the verified pilot bracket values."""
    resolved_basket_id = str(basket_id or intent.basket_id or f"pilot-{uuid4().hex[:16]}").strip()
    return intent.model_copy(
        update={
            "quantity": plan.quantity,
            "stop_loss_price": plan.stop_loss_price,
            "take_profit_price": plan.take_profit_price,
            "management_mode": plan.management_mode,
            "basket_id": resolved_basket_id,
            "estimated_fees_usdc": plan.estimated_fees_usdc,
            "estimated_funding_usdc": plan.estimated_funding_usdc,
            "estimated_slippage_usdc": plan.estimated_slippage_usdc,
            "position_side": plan.position_side,
            "reduce_only": False,
        }
    )


def validate_pilot_fill(
    *,
    side: OrderSide,
    average_entry_price: Decimal,
    filled_quantity: Decimal,
    stop_loss_price: Decimal,
    take_profit_price: Decimal,
    estimated_costs_usdc: Decimal,
    notional_cap_usdc: Decimal = DEFAULT_NOTIONAL_CAP_USDC,
    max_stop_risk_usdc: Decimal = DEFAULT_MAX_STOP_RISK_USDC,
    campaign_drawdown_headroom_usdc: Decimal = DEFAULT_CAMPAIGN_DRAWDOWN_CAP_USDC,
) -> None:
    """Fail closed when the actual weighted fill breaks the planned bracket.

    The bracket prices stay fixed after entry. A fill outside the planned
    risk and exposure envelope is a close-only condition for the caller.
    """
    entry = _decimal(average_entry_price, "average_entry_price", positive=True)
    quantity = _decimal(filled_quantity, "filled_quantity", positive=True)
    stop = _decimal(stop_loss_price, "stop_loss_price", positive=True)
    target = _decimal(take_profit_price, "take_profit_price", positive=True)
    costs = _decimal(estimated_costs_usdc, "estimated_costs_usdc", nonnegative=True)
    cap = _decimal(notional_cap_usdc, "notional_cap_usdc", positive=True)
    risk_cap = _decimal(max_stop_risk_usdc, "max_stop_risk_usdc", positive=True)
    headroom = _decimal(
        campaign_drawdown_headroom_usdc,
        "campaign_drawdown_headroom_usdc",
        positive=True,
    )
    if side == OrderSide.BUY:
        if not stop < entry < target:
            raise ValueError("post-fill risk: BUY fill is outside the existing bracket")
        stop_risk = (entry - stop) * quantity
    elif side == OrderSide.SELL:
        if not target < entry < stop:
            raise ValueError("post-fill risk: SELL fill is outside the existing bracket")
        stop_risk = (stop - entry) * quantity
    else:
        raise ValueError("post-fill risk: side must be BUY or SELL")
    if entry * quantity > cap:
        raise ValueError("post-fill risk: actual entry exposure exceeds the hard notional cap")
    if stop_risk + costs > min(risk_cap, headroom):
        raise ValueError("post-fill risk exceeds the reserved campaign risk budget")


__all__ = [
    "DEFAULT_CAMPAIGN_DRAWDOWN_CAP_USDC",
    "DEFAULT_ENTRY_TARGET_NOTIONAL_USDC",
    "DEFAULT_EXECUTION_RISK_BUFFER_USDC",
    "DEFAULT_MAX_STOP_RISK_USDC",
    "DEFAULT_MIN_NET_REWARD_USDC",
    "DEFAULT_MIN_REWARD_TO_RISK",
    "DEFAULT_NOTIONAL_CAP_USDC",
    "PilotBracketPlan",
    "apply_pilot_bracket_to_intent",
    "plan_pilot_bracket",
    "validate_pilot_fill",
]
