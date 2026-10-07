"""Pure Mainnet risk-math domain rules.

This module deliberately has no exchange, persistence, environment, or worker
dependencies.  It describes the bounded risk contract that a later execution
gate may consume; importing it cannot authorize or submit an order.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any

from domain.enums import OrderSide
from domain.models import OrderIntent

MAINNET_RISK_POLICY_PATH = (
    Path(__file__).resolve().parents[4] / "config" / "risk" / "mainnet_local_policy.json"
)

_EXPECTED_POLICY = MappingProxyType(
    {
        "version": "local-mainnet-risk-v1",
        "target": "LOCAL",
        "symbol": "ETHUSDC",
        "basket_budget_usdc": Decimal(250),
        "basket_drawdown_usdc": Decimal(125),
        "daily_loss_usdc": Decimal(5),
        "collateral_usdc": Decimal(250),
        "gross_exposure_usdc": Decimal(1000),
        "first_order_notional_usdc": Decimal(50),
        "active_exposure_chains": 1,
        "max_leverage": Decimal(10),
        "risk_reward_risk": Decimal(1),
        "risk_reward_reward": Decimal(2),
        "risk_reward_net_of_costs": True,
    }
)


@dataclass(frozen=True, slots=True)
class MainnetRiskPolicy:
    """Immutable policy loaded from the canonical local Mainnet JSON file."""

    version: str
    target: str
    symbol: str
    basket_budget_usdc: Decimal
    basket_drawdown_usdc: Decimal
    daily_loss_usdc: Decimal
    first_order_notional_usdc: Decimal
    gross_exposure_usdc: Decimal
    collateral_usdc: Decimal
    active_exposure_chains: int
    max_leverage: Decimal
    risk_reward_risk: Decimal
    risk_reward_reward: Decimal
    risk_reward_net_of_costs: bool

    @property
    def min_reward_to_risk(self) -> Decimal:
        return self.risk_reward_reward / self.risk_reward_risk

    def __post_init__(self) -> None:
        if self.version != _EXPECTED_POLICY["version"]:
            raise ValueError("unsupported Mainnet risk policy version")
        if self.target != _EXPECTED_POLICY["target"]:
            raise ValueError("Mainnet risk policy target must be LOCAL")
        if self.symbol != _EXPECTED_POLICY["symbol"]:
            raise ValueError("Mainnet risk policy symbol must be ETHUSDC")
        if (
            isinstance(self.active_exposure_chains, bool)
            or self.active_exposure_chains != _EXPECTED_POLICY["active_exposure_chains"]
        ):
            raise ValueError("active exposure chain limit must be one")
        if self.risk_reward_net_of_costs is not True:
            raise ValueError("risk:reward must be net of costs")
        values = (
            self.basket_budget_usdc,
            self.basket_drawdown_usdc,
            self.daily_loss_usdc,
            self.first_order_notional_usdc,
            self.gross_exposure_usdc,
            self.collateral_usdc,
            self.max_leverage,
            self.risk_reward_risk,
            self.risk_reward_reward,
        )
        if any(
            not isinstance(value, Decimal) or not value.is_finite() or value <= 0
            for value in values
        ):
            raise ValueError("Mainnet risk policy values must be finite positive Decimals")
        if self.basket_drawdown_usdc > self.basket_budget_usdc:
            raise ValueError("basket drawdown cannot exceed basket budget")


def _load_policy_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot load canonical Mainnet risk policy: {path}") from exc
    if not isinstance(payload, dict):
        raise TypeError("canonical Mainnet risk policy must be a JSON object")
    expected_keys = {
        "version",
        "target",
        "symbol",
        "basket_budget_usdc",
        "basket_drawdown_usdc",
        "daily_loss_usdc",
        "collateral_usdc",
        "gross_exposure_usdc",
        "first_order_notional_usdc",
        "active_exposure_chains",
        "max_leverage",
        "risk_reward",
    }
    if set(payload) != expected_keys:
        raise ValueError("canonical Mainnet risk policy has an unexpected schema")
    risk_reward = payload["risk_reward"]
    if not isinstance(risk_reward, dict) or set(risk_reward) != {
        "risk",
        "reward",
        "net_of_costs",
    }:
        raise ValueError("canonical Mainnet risk policy has an invalid risk:reward schema")
    payload["risk_reward_risk"] = risk_reward["risk"]
    payload["risk_reward_reward"] = risk_reward["reward"]
    payload["risk_reward_net_of_costs"] = risk_reward["net_of_costs"]
    return payload


def load_mainnet_risk_policy(path: Path = MAINNET_RISK_POLICY_PATH) -> MainnetRiskPolicy:
    """Load the canonical policy and fail closed on any unsafe value."""

    payload = _load_policy_payload(path)
    decimal_fields = {
        "basket_budget_usdc",
        "basket_drawdown_usdc",
        "daily_loss_usdc",
        "collateral_usdc",
        "gross_exposure_usdc",
        "first_order_notional_usdc",
        "max_leverage",
        "risk_reward_risk",
        "risk_reward_reward",
    }
    normalized: dict[str, Any] = {}
    for key, expected in _EXPECTED_POLICY.items():
        value = payload.get(key)
        if key == "active_exposure_chains":
            if isinstance(value, bool) or not isinstance(value, int) or value != expected:
                raise ValueError(f"canonical Mainnet risk policy field {key} is unsafe")
        elif key == "risk_reward_net_of_costs":
            if not isinstance(value, bool) or value is not expected:
                raise ValueError(f"canonical Mainnet risk policy field {key} is unsafe")
        elif key in decimal_fields:
            try:
                value = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(f"canonical Mainnet risk policy field {key} is invalid") from exc
            if not value.is_finite() or value != expected:
                raise ValueError(f"canonical Mainnet risk policy field {key} is unsafe")
        elif value != expected:
            raise ValueError(f"canonical Mainnet risk policy field {key} is unsafe")
        normalized[key] = value
    return MainnetRiskPolicy(**normalized)


MAINNET_RISK_POLICY_SHA256 = hashlib.sha256(MAINNET_RISK_POLICY_PATH.read_bytes()).hexdigest()
MAINNET_RISK_POLICY_VERSION = _EXPECTED_POLICY["version"]
MAINNET_RISK_POLICY = load_mainnet_risk_policy()

LOCAL_LIVE_PILOT_POLICY_PATH = (
    Path(__file__).resolve().parents[4] / "config" / "risk" / "live_research_pilot.json"
)
_LOCAL_LIVE_PILOT_EXPECTED = {
    "version": "local-live-research-pilot-v1",
    "target": "LOCAL",
    "symbol": "ETHUSDC",
    "market": "USD_M_FUTURES",
    "position_notional_usdc": "50",
    "order_notional_usdc": "50",
    "total_exposure_usdc": "50",
    "planned_stop_risk_usdc": "2",
    "campaign_drawdown_usdc": "5",
    "max_leverage": "10",
    "quick_target_net_usdc": "0.25",
    "quick_max_hold_seconds": 86400,
    "min_net_reward_usdc": "0.25",
    # Pilot exception: net target is at least 0.25 USDC against stop-risk <=2,
    # so the minimum planned reward/risk ratio is 0.125. Scale-up keeps 1:2.
    "min_reward_to_risk": "0.125",
    "management_mode": "QUICK",
    "max_entries": 1,
}


def load_local_live_pilot_policy(path: Path = LOCAL_LIVE_PILOT_POLICY_PATH) -> dict[str, Any]:
    """Load the exact bounded pilot policy; unexpected values fail closed."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("cannot load Local Live Research Pilot policy") from exc
    if payload != _LOCAL_LIVE_PILOT_EXPECTED:
        raise ValueError("Local Live Research Pilot policy differs from the reviewed limits")
    return dict(_LOCAL_LIVE_PILOT_EXPECTED)


