"""Pure, fail-closed USD-M conditional protection verification.

Inputs are already acquired GET /fapi/v1/algoOrder and
GET /fapi/v1/openAlgoOrders responses. No exchange client is used here.
See https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProtectionIntent:
    symbol: str
    entry_side: str
    position_side: str
    entry_qty: Decimal
    stop_trigger: Decimal
    take_profit_trigger: Decimal
    stop_algo_id: int
    stop_client_algo_id: str
    take_profit_algo_id: int
    take_profit_client_algo_id: str


@dataclass(frozen=True)
class ProtectionResult:
    protected: bool
    state: str  # PROTECTED, UNPROTECTED, AMBIGUOUS
    reasons: tuple[str, ...]
    evidence: Mapping[str, object] | None = None


def _decimal(value: object) -> Decimal | None:
    try:
        if isinstance(value, bool) or value is None or str(value).strip() == "":
            return None
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _flag(value: object) -> bool | None:
    if value is True or value == "true":
        return True
    if value is False or value == "false":
        return False
    return None


def _identity(order: Mapping[str, object]) -> tuple[int, str] | None:
    ident, client = order.get("algoId"), order.get("clientAlgoId")
    if isinstance(ident, bool) or not isinstance(ident, int) or ident <= 0:
        return None
    if not isinstance(client, str) or not client.strip():
        return None
    return ident, client


def verify_protection(
    intent: ProtectionIntent,
    *,
    position_qty: object,
    filled_qty: object,
    stop_query: Mapping[str, object] | None,
    take_profit_query: Mapping[str, object] | None,
    open_algos: Sequence[Mapping[str, object]] | None,
    average_entry_price: object,
    entry_terminal: bool,
    now_ms: int,
    query_observed_ms: int,
    open_observed_ms: int,
    position_observed_ms: int,
    max_age_ms: int = 5000,
) -> ProtectionResult:
    """Verify two independently identified live protections for an observed position.

    Observation timestamps are caller-supplied receipt times, not Binance order
    updateTime: a long-lived NEW order can still be protective. Snapshots must
    be simultaneous within max_age_ms and captured after the orders were created.
    """
    ambiguous: list[str] = []
    unsafe: list[str] = []
    timestamps = (query_observed_ms, open_observed_ms, position_observed_ms)
    if (isinstance(now_ms, bool) or not isinstance(now_ms, int)
            or isinstance(max_age_ms, bool) or not isinstance(max_age_ms, int)
            or max_age_ms <= 0 or any(isinstance(t, bool) or not isinstance(t, int)
                                     or t <= 0 or t > now_ms or now_ms - t > max_age_ms
                                     for t in timestamps)):
        ambiguous.append("stale_or_invalid_observation")
    entry = _decimal(intent.entry_qty)
    position = _decimal(position_qty)
    filled = _decimal(filled_qty)
    average_entry = _decimal(average_entry_price)
    if entry is None or entry <= 0 or filled is None or filled < 0 or filled > entry:
        ambiguous.append("invalid_entry_or_fill_qty")
    if average_entry is None or average_entry <= 0:
        ambiguous.append("invalid_average_entry_price")
    if entry_terminal is not True:
        unsafe.append("entry_order_not_terminal")
    if position is None:
        ambiguous.append("invalid_position_qty")
    elif position == 0:
        unsafe.append("no_open_position")
    elif filled is not None and abs(position) != filled:
        ambiguous.append("position_fill_mismatch")
    if intent.entry_side not in ("BUY", "SELL") or intent.position_side not in ("BOTH", "LONG", "SHORT"):
        ambiguous.append("invalid_entry_direction")
    elif (intent.position_side == "LONG" and intent.entry_side != "BUY") or (
        intent.position_side == "SHORT" and intent.entry_side != "SELL"
    ):
        ambiguous.append("entry_position_side_mismatch")
    if position is not None and position != 0 and intent.entry_side in ("BUY", "SELL"):
        expected_positive = (
            intent.position_side == "LONG"
            or (intent.position_side == "BOTH" and intent.entry_side == "BUY")
        )
        if (position > 0) != expected_positive:
            unsafe.append("position_direction_mismatch")
    if not isinstance(intent.symbol, str) or not intent.symbol:
        ambiguous.append("invalid_symbol")
    stop_price = _decimal(intent.stop_trigger)
    take_price = _decimal(intent.take_profit_trigger)
    if stop_price is None or take_price is None or stop_price <= 0 or take_price <= 0 or stop_price == take_price:
        ambiguous.append("invalid_triggers")
    elif average_entry is not None:
        sensible = (
            stop_price < average_entry < take_price
            if intent.entry_side == "BUY"
            else take_price < average_entry < stop_price
        )
        if not sensible:
            unsafe.append("triggers_do_not_bracket_average_entry")
    if not isinstance(open_algos, (list, tuple)) or any(not isinstance(x, Mapping) for x in open_algos):
        ambiguous.append("missing_open_snapshot")
        open_algos = ()
    expected = (
        ("stop", "STOP_MARKET", intent.stop_algo_id, intent.stop_client_algo_id, stop_price, stop_query),
        ("take_profit", "TAKE_PROFIT_MARKET", intent.take_profit_algo_id,
         intent.take_profit_client_algo_id, take_price, take_profit_query),
    )
    if (intent.stop_algo_id == intent.take_profit_algo_id
            or intent.stop_client_algo_id == intent.take_profit_client_algo_id):
        ambiguous.append("protection_identities_not_distinct")
    expected_by_id = {
        intent.stop_algo_id: intent.stop_client_algo_id,
        intent.take_profit_algo_id: intent.take_profit_client_algo_id,
    }
    seen_open_ids: set[int] = set()
    for open_order in open_algos:
        identity = _identity(open_order)
        if identity is None:
            ambiguous.append("open_algo_identity_unverifiable")
            continue
        algo_id, client_id = identity
        if expected_by_id.get(algo_id) != client_id:
            ambiguous.append("unowned_open_algo_present")
        if algo_id in seen_open_ids:
            ambiguous.append("duplicate_open_algo_identity")
        seen_open_ids.add(algo_id)
    for label, order_type, algo_id, client_id, trigger, query in expected:
        if isinstance(algo_id, bool) or not isinstance(algo_id, int) or algo_id <= 0 or not isinstance(client_id, str) or not client_id:
            ambiguous.append(f"{label}_invalid_expected_identity")
            continue
        matches = [x for x in open_algos if x.get("algoId") == algo_id or x.get("clientAlgoId") == client_id]
        if len(matches) != 1:
            (unsafe if len(matches) == 0 else ambiguous).append(f"{label}_not_uniquely_open")
            continue
        if not isinstance(query, Mapping):
            ambiguous.append(f"{label}_missing_query")
            continue
        for source, order in (("query", query), ("open", matches[0])):
            if _identity(order) != (algo_id, client_id):
                ambiguous.append(f"{label}_{source}_identity_mismatch")
            if (order.get("algoType") != "CONDITIONAL" or order.get("orderType") != order_type
                    or order.get("symbol") != intent.symbol or order.get("positionSide") != intent.position_side
                    or order.get("side") != ("SELL" if intent.entry_side == "BUY" else "BUY")):
                unsafe.append(f"{label}_{source}_contract_mismatch")
            if order.get("workingType") != "MARK_PRICE":
                unsafe.append(f"{label}_{source}_working_type_mismatch")
            if order.get("algoStatus") != "NEW":
                unsafe.append(f"{label}_{source}_not_new")
            if trigger is None or _decimal(order.get("triggerPrice")) != trigger:
                unsafe.append(f"{label}_{source}_trigger_mismatch")
            close = _flag(order.get("closePosition"))
            reduce = _flag(order.get("reduceOnly"))
            qty = _decimal(order.get("quantity"))
            if close is True:
                if reduce is True or (qty is not None and qty != 0):
                    unsafe.append(f"{label}_{source}_invalid_close_all")
            elif (
                close is False
                and intent.position_side == "BOTH"
                and reduce is True
                and qty == abs(position)
            ):
                pass
            else:
                unsafe.append(f"{label}_{source}_not_position_closing")
            created = order.get("createTime")
            if (isinstance(created, bool) or not isinstance(created, int) or created <= 0
                    or created > min(timestamps)):
                ambiguous.append(f"{label}_{source}_invalid_create_time")
    if ambiguous:
        return ProtectionResult(False, "AMBIGUOUS", tuple(ambiguous + unsafe))
    if unsafe:
        return ProtectionResult(False, "UNPROTECTED", tuple(unsafe))
    # Return only fields that have just been checked against both the
    # individual algo query and the signed open-algo snapshot.  The caller
    # uses these normalized values to bind the result to its durable owner.
    stop_close = _flag(stop_query.get("closePosition"))
    target_close = _flag(take_profit_query.get("closePosition"))
    stop_reduce = _flag(stop_query.get("reduceOnly"))
    target_reduce = _flag(take_profit_query.get("reduceOnly"))
    stop_quantity = _decimal(stop_query.get("quantity"))
    target_quantity = _decimal(take_profit_query.get("quantity"))
    if any(
        value is None
        for value in (
            stop_close,
            target_close,
            stop_reduce,
            target_reduce,
            stop_quantity,
            target_quantity,
        )
    ):
        return ProtectionResult(False, "AMBIGUOUS", ("protection_evidence_incomplete",))
    evidence: dict[str, object] = {
        "stop_order_id": intent.stop_algo_id,
        "stop_client_order_id": intent.stop_client_algo_id,
        "stop_order_type": str(stop_query["orderType"]),
        "stop_status": str(stop_query["algoStatus"]),
        "stop_side": str(stop_query["side"]),
        "stop_working_type": str(stop_query["workingType"]),
        "stop_quantity": stop_quantity,
        "stop_loss_price": stop_price,
        "take_profit_order_id": intent.take_profit_algo_id,
        "take_profit_client_order_id": intent.take_profit_client_algo_id,
        "take_profit_order_type": str(take_profit_query["orderType"]),
        "take_profit_status": str(take_profit_query["algoStatus"]),
        "take_profit_side": str(take_profit_query["side"]),
        "take_profit_working_type": str(take_profit_query["workingType"]),
        "take_profit_quantity": target_quantity,
        "take_profit_price": take_price,
        "position_side": intent.position_side,
        "close_position": stop_close is True and target_close is True,
        "reduce_only": stop_reduce is True and target_reduce is True,
    }
    return ProtectionResult(True, "PROTECTED", (), evidence)
