"""Fail-closed execution gates shared by the worker and Binance adapter."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import logging
import math
import os
import re
import time
from typing import Any, Mapping, Optional

from domain.enums import EconomicRiskClass, MarketType, OrderSide, OrderType, PositionSide, TimeInForce
from domain.models import ExecutionDecision, OrderIntent

from .config import BinanceEnvironment, environment_label
from .mainnet_risk import (
    LOCAL_LIVE_PILOT_POLICY,
    LOCAL_LIVE_PILOT_POLICY_SHA256,
    MAINNET_RISK_POLICY,
    MAINNET_RISK_POLICY_SHA256,
    validate_risk_increasing_order,
)
from .models import ConnectionState

logger = logging.getLogger("blessing.binance.gates")


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str
    prepared: Optional["PreparedOrder"] = None


@dataclass(frozen=True)
class PreparedOrder:
    symbol: str
    order_type: str
    quantity: Decimal
    price: Optional[Decimal]
    estimated_price: Decimal
    notional: Decimal


def _risk_class(value: Any) -> Optional[EconomicRiskClass]:
    try:
        return value if isinstance(value, EconomicRiskClass) else EconomicRiskClass(str(value))
    except (TypeError, ValueError):
        return None


def _is_risk_increasing(value: Any) -> bool:
    return _risk_class(value) in {
        EconomicRiskClass.NEW_RISK,
        EconomicRiskClass.INCREASE_RISK,
    }


def _is_risk_reducing(value: Any) -> bool:
    return _risk_class(value) in {
        EconomicRiskClass.REDUCE_RISK,
        EconomicRiskClass.RECOVERY,
        EconomicRiskClass.CLOSE,
        EconomicRiskClass.EMERGENCY,
    }


async def _local_mainnet_risk_gate(
    adapter: Any,
    intent: OrderIntent,
    *,
    entry_price: Decimal,
    quantity: Decimal,
) -> Optional[GateResult]:
    """Validate pre-entry risk and bracket intent without requiring live algos.

    Confirmed exchange protection is necessarily a post-fill condition. This
    gate only validates the planned stop/target geometry; it must never query
    for Algo orders that cannot exist until after the entry fills.
    """

    if getattr(adapter, 'env', None) != BinanceEnvironment.MAINNET:
        return None
    local_only = _env_flag('LOCAL_ONLY', False)
    runtime_target = os.getenv('LOCAL_RUNTIME_TARGET', '').strip().upper()
    if not local_only and runtime_target != 'LOCAL':
        return None
    if runtime_target == 'LOCAL' and not local_only:
        return GateResult(False, 'Local Mainnet runtime requires LOCAL_ONLY=true')
    if not entry_price.is_finite() or entry_price <= 0 or not quantity.is_finite() or quantity <= 0:
        return GateResult(False, 'Local Mainnet final price or normalized quantity is invalid')
    validated_intent = intent.model_copy(update={'quantity': quantity})


    # Validate the intended protection before entry. The post-fill lifecycle
    # separately submits and verifies fill-sized Algo orders within 5 seconds.
    try:
        stop_price = Decimal(str(intent.stop_loss_price))
        target_price = Decimal(str(intent.take_profit_price))
        side = str(getattr(intent.side, 'value', intent.side)).upper()
        if (
            not stop_price.is_finite()
            or not target_price.is_finite()
            or stop_price <= 0
            or target_price <= 0
            or (side == 'BUY' and not stop_price < entry_price < target_price)
            or (side == 'SELL' and not target_price < entry_price < stop_price)
            or side not in {'BUY', 'SELL'}
        ):
            raise ValueError('invalid bracket geometry')
        rules = getattr(adapter, 'symbol_rules', {}).get(MAINNET_RISK_POLICY.symbol)
        if (
            rules is None
            or rules.normalize_price(stop_price) != stop_price
            or rules.normalize_price(target_price) != target_price
            or rules.normalize_quantity(quantity) != quantity
        ):
            raise ValueError('bracket or quantity is not exchange-normalized')
        if str(getattr(intent.position_side, 'value', intent.position_side)).upper() != 'BOTH':
            raise ValueError('Local post-fill close-only lifecycle supports one-way position mode only')
    except (AttributeError, InvalidOperation, TypeError, ValueError):
        return GateResult(False, 'Local Mainnet pre-entry stop/target plan is invalid or not exchange-normalized')

    management_mode = str(getattr(intent, 'management_mode', '') or '').strip().upper()
    if management_mode not in {'QUICK', 'HOLD'}:
        return GateResult(False, 'Local Mainnet entry requires a strategy-selected, predeclared management mode')
    if management_mode == 'HOLD':
        return GateResult(False, 'HOLD management is blocked until no-fixed-target trailing and signal-exit lifecycle is implemented')

    context_provider = getattr(adapter, 'get_local_mainnet_risk_context', None)
    if not callable(context_provider):
        return GateResult(False, 'Local Mainnet durable basket-risk context is unavailable')

    context_input = {
        'runtime_target': 'LOCAL',
        'validated_entry_price': entry_price,
        'validated_quantity': quantity,
        'validated_notional_usdc': quantity * entry_price,
    }
    try:
        context = await context_provider(validated_intent, context_input)
    except Exception:
        return GateResult(False, 'Local Mainnet durable basket-risk context could not be verified')
    if not isinstance(context, Mapping):
        return GateResult(False, 'Local Mainnet durable basket-risk context is invalid')

    observed = context.get('observed_at')
    try:
        observed_at = observed if isinstance(observed, datetime) else datetime.fromisoformat(str(observed))
        if observed_at.tzinfo is None:
            return GateResult(False, 'Local Mainnet risk context timestamp is not timezone-aware')
        age = (datetime.now(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return GateResult(False, 'Local Mainnet risk context timestamp is invalid')
    if age < 0 or age > 5:
        return GateResult(False, 'Local Mainnet risk context is stale')

    pilot = context.get('live_research_pilot')
    if not isinstance(pilot, Mapping):
        return GateResult(False, 'Verified durable Live Research Pilot approval context is unavailable')
    pilot_binding = {
        'approval_verified': True,
        'approval_role': 'trading_admin',
        'binding_verified': True,
        'runtime_target': 'LOCAL',
        'symbol': MAINNET_RISK_POLICY.symbol,
        'status': 'ACTIVE',
        'management_mode': management_mode,
        'drawdown_triggered': False,
    }
    if any(pilot.get(key) != expected for key, expected in pilot_binding.items()):
        return GateResult(False, 'Live Research Pilot approval, status, or management binding is invalid')
    for key in ('source_hash', 'dependency_hash', 'migration_hash', 'strategy_hash', 'risk_policy_hash'):
        if not re.fullmatch(r'[0-9a-f]{64}', str(pilot.get(key) or '')):
            return GateResult(False, 'Live Research Pilot runtime or strategy fingerprint is incomplete')
    if (
        pilot.get('risk_policy_hash') != LOCAL_LIVE_PILOT_POLICY_SHA256
        or any(pilot.get(key) != context.get(key) for key in ('source_hash', 'dependency_hash', 'migration_hash'))
    ):
        return GateResult(False, 'Live Research Pilot binding differs from the verified runtime fingerprint')
    expires_at = pilot.get('campaign_expires_at')
    try:
        pilot_expiry = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at))
        if pilot_expiry.tzinfo is None or pilot_expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            return GateResult(False, 'Live Research Pilot approval is expired or timezone-invalid')
    except (TypeError, ValueError):
        return GateResult(False, 'Live Research Pilot expiry is invalid')
    expected_pilot_limits = {
        'max_position_notional_usdc': Decimal('50'),
        'max_order_notional_usdc': Decimal('50'),
        'max_total_exposure_usdc': Decimal('50'),
        'max_position_stop_risk_usdc': Decimal('2'),
        'campaign_drawdown_usdc': Decimal('5'),
        'max_leverage': Decimal('10'),
    }
    limits = pilot.get('limits')
    if not isinstance(limits, Mapping):
        return GateResult(False, 'Live Research Pilot risk limits are unavailable')
    try:
        if any(Decimal(str(limits.get(key))) != value for key, value in expected_pilot_limits.items()):
            return GateResult(False, 'Live Research Pilot limits differ from the approved policy')
        pilot_numeric = {
            key: Decimal(str(pilot.get(key)))
            for key in (
                'current_position_notional_usdc', 'current_total_exposure_usdc',
                'current_net_pnl_usdc', 'peak_net_pnl_usdc', 'configured_leverage',
                'effective_leverage',
            )
        }
        if any(not value.is_finite() for value in pilot_numeric.values()):
            raise ValueError('non-finite pilot context')
        current_position = pilot_numeric['current_position_notional_usdc']
        current_exposure = pilot_numeric['current_total_exposure_usdc']
        campaign_dd = pilot_numeric['peak_net_pnl_usdc'] - pilot_numeric['current_net_pnl_usdc']
        if (
            current_position < 0
            or current_exposure < 0
            or campaign_dd < 0
            or campaign_dd >= expected_pilot_limits['campaign_drawdown_usdc']
            or pilot_numeric['configured_leverage'] > expected_pilot_limits['max_leverage']
            or pilot_numeric['effective_leverage'] > expected_pilot_limits['max_leverage']
        ):
            return GateResult(False, 'Live Research Pilot exposure, drawdown, or leverage cap is reached')
        if pilot.get('active_exposure_chains') != 0 or pilot.get('active_position_count') != 0:
            return GateResult(False, 'Live Research Pilot permits no additional exposure while a position is active')
    except (InvalidOperation, TypeError, ValueError):
        return GateResult(False, 'Live Research Pilot numeric risk context is incomplete')

    required_context = {
        'runtime_target': 'LOCAL',
        'database_provider': 'POSTGRES_LOCAL',
        'database_identity_verified': True,
        'persistence_durable': True,
        'lease_held': True,
        'kill_switch_active': False,
        'market_data_fresh': True,
        'account_snapshot_fresh': True,
        'reconciliation_status': 'IN_SYNC',
    }
    if any(context.get(key) != expected for key, expected in required_context.items()):
        return GateResult(False, 'Local Mainnet risk context failed a runtime, persistence, or safety identity check')
    # Caller/strategy estimates, even when echoed by the adapter, are not
    # authoritative commission, funding, or executable-depth evidence. No
    # Local Mainnet cost evidence provider is implemented yet, so fail closed.
    cost_provider = getattr(adapter, 'get_local_mainnet_cost_evidence', None)
    if not callable(cost_provider):
        return GateResult(
            False,
            'Local Mainnet commission/funding/depth cost evidence is unavailable; caller estimates cannot authorize risk',
        )
    try:
        cost_evidence = await cost_provider(validated_intent, context)
    except Exception:
        return GateResult(False, 'Local Mainnet exchange-derived cost evidence could not be verified')
    if not isinstance(cost_evidence, Mapping):
        return GateResult(False, 'Local Mainnet exchange-derived cost evidence is unavailable')
    observed_costs = cost_evidence.get('observed_at')
    try:
        costs_at = (
            observed_costs
            if isinstance(observed_costs, datetime)
            else datetime.fromisoformat(str(observed_costs))
        )
        if costs_at.tzinfo is None:
            raise ValueError('cost evidence timestamp is not timezone-aware')
        costs_age = (datetime.now(timezone.utc) - costs_at.astimezone(timezone.utc)).total_seconds()
        if costs_age < 0 or costs_age > 5:
            raise ValueError('cost evidence is stale')
        if (
            cost_evidence.get('source') != 'BINANCE_FAPI_COMMISSION_FUNDING_DEPTH'
            or cost_evidence.get('symbol') != MAINNET_RISK_POLICY.symbol
            or cost_evidence.get('client_order_id') != intent.client_order_id
            or Decimal(str(cost_evidence.get('quantity'))) != quantity
            or str(cost_evidence.get('side', '')).upper() != side
        ):
            raise ValueError('cost evidence identity mismatch')
        observations = cost_evidence.get('request_observations')
        required_observations = {
            'commission', 'depth', 'funding', 'funding_info', 'leverage_brackets'
        }
        expected_routes = {
            'commission': '/fapi/v1/commissionRate',
            'depth': '/fapi/v1/depth',
            'funding': '/fapi/v1/fundingRate',
            'funding_info': '/fapi/v1/fundingInfo',
            'leverage_brackets': '/fapi/v1/leverageBracket',
        }
        expected_params = {
            'commission': {'symbol': MAINNET_RISK_POLICY.symbol},
            'depth': {'symbol': MAINNET_RISK_POLICY.symbol, 'limit': 1000},
            'funding': {'symbol': MAINNET_RISK_POLICY.symbol, 'limit': 3},
            'funding_info': {},
            'leverage_brackets': {'symbol': MAINNET_RISK_POLICY.symbol},
        }
        expected_signed = {'commission': True, 'depth': False, 'funding': False,
                           'funding_info': False, 'leverage_brackets': True}
        if not isinstance(observations, Mapping) or set(observations) != required_observations:
            raise ValueError('per-endpoint retrieval evidence is incomplete')
        monotonic_now = time.monotonic()
        for name, observation in observations.items():
            if not isinstance(observation, Mapping):
                raise ValueError('per-endpoint retrieval evidence is invalid')
            completed = observation.get('completed_at')
            completed_monotonic = observation.get('completed_monotonic')
            started_monotonic = observation.get('started_monotonic')
            duration_ms = observation.get('duration_ms')
            elapsed_ms = (
                (completed_monotonic - started_monotonic) * 1000
                if isinstance(completed_monotonic, (int, float))
                and isinstance(started_monotonic, (int, float))
                else math.inf
            )
            if not isinstance(completed, datetime) or completed.tzinfo is None:
                raise ValueError('endpoint retrieval completion time is invalid')
            started = observation.get('started_at')
            if not isinstance(started, datetime) or started.tzinfo is None:
                raise ValueError('endpoint retrieval start time is invalid')
            age = (datetime.now(timezone.utc) - completed.astimezone(timezone.utc)).total_seconds()
            wall_duration_ms = (
                completed.astimezone(timezone.utc) - started.astimezone(timezone.utc)
            ).total_seconds() * 1000
            if (
                not isinstance(completed_monotonic, (int, float))
                or isinstance(completed_monotonic, bool)
                or monotonic_now - completed_monotonic < 0
                or monotonic_now - completed_monotonic > 5
                or not isinstance(started_monotonic, (int, float))
                or isinstance(started_monotonic, bool)
                or completed_monotonic < started_monotonic
                or monotonic_now - started_monotonic < 0
                or monotonic_now - started_monotonic > 5
                or age < 0
                or age > 5
                or not isinstance(duration_ms, (int, float))
                or isinstance(duration_ms, bool)
                or not math.isfinite(duration_ms)
                or duration_ms < 0
                or duration_ms > 5000
                or abs(duration_ms - elapsed_ms) > max(1.0, elapsed_ms * 0.01)
                or wall_duration_ms < 0
                or abs(duration_ms - wall_duration_ms) > max(100.0, wall_duration_ms * 0.25)
                or observation.get('source_timestamp') is not None
                or observation.get('evidence_key') != name
                or observation.get('route') != expected_routes[name]
                or observation.get('method') != 'GET'
                or observation.get('signed') is not expected_signed[name]
                or observation.get('params') != expected_params[name]
            ):
                raise ValueError('per-endpoint cost retrieval is stale or falsely attributes source time')
        if (
            cost_evidence.get('depth_levels_requested') != 1000
            or type(cost_evidence.get('depth_bid_levels_received')) is not int
            or type(cost_evidence.get('depth_ask_levels_received')) is not int
            or not 1 <= cost_evidence.get('depth_bid_levels_received', 0) <= 1000
            or not 1 <= cost_evidence.get('depth_ask_levels_received', 0) <= 1000
        ):
            raise ValueError('depth coverage evidence is incomplete')
        horizon_seconds = LOCAL_LIVE_PILOT_POLICY['quick_max_hold_seconds']
        try:
            interval_hours = Decimal(str(cost_evidence['funding_interval_hours']))
            funding_events = cost_evidence['funding_events_assumed']
        except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
            raise ValueError('cost horizon is incomplete') from exc
        expected_events = math.ceil(
            Decimal(horizon_seconds) / (interval_hours * Decimal(3600))
        ) + 1 if interval_hours.is_finite() and interval_hours > 0 else -1
        if (
            cost_evidence.get('cost_horizon_seconds') != horizon_seconds
            or cost_evidence.get('cost_horizon_source') != 'LOCAL_LIVE_PILOT_POLICY'
            or cost_evidence.get('runtime_target') != 'LOCAL'
            or cost_evidence.get('venue') != 'BINANCE_MAINNET'
            or not interval_hours.is_finite()
            or interval_hours <= 0
            or interval_hours > 24
            or type(funding_events) is not int
            or funding_events != expected_events
        ):
            raise ValueError('cost horizon or provenance does not match the approved pilot')
        fees = Decimal(str(cost_evidence['fees_upper_bound_usdc']))
        funding = Decimal(str(cost_evidence['funding_upper_bound_usdc']))
        slippage = Decimal(str(cost_evidence['slippage_upper_bound_usdc']))
        if any(not value.is_finite() or value < 0 for value in (fees, funding, slippage)):
            raise ValueError('cost bounds are invalid')
    except (KeyError, TypeError, ValueError, InvalidOperation, AttributeError):
        return GateResult(False, 'Local Mainnet exchange-derived cost evidence is stale, incomplete, or mismatched')
    raw_pilot_reward_to_risk = LOCAL_LIVE_PILOT_POLICY.get('min_reward_to_risk')
    if isinstance(raw_pilot_reward_to_risk, bool) or raw_pilot_reward_to_risk is None:
        return GateResult(
            False,
            'Live Research Pilot reward-risk exception is absent from its hashed policy; new risk remains blocked',
        )
    try:
        pilot_min_reward_to_risk = Decimal(str(raw_pilot_reward_to_risk))
        if not pilot_min_reward_to_risk.is_finite() or pilot_min_reward_to_risk < 0:
            raise ValueError('unsafe pilot reward-risk exception')
    except (InvalidOperation, TypeError, ValueError):
        return GateResult(
            False,
            'Live Research Pilot reward-risk exception is invalid in its hashed policy; new risk remains blocked',
        )
    validated_intent = intent.model_copy(
        update={
            'quantity': quantity,
            'estimated_fees_usdc': fees,
            'estimated_funding_usdc': funding,
            'estimated_slippage_usdc': slippage,
        }
    )
    if str(context.get('basket_id') or '').strip() != str(intent.basket_id or '').strip():
        return GateResult(False, 'Local Mainnet basket identity does not match the order intent')
    if type(context.get('is_first_risk_increasing_order')) is not bool:
        return GateResult(False, 'Local Mainnet first-order state is unknown')
    same_active_basket = context.get('same_active_basket')
    active_exposure_chains = context.get('active_exposure_chains')
    if type(same_active_basket) is not bool:
        return GateResult(False, 'Local Mainnet active basket identity is unknown')
    if isinstance(active_exposure_chains, bool) or not isinstance(active_exposure_chains, int):
        return GateResult(False, 'Local Mainnet active exposure chain count is unknown')

    try:
        validation = validate_risk_increasing_order(
            validated_intent,
            entry_price=entry_price,
            basket_headroom_usdc=context.get('basket_headroom_usdc'),
            daily_loss_headroom_usdc=context.get('daily_loss_headroom_usdc'),
            current_gross_exposure_usdc=context.get('current_gross_exposure_usdc'),
            current_basket_exposure_usdc=context.get('current_basket_exposure_usdc'),
            collateral_usdc=context.get('collateral_usdc'),
            available_balance_usdc=context.get('available_balance_usdc'),
            configured_leverage=context.get('configured_leverage'),
            effective_leverage=context.get('effective_leverage'),
            active_exposure_chains=active_exposure_chains,
            same_active_basket=same_active_basket,
            is_first_risk_increasing_order=context['is_first_risk_increasing_order'],
            policy=MAINNET_RISK_POLICY,
            min_net_reward_usdc=Decimal('0.25'),
            min_reward_to_risk=pilot_min_reward_to_risk,
        )
    except Exception:
        return GateResult(False, 'Local Mainnet risk calculation could not be verified')
    if not validation.allowed:
        return GateResult(False, f'Local Mainnet risk policy blocked order: {validation.reason}')
    if (
        validation.notional_usdc > expected_pilot_limits['max_order_notional_usdc']
        or validation.notional_usdc + pilot_numeric['current_position_notional_usdc']
        > expected_pilot_limits['max_position_notional_usdc']
        or validation.notional_usdc + pilot_numeric['current_total_exposure_usdc']
        > expected_pilot_limits['max_total_exposure_usdc']
        or validation.stop_loss_risk_usdc > expected_pilot_limits['max_position_stop_risk_usdc']
    ):
        return GateResult(False, 'Live Research Pilot notional or planned stop-risk cap is exceeded')
    if campaign_dd + validation.stop_loss_risk_usdc > expected_pilot_limits['campaign_drawdown_usdc']:
        return GateResult(False, 'Live Research Pilot planned stop loss would exceed the campaign drawdown cap')
    if management_mode == 'QUICK' and validation.net_reward_usdc < Decimal('0.25'):
        return GateResult(False, 'QUICK management target must net at least 0.25 USDC after costs')

    return None


_KNOWN_ALPHA_STRATEGIES = frozenset({"grid", "trend", "shock", "carry"})
_SYSTEM_STRATEGIES = frozenset(
    {"meta_allocator", "portfolio", "recovery", "manual_testnet_trial"}
)
_STRATEGY_ALIASES = {
    "structural grid": "grid",
    "trend / breakout": "trend",
    "trend/breakout": "trend",
    "shock momentum": "shock",
    "funding carry": "carry",
    "funding_carry": "carry",
}
_LINEAGE_PREFIX_TO_STRATEGY = {
    "GRID": "grid",
    "TREND": "trend",
    "SHOCK": "shock",
    "CARRY": "carry",
}


def _canonical_strategy_id(value: Any) -> Optional[str]:
    normalized = str(value or "").strip().lower()
    if normalized in _KNOWN_ALPHA_STRATEGIES:
        return normalized
    return _STRATEGY_ALIASES.get(normalized)


def _disabled_strategy_reason(worker: Any, decision: ExecutionDecision) -> Optional[str]:
    """Reject risk-increasing decisions whose alpha lineage is disabled.

    The risk governor emits a system-level ``meta_allocator`` order, so the
    source intent IDs are checked as well as the order strategy ID. Reduction
    and emergency decisions intentionally skip this policy check so a disabled
    alpha can still be safely unwound.
    """

    if not _is_risk_increasing(getattr(decision, "risk_class", None)):
        return None
    enabled_getter = getattr(worker, "_enabled_strategies", None)
    enabled = {
        strategy
        for strategy in (enabled_getter() if callable(enabled_getter) else set())
        if strategy in _KNOWN_ALPHA_STRATEGIES
    }
    for intent in getattr(decision, "orders", []) or []:
        strategy_id = str(getattr(intent, "strategy_id", "")).strip().lower()
        if strategy_id in _SYSTEM_STRATEGIES:
            continue
        canonical = _canonical_strategy_id(strategy_id)
        if canonical is None:
            return f"Unknown or unsupported strategy {strategy_id!r}"
        if canonical not in enabled:
            return f"Strategy {canonical} is disabled in the active configuration"

    lineage_ids = list(getattr(decision, "source_intent_ids", []) or [])
    for intent in getattr(decision, "orders", []) or []:
        lineage_ids.extend(getattr(intent, "source_intent_ids", []) or [])
    for lineage_id in lineage_ids:
        prefix = str(lineage_id).strip().upper().split("-", 1)[0]
        strategy = _LINEAGE_PREFIX_TO_STRATEGY.get(prefix)
        if strategy is not None and strategy not in enabled:
            return f"Strategy {strategy} is disabled in the active configuration"
    return None


def _liquidation_safety_is_known_and_positive(snapshot: Any) -> bool:
    if getattr(snapshot, "liquidation_safety", "UNKNOWN") != "KNOWN":
        return False
    distance = getattr(snapshot, "min_liquidation_distance_pct", None)
    # A flat account has no applicable liquidation distance.  For an active
    # account, KNOWN must include a strictly positive, finite distance; zero
    # is a known danger state and cannot authorize more exposure.
    if distance is None:
        try:
            notional = Decimal(str(getattr(snapshot, "total_position_notional", "")))
        except (InvalidOperation, TypeError, ValueError):
            return False
        # ``None`` is only acceptable when the authoritative snapshot proves
        # that the account is flat. An active position without a usable
        # liquidation price remains UNKNOWN and must fail closed.
        return notional.is_finite() and notional == 0
    try:
        parsed = Decimal(str(distance))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return parsed.is_finite() and parsed > 0


def _available_balance_is_positive(snapshot: Any) -> bool:
    try:
        available = Decimal(str(getattr(snapshot, "available_balance", "")))
    except (InvalidOperation, TypeError, ValueError):
        return False
    return available.is_finite() and available > 0


def _margin_utilization_is_safe(snapshot: Any) -> bool:
    try:
        utilization = Decimal(str(getattr(snapshot, "margin_utilization_pct", "")))
        configured_limit = Decimal(os.getenv("MAX_MARGIN_UTILIZATION_PCT", "70"))
    except (InvalidOperation, TypeError, ValueError):
        configured_limit = Decimal("70")
        try:
            utilization = Decimal(str(getattr(snapshot, "margin_utilization_pct", "")))
        except (InvalidOperation, TypeError, ValueError):
            return False
    if (
        not configured_limit.is_finite()
        or configured_limit <= 0
        or configured_limit > 100
    ):
        configured_limit = Decimal("70")
    return utilization.is_finite() and utilization >= 0 and utilization < configured_limit


def _positive_float(name: str, fallback: float) -> float:
    import os

    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        value = float(raw)
    except ValueError:
        return fallback
    return value if math.isfinite(value) and value > 0 else fallback


def _age_seconds(timestamp: Any) -> Optional[float]:
    if not isinstance(timestamp, datetime):
        return None
    if timestamp.tzinfo is None:
        # A naive timestamp has no defensible exchange-time meaning. Treat it
        # as unknown so the stale-data gate fails closed.
        return None
    return (datetime.now(timezone.utc) - timestamp).total_seconds()


def _venue_label(adapter: Any) -> str:
    env = getattr(adapter, "env", BinanceEnvironment.TESTNET)
    return environment_label(env) if isinstance(env, BinanceEnvironment) else str(env)


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class DecisionExecutionGate:
    """Checks worker-wide conditions before any decision reaches the adapter."""

    def __init__(self, worker: Any):
        self.worker = worker

    def check(self, decision: ExecutionDecision) -> GateResult:
        mode = getattr(self.worker.execution_mode, "value", self.worker.execution_mode)
        if mode not in {"TESTNET", "LIVE"}:
            return GateResult(False, "Execution mode is not an exchange execution mode")
        if mode == "LIVE" and not _env_flag("MAINNET_LIVE_APPROVED", False):
            return GateResult(False, "MAINNET_LIVE_APPROVED is not enabled")
        engine_state = getattr(self.worker, "engine_state", None)
        engine_state = getattr(engine_state, "value", engine_state)
        if engine_state not in {"ARMED", "PAUSED_NEW_RISK", "RECOVERY_ONLY"}:
            return GateResult(False, "Worker is not in an executable armed state")
        if getattr(self.worker, "kill_switch_active", False):
            return GateResult(False, "Kill switch is active")

        configured_getter = getattr(
            self.worker,
            "_mainnet_configured" if mode == "LIVE" else "_testnet_configured",
            None,
        )
        if callable(configured_getter) and not configured_getter():
            expected_environment = (
                BinanceEnvironment.MAINNET
                if mode == "LIVE"
                else BinanceEnvironment.TESTNET
            )
            return GateResult(
                False,
                f"{environment_label(expected_environment)} configuration is not verified",
            )

        adapter = getattr(self.worker, "execution_adapter", None)
        if adapter is None:
            return GateResult(False, "Execution adapter is unavailable")
        adapter_state = getattr(adapter, "connection_state", None)
        adapter_state = getattr(adapter_state, "value", adapter_state)
        if adapter_state != ConnectionState.READY.value:
            return GateResult(False, "Adapter not READY")
        if not bool(getattr(adapter, "authenticated", False)):
            return GateResult(False, "Adapter is not authenticated")
        if not bool(
            getattr(getattr(adapter, "capabilities", None), "trade_authorized", False)
        ):
            return GateResult(False, f"{_venue_label(adapter)} trade permission is not verified")
        stream_health = getattr(adapter, "private_stream_healthy", None)
        if stream_health is None:
            stream_health = bool(
                getattr(adapter, "user_stream", None)
                and getattr(adapter.user_stream, "is_connected", False)
            )
        if not bool(stream_health):
            return GateResult(False, "Private stream disconnected")
        if getattr(adapter.reconciliation, "last_status", "UNKNOWN") != "IN_SYNC":
            return GateResult(False, "Reconciliation not IN_SYNC")

        risk_class = _risk_class(getattr(decision, "risk_class", None))
        if risk_class is None or risk_class == EconomicRiskClass.NOOP:
            return GateResult(False, "Decision has no executable economic risk class")
        if not getattr(decision, "orders", None):
            return GateResult(False, "Decision contains no orders")
        if str(getattr(decision, "action", "")).upper() == "NOOP":
            return GateResult(False, "NOOP decisions cannot reach execution")

        disabled_strategy_reason = _disabled_strategy_reason(self.worker, decision)
        if disabled_strategy_reason:
            return GateResult(False, disabled_strategy_reason)

        if _is_risk_increasing(risk_class):
            # Treat canonical engine state as a safety input as well as the
            # compatibility flags. Any disagreement fails closed instead of
            # allowing a stale control-plane flag to authorize new exposure.
            if (
                getattr(self.worker, "pause_new_risk", False)
                or engine_state == "PAUSED_NEW_RISK"
            ):
                return GateResult(False, "Paused new risk")
            if (
                getattr(self.worker, "recovery_only", False)
                or engine_state == "RECOVERY_ONLY"
            ):
                return GateResult(False, "Recovery only mode active")

        account_snapshot_ready = self.worker.is_account_snapshot_ready()
        if _is_risk_increasing(risk_class) and not account_snapshot_ready:
            return GateResult(
                False,
                f"Account snapshot is missing, stale, invalid, or not {_venue_label(adapter)}",
            )

        snapshot = getattr(adapter, "account_snapshot", None)
        if snapshot is None:
            snapshot = getattr(getattr(adapter, "ledger", None), "account_snapshot", None)
        if _is_risk_increasing(risk_class) and not _available_balance_is_positive(snapshot):
            return GateResult(False, f"Available {_venue_label(adapter)} balance is not positive")
        if _is_risk_increasing(risk_class) and not _margin_utilization_is_safe(snapshot):
            return GateResult(False, f"Margin utilization is at or above the {_venue_label(adapter)} safety limit")
        if _is_risk_increasing(risk_class) and not _liquidation_safety_is_known_and_positive(snapshot):
            return GateResult(False, "Liquidation safety is UNKNOWN")

        # Reductions may use an emergency fallback when the market stream is
        # stale.  MARKET orders still require a fresh REST mark price in the
        # individual order gate.  Risk-increasing decisions require both the
        # worker-wide health bit and per-symbol event freshness here.
        if _is_risk_increasing(risk_class):
            if not getattr(self.worker, "market_data_healthy", False):
                return GateResult(False, "Market data is stale")
            max_age = _positive_float("MAX_MARKET_DATA_AGE_SEC", 3.0)
            timestamps = getattr(self.worker, "last_market_event_at", {})
            adapter_timestamps = getattr(adapter, "last_market_event_at", {})
            for intent in decision.orders:
                symbol = str(intent.symbol).upper()
                has_market_sample = getattr(
                    adapter, "has_authoritative_market_sample", None
                )
                if callable(has_market_sample) and not has_market_sample(symbol):
                    return GateResult(
                        False,
                        f"Authoritative {_venue_label(adapter)} market sample unavailable for {symbol}",
                    )
                last_event = timestamps.get(symbol) or adapter_timestamps.get(symbol)
                age = _age_seconds(last_event)
                if age is None or age < 0 or age > max_age:
                    return GateResult(False, f"Market data stale for {symbol}")

        return GateResult(True, "Passed")


class OrderExecutionGate:
    """Validates and prepares each individual OrderIntent at the last boundary."""

    def __init__(self, adapter: Any):
        self.adapter = adapter

    async def check(
        self,
        intent: OrderIntent,
        risk_class: EconomicRiskClass,
        *,
        reserved_open_orders: int = 0,
        reserved_notional: Decimal = Decimal("0"),
        exclude_client_order_id: Optional[str] = None,
        require_reduce_only_for_risk_reduction: bool = True,
        allow_emergency_fallback: bool = False,
    ) -> GateResult:
        if self.adapter.env not in {
            BinanceEnvironment.TESTNET,
            BinanceEnvironment.MAINNET,
        }:
            return GateResult(False, "Mutable execution requires a fixed Binance environment")
        risk = _risk_class(risk_class)
        if risk is None or risk == EconomicRiskClass.NOOP:
            return GateResult(False, "Invalid economic risk class")
        emergency_fallback = (
            allow_emergency_fallback and risk == EconomicRiskClass.EMERGENCY
        )
        adapter_state = getattr(self.adapter.connection_state, "value", self.adapter.connection_state)
        if not emergency_fallback and adapter_state != ConnectionState.READY.value:
            return GateResult(False, "Adapter not READY")
        if not bool(getattr(self.adapter, "authenticated", False)):
            return GateResult(False, "Adapter is not authenticated")
        if not bool(
            getattr(getattr(self.adapter, "capabilities", None), "trade_authorized", False)
        ):
            return GateResult(False, f"{_venue_label(self.adapter)} trade permission is not verified")
        if (
            self.adapter.env == BinanceEnvironment.MAINNET
            and not emergency_fallback
            and not _env_flag("MAINNET_LIVE_APPROVED", False)
        ):
            return GateResult(False, "MAINNET_LIVE_APPROVED is not enabled")
        stream_health = getattr(self.adapter, "private_stream_healthy", None)
        if stream_health is None:
            stream_health = bool(
                getattr(self.adapter, "user_stream", None)
                and getattr(self.adapter.user_stream, "is_connected", False)
            )
        if not emergency_fallback and not bool(stream_health):
            return GateResult(False, "Private stream disconnected")
        if not emergency_fallback and getattr(self.adapter.reconciliation, "last_status", "UNKNOWN") != "IN_SYNC":
            return GateResult(False, "Reconciliation not IN_SYNC")

        risk_increasing = risk in {
            EconomicRiskClass.NEW_RISK,
            EconomicRiskClass.INCREASE_RISK,
        }
        local_mainnet_target = (
            self.adapter.env == BinanceEnvironment.MAINNET
            and (
                _env_flag('LOCAL_ONLY', False)
                or os.getenv('LOCAL_RUNTIME_TARGET', '').strip().upper() == 'LOCAL'
            )
        )
        snapshot_checker = getattr(self.adapter, "is_account_snapshot_fresh", None)
        if (
            risk_increasing
            and not emergency_fallback
            and (not callable(snapshot_checker) or not snapshot_checker())
        ):
            return GateResult(
                False,
                f"Account snapshot is missing, stale, invalid, or not {_venue_label(self.adapter)}",
            )
        if risk_increasing and not emergency_fallback:
            snapshot = getattr(self.adapter, "account_snapshot", None)
            if snapshot is None:
                snapshot = getattr(getattr(self.adapter, "ledger", None), "account_snapshot", None)
            if not _available_balance_is_positive(snapshot):
                return GateResult(False, f"Available {_venue_label(self.adapter)} balance is not positive")
            if not _margin_utilization_is_safe(snapshot):
                return GateResult(False, f"Margin utilization is at or above the {_venue_label(self.adapter)} safety limit")
            if not _liquidation_safety_is_known_and_positive(snapshot):
                return GateResult(False, "Liquidation safety is UNKNOWN")
            if self.adapter.env == BinanceEnvironment.MAINNET:
                limits = self.adapter.safety_limits
                try:
                    collateral = Decimal(str(getattr(snapshot, "margin_balance", None)))
                    wallet_balance = Decimal(str(getattr(snapshot, "wallet_balance", None)))
                    effective_leverage = Decimal(str(getattr(snapshot, "effective_leverage", None)))
                    configured_leverage = Decimal(str(getattr(snapshot, "configured_leverage", None)))
                except (InvalidOperation, TypeError, ValueError):
                    return GateResult(False, "Mainnet collateral or leverage is unknown")
                if (
                    str(getattr(snapshot, "collateral_asset", "")).upper() != "USDC"
                    or str(getattr(snapshot, "risk_currency", "")).upper() != "USDC"
                ):
                    return GateResult(False, "Mainnet USDC collateral is not verified")
                if not bool(getattr(snapshot, "margin_mode_known", False)) or str(
                    getattr(snapshot, "margin_mode", "UNKNOWN")
                ).upper() not in {"CROSS", "ISOLATED", "SINGLE_ASSET_CROSS"}:
                    return GateResult(False, "Mainnet margin mode is unknown or unsupported")
                if not collateral.is_finite() or collateral <= 0:
                    return GateResult(False, "Mainnet collateral is invalid")
                if (
                    not wallet_balance.is_finite()
                    or wallet_balance <= 0
                    or wallet_balance > limits.max_collateral
                    or collateral > limits.max_collateral
                ):
                    return GateResult(False, "Mainnet collateral cap exceeded")
                if (
                    not effective_leverage.is_finite()
                    or effective_leverage < 0
                    or effective_leverage > limits.max_leverage
                ):
                    return GateResult(False, "Mainnet effective leverage cap exceeded")
                if (
                    not bool(getattr(snapshot, "configured_leverage_known", False))
                    or not configured_leverage.is_finite()
                    or configured_leverage <= 0
                    or configured_leverage > Decimal("10")
                    or configured_leverage > limits.max_leverage
                ):
                    return GateResult(False, "Mainnet configured ETHUSDC leverage cap exceeded or unknown")
                if (
                    not bool(getattr(snapshot, "daily_loss_known", False))
                    or str(getattr(snapshot, "daily_loss_asset", "")).upper() != "USDC"
                    or not bool(getattr(snapshot, "daily_pnl_includes_fees", False))
                    or not bool(getattr(snapshot, "daily_pnl_includes_funding", False))
                ):
                    return GateResult(False, "Mainnet daily loss observation is unknown")
                window_start = getattr(snapshot, "daily_loss_window_start", None)
                window_end = getattr(snapshot, "daily_loss_window_end", None)
                if not isinstance(window_start, datetime) or not isinstance(window_end, datetime):
                    return GateResult(False, "Mainnet daily loss UTC window is unknown")
                if window_start.tzinfo is None or window_end.tzinfo is None:
                    return GateResult(False, "Mainnet daily loss UTC window is not timezone-aware")
                window_start = window_start.astimezone(timezone.utc)
                window_end = window_end.astimezone(timezone.utc)
                if (
                    window_start != window_start.replace(hour=0, minute=0, second=0, microsecond=0)
                    or window_end < window_start
                    or window_end > window_start + timedelta(days=1)
                ):
                    return GateResult(False, "Mainnet daily loss UTC window is invalid")
                try:
                    realized = Decimal(str(getattr(snapshot, "daily_realized_pnl", None)))
                    unrealized = Decimal(str(getattr(snapshot, "unrealized_pnl", None)))
                except (InvalidOperation, TypeError, ValueError):
                    return GateResult(False, "Mainnet daily loss observation is invalid")
                if not realized.is_finite() or not unrealized.is_finite():
                    return GateResult(False, "Mainnet daily loss observation is invalid")
                daily_loss = max(Decimal("0"), -(realized + unrealized))
                if not daily_loss.is_finite() or daily_loss >= limits.max_daily_loss:
                    logger.error(
                        "monitor_event=daily_loss_cap_breached environment=%s symbol=%s",
                        _venue_label(self.adapter),
                        str(getattr(intent, "symbol", "UNKNOWN")).upper(),
                    )
                    return GateResult(False, "Mainnet daily loss cap exceeded")

        symbol = str(intent.symbol).upper()
        limits = self.adapter.safety_limits
        if symbol not in limits.allowed_symbols:
            return GateResult(False, f"Symbol {symbol} is not allowed by {_venue_label(self.adapter)} limits")
        if intent.market_type != MarketType.USDM_FUTURES:
            return GateResult(False, "Only USDⓈ-M Futures intents are supported")
        order_type = getattr(intent.order_type, "value", intent.order_type)
        order_type = str(order_type).upper()
        if order_type not in {OrderType.LIMIT.value, OrderType.MARKET.value}:
            return GateResult(False, f"Unsupported order type {order_type}")

        rules = self.adapter.symbol_rules.get(symbol)
        if rules is None or not rules.is_ready_for(order_type):
            return GateResult(False, f"Trading rules are unavailable or symbol is not TRADING: {symbol}")

        try:
            side = (
                intent.side
                if isinstance(intent.side, OrderSide)
                else OrderSide(str(intent.side))
            )
        except (TypeError, ValueError):
            return GateResult(False, "Invalid order side")

        try:
            position_side = (
                intent.position_side
                if isinstance(intent.position_side, PositionSide)
                else PositionSide(str(intent.position_side))
            )
        except (TypeError, ValueError):
            return GateResult(False, "Invalid positionSide")
        if self.adapter.capabilities.hedge_mode and position_side == PositionSide.BOTH:
            return GateResult(False, "Hedge Mode requires LONG or SHORT positionSide")
        if not self.adapter.capabilities.hedge_mode and position_side != PositionSide.BOTH:
            return GateResult(False, "One-Way Mode requires BOTH positionSide")

        try:
            time_in_force = (
                intent.time_in_force
                if isinstance(intent.time_in_force, TimeInForce)
                else TimeInForce(str(intent.time_in_force).upper())
            )
        except (TypeError, ValueError):
            return GateResult(False, "Invalid timeInForce")
        if intent.post_only and order_type != OrderType.LIMIT.value:
            return GateResult(False, "post_only is supported only for LIMIT orders")
        if intent.post_only and time_in_force not in {
            TimeInForce.GTC,
            TimeInForce.POST_ONLY,
        }:
            return GateResult(False, "post_only LIMIT orders require GTC or POST_ONLY timeInForce")

        try:
            quantity = Decimal(str(intent.quantity))
        except (InvalidOperation, ValueError):
            return GateResult(False, "Invalid quantity")
        if not quantity.is_finite() or quantity <= 0:
            return GateResult(False, "Quantity must be positive and finite")
        quantity = rules.normalize_quantity(quantity, is_market=order_type == OrderType.MARKET.value)
        if not quantity.is_finite() or quantity <= 0:
            return GateResult(False, "Quantity becomes zero after exchange step normalization")
        min_qty = rules.market_min_qty if order_type == OrderType.MARKET.value and rules.market_min_qty else rules.min_qty
        max_qty = rules.market_max_qty if order_type == OrderType.MARKET.value and rules.market_max_qty else rules.max_qty
        if quantity < min_qty:
            return GateResult(False, f"Quantity {quantity} is below minimum {min_qty}")
        if max_qty > 0 and quantity > max_qty:
            return GateResult(False, f"Quantity {quantity} exceeds maximum {max_qty}")

        price: Optional[Decimal] = None
        if order_type == OrderType.LIMIT.value:
            if time_in_force not in {
                TimeInForce.GTC,
                TimeInForce.IOC,
                TimeInForce.FOK,
                TimeInForce.POST_ONLY,
            }:
                return GateResult(False, "Unsupported LIMIT timeInForce")
            if intent.price is None:
                return GateResult(False, "LIMIT order requires a price")
            try:
                price = rules.normalize_price(
                    Decimal(str(intent.price)),
                    round_up=side == OrderSide.SELL,
                )
            except (InvalidOperation, TypeError, ValueError):
                return GateResult(False, "Invalid limit price")
            if not price.is_finite() or price <= 0:
                return GateResult(False, "Limit price must be positive and finite")
            if rules.parsed_from_exchange_info and (
                price < rules.min_price or price > rules.max_price
            ):
                return GateResult(False, "Limit price is outside the exchange price bounds")
            estimated_price = price
        else:
            if time_in_force != TimeInForce.GTC:
                return GateResult(False, "MARKET orders do not support this timeInForce")
            if intent.price is not None:
                return GateResult(False, "MARKET order must not provide a limit price")
            estimated_price = await self.adapter.get_fresh_market_price(
                symbol,
                getattr(intent.side, "value", intent.side),
            )
            if estimated_price is None:
                return GateResult(False, f"Fresh market price unavailable for {symbol}")

        if order_type == OrderType.LIMIT.value:
            # Binance's percent-price filters apply to submitted limit prices.
            # MARKET orders have no client-supplied price and use the fresh
            # executable quote above for notional estimation instead.
            reference_getter = getattr(self.adapter, "get_market_reference_price", None)
            reference_price = (
                reference_getter(symbol)
                if callable(reference_getter)
                else (
                    None
                    if rules.has_percent_price_filter
                    else getattr(self.adapter, "last_market_price", {}).get(symbol)
                )
            )
            if reference_price is None and rules.has_percent_price_filter:
                # Binance evaluates PERCENT_PRICE against mark price. A recent
                # book-ticker midpoint is not a valid substitute, so obtain a
                # fresh mark sample before rejecting the order.
                mark_getter = getattr(self.adapter, "get_fresh_market_price", None)
                if callable(mark_getter):
                    reference_price = await mark_getter(symbol)
            percent_allowed, percent_reason = rules.validate_percent_price(
                estimated_price,
                getattr(side, "value", side),
                reference_price,
            )
            if not percent_allowed:
                return GateResult(False, f"Binance percent-price filter rejected order: {percent_reason}")

        # Check the timestamp after price discovery.  A MARKET order may have
        # had no usable cached quote and therefore refresh from the fixed
        # book; that fresh REST sample must be accepted only if its own event
        # or receipt timestamp is within the same per-symbol bound.
        if risk_increasing and not emergency_fallback:
            has_market_sample = getattr(
                self.adapter, "has_authoritative_market_sample", None
            )
            if callable(has_market_sample) and not has_market_sample(symbol):
                return GateResult(
                    False,
                    f"Authoritative {_venue_label(self.adapter)} market sample unavailable for {symbol}",
                )
            last_event = getattr(self.adapter, "last_market_event_at", {}).get(symbol)
            age = _age_seconds(last_event)
            if age is None or age < 0 or age > _positive_float("MAX_MARKET_DATA_AGE_SEC", 3.0):
                return GateResult(False, f"Market data stale for {symbol}")

            if order_type == OrderType.MARKET.value:
                depth = (
                    getattr(self.adapter, "last_market_ask_qty", {}).get(symbol)
                    if side == OrderSide.BUY
                    else getattr(self.adapter, "last_market_bid_qty", {}).get(symbol)
                )
                if depth is None or not depth.is_finite() or depth <= 0:
                    return GateResult(False, f"Top-of-book depth is unknown for {symbol}")
                if quantity > depth:
                    return GateResult(
                        False,
                        f"Market quantity exceeds available top-of-book depth for {symbol}",
                    )

        notional = quantity * estimated_price
        if notional.is_nan() or notional.is_infinite() or notional <= 0:
            return GateResult(False, "Order notional is invalid")
        min_notional = rules.min_notional_for(order_type)
        if min_notional > 0 and notional < min_notional:
            return GateResult(False, f"Notional {notional} is below minimum {min_notional}")
        max_notional = rules.max_notional_for(order_type)
        if max_notional > 0 and notional > max_notional:
            return GateResult(False, f"Notional {notional} exceeds maximum {max_notional}")
        if risk_increasing and local_mainnet_target:
            local_gate = await _local_mainnet_risk_gate(
                self.adapter,
                intent,
                entry_price=estimated_price,
                quantity=quantity,
            )
            if local_gate is not None:
                return local_gate
        if intent.reduce_only and risk in {
            EconomicRiskClass.NEW_RISK,
            EconomicRiskClass.INCREASE_RISK,
        }:
            return GateResult(False, "reduceOnly order cannot be classified as risk increasing")
        if (
            require_reduce_only_for_risk_reduction
            and _is_risk_reducing(risk)
            and not intent.reduce_only
        ):
            return GateResult(False, "Risk-reducing and emergency orders must be reduceOnly")

        if intent.reduce_only and _is_risk_reducing(risk):
            reducible_exposure = Decimal("0")
            for position in await self.adapter.ledger.get_positions():
                if position.symbol != symbol:
                    continue
                position_side_matches = (
                    position_side == PositionSide.BOTH
                    or position.position_side in {position_side, PositionSide.BOTH}
                )
                if position_side_matches:
                    position_quantity = position.quantity
                    if not position_quantity.is_finite() or position_quantity == 0:
                        continue
                    # Binance positionAmt is signed: SELL reduces a positive
                    # long/BOTH position and BUY reduces a negative short/BOTH
                    # position.  A reduceOnly flag alone is not enough to
                    # prove that the requested side actually de-risks.
                    if (
                        side == OrderSide.SELL and position_quantity > 0
                    ) or (
                        side == OrderSide.BUY and position_quantity < 0
                    ):
                        reducible_exposure += abs(position_quantity)
            if reducible_exposure < quantity:
                return GateResult(
                    False,
                    f"reduceOnly side or quantity exceeds known {_venue_label(self.adapter)} exposure",
                )

        if risk_increasing:
            open_orders = await self.adapter.ledger.get_open_orders()
            current_open_orders = [
                order
                for order in open_orders
                if order.client_order_id != exclude_client_order_id
            ]
            if len(current_open_orders) + reserved_open_orders >= limits.max_open_orders:
                return GateResult(False, f"Maximum {_venue_label(self.adapter)} open-order count reached")

            existing_notional = Decimal("0")
            for order in current_open_orders:
                if order.price <= 0:
                    return GateResult(False, "Existing open-order notional is unknown")
                existing_notional += abs(order.quantity * order.price)
            for position in await self.adapter.ledger.get_positions():
                if position.quantity == 0:
                    continue
                mark_price = position.mark_price
                if mark_price is None or not mark_price.is_finite() or mark_price <= 0:
                    return GateResult(False, "Existing position notional is unknown")
                existing_notional += abs(position.quantity * mark_price)
            if existing_notional + reserved_notional + notional > limits.max_total_open_notional:
                return GateResult(False, f"Maximum {_venue_label(self.adapter)} total open notional exceeded")
            if notional > limits.max_single_order_notional:
                return GateResult(False, f"Maximum {_venue_label(self.adapter)} single-order notional exceeded")

            active_symbols = {
                order.symbol for order in current_open_orders
            }
            for position in await self.adapter.ledger.get_positions():
                if position.quantity != 0:
                    active_symbols.add(position.symbol)
            if symbol not in active_symbols and len(active_symbols) >= limits.max_active_exposure_chains:
                return GateResult(False, f"Maximum {_venue_label(self.adapter)} active exposure chains exceeded")

        return GateResult(
            True,
            "Passed",
            PreparedOrder(
                symbol=symbol,
                order_type=order_type,
                quantity=quantity,
                price=price,
                estimated_price=estimated_price,
                notional=notional,
            ),
        )
