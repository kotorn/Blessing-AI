"""Bounded ETHUSDC Testnet trial sizing. This module does not submit orders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from apps.trading_worker.venues.binance.symbol_rules import SymbolTradingRules


@dataclass(frozen=True)
class ProtectedEthTestnetPlan:
    quantity: Decimal
    stop_trigger: Decimal
    target_trigger: Decimal
    conservative_entry_price: Decimal
    conservative_notional_usdc: Decimal
    planned_loss_usdc: Decimal


def plan_protected_ethusdc_trial(
    *,
    rules: SymbolTradingRules,
    bid: Decimal,
    ask: Decimal,
    quote_observed_at: datetime,
    estimated_roundtrip_cost_usdc: Decimal,
    actual_leverage: int,
    now: datetime | None = None,
) -> ProtectedEthTestnetPlan:
    """Fail closed when live rules, quote, cost or leverage cannot prove the cap."""
    observed_now = now or datetime.now(timezone.utc)
    if rules.symbol != "ETHUSDC" or not rules.is_ready_for("MARKET") or not rules.is_usdc_perpetual():
        raise ValueError("ETHUSDC Testnet contract rules are not verified")
    if quote_observed_at.tzinfo is None or observed_now.tzinfo is None:
        raise ValueError("Testnet quote time is unverified")
    quote_age = (observed_now - quote_observed_at).total_seconds()
    if not 0 <= quote_age <= 5:
        raise ValueError("Testnet quote is stale")
    if not isinstance(actual_leverage, int) or isinstance(actual_leverage, bool) or not 1 <= actual_leverage <= 10:
        raise ValueError("Testnet leverage is outside the approved limit")
    if not all(value.is_finite() and value > 0 for value in (bid, ask)) or bid >= ask:
        raise ValueError("Testnet bid/ask is invalid")
    if not estimated_roundtrip_cost_usdc.is_finite() or estimated_roundtrip_cost_usdc <= 0:
        raise ValueError("Testnet roundtrip cost evidence is missing")

    # One percent room for a market fill above the observed ask. This is a
    # planning bound, not a guarantee of the exchange execution price.
    conservative_entry = ask * Decimal("1.01")
    quantity = rules.normalize_quantity(
        Decimal("50") / conservative_entry, is_market=True
    )
    market_min_qty = rules.market_min_qty if rules.market_min_qty is not None else rules.min_qty
    market_max_qty = rules.market_max_qty if rules.market_max_qty is not None else rules.max_qty
    market_min_notional = rules.min_notional_for("MARKET")
    notional = quantity * conservative_entry
    if (quantity <= 0 or quantity < market_min_qty or quantity > market_max_qty
            or notional < market_min_notional or notional > Decimal("50")):
        raise ValueError("ETHUSDC Testnet filters cannot fit within 50 USDC")

    stop = rules.normalize_price(bid * Decimal("0.99"))
    target = rules.normalize_price(ask * Decimal("1.02"), round_up=True)
    if stop <= 0 or stop >= bid or target <= ask:
        raise ValueError("ETHUSDC Testnet protection triggers are invalid")
    planned_loss = quantity * (conservative_entry - stop) + estimated_roundtrip_cost_usdc
    if planned_loss > Decimal("2"):
        raise ValueError("ETHUSDC Testnet planned loss exceeds 2 USDC")
    return ProtectedEthTestnetPlan(
        quantity=quantity,
        stop_trigger=stop,
        target_trigger=target,
        conservative_entry_price=conservative_entry,
        conservative_notional_usdc=notional,
        planned_loss_usdc=planned_loss,
    )