LOCAL_LIVE_PILOT_POLICY = load_local_live_pilot_policy()
LOCAL_LIVE_PILOT_POLICY_SHA256 = hashlib.sha256(
    LOCAL_LIVE_PILOT_POLICY_PATH.read_bytes()
).hexdigest()


@dataclass(frozen=True, slots=True)
class RiskValidationResult:
    """Immutable result of validating one risk-increasing intent."""

    allowed: bool
    reason: str
    notional_usdc: Decimal = Decimal(0)
    stop_loss_risk_usdc: Decimal = Decimal(0)
    total_risk_usdc: Decimal = Decimal(0)
    gross_reward_usdc: Decimal = Decimal(0)
    net_reward_usdc: Decimal = Decimal(0)
    available_risk_usdc: Decimal = Decimal(0)
    projected_effective_leverage: Decimal = Decimal(0)
    initial_margin_requirement_usdc: Decimal = Decimal(0)


@dataclass(frozen=True, slots=True)
class LocalMainnetSnapshotRisk:
    """Risk values derived from one fresh, signed Mainnet account snapshot."""

    observed_at: datetime
    daily_loss_usdc: Decimal
    daily_loss_headroom_usdc: Decimal
    collateral_usdc: Decimal
    effective_leverage: Decimal
    configured_leverage: Decimal
    current_gross_exposure_usdc: Decimal


