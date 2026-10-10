"""Transactional outbox and idempotent persistence repositories."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional, Protocol

from apps.trading_worker.persistence.postgres.client import PostgresClient, redact_error
from domain.models import (
    ExchangeFill,
    ExchangePosition,
    ExecutionOrder,
    Instrument,
    RiskSnapshot,
)

logger = logging.getLogger("blessing.persistence.repositories")

_IMMUTABLE_IMAGE_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$", re.IGNORECASE)
_RUNTIME_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_LOCAL_RUNTIME_TARGET = "LOCAL"
_CLOUD_RUNTIME_TARGET = "CLOUD_RUN"
_HISTORY_SCAN_LEASE_SECONDS = 120
_HISTORY_REPLAY_OVERLAP = timedelta(hours=24)
_ALGO_PROTECTION_STATES = frozenset(
    {"PENDING", "PROTECTED", "CLOSE_PENDING", "CLOSED", "DEGRADED", "UNKNOWN"}
)
_ALGO_PROTECTION_TRANSITIONS = {
    "PENDING": frozenset(
        {"PENDING", "PROTECTED", "CLOSE_PENDING", "CLOSED", "DEGRADED", "UNKNOWN"}
    ),
    "PROTECTED": frozenset(
        {"PROTECTED", "CLOSE_PENDING", "DEGRADED", "UNKNOWN"}
    ),
    "CLOSE_PENDING": frozenset({"CLOSE_PENDING", "CLOSED", "DEGRADED", "UNKNOWN"}),
    "CLOSED": frozenset({"CLOSED"}),
    "DEGRADED": frozenset(
        {"DEGRADED", "PROTECTED", "CLOSE_PENDING", "UNKNOWN"}
    ),
    "UNKNOWN": frozenset(
        {"UNKNOWN", "PROTECTED", "CLOSE_PENDING", "DEGRADED"}
    ),
}
_ALGO_PROTECTION_IMMUTABLE_FIELDS = (
    "environment",
    "venue",
    "symbol",
    "entry_client_order_id",
    "basket_id",
    "mainnet_launch_id",
    "entry_side",
    "position_side",
    "requested_quantity",
    "stop_trigger_price",
    "take_profit_trigger_price",
    "stop_client_algo_id",
    "take_profit_client_algo_id",
    "management_mode",
)
_ALGO_PROTECTION_COLUMNS = (
    "environment",
    "venue",
    "symbol",
    "entry_client_order_id",
    "basket_id",
    "mainnet_launch_id",
    "entry_side",
    "position_side",
    "requested_quantity",
    "filled_quantity",
    "entry_average_price",
    "stop_trigger_price",
    "take_profit_trigger_price",
    "stop_algo_id",
    "take_profit_algo_id",
    "stop_client_algo_id",
    "take_profit_client_algo_id",
    "state",
    "state_reason",
    "first_fill_at",
    "protection_verified_at",
    "last_reconciled_at",
    "closed_at",
    "management_mode",
    "created_at",
    "updated_at",
)
_ALGO_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ALGO_EXCHANGE_ID_RE = re.compile(r"^[0-9]{1,64}$")
_LOCAL_EMERGENCY_CLOSE_ALGO_ID = "LOCAL_EMERGENCY_CLOSE"
_TRADING_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,32}$")
_HISTORY_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_BASKET_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_HISTORY_KINDS = frozenset({"ALL_ORDERS", "USER_TRADES", "ALL_ALGO_ORDERS"})
_HISTORY_FAILED_STATES = frozenset({"GAP", "UNKNOWN"})
_PREEXISTING_ALGO_TERMINAL_STATES = frozenset(
    {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED", "FINISHED"}
)


class InstrumentRulesProvider(Protocol):
    def __call__(self, symbol: str) -> Optional[Instrument]: ...


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _validate_launch_identity(
    *,
    runtime_target: str,
    image_digest: Optional[str],
    runtime_fingerprint: Optional[str],
) -> tuple[str, Optional[str], Optional[str]]:
    target = str(runtime_target).strip().upper()
    if target not in {_LOCAL_RUNTIME_TARGET, _CLOUD_RUNTIME_TARGET}:
        raise ValueError("launch session runtime_target must be LOCAL or CLOUD_RUN")

    if target == _LOCAL_RUNTIME_TARGET:
        if image_digest is not None:
            raise ValueError("Local launch sessions must not have an image digest")
        if runtime_fingerprint is None or not _RUNTIME_FINGERPRINT_RE.fullmatch(
            str(runtime_fingerprint).strip()
        ):
            raise ValueError("Local launch sessions require a 64-hex runtime fingerprint")
        return target, None, str(runtime_fingerprint).strip().lower()

    if not image_digest or not _IMMUTABLE_IMAGE_RE.fullmatch(str(image_digest)):
        raise ValueError("Cloud Run launch sessions require an immutable image digest")
    if runtime_fingerprint is not None:
        fingerprint = str(runtime_fingerprint).strip()
        if not _RUNTIME_FINGERPRINT_RE.fullmatch(fingerprint):
            raise ValueError("runtime fingerprint must be 64 hexadecimal characters")
        runtime_fingerprint = fingerprint.lower()
    return target, str(image_digest), runtime_fingerprint


def _utc_datetime(value: object) -> datetime:
    """Normalize exchange timestamps without ever falling back to naive UTC."""

    if value is None:
        return datetime.now(UTC)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float, Decimal)):
        numeric = float(value)
        if numeric > 100_000_000_000:
            numeric /= 1000.0
        parsed = datetime.fromtimestamp(numeric, tz=UTC)
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
    else:
        raise ValueError(f"unsupported timestamp type: {type(value).__name__}")

    if parsed.tzinfo is None:
        raise ValueError("persistence timestamps must be timezone-aware")
    return parsed.astimezone(UTC)


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return _utc_datetime(value).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(_enum_value(value))


def _decode_json_mapping(value: object, field_name: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} JSON is invalid") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} JSON must be an object")
    return dict(value)


def _decode_protection_record(row: Any) -> dict[str, Any]:
    result = dict(row)
    if result.get("closure_evidence") is not None:
        result["closure_evidence"] = _decode_json_mapping(
            result["closure_evidence"], "Mainnet closure evidence"
        )
    return result


def _model_payload(model: object) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        payload = model.model_dump(mode="json")  # type: ignore[attr-defined]
    elif hasattr(model, "dict"):
        payload = model.dict()  # type: ignore[attr-defined]
    else:
        raise TypeError(f"unsupported persistence model: {type(model).__name__}")
    return json.loads(json.dumps(payload, default=_json_default))


def _instrument_payload(instrument: Optional[Instrument]) -> Optional[dict[str, Any]]:
    if instrument is None:
        return None
    payload = _model_payload(instrument)
    payload["market_type"] = str(_enum_value(instrument.market_type))
    return payload


def _fill_storage_id(fill: ExchangeFill, instrument: Instrument) -> str:
    """Return a bounded id for a venue-scoped exchange trade identity."""

    identity = (
        f"{instrument.venue}:{str(fill.symbol).upper()}:{fill.exchange_trade_id}"
    )
    # fills.fill_id is VARCHAR(64); a digest preserves the complete identity
    # without truncating an exchange-provided trade id.
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class PersistenceEvent(Protocol):
    event_id: str
    event_type: str
    idempotency_key: str
    aggregate_type: str
    aggregate_id: str
    created_at: datetime
    payload: Mapping[str, Any]


class InstrumentRulesUnavailable(ValueError):
    """Raised when a durable event lacks exchange-derived instrument metadata."""


class OrderRepository:
    def __init__(self, db: PostgresClient) -> None:
        self.db = db

    async def save_order(
        self,
        order: ExecutionOrder,
        instrument: Instrument,
        connection: Any,
    ) -> None:
        await _ensure_instrument(connection, instrument)
        basket_id = str(getattr(order, "basket_id", None) or "").strip()
        if basket_id:
            strategy_id = str(getattr(order, "strategy_id", "portfolio") or "portfolio").strip()
            if len(basket_id) > 64 or not strategy_id or len(strategy_id) > 64:
                raise ValueError("durable order basket identity is invalid")
            direction = "LONG" if str(_enum_value(order.side)).upper() == "BUY" else "SHORT"
            if bool(getattr(order, "reduce_only", False)):
                # A closing order reverses the entry side by definition. It
                # must reference the existing exposure basket without
                # re-inferring or rewriting that basket's LONG/SHORT identity.
                basket = await connection.fetchrow(
                    """
                    SELECT basket_id FROM baskets
                    WHERE basket_id = $1 AND venue = $2 AND instrument = $3
                    """,
                    basket_id,
                    instrument.venue,
                    instrument.symbol,
                )
            else:
                basket = await connection.fetchrow(
                    """
                    INSERT INTO baskets (
                        basket_id, strategy_id, venue, instrument, direction, state,
                        grid_depth, max_grid_levels, created_at, last_updated
                    ) VALUES ($1, $2, $3, $4, $5, 'NEW', 0, 1, $6, $6)
                    ON CONFLICT (basket_id) DO UPDATE
                    SET basket_id = EXCLUDED.basket_id
                    WHERE baskets.venue = EXCLUDED.venue
                      AND baskets.instrument = EXCLUDED.instrument
                      AND baskets.direction = EXCLUDED.direction
                    RETURNING basket_id
                    """,
                    basket_id,
                    strategy_id,
                    instrument.venue,
                    instrument.symbol,
                    direction,
                    _utc_datetime(order.timestamp),
                )
            if basket is None or str(basket["basket_id"]) != basket_id:
                raise ValueError("durable order basket identity conflicts with the existing basket")
        await connection.execute(
            """
            INSERT INTO orders (
                client_order_id, exchange_order_id, basket_id, symbol, venue, side, order_type,
                order_role, price, quantity, status, time_in_force, position_side,
                created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $14
            ) ON CONFLICT (client_order_id) DO UPDATE SET
                exchange_order_id = EXCLUDED.exchange_order_id,
                basket_id = EXCLUDED.basket_id,
                venue = EXCLUDED.venue,
                status = EXCLUDED.status,
                price = EXCLUDED.price,
                quantity = EXCLUDED.quantity,
                time_in_force = EXCLUDED.time_in_force,
                position_side = EXCLUDED.position_side,
                updated_at = EXCLUDED.updated_at
            """,
            order.client_order_id,
            order.exchange_order_id,
            basket_id or None,
            order.symbol,
            instrument.venue,
            str(_enum_value(order.side)),
            str(_enum_value(order.order_type)),
            "SYSTEM_ORDER",
            order.price,
            order.quantity,
            str(order.status),
            str(_enum_value(order.time_in_force)),
            str(_enum_value(order.position_side)),
            _utc_datetime(order.timestamp),
        )


class FillRepository:
    def __init__(self, db: PostgresClient) -> None:
        self.db = db

    async def save_fill(
        self,
        fill: ExchangeFill,
        instrument: Instrument,
        connection: Any,
    ) -> None:
        await _ensure_instrument(connection, instrument)
        # The exchange trade id is not globally unique across Binance
        # environments. Store a bounded digest of the canonical venue-scoped
        # identity while preserving exchange_trade_id as the provider's
        # original identifier.
        fill_id = _fill_storage_id(fill, instrument)
        await connection.execute(
            """
            INSERT INTO fills (
                fill_id, client_order_id, exchange_order_id, exchange_trade_id, symbol, side,
                venue, position_side, price, quantity, fee, fee_asset, is_maker, executed_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (venue, exchange_trade_id) DO UPDATE SET
                client_order_id = EXCLUDED.client_order_id,
                exchange_order_id = EXCLUDED.exchange_order_id,
                exchange_trade_id = EXCLUDED.exchange_trade_id,
                venue = EXCLUDED.venue,
                position_side = EXCLUDED.position_side,
                price = EXCLUDED.price,
                quantity = EXCLUDED.quantity,
                fee = EXCLUDED.fee,
                fee_asset = EXCLUDED.fee_asset,
                is_maker = EXCLUDED.is_maker,
                executed_at = EXCLUDED.executed_at
            """,
            fill_id,
            fill.client_order_id,
            fill.exchange_order_id,
            fill.exchange_trade_id,
            fill.symbol,
            str(_enum_value(fill.side)),
            instrument.venue,
            str(_enum_value(fill.position_side)),
            fill.price,
            fill.quantity,
            fill.commission,
            fill.commission_asset,
            fill.maker,
            _utc_datetime(fill.event_time or fill.transaction_time),
        )


class PositionRepository:
    def __init__(self, db: PostgresClient) -> None:
        self.db = db

    async def save_position(
        self,
        position: ExchangePosition,
        instrument: Instrument,
        connection: Any,
    ) -> None:
        await _ensure_instrument(connection, instrument)
        quantity = Decimal(str(position.quantity))
        direction = "LONG" if quantity > 0 else "SHORT" if quantity < 0 else "FLAT"
        await connection.execute(
            """
            INSERT INTO positions (
                venue, symbol, position_side, direction, quantity, entry_price, mark_price,
                liquidation_price, unrealized_pnl, leverage, margin_type,
                initial_margin, maintenance_margin, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 0.0, 0.0, $12)
            ON CONFLICT (venue, symbol, position_side) DO UPDATE SET
                direction = EXCLUDED.direction,
                quantity = EXCLUDED.quantity,
                entry_price = EXCLUDED.entry_price,
                mark_price = EXCLUDED.mark_price,
                liquidation_price = EXCLUDED.liquidation_price,
                unrealized_pnl = EXCLUDED.unrealized_pnl,
                leverage = EXCLUDED.leverage,
                margin_type = EXCLUDED.margin_type,
                updated_at = EXCLUDED.updated_at
            """,
            instrument.venue,
            position.symbol,
            str(_enum_value(position.position_side)),
            direction,
            quantity,
            position.entry_price,
            position.mark_price or Decimal("0"),
            position.liquidation_price,
            position.unrealized_pnl,
            position.leverage,
            position.margin_type,
            _utc_datetime(position.event_time),
        )


class RiskSnapshotRepository:
    def __init__(self, db: PostgresClient) -> None:
        self.db = db

    async def save_snapshot(self, snapshot: RiskSnapshot, connection: Any) -> None:
        reasons_json = json.dumps(
            {
                "hard_violations": snapshot.hard_violations,
                "soft_violations": snapshot.soft_violations,
                "realized_pnl_24h": str(snapshot.realized_pnl_24h),
                "realized_pnl_24h_known": snapshot.realized_pnl_24h_known,
            }
        )
        await connection.execute(
            """
            INSERT INTO portfolio_snapshots (
                timestamp, total_equity, total_balance, free_margin, used_margin,
                margin_utilization_pct, effective_leverage, current_drawdown_pct,
                risk_state, aggregate_long_exposure, aggregate_short_exposure,
                crypto_beta_exposure_pct, active_baskets_count, kill_switch_active, reasons
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            """,
            _utc_datetime(snapshot.timestamp),
            snapshot.portfolio_equity,
            snapshot.portfolio_equity,
            snapshot.portfolio_equity
            * (Decimal("1") - snapshot.margin_utilization_pct / Decimal("100")),
            snapshot.portfolio_equity * snapshot.margin_utilization_pct / Decimal("100"),
            snapshot.margin_utilization_pct,
            snapshot.effective_leverage,
            snapshot.current_drawdown_pct,
            str(_enum_value(snapshot.risk_state)),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            0,
            "KILL_SWITCH" in {item.upper() for item in snapshot.hard_violations},
            reasons_json,
        )


def _protection_venue(value: object) -> tuple[str, str]:
    venue = str(value or "").strip().lower()
    environment_by_venue = {
        "binance_testnet": "TESTNET",
        "binance_mainnet": "MAINNET",
    }
    environment = environment_by_venue.get(venue)
    if environment is None:
        raise ValueError("protection venue must explicitly identify Binance Testnet or Mainnet")
    return venue, environment


def _protection_key(
    venue: object, symbol: object, entry_client_order_id: object
) -> tuple[str, str, str]:
    normalized_venue, _ = _protection_venue(venue)
    normalized_symbol = str(symbol or "").strip().upper()
    client_order_id = str(entry_client_order_id or "").strip()
    if not _TRADING_SYMBOL_RE.fullmatch(normalized_symbol):
        raise ValueError("protection symbol is invalid")
    if not _ALGO_CLIENT_ID_RE.fullmatch(client_order_id):
        raise ValueError("entry client order ID is invalid")
    return normalized_venue, normalized_symbol, client_order_id


def _protection_decimal(value: object, name: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{name} must be a finite decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite() or (result < 0 if allow_zero else result <= 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be finite and {qualifier}")
    try:
        representable = result == result.quantize(Decimal("0.0000000001"))
    except InvalidOperation:
        representable = False
    if not representable or abs(result) >= Decimal(10**18):
        raise ValueError(f"{name} exceeds NUMERIC(28, 10) precision")
    return result


def _protection_timestamp(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        return _utc_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a timezone-aware timestamp") from exc


def _validate_protection_shape(record: Mapping[str, Any]) -> None:
    state = str(record["state"]).upper()
    if state not in _ALGO_PROTECTION_STATES:
        raise ValueError("protection state is invalid")
    requested = record["requested_quantity"]
    filled = record["filled_quantity"]
    average = record["entry_average_price"]
    if filled > requested:
        raise ValueError("filled quantity cannot exceed requested quantity")
    if filled > 0 and average is None:
        raise ValueError("filled protection record requires an entry average price")
    if filled == 0 and average is not None:
        raise ValueError("entry average price requires a positive filled quantity")

    stop = record["stop_trigger_price"]
    target = record["take_profit_trigger_price"]
    if stop == target:
        raise ValueError("stop and take-profit trigger prices must be distinct")
    if average is not None:
        if record["entry_side"] == "BUY" and not stop < average < target:
            raise ValueError("BUY entry requires stop < average entry < take-profit")
        if record["entry_side"] == "SELL" and not target < average < stop:
            raise ValueError("SELL entry requires take-profit < average entry < stop")

    stop_algo_id = record["stop_algo_id"]
    target_algo_id = record["take_profit_algo_id"]
    if stop_algo_id is not None and target_algo_id is not None and stop_algo_id == target_algo_id:
        raise ValueError("stop and take-profit exchange Algo IDs must be distinct")
    if record["stop_client_algo_id"] == record["take_profit_client_algo_id"]:
        raise ValueError("stop and take-profit clientAlgoIds must be distinct")
    if state == "PROTECTED" and (
        filled <= 0
        or average is None
        or stop_algo_id is None
        or target_algo_id is None
        or record["protection_verified_at"] is None
    ):
        raise ValueError("PROTECTED requires a fill and verified stop and take-profit Algo IDs")
    if state == "CLOSED" and record["closed_at"] is None:
        raise ValueError("CLOSED protection requires a closed_at timestamp")
    if state == "CLOSED" and str(record.get("environment", "")).upper() == "MAINNET":
        evidence = _decode_json_mapping(
            record.get("closure_evidence"), "Mainnet closure evidence"
        )
        if evidence.get("kind") not in {
            "LEGACY_UNVERIFIED",
            "BINANCE_ALGO_CLOSE_VERIFIED",
            "LOCAL_EMERGENCY_CLOSE_VERIFIED",
            "UNFILLED_ENTRY_TERMINAL",
        }:
            raise ValueError("Mainnet CLOSED owner has no recognized durable closure evidence")
        if evidence.get("kind") in {
            "BINANCE_ALGO_CLOSE_VERIFIED",
            "LOCAL_EMERGENCY_CLOSE_VERIFIED",
        }:
            proof_hash = str(evidence.pop("proof_sha256", ""))
            canonical = json.dumps(
                evidence, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            if (
                not re.fullmatch(r"[0-9a-f]{64}", proof_hash)
                or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != proof_hash
                or evidence.get("symbol") != str(record.get("symbol", "")).upper()
                or evidence.get("entry_client_order_id")
                != str(record.get("entry_client_order_id", ""))
                or evidence.get("mainnet_launch_id") != record.get("mainnet_launch_id")
                or evidence.get("basket_id") != record.get("basket_id")
                or evidence.get("order_status") != "FILLED"
                or Decimal(str(evidence.get("executed_quantity")))
                != record.get("filled_quantity")
                or Decimal(str(evidence.get("trade_quantity")))
                != record.get("filled_quantity")
                or Decimal(str(evidence.get("position_quantity"))) != 0
                or evidence.get("open_child_order_ids") != []
                or evidence.get("open_owner_algo_ids") != []
                or (
                    evidence.get("kind") == "LOCAL_EMERGENCY_CLOSE_VERIFIED"
                    and evidence.get("algo_id") != _LOCAL_EMERGENCY_CLOSE_ALGO_ID
                )
            ):
                raise ValueError("Mainnet CLOSED owner closure proof is corrupt or mis-scoped")
        elif evidence.get("kind") == "UNFILLED_ENTRY_TERMINAL":
            proof_hash = str(evidence.pop("proof_sha256", ""))
            canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if (
                not re.fullmatch(r"[0-9a-f]{64}", proof_hash)
                or hashlib.sha256(canonical.encode("utf-8")).hexdigest() != proof_hash
                or evidence.get("symbol") != str(record.get("symbol", "")).upper()
                or evidence.get("entry_client_order_id") != str(record.get("entry_client_order_id", ""))
                or evidence.get("mainnet_launch_id") != record.get("mainnet_launch_id")
                or evidence.get("basket_id") != record.get("basket_id")
                or evidence.get("order_status") not in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
                or evidence.get("client_order_id") != str(record.get("entry_client_order_id", ""))
                or Decimal(str(evidence.get("executed_quantity"))) != 0
                or Decimal(str(evidence.get("original_quantity"))) != record.get("requested_quantity")
                or str(evidence.get("entry_side", "")).upper() != str(record.get("entry_side", "")).upper()
                or str(evidence.get("position_side", "")).upper() != str(record.get("position_side", "")).upper()
            ):
                raise ValueError("Mainnet zero-fill closure proof is corrupt or mis-scoped")
    elif record.get("closure_evidence") is not None:
        raise ValueError("closure evidence is only valid for a Mainnet CLOSED owner")


def _validate_protection_transition(previous: str, updated: str) -> None:
    if updated not in _ALGO_PROTECTION_TRANSITIONS[previous]:
        raise ValueError(f"unsafe protection state transition: {previous} -> {updated}")


class AlgoProtectionRepository:
    """Durably own one Binance stop/target pair per environment-scoped entry."""

    def __init__(self, db: PostgresClient) -> None:
        self.db = db

    @staticmethod
    def _normalize_input(record: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(record, Mapping):
            raise TypeError("protection record must be a mapping")
        allowed = set(_ALGO_PROTECTION_COLUMNS) - {"created_at", "updated_at"}
        unknown = set(record) - allowed
        if unknown:
            raise ValueError("protection record contains unsupported fields")

        venue, symbol, entry_client_order_id = _protection_key(
            record.get("venue"), record.get("symbol"), record.get("entry_client_order_id")
        )
        expected_environment = _protection_venue(venue)[1]
        environment = str(record.get("environment", "")).strip().upper()
        if environment != expected_environment:
            raise ValueError("protection environment must match its explicit Binance venue")

        normalized: dict[str, Any] = {
            "environment": environment,
            "venue": venue,
            "symbol": symbol,
            "entry_client_order_id": entry_client_order_id,
        }
        raw_basket_id = record.get("basket_id")
        basket_id = str(raw_basket_id or "").strip()
        if basket_id and not _BASKET_ID_RE.fullmatch(basket_id):
            raise ValueError("protection basket_id is invalid")
        if environment == "MAINNET" and not basket_id:
            raise ValueError("Mainnet protection owner requires a durable basket_id")
        normalized["basket_id"] = basket_id or None
        raw_launch_id = record.get("mainnet_launch_id")
        launch_id = str(raw_launch_id or "").strip()
        if launch_id and not _HISTORY_RUN_ID_RE.fullmatch(launch_id):
            raise ValueError("protection mainnet_launch_id is invalid")
        if environment == "TESTNET" and launch_id:
            raise ValueError("Testnet protection owner cannot reference a Mainnet launch")
        if environment == "MAINNET" and not launch_id:
            raise ValueError("Mainnet protection owner requires a durable launch identity")
        normalized["mainnet_launch_id"] = launch_id or None
        management_mode = record.get("management_mode")
        if management_mode is not None:
            management_mode = str(management_mode).strip().upper()
            if management_mode not in {"QUICK", "HOLD"}:
                raise ValueError("management_mode must be QUICK or HOLD")
        if environment == "MAINNET" and management_mode not in {"QUICK", "HOLD"}:
            raise ValueError("Mainnet protection owner requires a locked management_mode")
        normalized["management_mode"] = management_mode
        for name in ("entry_side", "position_side"):
            if name not in record:
                continue
            normalized[name] = str(record[name] or "").strip().upper()
        if "entry_side" in normalized and normalized["entry_side"] not in {"BUY", "SELL"}:
            raise ValueError("entry_side must be BUY or SELL")
        if "position_side" in normalized and normalized["position_side"] not in {
            "BOTH",
            "LONG",
            "SHORT",
        }:
            raise ValueError("position_side must be BOTH, LONG, or SHORT")
        if "entry_side" in normalized and "position_side" in normalized:
            allowed_position = (
                {"BOTH", "LONG"}
                if normalized["entry_side"] == "BUY"
                else {"BOTH", "SHORT"}
            )
            if normalized["position_side"] not in allowed_position:
                raise ValueError("entry side and position side are inconsistent")

        for name in (
            "requested_quantity",
            "filled_quantity",
            "entry_average_price",
            "stop_trigger_price",
            "take_profit_trigger_price",
        ):
            if name not in record:
                continue
            value = record[name]
            if value is None and name == "entry_average_price":
                normalized[name] = None
            else:
                normalized[name] = _protection_decimal(
                    value, name, allow_zero=(name == "filled_quantity")
                )

        for name in ("stop_algo_id", "take_profit_algo_id"):
            if name not in record:
                continue
            value = record[name]
            if value is None:
                normalized[name] = None
            else:
                token = str(value).strip()
                if not _ALGO_EXCHANGE_ID_RE.fullmatch(token) or int(token) <= 0:
                    raise ValueError(f"{name} must be a positive exchange Algo ID")
                normalized[name] = token

        for name in ("stop_client_algo_id", "take_profit_client_algo_id"):
            if name not in record:
                continue
            token = str(record[name] or "").strip()
            if not _ALGO_CLIENT_ID_RE.fullmatch(token):
                raise ValueError(f"{name} is invalid")
            normalized[name] = token

        if "state" in record:
            normalized["state"] = str(record["state"] or "").strip().upper()
        if "state_reason" in record:
            reason = record["state_reason"]
            if reason is not None:
                reason = str(reason).strip()
                if len(reason) > 256 or any(ord(char) < 32 for char in reason):
                    raise ValueError("state_reason is invalid")
            normalized["state_reason"] = reason

        for name in (
            "first_fill_at",
            "protection_verified_at",
            "last_reconciled_at",
            "closed_at",
        ):
            if name in record:
                normalized[name] = _protection_timestamp(record[name], name)
        return normalized

    @staticmethod
    def _new_record(values: dict[str, Any]) -> dict[str, Any]:
        required = set(_ALGO_PROTECTION_IMMUTABLE_FIELDS) | {
            "entry_side",
            "position_side",
        }
        # The immutable tuple includes the environment-scoped key and the
        # order/protection identity; retain an explicit guard for a new row.
        required |= {
            "environment",
            "venue",
            "symbol",
            "entry_client_order_id",
            "entry_side",
            "position_side",
            "requested_quantity",
            "stop_trigger_price",
            "take_profit_trigger_price",
            "stop_client_algo_id",
            "take_profit_client_algo_id",
        }
        missing = required - set(values)
        if missing:
            raise ValueError("new protection record is missing required identity or sizing fields")
        result = dict(values)
        result.setdefault("filled_quantity", Decimal(0))
        result.setdefault("entry_average_price", None)
        result.setdefault("stop_algo_id", None)
        result.setdefault("take_profit_algo_id", None)
        result.setdefault("management_mode", None)
        result.setdefault("state", "PENDING")
        result.setdefault("state_reason", None)
        if str(result["state"]).upper() == "CLOSED":
            raise ValueError("new protection owner cannot start CLOSED without a prior durable state")
        for name in ("first_fill_at", "protection_verified_at", "last_reconciled_at", "closed_at"):
            result.setdefault(name, None)
        now = datetime.now(UTC)
        if result["filled_quantity"] > 0 and result["first_fill_at"] is None:
            result["first_fill_at"] = now
        if result["state"] == "PROTECTED" and result["protection_verified_at"] is None:
            result["protection_verified_at"] = now
        if result["state"] == "CLOSED" and result["closed_at"] is None:
            result["closed_at"] = now
        _validate_protection_shape(result)
        return result

    @staticmethod
    def _merge_existing(current: Mapping[str, Any], supplied: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(current)
        for name, value in supplied.items():
            if name in _ALGO_PROTECTION_IMMUTABLE_FIELDS and result.get(name) != value:
                raise ValueError(f"protection identity field {name} cannot be changed")
            result[name] = value

        # Row-locked merge must retain irreversible exchange-attempt evidence
        # when a delayed worker submits a marker-free diagnostic snapshot.
        def reason_fields(value: Any) -> dict[str, str]:
            return dict(part.split("=", 1) for part in str(value or "").split(";") if "=" in part)

        old_reason = reason_fields(current.get("state_reason"))
        new_reason = reason_fields(result.get("state_reason"))
        priorities = ("local_close_client_order_id", "close_submission", "algo_cancel", "entry_cancel")
        for marker in priorities:
            if marker not in old_reason:
                continue
            old_value = old_reason[marker]
            new_value = new_reason.get(marker)
            if marker == "local_close_client_order_id" and new_value not in (None, old_value):
                raise ValueError("durable emergency close identity cannot change")
            if new_value is None or old_value in {"ATTEMPTED", "CONFIRMED"}:
                new_reason[marker] = old_value
            elif old_value == "ATTEMPTED_UNKNOWN" and new_value != "CONFIRMED":
                new_reason[marker] = old_value
        if any(marker in old_reason for marker in priorities):
            ordered = [key for key in priorities if key in new_reason]
            ordered.extend(key for key in new_reason if key not in priorities)
            result["state_reason"] = ";".join(f"{key}={new_reason[key]}" for key in ordered)[:256]

        for name in ("stop_algo_id", "take_profit_algo_id"):
            old, new = current.get(name), result.get(name)
            if old is not None and new != old:
                raise ValueError(f"assigned {name} is immutable")
        if result["filled_quantity"] < current["filled_quantity"]:
            raise ValueError("filled quantity cannot decrease")
        if (
            result["filled_quantity"] == current["filled_quantity"]
            and result["entry_average_price"] != current["entry_average_price"]
        ):
            raise ValueError("entry average cannot change without an additional fill")

        previous_state = str(current["state"])
        updated_state = str(result["state"])
        _validate_protection_transition(previous_state, updated_state)
        if previous_state == "CLOSED":
            for name in (
                "filled_quantity",
                "entry_average_price",
                "stop_algo_id",
                "take_profit_algo_id",
            ):
                if result.get(name) != current.get(name):
                    raise ValueError("CLOSED protection records cannot change execution facts")
        if updated_state == "CLOSED" and not (
            previous_state == "CLOSE_PENDING"
            or (previous_state == "PENDING" and result["filled_quantity"] == 0)
        ):
            raise ValueError("CLOSED requires CLOSE_PENDING or a definitively unfilled PENDING owner")
        if (
            updated_state == "CLOSED"
            and previous_state != "CLOSED"
            and str(result.get("environment", "")).upper() == "MAINNET"
        ):
            raise ValueError("Mainnet CLOSED requires exchange reconciliation proof")
        for name in ("first_fill_at", "closed_at"):
            old, new = current.get(name), result.get(name)
            if old is not None and new != old:
                raise ValueError(f"{name} is immutable once recorded")
        previous_reconciled = current.get("last_reconciled_at")
        updated_reconciled = result.get("last_reconciled_at")
        if (
            previous_reconciled is not None
            and (updated_reconciled is None or updated_reconciled < previous_reconciled)
        ):
            raise ValueError("last_reconciled_at cannot be cleared or move backwards")
        previous_verified = current.get("protection_verified_at")
        updated_verified = result.get("protection_verified_at")
        if (
            previous_verified is not None
            and (updated_verified is None or updated_verified < previous_verified)
        ):
            raise ValueError("protection_verified_at cannot be cleared or move backwards")

        now = datetime.now(UTC)
        if result["filled_quantity"] > 0 and result.get("first_fill_at") is None:
            result["first_fill_at"] = now
        if updated_state == "PROTECTED" and result.get("protection_verified_at") is None:
            result["protection_verified_at"] = now
        if updated_state == "CLOSED" and result.get("closed_at") is None:
            result["closed_at"] = now
        _validate_protection_shape(result)
        return result

    async def upsert_protection(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Create an entry ownership record or apply a validated monotone update."""

        supplied = self._normalize_input(record)
        key = (supplied["venue"], supplied["symbol"], supplied["entry_client_order_id"])
        async with self.db.transaction() as connection:
            # PostgresClient.transaction yields its pooled connection; direct
            # asyncpg.Connection.transaction() yields None while the connection
            # remains the transaction scope. Support both for isolated SQL
            # integration tests without changing production pooling semantics.
            connection = connection or self.db
            current = await connection.fetchrow(
                """
                SELECT * FROM binance_algo_protections
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                FOR UPDATE
                """,
                *key,
            )
            if current is None:
                values = self._new_record(supplied)
                inserted = await connection.fetchrow(
                    """
                    INSERT INTO binance_algo_protections (
                        environment, venue, symbol, entry_client_order_id, basket_id, mainnet_launch_id, entry_side,
                        position_side, requested_quantity, filled_quantity, entry_average_price,
                        stop_trigger_price, take_profit_trigger_price, stop_algo_id,
                        take_profit_algo_id, stop_client_algo_id, take_profit_client_algo_id,
                        management_mode, state, state_reason, first_fill_at, protection_verified_at,
                        last_reconciled_at, closed_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                        $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24
                    ) ON CONFLICT (venue, symbol, entry_client_order_id) DO NOTHING
                    RETURNING *
                    """,
                    *(
                        values[name]
                        for name in (
                            "environment",
                            "venue",
                            "symbol",
                            "entry_client_order_id",
                            "basket_id",
                            "mainnet_launch_id",
                            "entry_side",
                            "position_side",
                            "requested_quantity",
                            "filled_quantity",
                            "entry_average_price",
                            "stop_trigger_price",
                            "take_profit_trigger_price",
                            "stop_algo_id",
                            "take_profit_algo_id",
                            "stop_client_algo_id",
                            "take_profit_client_algo_id",
                            "management_mode",
                            "state",
                            "state_reason",
                            "first_fill_at",
                            "protection_verified_at",
                            "last_reconciled_at",
                            "closed_at",
                        )
                    ),
                )
                if inserted is not None:
                    return dict(inserted)
                # Another writer won the unique-key race. Lock its row, then
                # run the same immutable-identity and lifecycle checks as an
                # ordinary update.
                current = await connection.fetchrow(
                    """
                    SELECT * FROM binance_algo_protections
                    WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                    FOR UPDATE
                    """,
                    *key,
                )
                if current is None:
                    raise RuntimeError("Algo protection upsert lost its unique-key race")

            current_values = dict(current)
            merged = self._merge_existing(current_values, supplied)
            mutable_fields = (
                "filled_quantity",
                "entry_average_price",
                "stop_algo_id",
                "take_profit_algo_id",
                "state",
                "state_reason",
                "first_fill_at",
                "protection_verified_at",
                "last_reconciled_at",
                "closed_at",
            )
            if all(current_values.get(name) == merged.get(name) for name in mutable_fields):
                return current_values
            updated = await connection.fetchrow(
                """
                UPDATE binance_algo_protections
                SET filled_quantity = $4, entry_average_price = $5,
                    stop_algo_id = $6, take_profit_algo_id = $7,
                    state = $8, state_reason = $9, first_fill_at = $10,
                    protection_verified_at = $11, last_reconciled_at = $12,
                    closed_at = $13, updated_at = CURRENT_TIMESTAMP
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                RETURNING *
                """,
                *key,
                merged["filled_quantity"],
                merged["entry_average_price"],
                merged["stop_algo_id"],
                merged["take_profit_algo_id"],
                merged["state"],
                merged["state_reason"],
                merged["first_fill_at"],
                merged["protection_verified_at"],
                merged["last_reconciled_at"],
                merged["closed_at"],
            )
            if updated is None:
                raise RuntimeError("Algo protection update could not be read back")
            return dict(updated)

    async def get_protection(
        self, venue: str, symbol: str, entry_client_order_id: str
    ) -> dict[str, Any] | None:
        key = _protection_key(venue, symbol, entry_client_order_id)
        row = await self.db.fetchrow(
            """
            SELECT * FROM binance_algo_protections
            WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
            """,
            *key,
        )
        return _decode_protection_record(row) if row is not None else None

    async def claim_mainnet_entry_cancel(
        self, record: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Atomically persist the one-shot marker authorizing an entry DELETE.

        Returning ``None`` means this worker did not win the durable claim and
        must only perform exact-order read-back. The marker is permanent even
        if the exchange response is ambiguous; cancellation is never retried.
        """
        supplied = self._normalize_input(record)
        key = (supplied["venue"], supplied["symbol"], supplied["entry_client_order_id"])
        if supplied["environment"] != "MAINNET" or key[0] != "binance_mainnet":
            raise ValueError("entry cancel claim requires a Local Mainnet owner")

        async with self.db.transaction() as connection:
            connection = connection or self.db
            current_row = await connection.fetchrow(
                """
                SELECT * FROM binance_algo_protections
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                FOR UPDATE
                """,
                *key,
            )
            if current_row is None:
                return None
            current = dict(current_row)
            for name in (
                "environment", "venue", "symbol", "entry_client_order_id",
                "basket_id", "mainnet_launch_id", "entry_side", "position_side",
                "requested_quantity", "management_mode",
            ):
                if current.get(name) != supplied.get(name):
                    return None
            if str(current.get("state") or "").upper() not in {"PENDING", "PROTECTED", "UNKNOWN"}:
                return None

            markers = dict(
                part.split("=", 1)
                for part in str(current.get("state_reason") or "").split(";")
                if "=" in part
            )
            # Any prior marker or close reservation is a query-only state.
            if "entry_cancel" in markers or "local_close_client_order_id" in markers:
                return None
            markers["entry_cancel"] = "ATTEMPTED_UNKNOWN"
            priority = ("local_close_client_order_id", "close_submission", "algo_cancel", "entry_cancel")
            ordered = [name for name in priority if name in markers]
            ordered.extend(name for name in markers if name not in priority)
            reason = ";".join(f"{name}={markers[name]}" for name in ordered)
            if len(reason) > 256:
                return None

            updated = await connection.fetchrow(
                """
                UPDATE binance_algo_protections
                SET state_reason = $4, last_reconciled_at = clock_timestamp(),
                    updated_at = clock_timestamp()
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                  AND state = $5 AND state_reason IS NOT DISTINCT FROM $6
                RETURNING *
                """,
                *key,
                reason,
                current["state"],
                current.get("state_reason"),
            )
            if updated is None:
                return None
            result = dict(updated)
            if result.get("state_reason") != reason:
                return None
            return result

    async def claim_testnet_protection_close(
        self, *, venue: str, symbol: str, entry_client_order_id: str,
        entry_side: str, position_side: str, filled_quantity: Decimal,
        stop_algo_id: str, take_profit_algo_id: str, state_reason: str,
    ) -> dict[str, Any] | None:
        """Atomically claim a single Testnet close from PROTECTED to CLOSE_PENDING."""
        normalized_venue, environment = _protection_venue(venue)
        normalized_symbol, normalized_entry = _protection_key(
            normalized_venue, symbol, entry_client_order_id
        )[1:]
        if environment != "TESTNET" or normalized_venue != "binance_testnet":
            raise ValueError("Testnet close claim requires the Testnet venue")
        side = str(entry_side).strip().upper()
        pos_side = str(position_side).strip().upper()
        quantity = _protection_decimal(filled_quantity, "filled_quantity")
        reason = str(state_reason).strip()
        if side not in {"BUY", "SELL"} or pos_side != "BOTH" or len(reason) > 256:
            raise ValueError("Testnet close claim identity is invalid")
        async with self.db.transaction() as connection:
            connection = connection or self.db
            row = await connection.fetchrow(
                """
                UPDATE binance_algo_protections
                SET state = 'CLOSE_PENDING', state_reason = $8, updated_at = CURRENT_TIMESTAMP
                WHERE environment = 'TESTNET' AND venue = $1 AND symbol = $2
                  AND entry_client_order_id = $3 AND state = 'PROTECTED'
                  AND entry_side = $4 AND position_side = $5 AND filled_quantity = $6
                  AND stop_algo_id = $7 AND take_profit_algo_id = $9
                RETURNING *
                """,
                normalized_venue, normalized_symbol, normalized_entry,
                side, pos_side, quantity, str(stop_algo_id), reason, str(take_profit_algo_id),
            )
        return _decode_protection_record(row) if row is not None else None

    async def mark_testnet_protection_close_submitting(
        self, *, venue: str, symbol: str, entry_client_order_id: str,
        claimed_reason: str, submitting_reason: str,
    ) -> dict[str, Any] | None:
        """One-use CAS marker written immediately before the sole close POST."""
        normalized_venue, environment = _protection_venue(venue)
        normalized_symbol, normalized_entry = _protection_key(
            normalized_venue, symbol, entry_client_order_id
        )[1:]
        if environment != "TESTNET" or normalized_venue != "binance_testnet":
            raise ValueError("Testnet close submit marker requires the Testnet venue")
        if any(len(value) > 256 for value in (claimed_reason, submitting_reason)):
            raise ValueError("Testnet close submit marker is invalid")
        async with self.db.transaction() as connection:
            connection = connection or self.db
            row = await connection.fetchrow(
                """
                UPDATE binance_algo_protections
                SET state_reason = $5, updated_at = CURRENT_TIMESTAMP
                WHERE environment = 'TESTNET' AND venue = $1 AND symbol = $2
                  AND entry_client_order_id = $3 AND state = 'CLOSE_PENDING'
                  AND state_reason = $4
                RETURNING *
                """,
                normalized_venue, normalized_symbol, normalized_entry,
                claimed_reason, submitting_reason,
            )
        return _decode_protection_record(row) if row is not None else None

    async def list_active_protections(
        self, venue: str, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        normalized_venue, _ = _protection_venue(venue)
        if symbol is None:
            rows = await self.db.fetch(
                """
                SELECT * FROM binance_algo_protections
                WHERE venue = $1 AND state <> 'CLOSED'
                ORDER BY created_at, symbol, entry_client_order_id
                """,
                normalized_venue,
            )
        else:
            normalized_symbol = str(symbol).strip().upper()
            if not _TRADING_SYMBOL_RE.fullmatch(normalized_symbol):
                raise ValueError("protection symbol is invalid")
            rows = await self.db.fetch(
                """
                SELECT * FROM binance_algo_protections
                WHERE venue = $1 AND symbol = $2 AND state <> 'CLOSED'
                ORDER BY created_at, entry_client_order_id
                """,
                normalized_venue,
                normalized_symbol,
            )
        return [_decode_protection_record(row) for row in rows]

    async def list_protections(
        self, venue: str, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        """Return every durable Algo owner, including closed chains for history audit."""
        normalized_venue, _ = _protection_venue(venue)
        if symbol is None:
            rows = await self.db.fetch(
                """
                SELECT * FROM binance_algo_protections
                WHERE venue = $1
                ORDER BY created_at, symbol, entry_client_order_id
                """,
                normalized_venue,
            )
        else:
            normalized_symbol = str(symbol).strip().upper()
            if not _TRADING_SYMBOL_RE.fullmatch(normalized_symbol):
                raise ValueError("protection symbol is invalid")
            rows = await self.db.fetch(
                """
                SELECT * FROM binance_algo_protections
                WHERE venue = $1 AND symbol = $2
                ORDER BY created_at, entry_client_order_id
                """,
                normalized_venue,
                normalized_symbol,
            )
        return [_decode_protection_record(row) for row in rows]

    async def set_protection_state(
        self,
        venue: str,
        symbol: str,
        entry_client_order_id: str,
        state: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        key = _protection_key(venue, symbol, entry_client_order_id)
        normalized_state = str(state or "").strip().upper()
        if normalized_state not in _ALGO_PROTECTION_STATES:
            raise ValueError("protection state is invalid")
        if reason is not None:
            reason = str(reason).strip()
            if len(reason) > 256 or any(ord(char) < 32 for char in reason):
                raise ValueError("state reason is invalid")

        async with self.db.transaction() as connection:
            connection = connection or self.db
            current = await connection.fetchrow(
                """
                SELECT * FROM binance_algo_protections
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                FOR UPDATE
                """,
                *key,
            )
            if current is None:
                return None
            values = dict(current)
            if (
                normalized_state == "CLOSED"
                and str(values.get("environment", "")).upper() == "MAINNET"
            ):
                raise ValueError("Mainnet CLOSED requires exchange reconciliation proof")
            _validate_protection_transition(str(values["state"]), normalized_state)
            if normalized_state == "CLOSED" and not (
                str(values["state"]).upper() == "CLOSE_PENDING"
                or (
                    str(values["state"]).upper() == "PENDING"
                    and Decimal(str(values["filled_quantity"])) == 0
                )
            ):
                raise ValueError("CLOSED requires CLOSE_PENDING or a definitively unfilled PENDING owner")
            now = datetime.now(UTC)
            if normalized_state == "PROTECTED":
                values["state"] = normalized_state
                values["protection_verified_at"] = now
            elif normalized_state == "CLOSED":
                values["state"] = normalized_state
                values["closed_at"] = values.get("closed_at") or now
            else:
                values["state"] = normalized_state
            _validate_protection_shape(values)
            updated = await connection.fetchrow(
                """
                UPDATE binance_algo_protections
                SET state = $4, state_reason = $5, protection_verified_at = $6,
                    closed_at = $7, updated_at = CURRENT_TIMESTAMP
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                RETURNING *
                """,
                *key,
                normalized_state,
                reason,
                values.get("protection_verified_at"),
                values.get("closed_at"),
            )
        return dict(updated) if updated is not None else None

    async def claim_local_emergency_close(
        self, symbol: str, entry_client_order_id: str, close_client_order_id: str,
        *, claimant_id: str, lease_seconds: int = 30,
    ) -> dict[str, Any]:
        """Reserve one close identity; only an unattempted expired lease can transfer.

        `claimed` permits preparation, NOT POST. Immediately before POST the
        caller must win mark_local_emergency_close_attempted. A lost response
        from either operation confers no authority to submit.
        """
        key = _protection_key("binance_mainnet", symbol, entry_client_order_id)
        if not _ALGO_CLIENT_ID_RE.fullmatch(close_client_order_id or ""):
            raise ValueError("Emergency close client identity is invalid")
        if not _HISTORY_RUN_ID_RE.fullmatch(claimant_id or ""):
            raise ValueError("Emergency close claimant identity is invalid")
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 120:
            raise ValueError("Emergency close lease must be between 1 and 120 seconds")
        async with self.db.transaction() as connection:
            connection = connection or self.db
            owner = await connection.fetchrow(
                """
                SELECT p.*, s.runtime_target AS launch_runtime_target
                FROM binance_algo_protections AS p
                JOIN mainnet_launch_sessions AS s ON s.launch_id = p.mainnet_launch_id
                WHERE p.venue = $1 AND p.symbol = $2 AND p.entry_client_order_id = $3
                FOR UPDATE OF p
                """, *key,
            )
            if owner is None or owner.get("launch_runtime_target") != "LOCAL" or owner.get("environment") != "MAINNET":
                raise RuntimeError("Emergency close requires a durable Local Mainnet owner")
            if not owner.get("basket_id") or not owner.get("mainnet_launch_id"):
                raise RuntimeError("Emergency close owner has no durable launch/basket identity")
            entry_cancel_markers = [
                part.split("=", 1)[1]
                for part in str(owner.get("state_reason") or "").split(";")
                if part.startswith("entry_cancel=")
            ]
            # Entry cancellation and emergency close serialize on this owner
            # row. A close may proceed only after exact exchange read-back has
            # durably confirmed the entry terminal; otherwise the entry could
            # fill after the reduce-only close and recreate exposure.
            if entry_cancel_markers and entry_cancel_markers != ["CONFIRMED"]:
                raise RuntimeError("Emergency close is blocked by unresolved entry cancellation")
            current = await connection.fetchrow(
                """
                SELECT * FROM binance_emergency_close_claims
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                FOR UPDATE
                """, *key,
            )
            if current is not None and current["close_client_order_id"] != close_client_order_id:
                raise RuntimeError("Emergency close deterministic identity changed")
            if str(owner.get("state")).upper() == "CLOSED":
                return {"claimed": False, "status": "CLOSED", "close_client_order_id": close_client_order_id}
            reason = str(owner.get("state_reason") or "")
            # Any legacy reservation may already have reached the exchange.
            # Never create a fresh submission permission from legacy ambiguity.
            legacy_close_id = next((part.split("=", 1)[1] for part in reason.split(";")
                                    if part.startswith("local_close_client_order_id=")), None)
            if legacy_close_id is not None and legacy_close_id != close_client_order_id:
                raise RuntimeError("Emergency close legacy identity changed")
            if current is None:
                current = await connection.fetchrow(
                    """
                    INSERT INTO binance_emergency_close_claims (
                        venue, symbol, entry_client_order_id, close_client_order_id,
                        claimant_id, fencing_token, lease_until, status, attempted_at
                    ) VALUES ($1, $2, $3, $4, $5, 1,
                              clock_timestamp() + $6 * INTERVAL '1 second', $7::varchar,
                              CASE WHEN $7::varchar = 'ATTEMPTED' THEN clock_timestamp() ELSE NULL END)
                    RETURNING *
                    """, *key, close_client_order_id, claimant_id, lease_seconds,
                    "ATTEMPTED" if legacy_close_id is not None else "RESERVED",
                )
                claimed = legacy_close_id is None
            elif current["status"] == "ATTEMPTED":
                claimed = False
            else:
                transferred = await connection.fetchrow(
                    """
                    UPDATE binance_emergency_close_claims
                    SET claimant_id = $4, fencing_token = fencing_token + 1,
                        lease_until = clock_timestamp() + $5 * INTERVAL '1 second',
                        updated_at = clock_timestamp()
                    WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                      AND status = 'RESERVED' AND lease_until <= clock_timestamp()
                    RETURNING *
                    """, *key, claimant_id, lease_seconds,
                )
                claimed = transferred is not None
                current = transferred or current
            if claimed:
                close_reason = self._emergency_close_state_reason(
                    owner.get("state_reason"), close_client_order_id, "RESERVED"
                )
                updated = await connection.fetchrow(
                    """
                    UPDATE binance_algo_protections
                    SET state = 'CLOSE_PENDING', state_reason = $4, updated_at = clock_timestamp()
                    WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                      AND state <> 'CLOSED'
                    RETURNING entry_client_order_id
                    """, *key,
                    close_reason,
                )
                if updated is None:
                    raise RuntimeError("Emergency close owner changed during claim")
            if current is None:
                raise RuntimeError("Emergency close claim record unavailable")
            return {**dict(current), "claimed": claimed}

    @staticmethod
    def _emergency_close_state_reason(
        current_reason: object, close_client_order_id: str, submission_state: str
    ) -> str:
        entry_cancel_markers = [
            part.split("=", 1)[1]
            for part in str(current_reason or "").split(";")
            if part.startswith("entry_cancel=")
        ]
        if entry_cancel_markers and entry_cancel_markers != ["CONFIRMED"]:
            raise RuntimeError("Emergency close is blocked by unresolved entry cancellation")
        reason = (
            f"local_close_client_order_id={close_client_order_id};"
            f"close_submission={submission_state}"
        )
        if entry_cancel_markers:
            reason += ";entry_cancel=CONFIRMED"
        if len(reason) > 256:
            raise RuntimeError("Emergency close evidence exceeds the durable owner limit")
        return reason

    async def mark_local_emergency_close_attempted(
        self, symbol: str, entry_client_order_id: str, close_client_order_id: str,
        *, claimant_id: str, fencing_token: int,
    ) -> bool:
        """Consume a live fenced reservation once, BEFORE the single exchange POST.

        False, exceptions, and ambiguous/lost responses require read-only
        reconciliation. ATTEMPTED is permanent even after lease expiry.
        """
        key = _protection_key("binance_mainnet", symbol, entry_client_order_id)
        if (not _ALGO_CLIENT_ID_RE.fullmatch(close_client_order_id or "")
                or not _HISTORY_RUN_ID_RE.fullmatch(claimant_id or "")
                or isinstance(fencing_token, bool) or not isinstance(fencing_token, int)
                or fencing_token < 1):
            raise ValueError("Emergency close submission fence is invalid")
        async with self.db.transaction() as connection:
            connection = connection or self.db
            owner = await connection.fetchrow(
                """
                SELECT state, state_reason FROM binance_algo_protections
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                FOR UPDATE
                """, *key,
            )
            if owner is None or owner["state"] != "CLOSE_PENDING":
                return False
            attempted_reason = self._emergency_close_state_reason(
                owner.get("state_reason"), close_client_order_id, "ATTEMPTED"
            )
            attempted = await connection.fetchrow(
                """
                UPDATE binance_emergency_close_claims
                SET status = 'ATTEMPTED', attempted_at = clock_timestamp(), updated_at = clock_timestamp()
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                  AND close_client_order_id = $4 AND claimant_id = $5 AND fencing_token = $6
                  AND status = 'RESERVED' AND lease_until > clock_timestamp()
                RETURNING fencing_token
                """, *key, close_client_order_id, claimant_id, fencing_token,
            )
            if attempted is None:
                return False
            updated = await connection.fetchrow(
                """
                UPDATE binance_algo_protections
                SET state_reason = $4, updated_at = clock_timestamp()
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                  AND state = 'CLOSE_PENDING'
                RETURNING entry_client_order_id
                """, *key,
                attempted_reason,
            )
            if updated is None:
                raise RuntimeError("Emergency close owner changed during submission claim")
            return True

    async def close_mainnet_protection_with_proof(
        self,
        symbol: str,
        entry_client_order_id: str,
        proof: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Close a Mainnet owner only with a complete, durable flat-state proof."""

        key = _protection_key("binance_mainnet", symbol, entry_client_order_id)
        required = {
            "algo_id",
            "order_id",
            "client_order_id",
            "order_status",
            "executed_quantity",
            "trade_quantity",
            "position_quantity",
            "open_child_order_ids",
            "open_owner_algo_ids",
            "verified_at",
        }
        if not isinstance(proof, Mapping) or set(proof) != required:
            raise ValueError("Mainnet closure proof is incomplete or contains unsupported fields")
        try:
            algo_id = str(proof["algo_id"]).strip()
            order_id = str(proof["order_id"]).strip()
            executed = _protection_decimal(proof["executed_quantity"], "executed_quantity")
            traded = _protection_decimal(proof["trade_quantity"], "trade_quantity")
            position = Decimal(str(proof["position_quantity"]))
            verified_at = _utc_datetime(proof["verified_at"])
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Mainnet closure proof contains invalid values") from exc
        client_order_id = str(proof["client_order_id"] or "").strip()
        status = str(proof["order_status"] or "").strip().upper()
        emergency_close = algo_id == _LOCAL_EMERGENCY_CLOSE_ALGO_ID
        if (
            (not emergency_close and (
                not _ALGO_EXCHANGE_ID_RE.fullmatch(algo_id)
                or int(algo_id) <= 0
            ))
            or not _ALGO_EXCHANGE_ID_RE.fullmatch(order_id)
            or int(order_id) <= 0
            or not _ALGO_CLIENT_ID_RE.fullmatch(client_order_id)
            or status != "FILLED"
            or not executed.is_finite()
            or not traded.is_finite()
            or not position.is_finite()
            or executed <= 0
            or traded != executed
            or position != 0
            or proof["open_child_order_ids"] != []
            or proof["open_owner_algo_ids"] != []
            or verified_at > datetime.now(UTC)
        ):
            raise ValueError("Mainnet closure proof does not prove a flat, fully reconciled chain")

        async with self.db.transaction() as connection:
            connection = connection or self.db
            current = await connection.fetchrow(
                """
                SELECT * FROM binance_algo_protections
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                FOR UPDATE
                """,
                *key,
            )
            if current is None:
                return None
            record = dict(current)
            if (
                str(record.get("environment", "")).upper() != "MAINNET"
                or not record.get("mainnet_launch_id")
                or not record.get("basket_id")
                or str(record.get("state", "")).upper() not in (
                    {"CLOSE_PENDING", "CLOSED", "UNKNOWN"} if emergency_close else {"CLOSE_PENDING", "CLOSED"}
                )
            ):
                raise ValueError("Mainnet closure proof does not match a launch-owned CLOSE_PENDING record")
            if emergency_close:
                reason = str(record.get("state_reason") or "")
                close_client_id = next(
                    (
                        part.split("=", 1)[1]
                        for part in reason.split(";")
                        if part.startswith("local_close_client_order_id=")
                    ),
                    "",
                )
                if (
                    str(record.get("state", "")).upper() not in {"CLOSE_PENDING", "UNKNOWN"}
                    or close_client_id != client_order_id
                    or "close_submission=ATTEMPTED" not in reason
                ):
                    raise ValueError(
                        "Emergency close proof does not match a durably attempted close"
                    )
            elif algo_id not in {
                str(record.get("stop_algo_id") or ""),
                str(record.get("take_profit_algo_id") or ""),
            }:
                raise ValueError("Mainnet closure proof Algo ID differs from its durable owner")
            owner_filled = _protection_decimal(record.get("filled_quantity"), "filled_quantity")
            if owner_filled <= 0 or executed != owner_filled:
                raise ValueError("Mainnet close fill quantity differs from the durable entry owner")

            evidence: dict[str, Any] = {
                "kind": (
                    "LOCAL_EMERGENCY_CLOSE_VERIFIED"
                    if emergency_close
                    else "BINANCE_ALGO_CLOSE_VERIFIED"
                ),
                "symbol": key[1],
                "entry_client_order_id": key[2],
                "mainnet_launch_id": str(record["mainnet_launch_id"]),
                "basket_id": str(record["basket_id"]),
                "algo_id": algo_id,
                "order_id": order_id,
                "client_order_id": client_order_id,
                "order_status": status,
                "executed_quantity": str(executed),
                "trade_quantity": str(traded),
                "owner_filled_quantity": str(owner_filled),
                "position_quantity": str(position),
                "open_child_order_ids": [],
                "open_owner_algo_ids": [],
                "verified_at": verified_at.isoformat(),
            }
            canonical = json.dumps(
                evidence, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            proof_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            evidence["proof_sha256"] = proof_sha256
            encoded = json.dumps(
                evidence, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            if str(record.get("state", "")).upper() == "CLOSED":
                existing = _decode_json_mapping(
                    record.get("closure_evidence"), "Mainnet closure evidence"
                )
                if existing != evidence:
                    raise ValueError("Mainnet CLOSED owner already has different closure evidence")
                return record

            updated = await connection.fetchrow(
                """
                UPDATE binance_algo_protections
                SET state = 'CLOSED', state_reason = $4, closed_at = $5,
                    closure_evidence = $6::jsonb, updated_at = CURRENT_TIMESTAMP
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                  AND state = $7
                RETURNING *
                """,
                *key,
                f"Binance close verified; proof {proof_sha256}",
                verified_at,
                encoded,
                str(record["state"]),
            )
            if updated is None:
                raise RuntimeError("Mainnet closure proof lost its durable state transition")
            readback = dict(updated)
            persisted_evidence = _decode_json_mapping(
                readback.get("closure_evidence"), "Mainnet closure read-back evidence"
            )
            persisted_hash = str(persisted_evidence.pop("proof_sha256", ""))
            persisted_canonical = json.dumps(
                persisted_evidence, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            if (
                persisted_hash != proof_sha256
                or hashlib.sha256(persisted_canonical.encode("utf-8")).hexdigest()
                != persisted_hash
                or {**persisted_evidence, "proof_sha256": persisted_hash} != evidence
                or str(readback.get("state", "")).upper() != "CLOSED"
            ):
                raise RuntimeError("Mainnet closure proof failed durable read-back verification")
            return _decode_protection_record(readback)

    async def close_mainnet_unfilled_protection_with_proof(
        self,
        symbol: str,
        entry_client_order_id: str,
        proof: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Close a zero-fill owner with an exact terminal exchange-order proof."""

        key = _protection_key("binance_mainnet", symbol, entry_client_order_id)
        required = {
            "order_id", "client_order_id", "order_status", "executed_quantity",
            "original_quantity", "symbol", "entry_side", "position_side", "verified_at",
        }
        if not isinstance(proof, Mapping) or set(proof) != required:
            raise ValueError("Mainnet zero-fill closure proof is incomplete")
        try:
            order_id = str(proof["order_id"]).strip()
            status = str(proof["order_status"]).strip().upper()
            executed = _protection_decimal(proof["executed_quantity"], "executed_quantity", allow_zero=True)
            original = _protection_decimal(proof["original_quantity"], "original_quantity")
            verified_at = _utc_datetime(proof["verified_at"])
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Mainnet zero-fill proof values are invalid") from exc
        client_id = str(proof["client_order_id"] or "").strip()
        if (
            not re.fullmatch(r"[0-9]{1,64}", order_id)
            or int(order_id) <= 0
            or not _ALGO_CLIENT_ID_RE.fullmatch(client_id)
            or status not in {"CANCELED", "CANCELLED", "EXPIRED", "REJECTED"}
            or executed != 0
            or verified_at > datetime.now(UTC)
            or str(proof["symbol"]).strip().upper() != key[1]
            or str(proof["entry_side"]).strip().upper() not in {"BUY", "SELL"}
            or str(proof["position_side"]).strip().upper() not in {"BOTH", "LONG", "SHORT"}
        ):
            raise ValueError("Mainnet zero-fill proof is not a terminal, flat entry")

        async with self.db.transaction() as connection:
            connection = connection or self.db
            current = await connection.fetchrow(
                """
                SELECT * FROM binance_algo_protections
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                FOR UPDATE
                """,
                *key,
            )
            if current is None:
                return None
            record = dict(current)
            if (
                str(record.get("environment", "")).upper() != "MAINNET"
                or not record.get("mainnet_launch_id")
                or not record.get("basket_id")
                or str(record.get("state", "")).upper() not in {"PENDING", "CLOSED"}
                or Decimal(str(record.get("filled_quantity"))) != 0
                or record.get("entry_average_price") is not None
                or record.get("stop_algo_id") is not None
                or record.get("take_profit_algo_id") is not None
                or client_id != key[2]
                or original != Decimal(str(record.get("requested_quantity")))
                or str(proof["entry_side"]).strip().upper() != str(record.get("entry_side", "")).upper()
                or str(proof["position_side"]).strip().upper() != str(record.get("position_side", "")).upper()
            ):
                raise ValueError("Mainnet zero-fill proof does not match an unfilled durable owner")

            evidence: dict[str, Any] = {
                "kind": "UNFILLED_ENTRY_TERMINAL",
                "symbol": key[1],
                "entry_client_order_id": key[2],
                "mainnet_launch_id": str(record["mainnet_launch_id"]),
                "basket_id": str(record["basket_id"]),
                "order_id": order_id,
                "client_order_id": client_id,
                "order_status": status,
                "executed_quantity": "0",
                "original_quantity": str(original),
                "entry_side": str(record["entry_side"]).upper(),
                "position_side": str(record["position_side"]).upper(),
                "verified_at": verified_at.isoformat(),
            }
            canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
            evidence["proof_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if str(record.get("state", "")).upper() == "CLOSED":
                existing = _decode_json_mapping(record.get("closure_evidence"), "Mainnet closure evidence")
                if existing != evidence:
                    raise ValueError("closed Mainnet owner already has different zero-fill evidence")
                return _decode_protection_record(record)
            updated = await connection.fetchrow(
                """
                UPDATE binance_algo_protections
                SET state = 'CLOSED', state_reason = $4, closed_at = $5,
                    closure_evidence = $6::jsonb, updated_at = CURRENT_TIMESTAMP
                WHERE venue = $1 AND symbol = $2 AND entry_client_order_id = $3
                  AND state = 'PENDING'
                RETURNING *
                """,
                *key,
                f"unfilled_entry_order_id={order_id};unfilled_entry_status={status};unfilled_entry_executed_qty=0",
                verified_at,
                json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False),
            )
        if updated is None:
            raise RuntimeError("Mainnet zero-fill closure lost its durable transition")
        result = _decode_protection_record(updated)
        _validate_protection_shape(result)
        readback_evidence = dict(result["closure_evidence"])
        stored_hash = str(readback_evidence.pop("proof_sha256", ""))
        readback = json.dumps(readback_evidence, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if (
            str(result.get("state", "")).upper() != "CLOSED"
            or hashlib.sha256(readback.encode("utf-8")).hexdigest() != stored_hash
            or stored_hash != evidence["proof_sha256"]
        ):
            raise RuntimeError("Mainnet zero-fill proof failed durable read-back")
        return result


class BinanceHistoryRepository:
    """Persist launch-scoped exchange history coverage and immutable evidence."""

    def __init__(self, db: PostgresClient) -> None:
        self.db = db

    @staticmethod
    def _scope(runtime_target: object, run_id: object, symbol: object) -> tuple[str, str, str]:
        target = str(runtime_target or "").strip().upper()
        run = str(run_id or "").strip()
        normalized_symbol = str(symbol or "").strip().upper()
        if target not in {"LOCAL", "CLOUD_RUN", "TESTNET"}:
            raise ValueError("history runtime_target is invalid")
        if not _HISTORY_RUN_ID_RE.fullmatch(run):
            raise ValueError("history run_id is invalid")
        if not _TRADING_SYMBOL_RE.fullmatch(normalized_symbol):
            raise ValueError("history symbol is invalid")
        return target, run, normalized_symbol

    @staticmethod
    def _history_kind(value: object) -> str:
        kind = str(value or "").strip().upper()
        if kind not in _HISTORY_KINDS:
            raise ValueError("history kind is invalid")
        return kind

    @staticmethod
    def _now(value: datetime | None) -> datetime:
        current = value or datetime.now(UTC)
        if not isinstance(current, datetime) or current.tzinfo is None:
            raise ValueError("history timestamps must be timezone-aware")
        return current.astimezone(UTC)

    async def register_testnet_anchor(
        self,
        *,
        run_id: str,
        symbol: str,
        anchor_at: datetime,
    ) -> dict[str, Any]:
        """Register an explicit Testnet run anchor; never inferred from exchange rows."""

        target, run, normalized_symbol = self._scope("TESTNET", run_id, symbol)
        anchor = self._now(anchor_at)
        if anchor > datetime.now(UTC):
            raise ValueError("Testnet history anchor cannot be in the future")
        await self.db.execute(
            """
            INSERT INTO binance_history_anchors (
                runtime_target, run_id, symbol, anchor_at, anchor_source, mainnet_launch_id
            ) VALUES ($1, $2, $3, $4, 'TESTNET_READONLY_START', NULL)
            ON CONFLICT (runtime_target, run_id, symbol) DO NOTHING
            """,
            target,
            run,
            normalized_symbol,
            anchor,
        )
        row = await self.db.fetchrow(
            """
            SELECT runtime_target, run_id, symbol, anchor_at, anchor_source,
                   mainnet_launch_id, created_at
            FROM binance_history_anchors
            WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
            """,
            target,
            run,
            normalized_symbol,
        )
        if row is None or _utc_datetime(row["anchor_at"]) != anchor:
            raise RuntimeError("durable Testnet history anchor conflicts with the supplied run")
        return dict(row)

    async def _resolve_mainnet_launch(
        self, symbol: str
    ) -> tuple[str, str, datetime]:
        rows = await self.db.fetch(
            """
            SELECT launch_id, runtime_target, symbol, created_at
            FROM mainnet_launch_sessions
            WHERE symbol = $1
              AND runtime_target IN ('LOCAL', 'CLOUD_RUN')
              AND state IN (
                'ACTIVE', 'PAUSED_NEW_RISK', 'RECONCILIATION_REQUIRED',
                'AUTONOMOUS_ACTIVE', 'REAUTH_REQUIRED'
              )
            ORDER BY updated_at DESC, launch_id
            LIMIT 2
            """,
            symbol,
        )
        if len(rows) != 1:
            raise RuntimeError("durable active Mainnet launch anchor is missing or ambiguous")
        row = rows[0]
        target, run, normalized_symbol = self._scope(
            row.get("runtime_target"), row.get("launch_id"), row.get("symbol")
        )
        if target == "TESTNET" or normalized_symbol != symbol:
            raise RuntimeError("durable Mainnet launch identity is invalid")
        anchor = _utc_datetime(row.get("created_at"))
        return target, run, anchor

    async def _ensure_checkpoint(
        self,
        *,
        runtime_target: str,
        run_id: str,
        symbol: str,
        history_kind: str,
        anchor_at: datetime,
    ) -> dict[str, Any]:
        anchor_source = (
            "TESTNET_READONLY_START"
            if runtime_target == "TESTNET"
            else "MAINNET_LAUNCH_SESSION"
        )
        mainnet_launch_id = None if runtime_target == "TESTNET" else run_id
        await self.db.execute(
            """
            INSERT INTO binance_history_anchors (
                runtime_target, run_id, symbol, anchor_at, anchor_source, mainnet_launch_id
            ) VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (runtime_target, run_id, symbol) DO NOTHING
            """,
            runtime_target,
            run_id,
            symbol,
            anchor_at,
            anchor_source,
            mainnet_launch_id,
        )
        anchor_row = await self.db.fetchrow(
            """
            SELECT anchor_at, anchor_source, mainnet_launch_id
            FROM binance_history_anchors
            WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
            """,
            runtime_target,
            run_id,
            symbol,
        )
        if (
            anchor_row is None
            or _utc_datetime(anchor_row["anchor_at"]) != anchor_at
            or anchor_row.get("anchor_source") != anchor_source
            or (
                runtime_target != "TESTNET"
                and str(anchor_row.get("mainnet_launch_id")) != run_id
            )
            or (runtime_target == "TESTNET" and anchor_row.get("mainnet_launch_id") is not None)
        ):
            raise RuntimeError("durable history anchor does not match the launch session")

        await self.db.execute(
            """
            INSERT INTO binance_history_checkpoints (
                runtime_target, run_id, symbol, history_kind
            ) VALUES ($1, $2, $3, $4)
            ON CONFLICT (runtime_target, run_id, symbol, history_kind) DO NOTHING
            """,
            runtime_target,
            run_id,
            symbol,
            history_kind,
        )
        row = await self.db.fetchrow(
            """
            SELECT c.*, a.anchor_at
            FROM binance_history_checkpoints AS c
            JOIN binance_history_anchors AS a
              USING (runtime_target, run_id, symbol)
            WHERE c.runtime_target = $1 AND c.run_id = $2 AND c.symbol = $3
              AND c.history_kind = $4
            """,
            runtime_target,
            run_id,
            symbol,
            history_kind,
        )
        if row is None or _utc_datetime(row["anchor_at"]) != anchor_at:
            raise RuntimeError("durable Binance history checkpoint could not be read back")
        return dict(row)

    async def begin_mainnet_scan(
        self,
        *,
        symbol: str,
        history_kind: str,
        retention_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        normalized_symbol = str(symbol or "").strip().upper()
        if not _TRADING_SYMBOL_RE.fullmatch(normalized_symbol):
            raise ValueError("history symbol is invalid")
        kind = self._history_kind(history_kind)
        current_time = self._now(now)
        if isinstance(retention_seconds, bool) or retention_seconds <= 0:
            raise ValueError("history retention window must be positive")
        target, run_id, anchor_at = await self._resolve_mainnet_launch(normalized_symbol)
        context = await self._ensure_checkpoint(
            runtime_target=target,
            run_id=run_id,
            symbol=normalized_symbol,
            history_kind=kind,
            anchor_at=anchor_at,
        )
        await self._begin_checkpoint_scan(context, int(retention_seconds), current_time)
        refreshed = await self.db.fetchrow(
            """
            SELECT c.*, a.anchor_at
            FROM binance_history_checkpoints AS c
            JOIN binance_history_anchors AS a
              USING (runtime_target, run_id, symbol)
            WHERE c.runtime_target = $1 AND c.run_id = $2 AND c.symbol = $3
              AND c.history_kind = $4
            """,
            target,
            run_id,
            normalized_symbol,
            kind,
        )
        if refreshed is None:
            raise RuntimeError("history checkpoint disappeared after scan reservation")
        return dict(refreshed)

    async def begin_testnet_scan(
        self,
        *,
        run_id: str,
        symbol: str,
        history_kind: str,
        retention_seconds: int,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        target, run, normalized_symbol = self._scope("TESTNET", run_id, symbol)
        kind = self._history_kind(history_kind)
        current_time = self._now(now)
        if isinstance(retention_seconds, bool) or retention_seconds <= 0:
            raise ValueError("history retention window must be positive")
        anchor = await self.db.fetchrow(
            """
            SELECT anchor_at FROM binance_history_anchors
            WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
            """,
            target,
            run,
            normalized_symbol,
        )
        if anchor is None:
            raise RuntimeError("durable Testnet history anchor is unavailable")
        checkpoint = await self._ensure_checkpoint(
            runtime_target=target,
            run_id=run,
            symbol=normalized_symbol,
            history_kind=kind,
            anchor_at=_utc_datetime(anchor["anchor_at"]),
        )
        await self._begin_checkpoint_scan(checkpoint, int(retention_seconds), current_time)
        refreshed = await self.db.fetchrow(
            """
            SELECT c.*, a.anchor_at
            FROM binance_history_checkpoints AS c
            JOIN binance_history_anchors AS a
              USING (runtime_target, run_id, symbol)
            WHERE c.runtime_target = $1 AND c.run_id = $2 AND c.symbol = $3
              AND c.history_kind = $4
            """,
            target,
            run,
            normalized_symbol,
            kind,
        )
        if refreshed is None:
            raise RuntimeError("Testnet history checkpoint could not be read back")
        return dict(refreshed)

    async def _begin_checkpoint_scan(
        self, checkpoint: Mapping[str, Any], retention_seconds: int, now: datetime
    ) -> uuid.UUID:
        status = str(checkpoint.get("coverage_status") or "").upper()
        if status in _HISTORY_FAILED_STATES:
            raise RuntimeError("Binance history checkpoint is permanently fenced as incomplete")
        anchor_at = _utc_datetime(checkpoint.get("anchor_at"))
        covered_through = checkpoint.get("covered_through")
        reference_at = _utc_datetime(covered_through) if covered_through is not None else anchor_at
        previous_scan_id: Any = None
        scan_from_at: Optional[datetime] = None
        scan_to_at: Optional[datetime] = None
        scan_started: Any = None
        if status == "SCANNING":
            scan_started = checkpoint.get("scan_started_at")
            scan_from = checkpoint.get("scan_from_at")
            scan_to = checkpoint.get("scan_to_at")
            previous_scan_id = checkpoint.get("scan_id")
            if scan_started is None or scan_from is None or scan_to is None or previous_scan_id is None:
                raise RuntimeError("in-progress Binance history checkpoint has no durable scan fence")
            scan_from_at = _utc_datetime(scan_from)
            scan_to_at = _utc_datetime(scan_to)
            if scan_to_at <= scan_from_at:
                raise RuntimeError("Binance history scan window did not advance")
            # A takeover must still be able to replay the entire reserved window.
            reference_at = scan_from_at
        age_seconds = (now - reference_at).total_seconds()
        if age_seconds < 0 or age_seconds > retention_seconds:
            fenced = await self.db.fetchrow(
                """
                UPDATE binance_history_checkpoints
                SET coverage_status = 'GAP', failure_code = 'RETENTION_GAP',
                    scan_id = NULL, scan_started_at = NULL,
                    scan_from_at = NULL, scan_to_at = NULL, updated_at = $5
                WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
                  AND history_kind = $4 AND coverage_status = $6
                  AND scan_id IS NOT DISTINCT FROM $7::uuid
                  AND cursor_id = $8 AND updated_at = $9
                  AND scan_started_at IS NOT DISTINCT FROM $10::timestamptz
                  AND scan_from_at IS NOT DISTINCT FROM $11::timestamptz
                  AND scan_to_at IS NOT DISTINCT FROM $12::timestamptz
                  AND covered_through IS NOT DISTINCT FROM $13::timestamptz
                RETURNING coverage_status
                """,
                checkpoint["runtime_target"], checkpoint["run_id"],
                checkpoint["symbol"], checkpoint["history_kind"], now, status,
                checkpoint.get("scan_id"), checkpoint["cursor_id"], checkpoint["updated_at"],
                checkpoint.get("scan_started_at"), checkpoint.get("scan_from_at"),
                checkpoint.get("scan_to_at"), covered_through,
            )
            if fenced is None:
                raise RuntimeError("Binance history checkpoint changed before retention fencing")
            raise RuntimeError("Binance history anchor or checkpoint exceeds the route retention window")
        if status == "SCANNING":
            if (now - _utc_datetime(checkpoint.get("updated_at"))).total_seconds() <= _HISTORY_SCAN_LEASE_SECONDS:
                raise RuntimeError("Binance history checkpoint is owned by another active scanner")
            new_scan_id = uuid.uuid4()
            claimed = await self.db.fetchrow(
                """
                UPDATE binance_history_checkpoints
                SET scan_id = $5, scan_started_at = $6, updated_at = $6
                WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
                  AND history_kind = $4 AND coverage_status = 'SCANNING'
                  AND scan_id = $7 AND cursor_id = $8 AND updated_at = $9
                  AND scan_from_at = $10 AND scan_to_at = $11 AND scan_started_at = $12
                RETURNING scan_id
                """,
                checkpoint["runtime_target"], checkpoint["run_id"],
                checkpoint["symbol"], checkpoint["history_kind"],
                new_scan_id, now, previous_scan_id, checkpoint["cursor_id"],
                checkpoint["updated_at"],
                scan_from_at, scan_to_at, _utc_datetime(scan_started),
            )
            if claimed is None or claimed["scan_id"] != new_scan_id:
                raise RuntimeError("stale Binance history scan could not be fenced")
            return new_scan_id
        scan_from = (
            max(anchor_at, _utc_datetime(covered_through) - _HISTORY_REPLAY_OVERLAP,
                now - timedelta(seconds=retention_seconds))
            if covered_through is not None
            else anchor_at
        )
        if now <= scan_from:
            raise RuntimeError("Binance history coverage cursor did not advance")
        scan_id = uuid.uuid4()
        claimed = await self.db.fetchrow(
            """
            UPDATE binance_history_checkpoints
            SET coverage_status = 'SCANNING', scan_id = $5,
                scan_started_at = $6, scan_from_at = $7, scan_to_at = $8,
                failure_code = NULL, updated_at = $6
            WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
              AND history_kind = $4 AND coverage_status = $9
              AND cursor_id = $10 AND updated_at = $11
              AND scan_id IS NOT DISTINCT FROM $12::uuid
              AND covered_through IS NOT DISTINCT FROM $13::timestamptz
            RETURNING scan_id
            """,
            checkpoint["runtime_target"],
            checkpoint["run_id"],
            checkpoint["symbol"],
            checkpoint["history_kind"],
            scan_id,
            now,
            scan_from,
            now,
            status,
            checkpoint["cursor_id"],
            checkpoint["updated_at"],
            checkpoint.get("scan_id"), covered_through,
        )
        if claimed is None or claimed["scan_id"] != scan_id:
            raise RuntimeError("Binance history checkpoint changed before scan reservation")
        return scan_id

    @staticmethod
    def _normalize_page_items(
        items: list[Mapping[str, Any]], *, history_kind: str, expected_cursor_id: int
    ) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            raise TypeError("history page items must be a list")
        normalized_by_id: dict[int, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, Mapping):
                raise ValueError("history page item is invalid")
            try:
                item_id = int(item["item_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("history page item ID is invalid") from exc
            if isinstance(item.get("item_id"), bool) or item_id <= 0:
                raise ValueError("history page item ID is invalid")
            client_value = item.get("client_id")
            client_id = str(client_value).strip() if client_value not in (None, "") else None
            if client_id is not None and (len(client_id) > 128 or any(ord(c) < 32 for c in client_id)):
                raise ValueError("history page client identity is invalid")
            event_at = _utc_datetime(item.get("event_at"))
            payload_hash = str(item.get("payload_sha256") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", payload_hash):
                raise ValueError("history page payload fingerprint is invalid")
            normalized: dict[str, Any] = {
                "item_id": item_id,
                "client_id": client_id,
                "event_at": event_at,
                "payload_sha256": payload_hash,
            }
            if history_kind == "ALL_ALGO_ORDERS":
                payload = item.get("payload")
                if not isinstance(payload, Mapping):
                    raise ValueError("Binance history observation payload is missing")
                payload_dict: dict[str, Any] = {str(k): v for k, v in payload.items()}
                try:
                    canonical_payload = json.dumps(
                        payload_dict, sort_keys=True, separators=(",", ":"), allow_nan=False
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError("Binance history observation payload is invalid") from exc
                if hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest() != payload_hash:
                    raise ValueError("Binance history observation payload fingerprint is invalid")
                normalized["payload"] = payload_dict
            elif history_kind in {"ALL_ORDERS", "USER_TRADES"}:
                payload = item.get("payload")
                if not isinstance(payload, Mapping):
                    raise ValueError("Binance history observation payload is missing")
                payload_dict: dict[str, Any] = {str(k): v for k, v in payload.items()}
                try:
                    canonical_payload = json.dumps(
                        payload_dict, sort_keys=True, separators=(",", ":"), allow_nan=False
                    )
                except (TypeError, ValueError) as exc:
                    raise ValueError("Binance history observation payload is invalid") from exc
                if hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest() != payload_hash:
                    raise ValueError("Binance history observation payload fingerprint is invalid")
                normalized["payload"] = payload_dict
            previous = normalized_by_id.get(item_id)
            if previous is not None and previous != normalized:
                raise ValueError("history page repeats an ID with conflicting contents")
            normalized_by_id[item_id] = normalized
        # Binance AllAlgoOrders has been observed newest-first while its
        # algoId cursor is inclusive. Normalize every route page before the
        # durable insert; coverage is driven by bounded time windows, not by
        # assuming an exchange response order or advancing that ID cursor.
        return [normalized_by_id[item_id] for item_id in sorted(normalized_by_id)]

    async def persist_history_page(
        self,
        *,
        checkpoint: Mapping[str, Any],
        expected_cursor_id: int,
        items: list[Mapping[str, Any]],
        observed_at: datetime | None = None,
    ) -> int:
        kind = self._history_kind(checkpoint.get("history_kind"))
        current_time = self._now(observed_at)
        normalized = self._normalize_page_items(
            items, history_kind=kind, expected_cursor_id=expected_cursor_id
        )
        key = (
            str(checkpoint["runtime_target"]),
            str(checkpoint["run_id"]),
            str(checkpoint["symbol"]),
            kind,
        )
        new_cursor = max(
            [int(expected_cursor_id), *(item["item_id"] for item in normalized)]
        )
        async with self.db.transaction() as connection:
            connection = connection or self.db
            current = await connection.fetchrow(
                """
                SELECT cursor_id, coverage_status, scan_from_at, scan_to_at, scan_id
                FROM binance_history_checkpoints
                WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
                  AND history_kind = $4
                FOR UPDATE
                """,
                *key,
            )
            if (
                current is None
                or str(current["coverage_status"]).upper() != "SCANNING"
                or int(current["cursor_id"]) != int(expected_cursor_id)
                or current.get("scan_id") != checkpoint.get("scan_id")
            ):
                raise RuntimeError("durable Binance history cursor changed during page scan")
            scan_from = _utc_datetime(current["scan_from_at"])
            scan_to = _utc_datetime(current["scan_to_at"])
            for item in normalized:
                if item["event_at"] < scan_from or item["event_at"] > scan_to:
                    raise RuntimeError("Binance history row escaped its requested time window")
                await connection.execute(
                    """
                    INSERT INTO binance_history_items (
                        runtime_target, run_id, symbol, history_kind, item_id,
                        client_id, event_at, payload_sha256, observed_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (runtime_target, run_id, symbol, history_kind, item_id)
                    DO NOTHING
                    """,
                    *key,
                    item["item_id"],
                    item["client_id"],
                    item["event_at"],
                    item["payload_sha256"],
                    current_time,
                )
                persisted = await connection.fetchrow(
                    """
                    SELECT client_id, event_at, payload_sha256
                    FROM binance_history_items
                    WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
                      AND history_kind = $4 AND item_id = $5
                    """,
                    *key,
                    item["item_id"],
                )
                if (
                    persisted is None
                    or persisted.get("client_id") != item["client_id"]
                    or _utc_datetime(persisted["event_at"]) != item["event_at"]
                ):
                    raise RuntimeError("Binance history identity differs from its durable record")
                if kind == "ALL_ALGO_ORDERS":
                    encoded_payload = json.dumps(
                        item["payload"], sort_keys=True, separators=(",", ":"), allow_nan=False
                    )
                    await connection.execute(
                        """
                        INSERT INTO binance_algo_history_observations (
                            runtime_target, run_id, symbol, history_kind, item_id,
                            client_id, event_at, payload_sha256, payload, observed_at
                        ) VALUES ($1, $2, $3, 'ALL_ALGO_ORDERS', $4, $5, $6, $7, $8::jsonb, $9)
                        ON CONFLICT (runtime_target, run_id, symbol, item_id, payload_sha256)
                        DO NOTHING
                        """,
                        *key[:3],
                        item["item_id"],
                        item["client_id"],
                        item["event_at"],
                        item["payload_sha256"],
                        encoded_payload,
                        current_time,
                    )
                    observation = await connection.fetchrow(
                        """
                        SELECT client_id, event_at, payload_sha256, payload
                        FROM binance_algo_history_observations
                        WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
                          AND item_id = $4 AND payload_sha256 = $5
                        """,
                        *key[:3],
                        item["item_id"],
                        item["payload_sha256"],
                    )
                    if (
                        observation is None
                        or observation.get("client_id") != item["client_id"]
                        or _utc_datetime(observation["event_at"]) != item["event_at"]
                        or _decode_json_mapping(
                            observation["payload"], "Algo history observation payload"
                        ) != item["payload"]
                    ):
                        raise RuntimeError("Algo history lifecycle observation read-back failed")
                else:
                    encoded_payload = json.dumps(
                        item["payload"], sort_keys=True, separators=(",", ":"), allow_nan=False
                    )
                    await connection.execute(
                        """
                        INSERT INTO binance_history_item_observations (
                            runtime_target, run_id, symbol, history_kind, item_id,
                            client_id, event_at, payload_sha256, payload, observed_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
                        ON CONFLICT (runtime_target, run_id, symbol, history_kind,
                                     item_id, payload_sha256) DO NOTHING
                        """,
                        *key,
                        item["item_id"], item["client_id"], item["event_at"],
                        item["payload_sha256"], encoded_payload, current_time,
                    )
                    observation = await connection.fetchrow(
                        """
                        SELECT client_id, event_at, payload_sha256, payload
                        FROM binance_history_item_observations
                        WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
                          AND history_kind = $4 AND item_id = $5 AND payload_sha256 = $6
                        """,
                        *key, item["item_id"], item["payload_sha256"],
                    )
                    if (
                        observation is None
                        or observation.get("client_id") != item["client_id"]
                        or _utc_datetime(observation["event_at"]) != item["event_at"]
                        or _decode_json_mapping(
                            observation["payload"], "Binance history observation payload"
                        ) != item["payload"]
                    ):
                        raise RuntimeError("Binance order/trade lifecycle observation read-back failed")
            updated = await connection.fetchrow(
                """
                UPDATE binance_history_checkpoints
                SET cursor_id = $5, last_page_at = $6, updated_at = $6
                WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
                  AND history_kind = $4 AND coverage_status = 'SCANNING' AND scan_id = $7
                RETURNING cursor_id
                """,
                *key,
                new_cursor,
                current_time,
                checkpoint.get("scan_id"),
            )
            if updated is None or int(updated["cursor_id"]) != new_cursor:
                raise RuntimeError("Binance history page checkpoint read-back failed")
        return new_cursor

    async def complete_history_scan(self, checkpoint: Mapping[str, Any]) -> None:
        key = (
            str(checkpoint["runtime_target"]),
            str(checkpoint["run_id"]),
            str(checkpoint["symbol"]),
            self._history_kind(checkpoint.get("history_kind")),
        )
        scan_row = await self.db.fetchrow(
            """
            SELECT scan_to_at, scan_id FROM binance_history_checkpoints
            WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
              AND history_kind = $4 AND coverage_status = 'SCANNING'
            """,
            *key,
        )
        if scan_row is None or scan_row.get("scan_id") != checkpoint.get("scan_id"):
            raise RuntimeError("Binance history scan window is no longer active")
        completed_through = _utc_datetime(scan_row["scan_to_at"])
        updated = await self.db.fetchrow(
            """
            UPDATE binance_history_checkpoints
            SET coverage_status = 'COVERED', covered_through = $5,
                scan_started_at = NULL, scan_from_at = NULL, scan_to_at = NULL,
                scan_id = NULL, failure_code = NULL, updated_at = $5
            WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
              AND history_kind = $4 AND coverage_status = 'SCANNING'
              AND last_page_at IS NOT NULL AND scan_id = $6
            RETURNING coverage_status, covered_through, last_page_at
            """,
            *key,
            completed_through,
            checkpoint.get("scan_id"),
        )
        if (
            updated is None
            or updated["coverage_status"] != "COVERED"
            or _utc_datetime(updated["covered_through"]) != completed_through
            or updated.get("last_page_at") is None
        ):
            raise RuntimeError("Binance history coverage could not be durably completed")

    async def record_order_history_observation(
        self, *, checkpoint: Mapping[str, Any], item: Mapping[str, Any],
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Compatibility wrapper for exact ALL_ORDERS observations."""
        if not isinstance(checkpoint, Mapping):
            raise TypeError("History observation checkpoint must be a mapping")
        if self._history_kind(checkpoint.get("history_kind")) != "ALL_ORDERS":
            raise ValueError("exact order observations require an ALL_ORDERS checkpoint")
        return await self.record_history_observation(
            checkpoint=checkpoint, item=item, observed_at=observed_at,
        )

    async def record_history_observation(
        self, *, checkpoint: Mapping[str, Any], item: Mapping[str, Any],
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Append an exact order/trade refresh; never advance cursor or coverage.

        The returned row is the verified durable version of the supplied item.
        list_history_items selects the latest observation per ID in this scope.
        """
        if not isinstance(checkpoint, Mapping) or not isinstance(item, Mapping):
            raise TypeError("History observation checkpoint and item must be mappings")
        target, run_id, symbol = self._scope(
            checkpoint.get("runtime_target"), checkpoint.get("run_id"), checkpoint.get("symbol")
        )
        kind = self._history_kind(checkpoint.get("history_kind"))
        if kind not in {"ALL_ORDERS", "USER_TRADES"}:
            raise ValueError("exact history observations require an ALL_ORDERS or USER_TRADES checkpoint")
        id_field = "orderId" if kind == "ALL_ORDERS" else "id"
        normalized = self._normalize_page_items([item], history_kind=kind, expected_cursor_id=0)[0]
        payload = normalized["payload"]
        current_time = self._now(observed_at)
        if (current_time > datetime.now(UTC) or normalized["event_at"] > current_time
                or str(payload.get(id_field)) != str(normalized["item_id"])
                or str(payload.get("symbol") or "").upper() != symbol
                or str(payload.get("clientOrderId") or "") != str(normalized["client_id"] or "")):
            raise ValueError("exact history observation identity or timestamp is invalid")
        key = (target, run_id, symbol, kind)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        async with self.db.transaction() as connection:
            connection = connection or self.db
            current = await connection.fetchrow(
                """
                SELECT coverage_status FROM binance_history_checkpoints
                WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3 AND history_kind = $4
                FOR SHARE
                """, *key,
            )
            if current is None or current["coverage_status"] != "COVERED":
                raise RuntimeError("exact history observation requires a completely covered durable checkpoint")
            await connection.execute(
                """
                INSERT INTO binance_history_items (
                    runtime_target, run_id, symbol, history_kind, item_id,
                    client_id, event_at, payload_sha256, observed_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (runtime_target, run_id, symbol, history_kind, item_id) DO NOTHING
                """, *key, normalized["item_id"], normalized["client_id"],
                normalized["event_at"], normalized["payload_sha256"], current_time,
            )
            identity = await connection.fetchrow(
                """
                SELECT client_id, event_at FROM binance_history_items
                WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
                  AND history_kind = $4 AND item_id = $5
                """, *key, normalized["item_id"],
            )
            if (identity is None or identity["client_id"] != normalized["client_id"]
                    or _utc_datetime(identity["event_at"]) != normalized["event_at"]):
                raise RuntimeError("exact history observation differs from its durable identity")
            await connection.execute(
                """
                INSERT INTO binance_history_item_observations (
                    runtime_target, run_id, symbol, history_kind, item_id,
                    client_id, event_at, payload_sha256, payload, observed_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
                ON CONFLICT (runtime_target, run_id, symbol, history_kind, item_id, payload_sha256)
                DO NOTHING
                """, *key, normalized["item_id"], normalized["client_id"],
                normalized["event_at"], normalized["payload_sha256"], encoded, current_time,
            )
            row = await connection.fetchrow(
                """
                SELECT client_id, event_at, payload_sha256, payload, observed_at
                FROM binance_history_item_observations
                WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
                  AND history_kind = $4 AND item_id = $5 AND payload_sha256 = $6
                """, *key, normalized["item_id"], normalized["payload_sha256"],
            )
            if (row is None or row["client_id"] != normalized["client_id"]
                    or _utc_datetime(row["event_at"]) != normalized["event_at"]
                    or _utc_datetime(row["observed_at"]) > current_time
                    or row["payload_sha256"] != normalized["payload_sha256"]
                    or _decode_json_mapping(row["payload"], "History observation payload") != payload):
                raise RuntimeError("exact history observation failed durable read-back")
            return {**dict(row), "item_id": normalized["item_id"], "payload": payload}

    async def record_algo_history_observation(
        self,
        *,
        checkpoint: Mapping[str, Any],
        item: Mapping[str, Any],
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Append an exact-ID Algo refresh without advancing a page cursor."""

        if not isinstance(checkpoint, Mapping) or not isinstance(item, Mapping):
            raise TypeError("Algo observation checkpoint and item must be mappings")
        target, run_id, symbol = self._scope(
            checkpoint.get("runtime_target"), checkpoint.get("run_id"), checkpoint.get("symbol")
        )
        kind = self._history_kind(checkpoint.get("history_kind"))
        if kind != "ALL_ALGO_ORDERS":
            raise ValueError("exact Algo observations require an ALL_ALGO_ORDERS checkpoint")
        normalized_items = self._normalize_page_items(
            [item], history_kind=kind, expected_cursor_id=int(checkpoint.get("cursor_id", 0))
        )
        normalized = normalized_items[0]
        current_time = self._now(observed_at)
        if current_time > datetime.now(UTC):
            raise ValueError("Algo observation timestamp cannot be in the future")
        checkpoint_row = await self.db.fetchrow(
            """
            SELECT c.coverage_status, a.anchor_at
            FROM binance_history_checkpoints AS c
            JOIN binance_history_anchors AS a
              USING (runtime_target, run_id, symbol)
            WHERE c.runtime_target = $1 AND c.run_id = $2 AND c.symbol = $3
              AND c.history_kind = 'ALL_ALGO_ORDERS'
            """,
            target,
            run_id,
            symbol,
        )
        if checkpoint_row is None or str(checkpoint_row["coverage_status"]).upper() != "COVERED":
            raise RuntimeError("exact Algo observation requires a completely covered durable checkpoint")

        payload = normalized["payload"]
        encoded_payload = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        async with self.db.transaction() as connection:
            connection = connection or self.db
            await connection.execute(
                """
                INSERT INTO binance_algo_history_observations (
                    runtime_target, run_id, symbol, history_kind, item_id,
                    client_id, event_at, payload_sha256, payload, observed_at
                ) VALUES ($1, $2, $3, 'ALL_ALGO_ORDERS', $4, $5, $6, $7, $8::jsonb, $9)
                ON CONFLICT (runtime_target, run_id, symbol, item_id, payload_sha256)
                DO NOTHING
                """,
                target,
                run_id,
                symbol,
                normalized["item_id"],
                normalized["client_id"],
                normalized["event_at"],
                normalized["payload_sha256"],
                encoded_payload,
                current_time,
            )
            row = await connection.fetchrow(
                """
                SELECT client_id, event_at, payload_sha256, payload, observed_at
                FROM binance_algo_history_observations
                WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
                  AND item_id = $4 AND payload_sha256 = $5
                """,
                target,
                run_id,
                symbol,
                normalized["item_id"],
                normalized["payload_sha256"],
            )
        if (
            row is None
            or row.get("client_id") != normalized["client_id"]
            or _utc_datetime(row["event_at"]) != normalized["event_at"]
            or _utc_datetime(row["observed_at"]) > current_time
        ):
            raise RuntimeError("exact Algo observation failed durable identity read-back")
        persisted_payload = _decode_json_mapping(row["payload"], "Algo observation payload")
        canonical = json.dumps(
            persisted_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        if (
            persisted_payload != payload
            or hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            != normalized["payload_sha256"]
        ):
            raise RuntimeError("exact Algo observation failed payload checksum read-back")
        result = dict(row)
        result["item_id"] = normalized["item_id"]
        result["payload"] = persisted_payload
        return result

    async def list_history_items(self, checkpoint: Mapping[str, Any]) -> list[dict[str, Any]]:
        kind = self._history_kind(checkpoint.get("history_kind"))
        if kind == "ALL_ALGO_ORDERS":
            rows = await self.db.fetch(
                """
                SELECT DISTINCT ON (item_id)
                       item_id, client_id, event_at, payload_sha256, payload, observed_at
                FROM binance_algo_history_observations
                WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
                  AND history_kind = 'ALL_ALGO_ORDERS'
                ORDER BY item_id, observed_at DESC, observation_id DESC
                """,
                checkpoint["runtime_target"],
                checkpoint["run_id"],
                checkpoint["symbol"],
            )
            items: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                payload = _decode_json_mapping(item.get("payload"), "Algo history payload")
                canonical = json.dumps(
                    payload, sort_keys=True, separators=(",", ":"), allow_nan=False
                )
                if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != str(
                    item.get("payload_sha256", "")
                ):
                    raise RuntimeError("Durable Algo history payload fingerprint is invalid")
                item["payload"] = payload
                items.append(item)
            return items
        rows = await self.db.fetch(
            """
            SELECT DISTINCT ON (item_id)
                   item_id, client_id, event_at, payload_sha256, payload, observed_at
            FROM binance_history_item_observations
            WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
              AND history_kind = $4
            ORDER BY item_id, observed_at DESC, observation_id DESC
            """,
            checkpoint["runtime_target"],
            checkpoint["run_id"],
            checkpoint["symbol"],
            kind,
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            payload = _decode_json_mapping(item.get("payload"), "Binance history payload")
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != str(
                item.get("payload_sha256", "")
            ):
                raise RuntimeError("Durable Binance history payload fingerprint is invalid")
            item["payload"] = payload
            items.append(item)
        return items

    async def record_preexisting_algo_baseline(
        self, proof: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Append immutable evidence only against an already durable Testnet run anchor."""

        if not isinstance(proof, Mapping):
            raise TypeError("pre-existing Algo proof must be a mapping")
        allowed = {
            "run_id", "symbol", "algo_id", "client_algo_id", "algo_created_at",
            "terminal_status", "snapshot_observed_at", "position_snapshot",
            "open_orders_snapshot", "open_algo_orders_snapshot",
        }
        if set(proof) != allowed:
            raise ValueError("pre-existing Algo proof is incomplete or contains unsupported fields")
        target, run_id, symbol = self._scope("TESTNET", proof["run_id"], proof["symbol"])
        try:
            algo_id = int(proof["algo_id"])
        except (TypeError, ValueError) as exc:
            raise ValueError("pre-existing Algo ID is invalid") from exc
        if isinstance(proof["algo_id"], bool) or algo_id <= 0:
            raise ValueError("pre-existing Algo ID is invalid")
        client_algo_id = str(proof["client_algo_id"] or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", client_algo_id):
            raise ValueError("pre-existing Algo client ID is invalid")
        terminal_status = str(proof["terminal_status"] or "").strip().upper()
        if terminal_status not in _PREEXISTING_ALGO_TERMINAL_STATES:
            raise ValueError("pre-existing Algo status is not safely terminal")
        algo_created_at = self._now(proof["algo_created_at"])
        snapshot_observed_at = self._now(proof["snapshot_observed_at"])
        if snapshot_observed_at > datetime.now(UTC):
            raise ValueError("pre-existing Algo snapshot timestamp is in the future")
        snapshots: dict[str, list[dict[str, Any]]] = {}
        for field in ("position_snapshot", "open_orders_snapshot", "open_algo_orders_snapshot"):
            value = proof[field]
            if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
                raise ValueError(f"pre-existing Algo proof is missing a complete {field}")
            snapshots[field] = [dict(item) for item in value]

        anchor_row = await self.db.fetchrow(
            """
            SELECT anchor_at, anchor_source
            FROM binance_history_anchors
            WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
            """,
            target,
            run_id,
            symbol,
        )
        if anchor_row is None or anchor_row["anchor_source"] != "TESTNET_READONLY_START":
            raise ValueError("pre-existing Algo proof requires an existing durable Testnet run anchor")
        anchor_at = _utc_datetime(anchor_row["anchor_at"])
        if algo_created_at >= anchor_at:
            raise ValueError("Algo order does not predate the durable run anchor")
        if snapshot_observed_at < anchor_at:
            raise ValueError("open-state proof predates the durable run anchor")
        if snapshots["open_orders_snapshot"] or snapshots["open_algo_orders_snapshot"]:
            raise ValueError("pre-existing Algo proof contains an open order")
        target_positions = [
            position
            for position in snapshots["position_snapshot"]
            if str(position.get("symbol", "")).upper() == symbol
        ]
        if not target_positions:
            raise ValueError("pre-existing Algo proof has no target-symbol position snapshot")
        if any(not str(position.get("symbol", "")).strip() for position in snapshots["position_snapshot"]):
            raise ValueError("position proof contains an unscoped row")
        for position in snapshots["position_snapshot"]:
            amount = position.get("positionAmt")
            if amount in (None, ""):
                raise ValueError("position proof contains an incomplete position row")
            try:
                parsed_amount = Decimal(str(amount))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError("position proof contains an invalid position amount") from exc
            if not parsed_amount.is_finite() or parsed_amount != 0:
                raise ValueError("pre-existing Algo proof contains an open position")

        canonical = {
            "run_id": run_id,
            "symbol": symbol,
            "algo_id": algo_id,
            "client_algo_id": client_algo_id,
            "algo_created_at": algo_created_at,
            "anchor_at": anchor_at,
            "terminal_status": terminal_status,
            "snapshot_observed_at": snapshot_observed_at,
            **snapshots,
        }
        serialized = json.dumps(
            canonical, sort_keys=True, separators=(",", ":"), default=_json_default
        )
        proof_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        async with self.db.transaction() as connection:
            connection = connection or self.db
            await connection.execute(
                """
                INSERT INTO binance_preexisting_algo_baselines (
                    runtime_target, run_id, symbol, algo_id, client_algo_id,
                    algo_created_at, anchor_at, terminal_status, snapshot_observed_at,
                    position_snapshot, open_orders_snapshot, open_algo_orders_snapshot,
                    proof_sha256
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9,
                    $10::jsonb, $11::jsonb, $12::jsonb, $13
                )
                ON CONFLICT (runtime_target, run_id, symbol, algo_id) DO NOTHING
                """,
                target,
                run_id,
                symbol,
                algo_id,
                client_algo_id,
                algo_created_at,
                anchor_at,
                terminal_status,
                snapshot_observed_at,
                json.dumps(snapshots["position_snapshot"], default=_json_default),
                json.dumps(snapshots["open_orders_snapshot"], default=_json_default),
                json.dumps(snapshots["open_algo_orders_snapshot"], default=_json_default),
                proof_sha256,
            )
            row = await connection.fetchrow(
                """
                SELECT * FROM binance_preexisting_algo_baselines
                WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3 AND algo_id = $4
                """,
                target,
                run_id,
                symbol,
                algo_id,
            )
        if row is None or row["proof_sha256"] != proof_sha256:
            raise ValueError("immutable pre-existing Algo baseline conflicts with stored evidence")
        return dict(row)

    async def list_preexisting_algo_baselines(
        self, *, run_id: str, symbol: str
    ) -> list[dict[str, Any]]:
        target, run, normalized_symbol = self._scope("TESTNET", run_id, symbol)
        rows = await self.db.fetch(
            """
            SELECT * FROM binance_preexisting_algo_baselines
            WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
            ORDER BY algo_id
            """,
            target,
            run,
            normalized_symbol,
        )
        anchor = await self.db.fetchrow(
            """
            SELECT anchor_at, anchor_source FROM binance_history_anchors
            WHERE runtime_target = $1 AND run_id = $2 AND symbol = $3
            """,
            target,
            run,
            normalized_symbol,
        )
        if rows and (anchor is None or anchor["anchor_source"] != "TESTNET_READONLY_START"):
            raise RuntimeError("stored Algo baseline has no valid durable run anchor")
        result: list[dict[str, Any]] = []
        for row in rows:
            proof = {
                "run_id": run,
                "symbol": normalized_symbol,
                "algo_id": int(row["algo_id"]),
                "client_algo_id": row["client_algo_id"],
                "algo_created_at": _utc_datetime(row["algo_created_at"]),
                "anchor_at": _utc_datetime(row["anchor_at"]),
                "terminal_status": row["terminal_status"],
                "snapshot_observed_at": _utc_datetime(row["snapshot_observed_at"]),
                "position_snapshot": row["position_snapshot"],
                "open_orders_snapshot": row["open_orders_snapshot"],
                "open_algo_orders_snapshot": row["open_algo_orders_snapshot"],
            }
            if anchor is None or proof["anchor_at"] != _utc_datetime(anchor["anchor_at"]):
                raise RuntimeError("stored Algo baseline anchor differs from its durable run")
            fingerprint = hashlib.sha256(
                json.dumps(proof, sort_keys=True, separators=(",", ":"), default=_json_default)
                .encode("utf-8")
            ).hexdigest()
            if fingerprint != row["proof_sha256"]:
                raise RuntimeError("stored Algo baseline proof fingerprint is invalid")
            result.append(dict(row))
        return result
async def _ensure_instrument(connection: Any, instrument: Instrument) -> None:
    """Upsert only exchange-derived instrument metadata."""

    if not instrument.is_trading_enabled:
        raise InstrumentRulesUnavailable(
            f"instrument rules for {instrument.symbol} are not enabled for trading"
        )
    numeric_rules = (
        instrument.tick_size,
        instrument.step_size,
        instrument.min_notional,
    )
    if any(not value.is_finite() or value <= 0 for value in numeric_rules):
        raise InstrumentRulesUnavailable(
            f"instrument rules for {instrument.symbol} are incomplete"
        )
    await connection.execute(
        """
        INSERT INTO instruments (
            symbol, venue, market_type, base_asset, quote_asset, price_precision,
            quantity_precision, tick_size, step_size, min_notional, max_leverage,
            is_active, updated_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        ON CONFLICT (symbol) DO UPDATE SET
            venue = EXCLUDED.venue,
            market_type = EXCLUDED.market_type,
            base_asset = EXCLUDED.base_asset,
            quote_asset = EXCLUDED.quote_asset,
            price_precision = EXCLUDED.price_precision,
            quantity_precision = EXCLUDED.quantity_precision,
            tick_size = EXCLUDED.tick_size,
            step_size = EXCLUDED.step_size,
            min_notional = EXCLUDED.min_notional,
            max_leverage = EXCLUDED.max_leverage,
            is_active = EXCLUDED.is_active,
            updated_at = EXCLUDED.updated_at
        """,
        instrument.symbol,
        instrument.venue,
        str(_enum_value(instrument.market_type)),
        instrument.base_asset,
        instrument.quote_asset,
        instrument.price_precision,
        instrument.quantity_precision,
        instrument.tick_size,
        instrument.step_size,
        instrument.min_notional,
        instrument.max_leverage,
        instrument.is_trading_enabled,
        datetime.now(UTC),
    )


class PersistenceRepository:
    """Store events durably, then replay them into domain tables transactionally."""

    def __init__(
        self,
        db: PostgresClient,
        instrument_rules_provider: Optional[InstrumentRulesProvider] = None,
    ) -> None:
        self.db = db
        self.orders = OrderRepository(db)
        self.fills = FillRepository(db)
        self.positions = PositionRepository(db)
        self.risk = RiskSnapshotRepository(db)
        self.algo_protections = AlgoProtectionRepository(db)
        self.binance_history = BinanceHistoryRepository(db)
        self.instrument_rules_provider = instrument_rules_provider

    async def append_local_live_pilot_event(
        self,
        *,
        campaign_id: str,
        launch_id: str,
        run_id: str,
        symbol: str,
        event_key: str,
        event_type: str,
        source: str,
        observed_at: datetime,
        payload: Mapping[str, Any],
        net_pnl_delta_usdc: Decimal | str | int | None = None,
    ) -> Optional[Mapping[str, Any]]:
        """Append one immutable, idempotent pilot event and atomically update PnL.

        The stable pilot run identity is the durable launch_id. MARK events
        carry the current unrealized PnL component in their payload;
        replacing the prior component avoids adding the same mark snapshot more
        than once. FILL/FEE/FUNDING deltas are additive and must be reconciled
        by their caller against the authoritative Binance event.
        """
        normalized_type = str(event_type).strip().upper()
        normalized_source = str(source).strip().upper()
        normalized_symbol = str(symbol).strip().upper()
        if (
            not campaign_id or not launch_id or run_id != launch_id
            or normalized_symbol != "ETHUSDC"
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,192}", str(event_key))
            or normalized_type not in {"ORDER_INTENT", "ORDER", "FILL", "FEE", "FUNDING", "MARK", "PROTECTION", "CLOSE", "RECONCILIATION", "STATE"}
            or normalized_source not in {"WORKER", "BINANCE", "SUPERVISOR"}
            or observed_at.tzinfo is None
            or not isinstance(payload, Mapping)
        ):
            raise ValueError("Local Live Pilot event identity or evidence is invalid")
        safe_payload = json.loads(json.dumps(dict(payload), default=_json_default, sort_keys=True, separators=(",", ":")))
        if safe_payload.get("run_id") != run_id:
            raise ValueError("pilot event payload must be bound to the supplied run_id")
        payload_bytes = json.dumps(safe_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        normalized_observed_at = observed_at.astimezone(UTC)
        delta: Optional[Decimal] = None
        snapshot_unrealized: Optional[Decimal] = None
        if net_pnl_delta_usdc is not None:
            try:
                delta = Decimal(str(net_pnl_delta_usdc))
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("pilot PnL delta is invalid") from exc
            if not delta.is_finite():
                raise ValueError("pilot PnL delta is invalid")
        if normalized_type == "MARK":
            try:
                snapshot_unrealized = Decimal(str(safe_payload["unrealized_pnl_usdc"]))
            except (KeyError, InvalidOperation, ValueError) as exc:
                raise ValueError("pilot MARK requires an authoritative unrealized_pnl_usdc snapshot") from exc
            if not snapshot_unrealized.is_finite() or delta is not None:
                raise ValueError("pilot MARK snapshot must be finite and cannot carry a delta")
        if normalized_type in {"FILL", "FEE", "FUNDING"} and delta is None:
            raise ValueError("pilot financial event requires an authoritative PnL delta")
        if normalized_type == "FEE" and delta is not None and delta > 0:
            raise ValueError("pilot fee deltas must be non-positive; rebates require a separate income event")
        if normalized_type in {"FILL", "FEE", "FUNDING"} and normalized_source != "BINANCE":
            raise ValueError("pilot financial deltas must come from Binance evidence")
        if normalized_type == "MARK" and normalized_source != "BINANCE":
            raise ValueError("pilot mark snapshots must come from Binance account evidence")
        if normalized_type not in {"FILL", "FEE", "FUNDING", "MARK"} and delta is not None:
            raise ValueError("non-financial pilot events cannot change PnL")
        if normalized_type in {"FILL", "FEE", "FUNDING", "MARK"}:
            evidence_id_field = "account_snapshot_id" if normalized_type == "MARK" else "exchange_event_id"
            evidence_id = str(safe_payload.get(evidence_id_field, ""))
            if (
                not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", evidence_id)
                or str(event_key) != f"{normalized_type}:{evidence_id}"
            ):
                raise ValueError("pilot PnL event key must bind to its exchange evidence identity")

        async with self.db.transaction() as transaction_connection:
            connection = transaction_connection or self.db
            session = await connection.fetchrow(
                """SELECT launch_id, symbol, policy, runtime_target, pilot_campaign_id,
                          pilot_net_pnl_usdc, pilot_peak_pnl_usdc, pilot_drawdown_triggered,
                          pilot_status, pilot_last_account_snapshot_at, state,
                          max_risk_increasing_orders, reserved_orders, submitted_orders
                   FROM mainnet_launch_sessions WHERE launch_id = $1 FOR UPDATE""",
                launch_id,
            )
            closing_state_event = (
                normalized_type == "STATE"
                and safe_payload.get("kind") in {
                    "SESSION_CLOSE_CLAIMED", "SESSION_CLOSE_SUBMITTING"
                }
            )
            if (
                session is None
                or str(session["symbol"]).upper() != normalized_symbol
                or session["policy"] != "LIVE_RESEARCH_PILOT"
                or session["runtime_target"] != "LOCAL"
                or session["pilot_campaign_id"] != campaign_id
                or (
                    session["state"] in {"REAUTH_REQUIRED", "RECONCILIATION_REQUIRED"}
                    and not closing_state_event
                )
            ):
                raise RuntimeError("pilot event does not match its durable Local campaign")
            if closing_state_event:
                armed = await connection.fetchrow(
                    """SELECT event_id FROM local_live_pilot_events
                       WHERE campaign_id = $1 AND event_type = 'STATE'
                         AND payload->>'kind' = 'SESSION_ARMED'
                       LIMIT 1""",
                    campaign_id,
                )
                if (
                    armed is None
                    or session["state"] == "CLOSED"
                    or normalized_source != "WORKER"
                    or not str(safe_payload.get("close_client_order_id") or "")
                ):
                    raise RuntimeError("pilot session close event lacks its durable ARM binding")
            if safe_payload.get("kind") == "SESSION_ARMED":
                if (
                    normalized_type != "STATE"
                    or normalized_source != "WORKER"
                    or str(event_key) != "STATE:SESSION_ARMED"
                    or session["state"] != "ACTIVE"
                    or session["max_risk_increasing_orders"] != 1
                    or int(session["reserved_orders"] or 0) != 0
                    or int(session["submitted_orders"] or 0) != 0
                ):
                    raise RuntimeError("pilot session ARM is already used or not safely reserved")
                session_armed = await connection.fetchrow(
                    """SELECT launch_id FROM local_live_pilot_events
                       WHERE campaign_id = $1 AND event_type = 'STATE'
                         AND payload->>'kind' = 'SESSION_ARMED'
                       LIMIT 1""",
                    campaign_id,
                )
                if session_armed is not None:
                    raise RuntimeError("pilot campaign already has a durable SESSION_ARMED event")
                if any(
                    not isinstance(safe_payload.get(field), str)
                    or not safe_payload[field]
                    for field in (
                        "armed_at", "entry_cutoff_at", "close_after_at", "end_at"
                    )
                ):
                    raise ValueError("pilot SESSION_ARMED event is missing immutable deadlines")
            prior = await connection.fetchrow(
                """SELECT launch_id, event_type, source, observed_at,
                          net_pnl_delta_usdc, payload_sha256
                   FROM local_live_pilot_events
                   WHERE campaign_id = $1 AND event_key = $2""",
                campaign_id, str(event_key),
            )
            if prior is not None:
                if (
                    prior["launch_id"] != launch_id
                    or prior["payload_sha256"] != payload_hash
                    or prior["event_type"] != normalized_type
                    or prior["source"] != normalized_source
                    or prior["observed_at"] != normalized_observed_at
                    or (
                        None if prior["net_pnl_delta_usdc"] is None
                        else Decimal(str(prior["net_pnl_delta_usdc"]))
                    ) != delta
                ):
                    raise RuntimeError("pilot event key was reused with conflicting evidence")
                updated = await connection.fetchrow(
                    """SELECT launch_id, pilot_campaign_id, pilot_net_pnl_usdc,
                              pilot_peak_pnl_usdc, pilot_drawdown_triggered, pilot_status,
                              state, updated_at
                       FROM mainnet_launch_sessions WHERE launch_id = $1""",
                    launch_id,
                )
                return dict(updated) if updated is not None else None

            last_mark = session.get("pilot_last_account_snapshot_at")
            if normalized_type == "MARK" and last_mark is not None and observed_at <= last_mark:
                raise RuntimeError("out-of-order pilot account snapshot rejected")
            if normalized_type == "MARK":
                latest_financial_at = await connection.fetchval(
                    """SELECT MAX(observed_at) FROM local_live_pilot_events
                       WHERE campaign_id = $1 AND event_type IN ('FILL', 'FEE', 'FUNDING')""",
                    campaign_id,
                )
                if latest_financial_at is not None and observed_at < latest_financial_at:
                    raise RuntimeError("pilot mark predates a financial event")
            last_accounting_event = await connection.fetchrow(
                """SELECT event_type, event_id
                   FROM local_live_pilot_events
                   WHERE campaign_id = $1 AND event_type IN ('MARK', 'FILL', 'FEE', 'FUNDING')
                   ORDER BY event_id DESC LIMIT 1""",
                campaign_id,
            )
            accounting_snapshot_pending = bool(
                last_accounting_event
                and last_accounting_event["event_type"] != "MARK"
            )
            accounting_resume_eligible = False
            if normalized_type in {"MARK", "FILL", "FEE", "FUNDING"}:
                resume_marker = await connection.fetchval(
                    """SELECT payload->>'resume_eligible'
                       FROM local_live_pilot_events
                       WHERE campaign_id = $1 AND event_type = 'STATE'
                         AND payload->>'kind' = 'ACCOUNTING_SNAPSHOT_PENDING'
                       ORDER BY event_id DESC LIMIT 1""",
                    campaign_id,
                )
                accounting_resume_eligible = str(resume_marker).lower() == "true"
            fill_resume_eligible = False
            launch_state: Optional[str] = None
            inserted = await connection.fetchrow(
                """INSERT INTO local_live_pilot_events
                       (campaign_id, launch_id, event_key, event_type, source, observed_at,
                        net_pnl_delta_usdc, payload_sha256, payload)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb)
                   ON CONFLICT (campaign_id, event_key) DO NOTHING
                   RETURNING event_id""",
                campaign_id, launch_id, str(event_key), normalized_type,
                normalized_source, normalized_observed_at,
                None if normalized_type in {"MARK", "ORDER_INTENT", "ORDER", "PROTECTION", "CLOSE", "RECONCILIATION", "STATE"} else delta,
                payload_hash, json.dumps(safe_payload, separators=(",", ":")),
            )
            if inserted is None:
                prior = await connection.fetchrow(
                    """SELECT launch_id, event_type, source, observed_at,
                              net_pnl_delta_usdc, payload_sha256
                       FROM local_live_pilot_events
                       WHERE campaign_id = $1 AND event_key = $2""",
                    campaign_id, str(event_key),
                )
                if (
                    prior is None
                    or prior["launch_id"] != launch_id
                    or prior["payload_sha256"] != payload_hash
                    or prior["event_type"] != normalized_type
                    or prior["source"] != normalized_source
                    or prior["observed_at"] != normalized_observed_at
                    or (
                        None if prior["net_pnl_delta_usdc"] is None
                        else Decimal(str(prior["net_pnl_delta_usdc"]))
                    ) != delta
                ):
                    raise RuntimeError("pilot event key was reused with conflicting evidence")
            else:
                fill_resume_eligible = bool(
                    normalized_type in {"FILL", "FEE", "FUNDING"}
                    and (
                        accounting_resume_eligible
                        or (
                            not accounting_snapshot_pending
                            and session["state"] == "ACTIVE"
                            and session["pilot_status"] == "ACTIVE"
                            and session["pilot_drawdown_triggered"] is False
                        )
                    )
                )
                if normalized_type in {"FILL", "FEE", "FUNDING"}:
                    financial_evidence_id = str(safe_payload["exchange_event_id"])
                    state_payload = {
                        "run_id": run_id,
                        "kind": "ACCOUNTING_SNAPSHOT_PENDING",
                        "exchange_event_id": financial_evidence_id,
                        "resume_eligible": fill_resume_eligible,
                    }
                    state_payload_bytes = json.dumps(
                        state_payload, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                    await connection.fetchrow(
                        """INSERT INTO local_live_pilot_events
                               (campaign_id, launch_id, event_key, event_type, source, observed_at,
                                net_pnl_delta_usdc, payload_sha256, payload)
                           VALUES ($1,$2,$3,'STATE','WORKER',$4,NULL,$5,$6::jsonb)
                           ON CONFLICT (campaign_id, event_key) DO NOTHING
                           RETURNING event_id""",
                        campaign_id,
                        launch_id,
                        f"STATE:ACCOUNTING_SNAPSHOT:{normalized_type}:{financial_evidence_id}",
                        normalized_observed_at,
                        hashlib.sha256(state_payload_bytes).hexdigest(),
                        json.dumps(state_payload, separators=(",", ":")),
                    )

                prior_net = Decimal(str(session["pilot_net_pnl_usdc"]))
                if normalized_type == "MARK":
                    realized_total = await connection.fetchval(
                        """SELECT COALESCE(SUM(net_pnl_delta_usdc), 0)::numeric
                           FROM local_live_pilot_events
                           WHERE campaign_id = $1
                             AND event_type IN ('FILL', 'FEE', 'FUNDING')""",
                        campaign_id,
                    )
                    next_net = Decimal(str(realized_total or 0)) + (
                        snapshot_unrealized or Decimal("0")
                    )
                elif normalized_type in {"FILL", "FEE", "FUNDING"}:
                    # A financial event can arrive while the last position
                    # mark describes a pre-fill position. Keep the displayed
                    # value stale, freeze the peak, and require a fresh mark
                    # before accounting can resume or new risk is allowed.
                    next_net = prior_net
                else:
                    next_net = prior_net
                prior_peak = Decimal(str(session["pilot_peak_pnl_usdc"]))
                peak = (
                    prior_peak
                    if normalized_type in {"FILL", "FEE", "FUNDING"}
                    or (accounting_snapshot_pending and normalized_type != "MARK")
                    else max(prior_peak, next_net)
                )
                drawdown = peak - next_net
                accounting_unknown = (
                    normalized_type in {"FILL", "FEE", "FUNDING"}
                    or (accounting_snapshot_pending and normalized_type != "MARK")
                )
                trigger = bool(session["pilot_drawdown_triggered"]) or (
                    not accounting_unknown and drawdown >= Decimal("5")
                )
                status = (
                    "CLOSE_ONLY"
                    if trigger
                    else session["pilot_status"]
                )
                resume_after_snapshot = bool(
                    normalized_type == "MARK"
                    and accounting_snapshot_pending
                    and accounting_resume_eligible
                    and session["state"] == "PAUSED_NEW_RISK"
                    and session["pilot_status"] == "ACTIVE"
                    and not bool(session["pilot_drawdown_triggered"])
                    and not trigger
                )
                launch_state = (
                    "PAUSED_NEW_RISK"
                    if trigger or normalized_type in {"FILL", "FEE", "FUNDING"} or (
                        accounting_snapshot_pending and normalized_type != "MARK"
                    )
                    else "ACTIVE" if resume_after_snapshot else None
                )
                await connection.execute(
                    """UPDATE mainnet_launch_sessions
                       SET pilot_net_pnl_usdc = $2, pilot_peak_pnl_usdc = $3,
                           pilot_drawdown_triggered = $4, pilot_status = $5,
                           state = COALESCE($6, state),
                           pilot_last_account_snapshot_at = CASE WHEN $7 THEN $8 ELSE pilot_last_account_snapshot_at END,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE launch_id = $1""",
                    launch_id, next_net, peak, trigger, status, launch_state,
                    normalized_type == "MARK", normalized_observed_at,
                )
            updated = await connection.fetchrow(
                """SELECT launch_id, pilot_campaign_id, pilot_net_pnl_usdc,
                          pilot_peak_pnl_usdc, pilot_drawdown_triggered, pilot_status,
                          state, updated_at
                   FROM mainnet_launch_sessions WHERE launch_id = $1""",
                launch_id,
            )
            if updated is None:
                return None
            result = dict(updated)
            result["pilot_accounting_resume_eligible"] = (
                fill_resume_eligible
                if normalized_type in {"FILL", "FEE", "FUNDING"} and inserted is not None
                else False
            )
            result["pilot_accounting_resumed"] = bool(
                normalized_type == "MARK" and launch_state == "ACTIVE"
            )
            return result

    async def record_local_live_pilot_session_armed(
        self,
        launch_id: str,
        *,
        armed_at: datetime,
        entry_cutoff_seconds: int,
        close_after_seconds: int,
        end_seconds: int,
    ) -> Mapping[str, Any]:
        """Persist the one allowed T0 event before the Worker exposes ARMED."""
        from apps.trading_worker.venues.binance.session import SessionLimits

        limits = SessionLimits(
            entry_cutoff_seconds=entry_cutoff_seconds,
            close_after_seconds=close_after_seconds,
            end_seconds=end_seconds,
        )
        normalized_armed_at = _utc_datetime(armed_at)
        if normalized_armed_at is None:
            raise ValueError("pilot SESSION_ARMED timestamp must be timezone-aware")
        armed_at_text = normalized_armed_at.isoformat().replace("+00:00", "Z")
        payload = {
            "run_id": launch_id,
            "kind": "SESSION_ARMED",
            "armed_at": armed_at_text,
            "entry_cutoff_at": (
                normalized_armed_at + timedelta(seconds=limits.entry_cutoff_seconds)
            ).isoformat().replace("+00:00", "Z"),
            "close_after_at": (
                normalized_armed_at + timedelta(seconds=limits.close_after_seconds)
            ).isoformat().replace("+00:00", "Z"),
            "end_at": (
                normalized_armed_at + timedelta(seconds=limits.end_seconds)
            ).isoformat().replace("+00:00", "Z"),
        }
        session = await self.get_mainnet_launch(launch_id)
        if not session or not session.get("pilot_campaign_id"):
            raise RuntimeError("pilot session ARM has no durable campaign binding")
        await self.append_local_live_pilot_event(
            campaign_id=str(session["pilot_campaign_id"]),
            launch_id=launch_id,
            run_id=launch_id,
            symbol=str(session.get("symbol") or ""),
            event_key="STATE:SESSION_ARMED",
            event_type="STATE",
            source="WORKER",
            observed_at=normalized_armed_at,
            payload=payload,
        )
        readback = await self.get_mainnet_launch(launch_id)
        if not readback or readback.get("pilot_session_armed_at") != armed_at_text:
            raise RuntimeError("durable SESSION_ARMED event failed read-back")
        return readback

    async def record_local_live_pilot_session_close_claim(
        self,
        launch_id: str,
        *,
        client_order_id: str,
        side: str,
        position_side: str,
        quantity: Decimal,
        claimed_at: datetime,
    ) -> Mapping[str, Any]:
        """Persist the immutable close identity before the exchange submission."""
        normalized_side = str(side).strip().upper()
        normalized_position_side = str(position_side).strip().upper()
        normalized_quantity = Decimal(str(quantity))
        normalized_claimed_at = _utc_datetime(claimed_at)
        if (
            not client_order_id
            or normalized_side not in {"BUY", "SELL"}
            or normalized_position_side not in {"BOTH", "LONG", "SHORT"}
            or not normalized_quantity.is_finite()
            or normalized_quantity <= 0
            or normalized_claimed_at is None
        ):
            raise ValueError("pilot session close identity is invalid")
        session = await self.get_mainnet_launch(launch_id)
        if (
            not session
            or not session.get("pilot_campaign_id")
            or not session.get("pilot_session_armed_at")
        ):
            raise RuntimeError("pilot session close has no durable ARM binding")
        campaign_id = str(session["pilot_campaign_id"])
        payload = {
            "run_id": launch_id,
            "kind": "SESSION_CLOSE_CLAIMED",
            "close_client_order_id": client_order_id,
            "close_symbol": "ETHUSDC",
            "close_side": normalized_side,
            "close_position_side": normalized_position_side,
            "close_quantity": str(normalized_quantity),
            "claimed_at": normalized_claimed_at.isoformat().replace("+00:00", "Z"),
        }
        prior = await self.db.fetchrow(
            """SELECT observed_at, payload FROM local_live_pilot_events
               WHERE campaign_id = $1 AND launch_id = $2
                 AND event_key = 'STATE:SESSION_CLOSE_CLAIMED'""",
            campaign_id, launch_id,
        )
        event_time = normalized_claimed_at
        if prior is not None:
            prior_payload = prior["payload"]
            if isinstance(prior_payload, str):
                prior_payload = json.loads(prior_payload)
            if not isinstance(prior_payload, Mapping) or any(
                prior_payload.get(key) != value
                for key, value in payload.items()
                if key != "claimed_at"
            ):
                raise RuntimeError("pilot session close identity changed after durable claim")
            event_time = prior["observed_at"]
            payload["claimed_at"] = str(prior_payload.get("claimed_at") or "")
        saved = await self.append_local_live_pilot_event(
            campaign_id=campaign_id,
            launch_id=launch_id,
            run_id=launch_id,
            symbol="ETHUSDC",
            event_key="STATE:SESSION_CLOSE_CLAIMED",
            event_type="STATE",
            source="WORKER",
            observed_at=event_time,
            payload=payload,
        )
        if not isinstance(saved, dict):
            raise RuntimeError("pilot session close identity was not durably acknowledged")
        readback = await self.get_mainnet_launch(launch_id)
        if (
            not readback
            or readback.get("pilot_session_close_client_order_id") != client_order_id
            or str(readback.get("pilot_session_close_quantity")) != str(normalized_quantity)
        ):
            raise RuntimeError("pilot session close identity failed read-back")
        return readback

    async def record_local_live_pilot_session_close_attempt(
        self,
        launch_id: str,
        *,
        client_order_id: str,
        attempt: int,
        attempted_at: datetime,
    ) -> Mapping[str, Any]:
        """Append a bounded attempt marker before each emergency close POST."""
        if type(attempt) is not int or attempt not in {1, 2}:
            raise ValueError("pilot session close attempt exceeds its two-attempt bound")
        normalized_at = _utc_datetime(attempted_at)
        if normalized_at is None:
            raise ValueError("pilot session close attempt time must be timezone-aware")
        session = await self.get_mainnet_launch(launch_id)
        if (
            not session
            or session.get("pilot_session_close_client_order_id") != client_order_id
            or not session.get("pilot_session_armed_at")
        ):
            raise RuntimeError("pilot session close attempt lacks its durable claim")
        campaign_id = str(session["pilot_campaign_id"])
        event_key = f"STATE:SESSION_CLOSE_SUBMITTING:{attempt}"
        payload = {
            "run_id": launch_id,
            "kind": "SESSION_CLOSE_SUBMITTING",
            "close_client_order_id": client_order_id,
            "attempt": attempt,
        }
        prior = await self.db.fetchrow(
            """SELECT observed_at, payload FROM local_live_pilot_events
               WHERE campaign_id = $1 AND launch_id = $2 AND event_key = $3""",
            campaign_id, launch_id, event_key,
        )
        if prior is not None:
            prior_payload = prior["payload"]
            if isinstance(prior_payload, str):
                prior_payload = json.loads(prior_payload)
            if not isinstance(prior_payload, Mapping) or any(
                prior_payload.get(key) != value for key, value in payload.items()
            ):
                raise RuntimeError("pilot session close attempt conflicts with durable evidence")
            normalized_at = prior["observed_at"]
        elif attempt != int(session.get("pilot_session_close_attempt_count") or 0) + 1:
            raise RuntimeError("pilot session close attempt sequence is not contiguous")
        saved = await self.append_local_live_pilot_event(
            campaign_id=campaign_id,
            launch_id=launch_id,
            run_id=launch_id,
            symbol="ETHUSDC",
            event_key=event_key,
            event_type="STATE",
            source="WORKER",
            observed_at=normalized_at,
            payload=payload,
        )
        if not isinstance(saved, dict):
            raise RuntimeError("pilot session close attempt was not durably acknowledged")
        readback = await self.get_mainnet_launch(launch_id)
        if int(readback.get("pilot_session_close_attempt_count") or 0) < attempt:
            raise RuntimeError("pilot session close attempt failed read-back")
        return readback

    async def create_mainnet_launch_session(
        self,
        *,
        launch_id: str,
        approval_id: str,
        image_digest: Optional[str] = None,
        symbol: str = "ETHUSDC",
        policy: str = "STAGED_FIRST_ORDER",
        max_risk_increasing_orders: int = 1,
        runtime_target: str = _CLOUD_RUNTIME_TARGET,
        runtime_fingerprint: Optional[str] = None,
        pilot_binding: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        """Create or verify the durable staged-launch session.

        Repeating ARM after a process restart is idempotent for the same
        approval, but it can never reset reserved/submitted order counters.
        """

        if not launch_id or not approval_id:
            raise ValueError("launch session identity is incomplete")
        normalized_target, image_digest, runtime_fingerprint = _validate_launch_identity(
            runtime_target=runtime_target,
            image_digest=image_digest,
            runtime_fingerprint=runtime_fingerprint,
        )
        if symbol.upper() != "ETHUSDC" or policy not in {"STAGED_FIRST_ORDER", "LIVE_RESEARCH_PILOT"}:
            raise ValueError("Mainnet launch session has an unsupported symbol or policy")
        if policy == "STAGED_FIRST_ORDER" and max_risk_increasing_orders != 1:
            raise ValueError("Mainnet staged launch permits exactly one risk-increasing order")
        normalized_pilot: dict[str, Any] | None = None
        if policy == "LIVE_RESEARCH_PILOT":
            if normalized_target != _LOCAL_RUNTIME_TARGET or max_risk_increasing_orders != 1:
                raise ValueError("Live Research Pilot is Local-only and permits exactly one risk-increasing order")
            required_pilot_fields = {
                "campaign_id", "git_sha", "source_hash", "dependency_hash", "migration_hash",
                "strategy_hash", "secret_project_id", "api_key_version", "api_secret_version",
                "management_mode", "expires_at", "risk_policy_hash",
            }
            if not isinstance(pilot_binding, Mapping) or set(pilot_binding) != required_pilot_fields:
                raise ValueError("Live Research Pilot binding is incomplete")
            hash_fields = ("source_hash", "dependency_hash", "migration_hash", "strategy_hash", "risk_policy_hash")
            if any(not re.fullmatch(r"[0-9a-f]{64}", str(pilot_binding.get(key, ""))) for key in hash_fields):
                raise ValueError("Live Research Pilot hashes are invalid")
            if not re.fullmatch(r"[0-9a-f]{40,64}", str(pilot_binding.get("git_sha", ""))):
                raise ValueError("Live Research Pilot commit identity is invalid")
            if not re.fullmatch(r"pilot-[A-Za-z0-9][A-Za-z0-9_-]{7,126}", str(pilot_binding.get("campaign_id", ""))):
                raise ValueError("Live Research Pilot campaign identity is invalid")
            if pilot_binding.get("management_mode") != "QUICK":
                raise ValueError("the first Live Research Pilot must use QUICK management")
            if not re.fullmatch(r"^[1-9][0-9]*$", str(pilot_binding.get("api_key_version", ""))) or not re.fullmatch(r"^[1-9][0-9]*$", str(pilot_binding.get("api_secret_version", ""))):
                raise ValueError("Live Research Pilot Secret Manager versions are invalid")
            if not str(pilot_binding.get("secret_project_id", "")):
                raise ValueError("Live Research Pilot Secret Manager project is missing")
            expires_at = _utc_datetime(pilot_binding.get("expires_at"))
            if expires_at is None or expires_at <= datetime.now(UTC):
                raise ValueError("Live Research Pilot approval is expired")
            normalized_pilot = dict(pilot_binding)
            normalized_pilot["expires_at"] = expires_at
        await self.db.execute(
            """
            UPDATE mainnet_launch_sessions
            SET state = 'CLOSED', updated_at = CURRENT_TIMESTAMP
            WHERE symbol = $1
              AND approval_id != $2
              AND state IN ('ACTIVE', 'PAUSED_NEW_RISK', 'RECONCILIATION_REQUIRED', 'REAUTH_REQUIRED')
              AND submitted_orders = 0
              AND pending_order_client_order_id IS NULL
            """,
            symbol.upper(),
            approval_id,
        )
        unreviewed = await self.db.fetchrow(
            """
            SELECT launch_id, approval_id, submitted_orders,
                   pending_order_client_order_id, state
            FROM mainnet_launch_sessions
            WHERE symbol = $1
              AND approval_id != $2
              AND state IN ('ACTIVE', 'PAUSED_NEW_RISK', 'RECONCILIATION_REQUIRED', 'AUTONOMOUS_ACTIVE', 'REAUTH_REQUIRED')
              AND (
                submitted_orders > 0
                OR pending_order_client_order_id IS NOT NULL
              )
            """,
            symbol.upper(),
            approval_id,
        )
        if unreviewed is not None:
            raise RuntimeError(
                f"existing session {unreviewed['launch_id']} has a submitted order pending review or an unresolved client order"
            )
        try:
            await self.db.execute(
                """
                INSERT INTO mainnet_launch_sessions (
                    launch_id, approval_id, image_digest, symbol, policy,
                    max_risk_increasing_orders, reserved_orders, submitted_orders,
                    state, created_at, updated_at, runtime_target, runtime_fingerprint,
                    pilot_campaign_id, pilot_git_sha, pilot_source_hash,
                    pilot_dependency_hash, pilot_migration_hash, pilot_strategy_hash,
                    pilot_secret_project_id, pilot_api_key_version,
                    pilot_api_secret_version, pilot_management_mode,
                    pilot_campaign_expires_at, pilot_risk_policy_hash,
                    pilot_max_position_notional_usdc, pilot_per_position_risk_usdc,
                    pilot_max_drawdown_usdc, pilot_quick_target_net_usdc,
                    pilot_quick_max_hold_seconds, pilot_max_leverage, pilot_status
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, 0, 0, 'ACTIVE', CURRENT_TIMESTAMP,
                    CURRENT_TIMESTAMP, $7, $8, $9, $10, $11, $12, $13, $14,
                    $15, $16, $17, $18, $19, $20,
                    CASE WHEN $5::varchar = 'LIVE_RESEARCH_PILOT' THEN 50 ELSE NULL END,
                    CASE WHEN $5::varchar = 'LIVE_RESEARCH_PILOT' THEN 2 ELSE NULL END,
                    CASE WHEN $5::varchar = 'LIVE_RESEARCH_PILOT' THEN 5 ELSE NULL END,
                    CASE WHEN $5::varchar = 'LIVE_RESEARCH_PILOT' THEN 0.25 ELSE NULL END,
                    CASE WHEN $5::varchar = 'LIVE_RESEARCH_PILOT' THEN 86400 ELSE NULL END,
                    CASE WHEN $5::varchar = 'LIVE_RESEARCH_PILOT' THEN 10 ELSE NULL END,
                    CASE WHEN $5::varchar = 'LIVE_RESEARCH_PILOT' THEN 'ACTIVE' ELSE NULL END
                )
                ON CONFLICT (approval_id) DO NOTHING
                """,
                launch_id,
                approval_id,
                image_digest,
                symbol.upper(),
                policy,
                max_risk_increasing_orders,
                normalized_target,
                runtime_fingerprint,
                normalized_pilot["campaign_id"] if normalized_pilot else None,
                normalized_pilot["git_sha"] if normalized_pilot else None,
                normalized_pilot["source_hash"] if normalized_pilot else None,
                normalized_pilot["dependency_hash"] if normalized_pilot else None,
                normalized_pilot["migration_hash"] if normalized_pilot else None,
                normalized_pilot["strategy_hash"] if normalized_pilot else None,
                normalized_pilot["secret_project_id"] if normalized_pilot else None,
                normalized_pilot["api_key_version"] if normalized_pilot else None,
                normalized_pilot["api_secret_version"] if normalized_pilot else None,
                normalized_pilot["management_mode"] if normalized_pilot else None,
                normalized_pilot["expires_at"] if normalized_pilot else None,
                normalized_pilot["risk_policy_hash"] if normalized_pilot else None,
            )
        except Exception as exc:
            exc_name = type(exc).__name__
            if "UniqueViolation" in exc_name or "unique" in str(exc).lower():
                raise RuntimeError("existing session has a submitted order pending review") from exc
            raise
        row = await self.db.fetchrow(
            """
            SELECT launch_id, approval_id, image_digest, symbol, policy, basket_id,
                   max_risk_increasing_orders, reserved_orders, submitted_orders,
                   state, continuation_approval_id, first_order_verified_at,
                   pending_order_client_order_id, first_order_client_order_id,
                   autonomous_approved_at, last_restart_at, created_at, updated_at,
                   runtime_target, runtime_fingerprint, pilot_campaign_id,
                   pilot_git_sha, pilot_source_hash, pilot_dependency_hash,
                   pilot_migration_hash, pilot_strategy_hash, pilot_secret_project_id,
                   pilot_api_key_version, pilot_api_secret_version, pilot_management_mode,
                   pilot_campaign_expires_at, pilot_risk_policy_hash,
                   pilot_max_position_notional_usdc, pilot_per_position_risk_usdc,
                   pilot_max_drawdown_usdc, pilot_quick_target_net_usdc,
                   pilot_quick_max_hold_seconds, pilot_max_leverage, pilot_status,
                   pilot_net_pnl_usdc, pilot_peak_pnl_usdc, pilot_drawdown_triggered
                   , pilot_last_account_snapshot_at
            FROM mainnet_launch_sessions
            WHERE approval_id = $1
            """,
            approval_id,
        )
        if row is None:
            raise RuntimeError("mainnet launch session could not be read after creation")
        row_dict: dict[str, Any] = dict(row)
        if (
            str(row_dict.get("launch_id", "")) != launch_id
            or (
                normalized_target == _CLOUD_RUNTIME_TARGET
                and str(row_dict.get("image_digest", "")) != image_digest
            )
            or (
                normalized_target == _LOCAL_RUNTIME_TARGET
                and row_dict.get("image_digest") is not None
            )
            or str(row_dict.get("runtime_target", _CLOUD_RUNTIME_TARGET)).upper()
            != normalized_target
            or (
                normalized_target == _LOCAL_RUNTIME_TARGET
                and str(row_dict.get("runtime_fingerprint", "")).lower()
                != str(runtime_fingerprint).lower()
            )
            or str(row_dict.get("symbol", "")).upper() != symbol.upper()
            or str(row_dict.get("policy", "")) != policy
        ):
            raise RuntimeError("existing launch session does not match release approval")
        if policy == "LIVE_RESEARCH_PILOT":
            if normalized_pilot is None:
                raise RuntimeError("existing launch session does not match release approval")
            pilot_data = normalized_pilot
            for column, value in (
                ("pilot_campaign_id", pilot_data["campaign_id"]),
                ("pilot_git_sha", pilot_data["git_sha"]),
                ("pilot_source_hash", pilot_data["source_hash"]),
                ("pilot_dependency_hash", pilot_data["dependency_hash"]),
                ("pilot_migration_hash", pilot_data["migration_hash"]),
                ("pilot_strategy_hash", pilot_data["strategy_hash"]),
                ("pilot_secret_project_id", pilot_data["secret_project_id"]),
                ("pilot_api_key_version", pilot_data["api_key_version"]),
                ("pilot_api_secret_version", pilot_data["api_secret_version"]),
                ("pilot_management_mode", pilot_data["management_mode"]),
                ("pilot_risk_policy_hash", pilot_data["risk_policy_hash"]),
            ):
                if row_dict.get(column) != value:
                    raise RuntimeError("existing launch session does not match release approval")
        return row_dict

    async def reserve_mainnet_risk_order(
        self, launch_id: str, client_order_id: str, basket_id: str | None = None
    ) -> bool:
        """Atomically reserve a risk-increasing order slot.

        Staged sessions have one slot.  Autonomous sessions deliberately have
        no session-wide count limit; the deterministic risk governor and
        exchange-derived order caps remain the limits for each order.
        """

        normalized_client_order_id = str(client_order_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", normalized_client_order_id):
            return False
        normalized_basket_id = str(basket_id or "").strip() or None
        if normalized_basket_id is not None and not _BASKET_ID_RE.fullmatch(normalized_basket_id):
            return False
        row = await self.db.fetchrow(
            """
            UPDATE mainnet_launch_sessions
            SET reserved_orders = reserved_orders + 1,
                pending_order_client_order_id = $2,
                basket_id = COALESCE(basket_id, $3::varchar),
                updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1
              AND pending_order_client_order_id IS NULL
              AND (
                (runtime_target = 'LOCAL' AND $3::varchar IS NOT NULL
                 AND (basket_id IS NULL OR basket_id = $3::varchar))
                OR (runtime_target = 'CLOUD_RUN' AND $3::varchar IS NULL)
              )
              AND (
                (policy = 'STAGED_FIRST_ORDER'
                 AND state = 'ACTIVE'
                 AND submitted_orders = 0
                 AND reserved_orders < max_risk_increasing_orders)
                OR
                (policy = 'AUTONOMOUS_AFTER_REVIEW'
                 AND state = 'AUTONOMOUS_ACTIVE')
                OR
                (policy = 'LIVE_RESEARCH_PILOT'
                 AND state = 'ACTIVE'
                 AND pilot_status = 'ACTIVE'
                 AND pilot_campaign_expires_at > CURRENT_TIMESTAMP
                 AND pilot_drawdown_triggered = FALSE
                 AND reserved_orders < max_risk_increasing_orders)
              )
            RETURNING launch_id
            """,
            launch_id,
            normalized_client_order_id,
            normalized_basket_id,
        )
        return row is not None

    async def bind_mainnet_launch_basket(
        self, launch_id: str, basket_id: str
    ) -> bool:
        """Bind the single basket identity before an owner/order can reference it."""

        normalized_launch = str(launch_id or "").strip()
        normalized_basket = str(basket_id or "").strip()
        if (
            not _HISTORY_RUN_ID_RE.fullmatch(normalized_launch)
            or not _BASKET_ID_RE.fullmatch(normalized_basket)
            or len(normalized_basket) > 64
        ):
            return False
        row = await self.db.fetchrow(
            """
            UPDATE mainnet_launch_sessions
            SET basket_id = COALESCE(basket_id, $2), updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1 AND runtime_target = 'LOCAL' AND symbol = 'ETHUSDC'
              AND state IN ('ACTIVE', 'PAUSED_NEW_RISK', 'RECONCILIATION_REQUIRED', 'REAUTH_REQUIRED')
              AND (basket_id IS NULL OR basket_id = $2)
            RETURNING basket_id
            """,
            normalized_launch,
            normalized_basket,
        )
        return row is not None and str(row["basket_id"]) == normalized_basket

    async def release_mainnet_risk_order_reservation(self, launch_id: str, client_order_id: str) -> bool:
        """Release a slot only after a definitive exchange rejection."""

        normalized_client_order_id = str(client_order_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", normalized_client_order_id):
            return False
        result = await self.db.execute(
            """
            UPDATE mainnet_launch_sessions
            SET reserved_orders = reserved_orders - 1,
                pending_order_client_order_id = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1
              AND state IN ('ACTIVE', 'AUTONOMOUS_ACTIVE', 'RECONCILIATION_REQUIRED')
              AND reserved_orders > submitted_orders
              AND pending_order_client_order_id = $2
              AND (policy <> 'LIVE_RESEARCH_PILOT' OR pilot_status IN ('ACTIVE', 'CLOSE_ONLY', 'EXPIRED', 'REVOKED'))
            """,
            launch_id,
            normalized_client_order_id,
        )
        return str(result).upper().startswith("UPDATE 1")

    async def mark_mainnet_risk_order_submitted(self, launch_id: str, client_order_id: str) -> bool:
        """Persist an order outcome atomically with the launch lifecycle."""

        normalized_client_order_id = str(client_order_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", normalized_client_order_id):
            return False
        row = await self.db.fetchrow(
            """
            UPDATE mainnet_launch_sessions
            SET submitted_orders = submitted_orders + 1,
                first_order_client_order_id = CASE
                    WHEN policy IN ('STAGED_FIRST_ORDER', 'LIVE_RESEARCH_PILOT') AND submitted_orders = 0
                    THEN pending_order_client_order_id
                    ELSE first_order_client_order_id
                END,
                pending_order_client_order_id = NULL,
                state = CASE
                    WHEN state = 'RECONCILIATION_REQUIRED' THEN 'RECONCILIATION_REQUIRED'
                    WHEN policy = 'STAGED_FIRST_ORDER' THEN 'PAUSED_NEW_RISK'
                    -- The entry fill's FILL/FEE accounting pauses new risk before
                    -- this mark; keep that pause (it waits for a fresh MARK).
                    WHEN policy = 'LIVE_RESEARCH_PILOT' THEN state
                    ELSE 'AUTONOMOUS_ACTIVE'
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1
              AND (
                state IN ('ACTIVE', 'AUTONOMOUS_ACTIVE', 'RECONCILIATION_REQUIRED')
                OR (policy = 'LIVE_RESEARCH_PILOT' AND state = 'PAUSED_NEW_RISK')
              )
              AND reserved_orders > submitted_orders
              AND pending_order_client_order_id = $2
              AND (
                max_risk_increasing_orders IS NULL
                OR submitted_orders < max_risk_increasing_orders
              )
              AND (policy <> 'LIVE_RESEARCH_PILOT' OR (
                    pilot_status = 'ACTIVE'
                    AND pilot_campaign_expires_at > CURRENT_TIMESTAMP
                    AND pilot_drawdown_triggered = FALSE
                  ))
            RETURNING launch_id, submitted_orders, state
            """,
            launch_id,
            normalized_client_order_id,
        )
        return row is not None

    async def mark_mainnet_launch_reconciliation_required(self, launch_id: str) -> bool:
        result = await self.db.execute(
            """
            UPDATE mainnet_launch_sessions
            SET state = 'RECONCILIATION_REQUIRED', updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1
              AND state IN ('ACTIVE', 'PAUSED_NEW_RISK', 'AUTONOMOUS_ACTIVE')
            """,
            launch_id,
        )
        return str(result).upper().startswith("UPDATE 1")

    async def mark_mainnet_launch_reconciled(self, launch_id: str) -> bool:
        """Clear an exchange-ambiguity fence without resuming risk locally.

        A later authoritative reconciliation may clear the fence, but it must
        never resume autonomous risk by itself: staged sessions return to the
        paused review state, while autonomous sessions require a fresh
        continuation approval.
        """
        result = await self.db.execute(
            """
            UPDATE mainnet_launch_sessions
            SET state = CASE
                    WHEN policy = 'STAGED_FIRST_ORDER' THEN 'PAUSED_NEW_RISK'
                    ELSE 'REAUTH_REQUIRED'
                END,
                updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1
              AND state = 'RECONCILIATION_REQUIRED'
              AND submitted_orders >= 1
              AND reserved_orders >= submitted_orders
              AND pending_order_client_order_id IS NULL
            """,
            launch_id,
        )
        return str(result).upper().startswith("UPDATE 1")

    async def resume_mainnet_pilot_campaign(
        self, launch_id: str
    ) -> Optional[Mapping[str, Any]]:
        """Resume an active pilot campaign after restart if exchange state is reconciled."""
        row = await self.db.fetchrow(
            """
            UPDATE mainnet_launch_sessions
            SET state = CASE WHEN
                    COALESCE((
                        SELECT MAX(event_id) FROM local_live_pilot_events
                        WHERE campaign_id = mainnet_launch_sessions.pilot_campaign_id
                          AND event_type IN ('FILL', 'FEE', 'FUNDING')
                    ), 0) > COALESCE((
                        SELECT MAX(event_id) FROM local_live_pilot_events
                        WHERE campaign_id = mainnet_launch_sessions.pilot_campaign_id
                          AND event_type = 'MARK'
                    ), 0)
                    THEN 'PAUSED_NEW_RISK' ELSE 'ACTIVE' END,
                updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1
              AND runtime_target = 'LOCAL'
              AND policy = 'LIVE_RESEARCH_PILOT'
              AND state IN ('REAUTH_REQUIRED', 'RECONCILIATION_REQUIRED')
              AND pilot_status = 'ACTIVE'
              AND pilot_drawdown_triggered IS FALSE
              AND pilot_campaign_expires_at > CURRENT_TIMESTAMP
              AND pending_order_client_order_id IS NULL
            RETURNING launch_id, approval_id, image_digest, symbol, policy, basket_id,
                      max_risk_increasing_orders, reserved_orders, submitted_orders,
                      state, continuation_approval_id, first_order_verified_at,
                      pending_order_client_order_id, first_order_client_order_id,
                      autonomous_approved_at, last_restart_at, created_at, updated_at,
                      runtime_target, runtime_fingerprint, pilot_campaign_id,
                      pilot_git_sha, pilot_source_hash, pilot_dependency_hash,
                      pilot_migration_hash, pilot_strategy_hash, pilot_secret_project_id,
                      pilot_api_key_version, pilot_api_secret_version, pilot_management_mode,
                      pilot_campaign_expires_at, pilot_risk_policy_hash,
                      pilot_max_position_notional_usdc, pilot_per_position_risk_usdc,
                      pilot_max_drawdown_usdc, pilot_quick_target_net_usdc,
                      pilot_quick_max_hold_seconds, pilot_max_leverage, pilot_status,
                      pilot_net_pnl_usdc, pilot_peak_pnl_usdc, pilot_drawdown_triggered,
                      pilot_last_account_snapshot_at
            """,
            launch_id,
        )
        return dict(row) if row is not None else None

    async def trigger_pilot_drawdown(
        self, launch_id: str, reason: str = "PILOT_DRAWDOWN_LIMIT_REACHED"
    ) -> Optional[Mapping[str, Any]]:
        """Trigger the drawdown-specific CLOSE_ONLY circuit breaker."""
        return await self.enter_local_live_pilot_close_only(
            launch_id, reason=reason, drawdown=True
        )

    async def enter_local_live_pilot_close_only(
        self,
        launch_id: str,
        *,
        reason: str,
        drawdown: bool = False,
    ) -> Optional[Mapping[str, Any]]:
        """Durably fence new risk and audit the cause before any exit mutation."""
        normalized_reason = str(reason or "").strip().upper()
        allowed_reasons = {
            "PILOT_DRAWDOWN_LIMIT_REACHED",
            "QUICK_MAX_HOLD_EXPIRED",
            "PILOT_CAMPAIGN_EXPIRED",
            "PILOT_APPROVAL_REVOKED",
            "PILOT_ACCOUNTING_UNKNOWN",
            "PILOT_RECONCILIATION_UNKNOWN",
        }
        if normalized_reason not in allowed_reasons:
            raise ValueError("Local Pilot close-only reason is unsupported")
        event_key = f"STATE:CLOSE_ONLY:{normalized_reason}"
        payload = {"run_id": launch_id, "reason": normalized_reason}
        payload_json = json.dumps(payload, separators=(",", ":"))
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = datetime.now(UTC)
        async with self.db.transaction() as transaction_connection:
            connection = transaction_connection or self.db
            current = await connection.fetchrow(
                """SELECT launch_id, policy, runtime_target, pilot_status, state
                   FROM mainnet_launch_sessions WHERE launch_id = $1 FOR UPDATE""",
                launch_id,
            )
            if (
                current is None
                or current["policy"] != "LIVE_RESEARCH_PILOT"
                or current["runtime_target"] != "LOCAL"
            ):
                return None
            await connection.execute(
                """UPDATE mainnet_launch_sessions
                   SET pilot_status = CASE WHEN pilot_status = 'ACTIVE' THEN 'CLOSE_ONLY' ELSE pilot_status END,
                       pilot_drawdown_triggered = pilot_drawdown_triggered OR $2,
                       state = CASE WHEN state IN ('REAUTH_REQUIRED', 'RECONCILIATION_REQUIRED')
                                    THEN state ELSE 'PAUSED_NEW_RISK' END,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE launch_id = $1""",
                launch_id,
                bool(drawdown),
            )
            prior = await connection.fetchrow(
                """SELECT payload_sha256, payload FROM local_live_pilot_events
                   WHERE campaign_id = (SELECT pilot_campaign_id FROM mainnet_launch_sessions WHERE launch_id = $1)
                     AND event_key = $2""",
                launch_id,
                event_key,
            )
            if prior is None:
                await connection.execute(
                    """INSERT INTO local_live_pilot_events
                       (campaign_id, launch_id, event_key, event_type, source, observed_at,
                        net_pnl_delta_usdc, payload_sha256, payload)
                       SELECT pilot_campaign_id, launch_id, $2, 'STATE', 'SUPERVISOR', $3,
                              NULL, $4, $5::jsonb
                       FROM mainnet_launch_sessions WHERE launch_id = $1
                       ON CONFLICT (campaign_id, event_key) DO NOTHING""",
                    launch_id,
                    event_key,
                    now,
                    payload_hash,
                    payload_json,
                )
            else:
                prior_payload = prior["payload"]
                if isinstance(prior_payload, str):
                    prior_payload = json.loads(prior_payload)
                if prior["payload_sha256"] != payload_hash or prior_payload != payload:
                    raise RuntimeError("Local Pilot close-only audit key conflicts with prior evidence")
        # Do not return the short UPDATE shape: the execution monitor must keep
        # the full approval/policy binding after refreshing its cache.
        return await self.get_mainnet_launch(launch_id)

    async def activate_mainnet_autonomous(
        self,
        *,
        launch_id: str,
        continuation_approval_id: str,
        first_order_verified_at: Optional[datetime] = None,
        image_digest: Optional[str] = None,
        runtime_target: str = _CLOUD_RUNTIME_TARGET,
        runtime_fingerprint: Optional[str] = None,
    ) -> Optional[Mapping[str, Any]]:
        """Atomically convert a verified staged launch into autonomous mode.

        The WHERE clause is the durable authorization boundary: exactly one
        submitted staged order, paused state, matching image, and a unique
        continuation approval are all required.  A retry after success returns
        no row and therefore cannot silently re-authorize a different session.
        """

        if not launch_id or not continuation_approval_id:
            raise ValueError("autonomous continuation identity is incomplete")
        normalized_target, image_digest, runtime_fingerprint = _validate_launch_identity(
            runtime_target=runtime_target,
            image_digest=image_digest,
            runtime_fingerprint=runtime_fingerprint,
        )
        verified_at = _utc_datetime(first_order_verified_at)
        row = await self.db.fetchrow(
            """
            UPDATE mainnet_launch_sessions
            SET policy = 'AUTONOMOUS_AFTER_REVIEW',
                max_risk_increasing_orders = NULL,
                continuation_approval_id = $2,
                first_order_verified_at = $3,
                autonomous_approved_at = CURRENT_TIMESTAMP,
                state = 'AUTONOMOUS_ACTIVE',
                updated_at = CURRENT_TIMESTAMP
            WHERE launch_id = $1
              AND runtime_target = $5
              AND (
                    (
                        runtime_target = 'LOCAL'
                        AND image_digest IS NULL
                        AND runtime_fingerprint = $6
                    )
                    OR
                    (
                        runtime_target = 'CLOUD_RUN'
                        AND image_digest = $4
                    )
              )
              AND symbol = 'ETHUSDC'
              AND pending_order_client_order_id IS NULL
              AND (
                    runtime_target <> 'LOCAL'
                    OR (
                        first_order_client_order_id ~ '^[A-Za-z0-9_-]{1,64}$'
                    )
              )
              AND (
                (
                    policy = 'STAGED_FIRST_ORDER'
                    AND state = 'PAUSED_NEW_RISK'
                    AND submitted_orders = 1
                    AND reserved_orders >= submitted_orders
                    AND continuation_approval_id IS NULL
                )
                OR
                (
                    policy = 'AUTONOMOUS_AFTER_REVIEW'
                    AND state = 'REAUTH_REQUIRED'
                    AND submitted_orders >= 1
                )
              )
            RETURNING launch_id, approval_id, image_digest, symbol, policy, basket_id,
                      max_risk_increasing_orders, reserved_orders,
                      submitted_orders, state, continuation_approval_id,
                      first_order_verified_at, pending_order_client_order_id,
                      first_order_client_order_id, autonomous_approved_at,
                      last_restart_at, created_at, updated_at,
                      runtime_target, runtime_fingerprint
            """,
            launch_id,
            continuation_approval_id,
            verified_at,
            image_digest,
            normalized_target,
            runtime_fingerprint,
        )
        return dict(row) if row is not None else None

    async def mark_mainnet_launches_reauth_required(
        self, symbol: str = "ETHUSDC"
    ) -> int:
        """Fence autonomous and unresolved staged sessions on process restart."""

        closed = await self.db.execute(
            """
            UPDATE mainnet_launch_sessions
            SET state = 'CLOSED', updated_at = CURRENT_TIMESTAMP
            WHERE symbol = $1
              AND runtime_target = 'LOCAL'
              AND policy = 'STAGED_FIRST_ORDER'
              AND state = 'ACTIVE'
              AND reserved_orders = 0
              AND submitted_orders = 0
              AND pending_order_client_order_id IS NULL
            """,
            symbol.upper(),
        )
        result = await self.db.execute(
            """
            UPDATE mainnet_launch_sessions
            SET state = CASE
                    WHEN pending_order_client_order_id IS NOT NULL
                    THEN 'RECONCILIATION_REQUIRED'
                    ELSE 'REAUTH_REQUIRED'
                END,
                last_restart_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE symbol = $1
              AND (
                (policy = 'AUTONOMOUS_AFTER_REVIEW' AND state = 'AUTONOMOUS_ACTIVE')
                OR (
                    runtime_target = 'LOCAL'
                    AND policy = 'LIVE_RESEARCH_PILOT'
                    AND state IN ('ACTIVE', 'PAUSED_NEW_RISK')
                )
                OR (
                    runtime_target = 'LOCAL'
                    AND policy = 'STAGED_FIRST_ORDER'
                    AND state = 'ACTIVE'
                    AND pending_order_client_order_id IS NOT NULL
                )
              )
            """,
            symbol.upper(),
        )
        closed_match = re.search(r"UPDATE\s+(\d+)", str(closed).upper())
        reauth_match = re.search(r"UPDATE\s+(\d+)", str(result).upper())
        return (int(closed_match.group(1)) if closed_match else 0) + (
            int(reauth_match.group(1)) if reauth_match else 0
        )

    async def get_mainnet_launch(self, launch_id: str) -> Optional[Mapping[str, Any]]:
        row = await self.db.fetchrow(
            """
            SELECT launch_id, approval_id, image_digest, symbol, policy, basket_id,
                   max_risk_increasing_orders, reserved_orders, submitted_orders,
                   state, continuation_approval_id, first_order_verified_at,
                   pending_order_client_order_id, first_order_client_order_id,
                   autonomous_approved_at, last_restart_at, created_at, updated_at,
                   runtime_target, runtime_fingerprint, pilot_campaign_id,
                   pilot_git_sha, pilot_source_hash, pilot_dependency_hash,
                   pilot_migration_hash, pilot_strategy_hash, pilot_secret_project_id,
                   pilot_api_key_version, pilot_api_secret_version, pilot_management_mode,
                   pilot_campaign_expires_at, pilot_risk_policy_hash,
                   pilot_max_position_notional_usdc, pilot_per_position_risk_usdc,
                   pilot_max_drawdown_usdc, pilot_quick_target_net_usdc,
                   pilot_quick_max_hold_seconds, pilot_max_leverage, pilot_status,
                   pilot_net_pnl_usdc, pilot_peak_pnl_usdc, pilot_drawdown_triggered,
                   pilot_last_account_snapshot_at,
                   (SELECT e.payload->>'armed_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_ARMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_armed_at,
                   (SELECT e.payload->>'entry_cutoff_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_ARMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_entry_cutoff_at,
                   (SELECT e.payload->>'close_after_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_ARMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_after_at,
                   (SELECT e.payload->>'end_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_ARMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_end_at,
                   (SELECT COUNT(*) FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'FILL' AND e.payload->>'pilot_entry' = 'true'
                   ) AS pilot_session_entry_count,
                   (SELECT e.payload->>'close_client_order_id' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_client_order_id,
                   (SELECT e.payload->>'close_side' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_side,
                   (SELECT e.payload->>'close_position_side' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_position_side,
                   (SELECT e.payload->>'close_quantity' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_quantity,
                   (SELECT e.payload->>'claimed_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_claimed_at,
                   (SELECT COUNT(*) FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_SUBMITTING'
                   ) AS pilot_session_close_attempt_count,
                   COALESCE(
                       state IN ('PAUSED_NEW_RISK', 'REAUTH_REQUIRED', 'RECONCILIATION_REQUIRED')
                       AND pilot_status = 'ACTIVE'
                       AND pilot_drawdown_triggered IS FALSE
                       AND (
                           SELECT MAX(event_id) FROM local_live_pilot_events
                           WHERE campaign_id = mainnet_launch_sessions.pilot_campaign_id
                             AND event_type IN ('FILL', 'FEE', 'FUNDING')
                       ) > COALESCE((
                           SELECT MAX(event_id) FROM local_live_pilot_events
                           WHERE campaign_id = mainnet_launch_sessions.pilot_campaign_id
                             AND event_type = 'MARK'
                       ), 0)
                       AND (
                           SELECT payload->>'resume_eligible' FROM local_live_pilot_events
                           WHERE campaign_id = mainnet_launch_sessions.pilot_campaign_id
                             AND event_type = 'STATE'
                             AND payload->>'kind' = 'ACCOUNTING_SNAPSHOT_PENDING'
                           ORDER BY event_id DESC LIMIT 1
                       ) = 'true',
                       FALSE
                   ) AS pilot_accounting_resume_eligible
            FROM mainnet_launch_sessions
            WHERE launch_id = $1
            """,
            launch_id,
        )
        return dict(row) if row is not None else None

    async def get_local_live_pilot_accounting(
        self, launch_id: str
    ) -> Optional[Mapping[str, Any]]:
        """Read campaign PnL components from its durable event ledger.

        The aggregate is only VERIFIED when an authoritative position mark is
        newer than every financial event. Callers must additionally establish
        live exchange reconciliation before presenting it as current.
        """
        row = await self.db.fetchrow(
            """
            SELECT s.launch_id, s.pilot_campaign_id, s.symbol,
                   s.pilot_net_pnl_usdc, s.pilot_peak_pnl_usdc,
                   s.pilot_drawdown_triggered, s.pilot_status,
                   s.pilot_last_account_snapshot_at,
                   (SELECT e.payload->>'armed_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_ARMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_armed_at,
                   (SELECT e.payload->>'entry_cutoff_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_ARMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_entry_cutoff_at,
                   (SELECT e.payload->>'close_after_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_ARMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_after_at,
                   (SELECT e.payload->>'end_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_ARMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_end_at,
                   (SELECT COUNT(*) FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type = 'FILL' AND e.payload->>'pilot_entry' = 'true'
                   ) AS pilot_session_entry_count,
                   (SELECT e.payload->>'close_client_order_id' FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_client_order_id,
                   (SELECT e.payload->>'close_side' FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_side,
                   (SELECT e.payload->>'close_position_side' FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_position_side,
                   (SELECT e.payload->>'close_quantity' FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_quantity,
                   (SELECT e.payload->>'claimed_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_claimed_at,
                   (SELECT COUNT(*) FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_SUBMITTING'
                   ) AS pilot_session_close_attempt_count,
                   COALESCE((SELECT SUM(e.net_pnl_delta_usdc)
                             FROM local_live_pilot_events e
                             WHERE e.campaign_id = s.pilot_campaign_id
                               AND e.event_type = 'FILL'), 0) AS realized_pnl_usdc,
                   COALESCE((SELECT -SUM(e.net_pnl_delta_usdc)
                             FROM local_live_pilot_events e
                             WHERE e.campaign_id = s.pilot_campaign_id
                               AND e.event_type = 'FEE'), 0) AS fees_usdc,
                   COALESCE((SELECT SUM(e.net_pnl_delta_usdc)
                             FROM local_live_pilot_events e
                             WHERE e.campaign_id = s.pilot_campaign_id
                               AND e.event_type = 'FUNDING'), 0) AS funding_usdc,
                   (SELECT e.payload->>'unrealized_pnl_usdc'
                    FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type = 'MARK'
                    ORDER BY e.event_id DESC LIMIT 1) AS unrealized_pnl_usdc,
                   (SELECT MAX(e.event_id)
                    FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type IN ('FILL', 'FEE', 'FUNDING')) AS last_financial_event_id,
                   (SELECT MAX(e.event_id)
                    FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type = 'MARK') AS last_mark_event_id,
                   (SELECT MAX(e.observed_at)
                    FROM local_live_pilot_events e
                    WHERE e.campaign_id = s.pilot_campaign_id
                      AND e.event_type IN ('FILL', 'FEE', 'FUNDING', 'MARK')) AS last_event_at
            FROM mainnet_launch_sessions s
            WHERE s.launch_id = $1
              AND s.runtime_target = 'LOCAL'
              AND s.policy = 'LIVE_RESEARCH_PILOT'
              AND s.symbol = 'ETHUSDC'
            """,
            launch_id,
        )
        return dict(row) if row is not None else None

    async def get_active_mainnet_launch(self, symbol: str = "ETHUSDC") -> Optional[Mapping[str, Any]]:
        row = await self.db.fetchrow(
            """
            SELECT launch_id, approval_id, image_digest, symbol, policy, basket_id,
                   max_risk_increasing_orders, reserved_orders, submitted_orders,
                   state, continuation_approval_id, first_order_verified_at,
                   pending_order_client_order_id, first_order_client_order_id,
                   autonomous_approved_at, last_restart_at, created_at, updated_at,
                   runtime_target, runtime_fingerprint, pilot_campaign_id,
                   pilot_git_sha, pilot_source_hash, pilot_dependency_hash,
                   pilot_migration_hash, pilot_strategy_hash, pilot_secret_project_id,
                   pilot_api_key_version, pilot_api_secret_version, pilot_management_mode,
                   pilot_campaign_expires_at, pilot_risk_policy_hash,
                   pilot_max_position_notional_usdc, pilot_per_position_risk_usdc,
                   pilot_max_drawdown_usdc, pilot_quick_target_net_usdc,
                   pilot_quick_max_hold_seconds, pilot_max_leverage, pilot_status,
                   pilot_net_pnl_usdc, pilot_peak_pnl_usdc, pilot_drawdown_triggered,
                   pilot_last_account_snapshot_at,
                   (SELECT e.payload->>'armed_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_ARMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_armed_at,
                   (SELECT e.payload->>'entry_cutoff_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_ARMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_entry_cutoff_at,
                   (SELECT e.payload->>'close_after_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_ARMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_after_at,
                   (SELECT e.payload->>'end_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_ARMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_end_at,
                   (SELECT COUNT(*) FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'FILL' AND e.payload->>'pilot_entry' = 'true'
                   ) AS pilot_session_entry_count,
                   (SELECT e.payload->>'close_client_order_id' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_client_order_id,
                   (SELECT e.payload->>'close_side' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_side,
                   (SELECT e.payload->>'close_position_side' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_position_side,
                   (SELECT e.payload->>'close_quantity' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_quantity,
                   (SELECT e.payload->>'claimed_at' FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_CLAIMED'
                    ORDER BY e.event_id LIMIT 1) AS pilot_session_close_claimed_at,
                   (SELECT COUNT(*) FROM local_live_pilot_events e
                    WHERE e.campaign_id = mainnet_launch_sessions.pilot_campaign_id
                      AND e.event_type = 'STATE' AND e.payload->>'kind' = 'SESSION_CLOSE_SUBMITTING'
                   ) AS pilot_session_close_attempt_count,
                   COALESCE(
                       state IN ('PAUSED_NEW_RISK', 'REAUTH_REQUIRED', 'RECONCILIATION_REQUIRED')
                       AND pilot_status = 'ACTIVE'
                       AND pilot_drawdown_triggered IS FALSE
                       AND (
                           SELECT MAX(event_id) FROM local_live_pilot_events
                           WHERE campaign_id = mainnet_launch_sessions.pilot_campaign_id
                             AND event_type IN ('FILL', 'FEE', 'FUNDING')
                       ) > COALESCE((
                           SELECT MAX(event_id) FROM local_live_pilot_events
                           WHERE campaign_id = mainnet_launch_sessions.pilot_campaign_id
                             AND event_type = 'MARK'
                       ), 0)
                       AND (
                           SELECT payload->>'resume_eligible' FROM local_live_pilot_events
                           WHERE campaign_id = mainnet_launch_sessions.pilot_campaign_id
                             AND event_type = 'STATE'
                             AND payload->>'kind' = 'ACCOUNTING_SNAPSHOT_PENDING'
                           ORDER BY event_id DESC LIMIT 1
                       ) = 'true',
                       FALSE
                   ) AS pilot_accounting_resume_eligible
            FROM mainnet_launch_sessions
            WHERE symbol = $1
              AND state IN (
                'ACTIVE', 'PAUSED_NEW_RISK', 'RECONCILIATION_REQUIRED',
                'AUTONOMOUS_ACTIVE', 'REAUTH_REQUIRED'
              )
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            symbol.upper(),
        )
        return dict(row) if row is not None else None

    async def append_outbox(self, event: PersistenceEvent) -> bool:
        payload = json.dumps(dict(event.payload), default=_json_default, separators=(",", ":"))
        await self.db.execute(
            """
            INSERT INTO persistence_outbox (
                event_id, event_type, idempotency_key, aggregate_type, aggregate_id,
                payload, status, attempt_count, next_attempt_at, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'PENDING', 0, $7, $8)
            ON CONFLICT DO NOTHING
            """,
            event.event_id,
            event.event_type,
            event.idempotency_key,
            event.aggregate_type,
            event.aggregate_id,
            payload,
            _utc_datetime(event.created_at),
            _utc_datetime(event.created_at),
        )
        # asyncpg returns a command tag, while test doubles and alternate
        # drivers may return nothing. No exception means the idempotent insert
        # was accepted or already present, so both are durable outcomes.
        return True

    async def pending_count(self) -> int:
        row = await self.db.fetchrow(
            """
            SELECT COUNT(*) AS count
            FROM persistence_outbox
            WHERE status IN ('PENDING', 'PROCESSING')
            """
        )
        return int(row["count"]) if row is not None else 0

    async def dispatch_one(self) -> bool:
        """Claim and apply one event; failed transactions remain replayable."""

        row: Any = None
        try:
            async with self.db.transaction() as connection:
                await connection.execute(
                    """
                    UPDATE persistence_outbox
                    SET status = 'PENDING', claimed_at = NULL
                    WHERE status = 'PROCESSING'
                      AND (
                          claimed_at IS NULL
                          OR claimed_at <= CURRENT_TIMESTAMP - INTERVAL '5 minutes'
                      )
                    """
                )
                row = await connection.fetchrow(
                    """
                    UPDATE persistence_outbox
                    SET status = 'PROCESSING',
                        claimed_at = CURRENT_TIMESTAMP,
                        attempt_count = attempt_count + 1
                    WHERE event_id = (
                        SELECT event_id
                        FROM persistence_outbox
                        WHERE status = 'PENDING' AND next_attempt_at <= CURRENT_TIMESTAMP
                        ORDER BY created_at, event_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    RETURNING event_id, event_type, payload
                    """
                )
                if row is None:
                    return False
                await self._apply_event(connection, row)
                await connection.execute(
                    """
                    UPDATE persistence_outbox
                    SET status = 'PROCESSED',
                        claimed_at = NULL,
                        processed_at = CURRENT_TIMESTAMP,
                        last_error = NULL
                    WHERE event_id = $1
                    """,
                    row["event_id"],
                )
            return True
        except Exception as exc:
            if row is not None:
                await self._mark_retry(row["event_id"], exc)
            raise

    async def _mark_retry(self, event_id: str, error: Exception) -> None:
        # PostgreSQL keeps the outbox row even after a failed domain transaction.
        # The next attempt is bounded, so the dispatcher cannot hot-loop.
        await self.db.execute(
            """
            UPDATE persistence_outbox
            SET status = 'PENDING',
                claimed_at = NULL,
                attempt_count = attempt_count + 1,
                next_attempt_at = CURRENT_TIMESTAMP + INTERVAL '1 second'
                    * LEAST(300, GREATEST(1, POWER(2, attempt_count))),
                last_error = $2
            WHERE event_id = $1
              AND status = 'PENDING'
            """,
            event_id,
            redact_error(f"{type(error).__name__}: {str(error)}"),
        )

    async def _apply_event(self, connection: Any, row: Mapping[str, Any]) -> None:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        event_type = str(row["event_type"])
        if event_type == "ORDER":
            entity = ExecutionOrder.model_validate(payload["entity"])
            await self.orders.save_order(
                entity, self._instrument(payload, entity.symbol), connection
            )
        elif event_type == "FILL":
            entity = ExchangeFill.model_validate(payload["entity"])
            await self.fills.save_fill(
                entity, self._instrument(payload, entity.symbol), connection
            )
        elif event_type == "POSITION":
            entity = ExchangePosition.model_validate(payload["entity"])
            await self.positions.save_position(
                entity, self._instrument(payload, entity.symbol), connection
            )
        elif event_type == "RISK_SNAPSHOT":
            entity = RiskSnapshot.model_validate(payload["entity"])
            await self.risk.save_snapshot(entity, connection)
        else:
            raise ValueError(f"unsupported persistence event type: {event_type}")

    def _instrument(self, payload: Mapping[str, Any], symbol: str) -> Instrument:
        raw = payload.get("instrument")
        if raw is not None:
            return Instrument.model_validate(raw)
        if self.instrument_rules_provider is not None:
            instrument = self.instrument_rules_provider(symbol)
            if instrument is not None:
                return instrument
        raise InstrumentRulesUnavailable(
            f"exchange-derived instrument rules unavailable for {symbol}"
        )