def _decimal(
    value: Any, field: str, *, nonnegative: bool = False, positive: bool = False
) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field} is required")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be Decimal-compatible") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    if positive and parsed <= 0:
        raise ValueError(f"{field} must be positive")
    if nonnegative and parsed < 0:
        raise ValueError(f"{field} must be nonnegative")
    return parsed


def derive_local_mainnet_snapshot_risk(
    snapshot: Any,
    *,
    now: datetime,
    max_age_seconds: Decimal = Decimal("5"),
    policy: MainnetRiskPolicy = MAINNET_RISK_POLICY,
) -> LocalMainnetSnapshotRisk:
    """Validate and project risk from a fresh signed Mainnet account snapshot.

    This deliberately rejects missing fee/funding coverage, a stale snapshot,
    an incomplete UTC-day window, or an account outside the immutable policy.
    It does not treat unknown PnL as zero.
    """

    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("current observation time must be timezone-aware")
    if snapshot is None or getattr(snapshot, "valid", False) is not True:
        raise ValueError("signed Mainnet account snapshot is invalid")
    if str(getattr(snapshot, "exchange_environment", "")).upper() != "BINANCE_MAINNET":
        raise ValueError("account snapshot is not from Binance Mainnet")
    observed_at = getattr(snapshot, "timestamp", None)
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise ValueError("account snapshot timestamp is invalid")
    now_utc = now.astimezone(timezone.utc)
    observed_utc = observed_at.astimezone(timezone.utc)
    age = Decimal(str((now_utc - observed_utc).total_seconds()))
    if not age.is_finite() or age < 0 or age > max_age_seconds:
        raise ValueError("signed Mainnet account snapshot is stale")

    if (
        str(getattr(snapshot, "collateral_asset", "")).upper() != "USDC"
        or str(getattr(snapshot, "risk_currency", "")).upper() != "USDC"
        or str(getattr(snapshot, "daily_loss_asset", "")).upper() != "USDC"
        or getattr(snapshot, "daily_loss_known", False) is not True
        or getattr(snapshot, "daily_pnl_includes_fees", False) is not True
        or getattr(snapshot, "daily_pnl_includes_funding", False) is not True
        or getattr(snapshot, "configured_leverage_known", False) is not True
        or getattr(snapshot, "margin_mode_known", False) is not True
        or str(getattr(snapshot, "liquidation_safety", "")).upper() != "KNOWN"
    ):
        raise ValueError("signed Mainnet account risk provenance is incomplete")

    window_start = getattr(snapshot, "daily_loss_window_start", None)
    window_end = getattr(snapshot, "daily_loss_window_end", None)
    if (
        not isinstance(window_start, datetime)
        or not isinstance(window_end, datetime)
        or window_start.tzinfo is None
        or window_end.tzinfo is None
    ):
        raise ValueError("signed daily-loss UTC window is incomplete")
    start_utc = window_start.astimezone(timezone.utc)
    end_utc = window_end.astimezone(timezone.utc)
    if (
        start_utc != start_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        or end_utc != start_utc + timedelta(days=1)
        or not start_utc <= now_utc < end_utc
    ):
        raise ValueError("signed daily-loss UTC window is invalid or stale")

    try:
        realized_pnl = Decimal(str(getattr(snapshot, "daily_realized_pnl", None)))
        unrealized_pnl = _decimal(
            getattr(snapshot, "unrealized_pnl", None), "unrealized_pnl"
        )
        collateral = _decimal(
            getattr(snapshot, "margin_balance", None), "margin_balance", positive=True
        )
        wallet = _decimal(
            getattr(snapshot, "wallet_balance", None), "wallet_balance", positive=True
        )
        available = _decimal(
            getattr(snapshot, "available_balance", None), "available_balance", positive=True
        )
        effective_leverage = _decimal(
            getattr(snapshot, "effective_leverage", None),
            "effective_leverage",
            nonnegative=True,
        )
        configured_leverage = _decimal(
            getattr(snapshot, "configured_leverage", None),
            "configured_leverage",
            positive=True,
        )
        gross_exposure = _decimal(
            getattr(snapshot, "total_position_notional", None),
            "total_position_notional",
            nonnegative=True,
        )
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("signed Mainnet account risk values are invalid") from exc
    if not realized_pnl.is_finite():
        raise ValueError("signed daily realized PnL is invalid")
    if (
        collateral > policy.collateral_usdc
        or wallet > policy.collateral_usdc
        or effective_leverage > policy.max_leverage
        or configured_leverage > policy.max_leverage
        or gross_exposure > policy.gross_exposure_usdc
    ):
        raise ValueError("signed Mainnet account exceeds the immutable risk policy")

    daily_loss = max(Decimal("0"), -(realized_pnl + unrealized_pnl))
    headroom = max(Decimal("0"), policy.daily_loss_usdc - daily_loss)
    return LocalMainnetSnapshotRisk(
        observed_at=observed_utc,
        daily_loss_usdc=daily_loss,
        daily_loss_headroom_usdc=headroom,
        collateral_usdc=collateral,
        effective_leverage=effective_leverage,
        configured_leverage=configured_leverage,
        current_gross_exposure_usdc=gross_exposure,
    )


def _reject(reason: str, **metrics: Decimal) -> RiskValidationResult:
    return RiskValidationResult(False, reason, **metrics)


def validate_risk_increasing_order(
    intent: OrderIntent,
    *,
    entry_price: Decimal,
    basket_headroom_usdc: Decimal,
    daily_loss_headroom_usdc: Decimal,
    current_gross_exposure_usdc: Decimal | None = None,
    current_basket_exposure_usdc: Decimal | None = None,
    collateral_usdc: Decimal | None = None,
    available_balance_usdc: Decimal | None = None,
    configured_leverage: Decimal | None = None,
    effective_leverage: Decimal | None = None,
    active_exposure_chains: int | None = None,
    same_active_basket: bool | None = None,
    is_first_risk_increasing_order: bool = True,
    policy: MainnetRiskPolicy = MAINNET_RISK_POLICY,
    min_net_reward_usdc: Decimal | None = None,
    min_reward_to_risk: Decimal | None = None,
) -> RiskValidationResult:
    """Validate bounded risk math without performing any side effect.

    ``R`` is the stop-loss loss plus all supplied nonnegative costs.  Net
    reward is target profit less those same costs. Optional reward floors let
    the bounded research pilot use its separately approved exit objective
    while the default release policy continues to require net reward >= 2R.
    """

    try:
        basket_id = str(intent.basket_id or "").strip()
        if not basket_id:
            return _reject("basket_id is required for risk-increasing orders")
        if bool(intent.reduce_only):
            return _reject("risk-increasing orders cannot be reduce_only")
        if str(intent.symbol).upper() != policy.symbol:
            return _reject(f"symbol must be {policy.symbol}")

        side = intent.side.value if isinstance(intent.side, OrderSide) else str(intent.side).upper()
        if side not in {OrderSide.BUY.value, OrderSide.SELL.value}:
            return _reject("side must be BUY or SELL")

        quantity = _decimal(intent.quantity, "quantity", positive=True)
        entry = _decimal(entry_price, "entry_price", positive=True)
        stop = _decimal(intent.stop_loss_price, "stop_loss_price", positive=True)
        target = _decimal(intent.take_profit_price, "take_profit_price", positive=True)
        fees = _decimal(intent.estimated_fees_usdc, "estimated_fees_usdc", nonnegative=True)
        funding = _decimal(
            intent.estimated_funding_usdc, "estimated_funding_usdc", nonnegative=True
        )
        slippage = _decimal(
            intent.estimated_slippage_usdc, "estimated_slippage_usdc", nonnegative=True
        )
        basket_headroom = _decimal(basket_headroom_usdc, "basket_headroom_usdc", nonnegative=True)
        daily_headroom = _decimal(
            daily_loss_headroom_usdc, "daily_loss_headroom_usdc", nonnegative=True
        )
        current_gross = _decimal(
            current_gross_exposure_usdc, "current_gross_exposure_usdc", nonnegative=True
        )
        current_basket = _decimal(
            current_basket_exposure_usdc,
            "current_basket_exposure_usdc",
            nonnegative=True,
        )
        collateral = _decimal(collateral_usdc, "collateral_usdc", positive=True)
        available_balance = _decimal(
            available_balance_usdc, "available_balance_usdc", positive=True
        )
        configured = _decimal(configured_leverage, "configured_leverage", positive=True)
        leverage = _decimal(effective_leverage, "effective_leverage", nonnegative=True)
    except ValueError as exc:
        return _reject(str(exc))

    if side == OrderSide.BUY.value:
        if not stop < entry < target:
            return _reject("BUY requires stop_loss_price < entry_price < take_profit_price")
    elif not target < entry < stop:
        return _reject("SELL requires take_profit_price < entry_price < stop_loss_price")
    if (
        isinstance(active_exposure_chains, bool)
        or not isinstance(active_exposure_chains, int)
        or active_exposure_chains < 0
    ):
        return _reject("active_exposure_chains must be a known nonnegative integer")
    if not isinstance(same_active_basket, bool):
        return _reject("same_active_basket must be explicitly verified")

    notional = quantity * entry
    stop_loss_risk = abs(entry - stop) * quantity
    costs = fees + funding + slippage
    total_risk = stop_loss_risk + costs
    gross_reward = abs(target - entry) * quantity
    net_reward = gross_reward - costs
    projected_effective_leverage = (current_gross + notional) / collateral
    initial_margin_requirement = notional / configured
    available_risk = min(
        basket_headroom,
        policy.basket_drawdown_usdc,
        daily_headroom,
        policy.daily_loss_usdc,
    )
    metrics = {
        "notional_usdc": notional,
        "stop_loss_risk_usdc": stop_loss_risk,
        "total_risk_usdc": total_risk,
        "gross_reward_usdc": gross_reward,
        "net_reward_usdc": net_reward,
        "available_risk_usdc": available_risk,
        "projected_effective_leverage": projected_effective_leverage,
        "initial_margin_requirement_usdc": initial_margin_requirement,
    }

    if is_first_risk_increasing_order and notional > policy.first_order_notional_usdc:
        return _reject(
            f"first risk-increasing order exceeds {policy.first_order_notional_usdc} USDC",
            **metrics,
        )
    if current_gross + notional > policy.gross_exposure_usdc:
        return _reject(
            f"projected gross exposure exceeds {policy.gross_exposure_usdc} USDC",
            **metrics,
        )
    if current_basket + notional > policy.basket_budget_usdc:
        return _reject(
            f"projected basket exposure exceeds {policy.basket_budget_usdc} USDC",
            **metrics,
        )
    if active_exposure_chains > policy.active_exposure_chains or (
        active_exposure_chains == policy.active_exposure_chains
        and not same_active_basket
    ):
        return _reject(
            f"active exposure chains exceed the {policy.active_exposure_chains}-chain policy",
            **metrics,
        )
    if collateral > policy.collateral_usdc:
        return _reject(f"collateral exceeds {policy.collateral_usdc} USDC", **metrics)
    if configured > policy.max_leverage:
        return _reject(f"configured leverage exceeds {policy.max_leverage}x", **metrics)
    if leverage > policy.max_leverage:
        return _reject(f"effective leverage exceeds {policy.max_leverage}x", **metrics)
    if projected_effective_leverage > policy.max_leverage:
        return _reject(f"projected effective leverage exceeds {policy.max_leverage}x", **metrics)
    if initial_margin_requirement + costs > available_balance:
        return _reject("available balance cannot cover projected initial margin and costs", **metrics)
    if total_risk > available_risk:
        return _reject("risk exceeds the available basket/daily headroom", **metrics)
    reward_ratio = (
        policy.min_reward_to_risk
        if min_reward_to_risk is None
        else _decimal(min_reward_to_risk, "min_reward_to_risk", nonnegative=True)
    )
    reward_floor = (
        Decimal("0")
        if min_net_reward_usdc is None
        else _decimal(min_net_reward_usdc, "min_net_reward_usdc", nonnegative=True)
    )
    if net_reward < max(reward_floor, reward_ratio * total_risk):
        if reward_ratio > 0:
            reward_label = format(reward_ratio.normalize(), "f")
            reason = f"net reward is below the required 1:{reward_label} risk/reward floor"
        else:
            reason = "net reward is below the required absolute risk/reward floor"
        return _reject(reason, **metrics)

    return RiskValidationResult(True, "Passed", **metrics)


__all__ = [
    "MAINNET_RISK_POLICY",
    "MAINNET_RISK_POLICY_PATH",
    "MAINNET_RISK_POLICY_SHA256",
    "MAINNET_RISK_POLICY_VERSION",
    "LOCAL_LIVE_PILOT_POLICY",
    "LOCAL_LIVE_PILOT_POLICY_PATH",
    "LOCAL_LIVE_PILOT_POLICY_SHA256",
    "load_local_live_pilot_policy",
    "LocalMainnetSnapshotRisk",
    "MainnetRiskPolicy",
    "RiskValidationResult",
    "load_mainnet_risk_policy",
    "derive_local_mainnet_snapshot_risk",
    "validate_risk_increasing_order",
]
