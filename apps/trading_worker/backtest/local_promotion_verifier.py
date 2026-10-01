"""Independent verifier for Local OOS and Shadow promotion evidence.

The verifier intentionally accepts no caller-supplied basket rows or metrics.
It reopens the event dataset, replay configuration, and replay attestation;
executes the repository's deterministic replay code; then derives completed
flat-to-flat exposure chains and net economics from the replay output. OOS
verification re-fetches allowlisted public Binance checksums and API snapshots
with GET only; it never authenticates or sends orders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from pydantic import ValidationError

from domain.enums import EconomicRiskClass, RiskState

from .economic import EconomicCostModel, cost_trade, evaluate_trades
from .event_dataset import historical_events_sha256, read_historical_events_jsonl, sha256_file
from .evidence_artifact import (
    ResearchDatasetManifest,
    ResearchSourceRecord,
    load_vision_replay_inputs,
    parse_replay_symbol_rules_from_exchange_info,
)
from .replay import (
    EventWalkForwardConfig,
    ReplayExecutionConfig,
    ReplayParameterVariant,
    ReplayResult,
    ReplayWalkForwardFoldResult,
    ReplayWalkForwardResult,
    run_replay,
    walk_forward_event_splits,
)

SCHEMA_VERSION = 1
BUNDLE_SCHEMA_VERSION = 2
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_REPLAY_BYTES = 32 * 1024 * 1024
MAX_DATASET_BYTES = 512 * 1024 * 1024
MAX_PUBLIC_JSON_BYTES = 32 * 1024 * 1024
MAX_PUBLIC_CHECKSUM_BYTES = 4096
PUBLIC_GET_TIMEOUT_SECONDS = 15
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
COHORT_NAMES = ("OOS", "SHADOW")
PROMOTION_SYMBOL = "ETHUSDC"
RESEARCH_SOURCE = "BINANCE_PUBLIC_MAINNET_READ_ONLY"
THRESHOLDS = {
    "totalClosedBaskets": 50,
    "maxDrawdownPct": 3.5,
    "minSharpe": 1.0,
    "minWinRatePct": 50.0,
    "maxRule0Violations": 0,
}
# Promotion replay uses a versioned conservative cost floor. These are policy
# values, not a claim about the account's current Binance fee tier. A producer
# may model higher costs, but it cannot lower a cohort below this floor.
TRUSTED_COST_POLICY = {
    "version": "LOCAL_PROMOTION_COST_FLOOR_V1",
    "minMakerFeeRate": "0.0002",
    "minTakerFeeRate": "0.0005",
    "minMarketSlippageBps": "2",
}
TRUSTED_COST_POLICY_SHA256 = hashlib.sha256(
    json.dumps(TRUSTED_COST_POLICY, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


class EvidenceNotRun(ValueError):
    """Evidence is absent or incomplete, so no reliable determination ran."""


class EvidenceRejected(ValueError):
    """Evidence was present but failed an integrity or reproducibility check."""


@dataclass(frozen=True)
class ClosedBasket:
    basket_id: str
    closed_at: datetime
    net_pnl: Decimal


@dataclass(frozen=True)
class CohortReplay:
    initial_capital: Decimal
    replay_hash: str
    baskets: tuple[ClosedBasket, ...]
    rule0_violations: int
    event_max_drawdown_pct: Decimal
    daily_equity_curve: tuple[tuple[datetime, Decimal], ...] = ()


def _json_no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _read_json(path: Path, *, max_bytes: int) -> tuple[Any, bytes]:
    size = path.stat().st_size
    if size <= 0 or size > max_bytes:
        raise EvidenceRejected(f"evidence file size is outside the allowed range: {path.name}")
    raw = path.read_bytes()
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_json_no_duplicate_keys), raw
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvidenceRejected(f"evidence JSON is invalid: {path.name}") from exc


def _resolve_evidence_file(root: Path, relative: Any, *, max_bytes: int) -> Path:
    if not isinstance(relative, str) or not relative.strip():
        raise EvidenceNotRun("an evidence file path is missing")
    normalized = relative.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(relative)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or any(part in {"", ".", ".."} for part in posix_path.parts)
    ):
        raise EvidenceRejected("evidence paths must be normalized relative paths")
    resolved_root = root.resolve(strict=True)
    try:
        candidate = (resolved_root / Path(*posix_path.parts)).resolve(strict=True)
    except FileNotFoundError as exc:
        raise EvidenceNotRun(f"evidence file is missing: {relative}") from exc
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise EvidenceRejected("evidence path escapes the evidence root") from exc
    if not candidate.is_file():
        raise EvidenceNotRun(f"evidence file is missing: {relative}")
    if candidate.stat().st_size <= 0 or candidate.stat().st_size > max_bytes:
        raise EvidenceRejected(f"evidence file size is outside the allowed range: {relative}")
    return candidate


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _canonical_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return value


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        _canonical_value(value), sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return _sha256(encoded)


def _contains_illustrative_marker(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("syntheticUnitTestOnly") is True or value.get("illustrative") is True:
            return True
        return any(_contains_illustrative_marker(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_illustrative_marker(item) for item in value)
    return False


def _parse_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceRejected(f"{field} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceRejected(f"{field} must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceRejected(f"{field} must be a timezone-aware timestamp")
    return parsed.astimezone(UTC)


def _require_hash(value: Any, field: str, pattern: re.Pattern[str] = SHA256_RE) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value.lower()) is None:
        raise EvidenceRejected(f"{field} is missing or malformed")
    return value.lower()


def _load_events(dataset_path: Path) -> tuple[Any, ...]:
    try:
        events = read_historical_events_jsonl(dataset_path)
    except (OSError, ValueError, ValidationError) as exc:
        raise EvidenceRejected("dataset could not be revalidated as replay events") from exc
    if not events:
        raise EvidenceNotRun("dataset contains no replay events")
    if any(
        event.symbol != PROMOTION_SYMBOL
        or event.venue != "BINANCE_MAINNET"
        or event.data_source != RESEARCH_SOURCE
        for event in events
    ):
        raise EvidenceRejected("promotion dataset must be Mainnet read-only ETHUSDC data")
    return events


def _validate_config(config: ReplayExecutionConfig, events: tuple[Any, ...]) -> None:
    if config.force_close_at_end is not True:
        raise EvidenceRejected("replay configuration must force-close the sample")
    if config.require_funding_events is not True:
        raise EvidenceRejected("replay configuration must require observed funding events")
    if PROMOTION_SYMBOL not in {rule.symbol for rule in config.symbol_rules}:
        raise EvidenceRejected("replay configuration has no ETHUSDC exchange rules")
    if any(event.symbol not in {rule.symbol for rule in config.symbol_rules} for event in events):
        raise EvidenceRejected("dataset symbols do not match captured exchange rules")


def _validate_trusted_cost_policy(configs: tuple[ReplayExecutionConfig, ...]) -> None:
    if not configs:
        raise EvidenceNotRun("cohort has no replay configuration for cost validation")
    minimum_maker = Decimal(TRUSTED_COST_POLICY["minMakerFeeRate"])
    minimum_taker = Decimal(TRUSTED_COST_POLICY["minTakerFeeRate"])
    minimum_slippage = Decimal(TRUSTED_COST_POLICY["minMarketSlippageBps"])
    for config in configs:
        if (
            config.cost_model.maker_fee_rate < minimum_maker
            or config.cost_model.taker_fee_rate < minimum_taker
            or config.market_slippage_bps < minimum_slippage
        ):
            raise EvidenceRejected(
                "fee/slippage assumptions are below the trusted "
                f"{TRUSTED_COST_POLICY['version']} floor"
            )
        if config.cost_model.maker_fee_rate == 0 or config.cost_model.taker_fee_rate == 0:
            raise EvidenceRejected("zero-fee assumptions are not eligible for promotion")


def _shadow_decision_trace_sha256(result: ReplayResult) -> str:
    """Hash only the decisions observable in a no-order Shadow runtime."""
    return _canonical_hash(
        {
            "strategyIntents": result.strategy_intents,
            "targetExposures": result.target_exposures,
            "riskSnapshots": result.risk_snapshots,
            "executionDecisions": result.execution_decisions,
            "decisions": result.decisions,
        }
    )


def _replay_shadow(events: tuple[Any, ...], config: ReplayExecutionConfig) -> CohortReplay:
    try:
        result = run_replay(events, config)
    except (ValueError, ValidationError, ArithmeticError) as exc:
        raise EvidenceRejected("Shadow runtime dataset could not be replayed") from exc
    baskets = _closed_baskets(result, config.cost_model)
    equity_curve = tuple((point.timestamp, Decimal(point.equity)) for point in result.equity_curve)
    return CohortReplay(
        initial_capital=config.initial_capital,
        replay_hash=_canonical_hash(
            {
                "replay": result,
                "basketIds": [basket.basket_id for basket in baskets],
                "netBasketPnl": [basket.net_pnl for basket in baskets],
            }
        ),
        baskets=baskets,
        rule0_violations=_rule0_violations(result),
        event_max_drawdown_pct=_maximum_drawdown_pct(
            config.initial_capital, [point.equity for point in result.equity_curve]
        ),
        daily_equity_curve=equity_curve,
    )


def _order_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _rule0_violations(result: ReplayResult) -> int:
    snapshots: dict[datetime, list[Any]] = defaultdict(list)
    for snapshot in result.risk_snapshots:
        snapshots[snapshot.timestamp].append(snapshot)
    executions = {decision.decision_id: decision for decision in result.execution_decisions}
    filled_order_ids = {fill.exchange_order_id for fill in result.fills}
    accepted_order_ids: set[str] = set()
    violations = 0

    for record in result.decisions:
        if not record.accepted:
            continue
        if not record.exchange_order_id or record.exchange_order_id in accepted_order_ids:
            raise EvidenceRejected("accepted replay order has a missing or duplicate exchange ID")
        accepted_order_ids.add(record.exchange_order_id)
        decision = executions.get(record.decision_id)
        if decision is None or _order_value(decision.risk_class) != _order_value(record.risk_class):
            raise EvidenceRejected("accepted replay order cannot be joined to its risk decision")
        if _order_value(record.risk_class) not in {
            EconomicRiskClass.NEW_RISK.value,
            EconomicRiskClass.INCREASE_RISK.value,
        }:
            continue
        at_time = snapshots.get(record.timestamp, [])
        if len(at_time) == 2 and record.timestamp == result.end_time:
            # The replay records one pre-decision snapshot per event and may
            # append a second end-of-sample snapshot before force-closing.
            at_time = at_time[:1]
        if len(at_time) != 1:
            raise EvidenceRejected(
                "Rule #0 cannot be evaluated without one risk snapshot per risk increase"
            )
        snapshot = at_time[0]
        if _order_value(snapshot.risk_state) != RiskState.NORMAL.value or snapshot.hard_violations:
            violations += 1

    if accepted_order_ids != filled_order_ids:
        raise EvidenceRejected("accepted replay orders do not match the replay fill ledger")
    return violations


def _closed_baskets(
    result: ReplayResult, cost_model: EconomicCostModel
) -> tuple[ClosedBasket, ...]:
    unused_trade_indices = set(range(len(result.trades)))
    position = Decimal(0)
    active_id: str | None = None
    active_net = Decimal(0)
    baskets: list[ClosedBasket] = []

    for fill in result.fills:
        quantity = Decimal(fill.quantity)
        if not quantity.is_finite() or quantity <= 0:
            raise EvidenceRejected("replay fill has an invalid quantity")
        side = _order_value(fill.side)
        if side not in {"BUY", "SELL"}:
            raise EvidenceRejected("replay fill has an unsupported side")
        signed = quantity if side == "BUY" else -quantity

        if position == 0:
            if active_id is not None:
                raise EvidenceRejected("flat replay position retained an unfinished basket")
            active_id = hashlib.sha256(
                f"{fill.symbol}|{fill.exchange_trade_id}|{fill.event_time.isoformat()}".encode()
            ).hexdigest()
            active_net = Decimal(0)
            position = signed
            continue

        if (position > 0) == (signed > 0):
            position += signed
            continue
        if quantity > abs(position):
            raise EvidenceRejected(
                "replay fill crosses through flat; basket ownership is ambiguous"
            )

        matches = [
            index
            for index in unused_trade_indices
            if result.trades[index].timestamp == fill.event_time
            and result.trades[index].symbol == fill.symbol
            and result.trades[index].gross_pnl == fill.realized_pnl
        ]
        if len(matches) != 1:
            raise EvidenceRejected("replay close fill has no unique realized trade record")
        trade_index = matches[0]
        unused_trade_indices.remove(trade_index)
        trade = result.trades[trade_index]
        active_net += cost_trade(trade, cost_model).net_pnl
        position += signed
        if position == 0:
            if active_id is None:
                raise EvidenceRejected("replay closed a position without an active basket")
            baskets.append(
                ClosedBasket(
                    basket_id=active_id,
                    closed_at=fill.event_time,
                    net_pnl=active_net,
                )
            )
            active_id = None
            active_net = Decimal(0)

    if position != 0 or active_id is not None:
        raise EvidenceRejected("replay basket did not close to a verified flat position")
    if unused_trade_indices:
        raise EvidenceRejected("replay has realized trades not attributable to closed baskets")
    return tuple(baskets)


def _replay_oos(events: tuple[Any, ...], config_payload: dict[str, Any]) -> CohortReplay:
    if set(config_payload) != {"kind", "windowConfig", "variants"}:
        raise EvidenceRejected("OOS configuration has unsupported or missing fields")
    if config_payload.get("kind") != "WALK_FORWARD":
        raise EvidenceRejected("OOS configuration must use train-only walk-forward selection")
    try:
        window = EventWalkForwardConfig.model_validate(config_payload["windowConfig"])
        variants = tuple(
            ReplayParameterVariant.model_validate(item) for item in config_payload["variants"]
        )
    except (ValidationError, TypeError, KeyError) as exc:
        raise EvidenceRejected("OOS walk-forward configuration is invalid") from exc
    if not variants:
        raise EvidenceNotRun("OOS has no train-select parameter variants")
    for variant in variants:
        _validate_config(variant.config, events)

    try:
        folds = walk_forward_event_splits(events, window)
    except (ValueError, ValidationError, ArithmeticError) as exc:
        raise EvidenceRejected("OOS train-select/test replay could not be reproduced") from exc

    variant_by_id = {variant.variant_id: variant for variant in variants}
    if len(variant_by_id) != len(variants):
        raise EvidenceRejected("OOS variant identifiers are duplicated")
    test_replays: list[dict[str, Any]] = []
    fold_results: list[ReplayWalkForwardFoldResult] = []
    all_baskets: list[ClosedBasket] = []
    rule0_count = 0
    oos_trades: list[Any] = []
    fold_replays: list[ReplayResult] = []
    for fold in folds:
        # Recompute every selector score only from this fold's training slice;
        # the test slice never participates in variant selection.
        train_events = events[fold.train_start : fold.train_end]
        train_scores: dict[str, Decimal | None] = {}
        for variant in variants:
            train_result = run_replay(train_events, variant.config)
            train_scores[variant.variant_id] = (
                train_result.economic_result.net_pnl
                if train_result.economic_result is not None
                else None
            )
        eligible = [
            (variant_id, score)
            for variant_id, score in train_scores.items()
            if score is not None
        ]
        if not eligible:
            raise EvidenceNotRun(
                f"OOS fold {fold.fold_index} has no replayable train-only variant score"
            )
        selected_variant_id = min(eligible, key=lambda item: (-item[1], item[0]))[0]
        selection_hash = _canonical_hash(
            {
                "fold_index": fold.fold_index,
                "train_event_ids": [event.event_id for event in train_events],
                "train_dataset_sha256": _canonical_hash(train_events),
                "variant_config_sha256": {
                    variant.variant_id: _canonical_hash(variant.config)
                    for variant in variants
                },
                "train_net_pnl_by_variant": train_scores,
                "selected_variant_id": selected_variant_id,
            }
        )
        selected = variant_by_id[selected_variant_id]
        test_result = run_replay(events[fold.test_start : fold.test_end], selected.config)
        fold_replays.append(test_result)
        test_net_pnl = (
            test_result.economic_result.net_pnl if test_result.economic_result is not None else None
        )
        test_trades = [
            trade.model_copy(update={"trade_id": f"WF{fold.fold_index}-{trade.trade_id}"})
            for trade in test_result.trades
        ]
        oos_trades.extend(test_trades)
        fold_results.append(
            ReplayWalkForwardFoldResult(
                fold_index=fold.fold_index,
                train_start=fold.train_start,
                train_end=fold.train_end,
                test_start=fold.test_start,
                test_end=fold.test_end,
                selected_variant_id=selected_variant_id,
                train_net_pnl_by_variant=train_scores,
                selection_artifact_sha256=selection_hash,
                test_trade_count=len(test_trades),
                test_net_pnl=test_net_pnl,
            )
        )
        all_baskets.extend(_closed_baskets(test_result, selected.config.cost_model))
        rule0_count += _rule0_violations(test_result)
        test_replays.append(
            {
                "foldIndex": fold.fold_index,
                "selectedVariantId": selected_variant_id,
                "selectionArtifactSha256": selection_hash,
                "testReplaySha256": _canonical_hash(test_result),
            }
        )

    if not all_baskets:
        raise EvidenceNotRun("OOS replay produced no closed exposure baskets")
    if any(
        variant.config.initial_capital != variants[0].config.initial_capital
        or variant.config.cost_model != variants[0].config.cost_model
        for variant in variants
    ):
        raise EvidenceRejected("OOS variants must share capital and net cost assumptions")

    walk_forward = ReplayWalkForwardResult(
        dataset_sha256=_canonical_hash(events),
        variant_ids=tuple(variant.variant_id for variant in variants),
        folds=tuple(fold_results),
        oos_trades=tuple(oos_trades),
        oos_economic_result=(
            evaluate_trades(
                oos_trades,
                initial_capital=variants[0].config.initial_capital,
                cost_model=variants[0].config.cost_model,
            )
            if oos_trades
            else None
        ),
    )

    proof = {
        "walkForwardResultSha256": _canonical_hash(walk_forward),
        "testReplays": test_replays,
        "datasetSha256": historical_events_sha256(events),
    }
    event_max_drawdown = _oos_event_max_drawdown_pct(
        fold_replays, variants[0].config.initial_capital
    )
    daily_equity_curve: list[tuple[datetime, Decimal]] = []
    cumulative_realized_pnl = Decimal(0)
    for result in fold_replays:
        for point in result.equity_curve:
            if point.timestamp.tzinfo is None:
                raise EvidenceRejected("OOS equity point timestamp is not timezone-aware")
            equity = Decimal(point.equity)
            if not equity.is_finite():
                raise EvidenceRejected("OOS equity curve contains a non-finite value")
            daily_equity_curve.append(
                (
                    point.timestamp.astimezone(UTC),
                    variants[0].config.initial_capital
                    + cumulative_realized_pnl
                    + (equity - variants[0].config.initial_capital),
                )
            )
        final_equity = Decimal(result.final_equity)
        if not final_equity.is_finite():
            raise EvidenceRejected("OOS fold has a non-finite final equity")
        cumulative_realized_pnl += final_equity - variants[0].config.initial_capital
    return CohortReplay(
        initial_capital=variants[0].config.initial_capital,
        replay_hash=_canonical_hash(proof),
        baskets=tuple(all_baskets),
        rule0_violations=rule0_count,
        event_max_drawdown_pct=event_max_drawdown,
        daily_equity_curve=tuple(daily_equity_curve),
    )


def _maximum_drawdown_pct(
    initial_capital: Decimal,
    equity_values: Any,
) -> Decimal:
    if not initial_capital.is_finite() or initial_capital <= 0:
        raise EvidenceRejected("replay initial capital must be finite and positive")
    peak = initial_capital
    maximum_drawdown = Decimal(0)
    seen = False
    for raw_equity in equity_values:
        try:
            equity = Decimal(raw_equity)
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise EvidenceRejected("event-level equity curve contains an invalid value") from exc
        if not equity.is_finite():
            raise EvidenceRejected("event-level equity curve contains a non-finite value")
        seen = True
        drawdown = Decimal(100) * (peak - equity) / peak if peak > 0 else Decimal(100)
        maximum_drawdown = max(maximum_drawdown, drawdown)
        peak = max(peak, equity)
    if not seen:
        raise EvidenceRejected("replay has no event-level mark-to-market equity curve")
    return maximum_drawdown


def _oos_event_max_drawdown_pct(
    fold_replays: list[ReplayResult], initial_capital: Decimal
) -> Decimal:
    if not fold_replays:
        raise EvidenceNotRun("OOS has no independently replayed test folds")
    fold_drawdowns: list[Decimal] = []
    stitched_equity: list[Decimal] = []
    cumulative_realized_pnl = Decimal(0)
    for result in fold_replays:
        fold_equity = [point.equity for point in result.equity_curve]
        fold_drawdowns.append(_maximum_drawdown_pct(initial_capital, fold_equity))
        stitched_equity.extend(
            initial_capital + cumulative_realized_pnl + (equity - initial_capital)
            for equity in fold_equity
        )
        final_equity = Decimal(result.final_equity)
        if not final_equity.is_finite():
            raise EvidenceRejected("OOS fold has a non-finite final equity")
        cumulative_realized_pnl += final_equity - initial_capital
    stitched_drawdown = _maximum_drawdown_pct(initial_capital, stitched_equity)
    # Fold resets can conceal a loss after gains; take the largest individual
    # fold drawdown and the drawdown of the cohort-level rebased equity curve.
    return max(*fold_drawdowns, stitched_drawdown)


def _metrics(replay: CohortReplay) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    baskets = sorted(replay.baskets, key=lambda item: item.closed_at)
    if len({basket.basket_id for basket in baskets}) != len(baskets):
        raise EvidenceRejected("replayed flat-to-flat basket IDs are not unique")
    equity = replay.initial_capital
    peak = equity
    maximum_drawdown = Decimal(0)
    wins = 0
    for basket in baskets:
        equity += basket.net_pnl
        peak = max(peak, equity)
        drawdown = Decimal(100) * (peak - equity) / peak if peak > 0 else Decimal(100)
        maximum_drawdown = max(maximum_drawdown, drawdown)
        if basket.net_pnl > 0:
            wins += 1

    daily_close: dict[date, Decimal] = {}
    prior_timestamp: datetime | None = None
    for timestamp, raw_equity in replay.daily_equity_curve:
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise EvidenceRejected("replayed equity point timestamp is invalid")
        if prior_timestamp is not None and timestamp.astimezone(UTC) < prior_timestamp:
            raise EvidenceRejected("replayed equity curve timestamps are not ordered")
        normalized_timestamp = timestamp.astimezone(UTC)
        try:
            equity_value = Decimal(raw_equity)
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise EvidenceRejected("replayed daily equity curve contains an invalid value") from exc
        if not equity_value.is_finite():
            raise EvidenceRejected("replayed daily equity curve contains a non-finite value")
        daily_close[normalized_timestamp.date()] = equity_value
        prior_timestamp = normalized_timestamp

    daily_returns: list[float] = []
    ordered_days = sorted(daily_close)
    if ordered_days:
        prior_equity = replay.initial_capital
        day = ordered_days[0]
        final_day = ordered_days[-1]
        while day <= final_day:
            close_equity = daily_close.get(day, prior_equity)
            if prior_equity <= 0 or close_equity <= 0:
                daily_returns.append(math.nan)
            else:
                daily_returns.append(float(close_equity / prior_equity - Decimal(1)))
            prior_equity = close_equity
            day += timedelta(days=1)
    sharpe: float | None = None
    if len(daily_returns) >= 2 and all(math.isfinite(value) for value in daily_returns):
        mean = sum(daily_returns) / len(daily_returns)
        variance = sum((value - mean) ** 2 for value in daily_returns) / (len(daily_returns) - 1)
        deviation = math.sqrt(variance)
        if deviation > 0:
            sharpe = mean / deviation * math.sqrt(365)

    count = len(baskets)
    if not replay.event_max_drawdown_pct.is_finite() or replay.event_max_drawdown_pct < 0:
        raise EvidenceRejected("event-level drawdown metric is invalid")
    event_max_drawdown_pct = float(replay.event_max_drawdown_pct)
    max_drawdown_pct = max(float(maximum_drawdown), event_max_drawdown_pct)
    win_rate_pct = (wins / count * 100.0) if count else 0.0
    values = {
        "closedBaskets": count,
        "maxDrawdownPct": max_drawdown_pct,
        "eventMaxDrawdownPct": event_max_drawdown_pct,
        "sharpe": sharpe,
        "winRatePct": win_rate_pct,
        "rule0Violations": replay.rule0_violations,
    }
    if count == 0:
        failures.append("cohort has no replay-verified closed baskets")
    if max_drawdown_pct > THRESHOLDS["maxDrawdownPct"]:
        failures.append("MaxDD including event-level mark-to-market exceeds 3.5%")
    if sharpe is None or not math.isfinite(sharpe) or sharpe < THRESHOLDS["minSharpe"]:
        failures.append("Sharpe is below 1.0 or cannot be calculated")
    if win_rate_pct < THRESHOLDS["minWinRatePct"]:
        failures.append("Win rate is below 50%")
    if replay.rule0_violations != THRESHOLDS["maxRule0Violations"]:
        failures.append("Rule #0 violations must be zero")
    return values, failures


def _validate_source_manifest(
    artifact: dict[str, Any],
    *,
    expected_git_sha: str,
    events: tuple[Any, ...],
    config_payload: dict[str, Any],
    config_sha256: str,
) -> None:
    manifest_payload = artifact.get("manifest")
    try:
        manifest = ResearchDatasetManifest.model_validate(manifest_payload)
    except (ValidationError, TypeError) as exc:
        raise EvidenceRejected("replay artifact has no valid Binance source manifest") from exc
    if manifest.code_sha.lower() != expected_git_sha.lower():
        raise EvidenceRejected("source manifest Git SHA does not match reviewed runtime")
    if manifest.symbol != PROMOTION_SYMBOL or manifest.venue != "BINANCE_MAINNET":
        raise EvidenceRejected("source manifest is not Mainnet ETHUSDC evidence")
    if manifest.event_count != len(events):
        raise EvidenceRejected("source manifest event count does not match dataset")
    event_hash = historical_events_sha256(events)
    if manifest.dataset_sha256 != event_hash:
        raise EvidenceRejected("source manifest dataset fingerprint does not match reopened events")
    if manifest.config_sha256 != config_sha256:
        raise EvidenceRejected(
            "source manifest config fingerprint does not match exact config file"
        )
    if manifest.start_time != events[0].event_time or manifest.end_time != events[-1].event_time:
        raise EvidenceRejected("source manifest time window does not match reopened events")
    if manifest.cost_model.model_dump(mode="json") != _config_cost_model(config_payload).model_dump(
        mode="json"
    ):
        raise EvidenceRejected("source manifest fee model does not match replay configuration")
    rules = _config_symbol_rules(config_payload)
    if tuple(manifest.symbol_rules) != tuple(rules):
        raise EvidenceRejected("source manifest exchange rules do not match replay configuration")


def _read_verified_binance_public_url(
    url: str, *, expected_host: str, max_bytes: int
) -> bytes:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.fragment
    ):
        raise EvidenceRejected("OOS source URL is outside the fixed Binance public HTTPS allowlist")
    request = Request(
        url,
        headers={"Accept": "application/json, text/plain, */*", "User-Agent": "Blessing-Research-Verifier/1"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=PUBLIC_GET_TIMEOUT_SECONDS) as response:
            final_url = urlsplit(response.geturl())
            if final_url.scheme != "https" or final_url.hostname != expected_host:
                raise EvidenceRejected("OOS source redirected outside the Binance public host")
            if response.status != 200:
                raise EvidenceNotRun("Binance public source could not be independently re-fetched")
            raw = response.read(max_bytes + 1)
    except (EvidenceNotRun, EvidenceRejected):
        raise
    except Exception as exc:
        raise EvidenceNotRun(
            f"Binance public source re-fetch is unavailable ({type(exc).__name__})"
        ) from exc
    if len(raw) > max_bytes:
        raise EvidenceRejected("Binance public source response exceeds its verification size limit")
    return raw


def _validate_vision_archive_identity(
    source_type: str,
    record: ResearchSourceRecord,
    archive_path: Path,
    *,
    dataset_start: datetime,
    dataset_end: datetime,
) -> None:
    """Bind a raw ZIP and its URL to the exact ETHUSDC UM feed and interval."""
    source_type = source_type.upper()
    parsed = urlsplit(record.url)
    parts = PurePosixPath(parsed.path).parts
    if (
        parsed.scheme != "https"
        or parsed.hostname != "data.binance.vision"
        or parsed.query
        or parsed.fragment
        or not archive_path.is_file()
        or not archive_path.name.endswith(".zip")
    ):
        raise EvidenceRejected(f"OOS {source_type} archive URL or file is invalid")

    feed = {
        "KLINES_1M": ("klines", "ETHUSDC", "1m", re.compile(r"ETHUSDC-1m-(\d{4})-(\d{2})(?:-(\d{2}))?\.zip")),
        "MARK_PRICE_KLINES_1M": ("markPriceKlines", "ETHUSDC", "1m", re.compile(r"ETHUSDC-1m-(\d{4})-(\d{2})(?:-(\d{2}))?\.zip")),
        "BOOK_TICKER": ("bookTicker", "ETHUSDC", None, re.compile(r"ETHUSDC-bookTicker-(\d{4})-(\d{2})(?:-(\d{2}))?\.zip")),
    }.get(source_type)
    if feed is None:
        raise EvidenceRejected("unsupported Binance Vision archive source type")
    feed_directory, symbol, interval, filename_pattern = feed
    # URL path has a leading empty component; enforce a fixed depth instead of
    # trusting manifest labels to identify the archive family.
    if interval is not None:
        valid_layout = (
            len(parts) == 9
            and parts[1:3] == ("data", "futures")
            and parts[3] == "um"
            and parts[4] in {"daily", "monthly"}
            and parts[5:8] == (feed_directory, symbol, interval)
        )
    else:
        valid_layout = (
            len(parts) == 8
            and parts[1:3] == ("data", "futures")
            and parts[3] == "um"
            and parts[4] in {"daily", "monthly"}
            and parts[5:7] == (feed_directory, symbol)
        )
    if not valid_layout or parts[-1] != archive_path.name:
        raise EvidenceRejected(f"OOS {source_type} URL does not bind ETHUSDC UM feed and interval")
    match = filename_pattern.fullmatch(archive_path.name)
    if match is None:
        raise EvidenceRejected(f"OOS {source_type} archive filename is invalid")
    year, month, day = (int(value) if value else None for value in match.groups())
    if (parts[4] == "daily") != (day is not None):
        raise EvidenceRejected(f"OOS {source_type} archive cadence disagrees with its URL path")
    try:
        period_start = datetime(year, month, day or 1, tzinfo=UTC)
        period_end = (
            period_start + timedelta(days=1)
            if day is not None
            else (datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=UTC))
        )
    except ValueError as exc:
        raise EvidenceRejected(f"OOS {source_type} archive period is invalid") from exc
    start = dataset_start.astimezone(UTC)
    end = dataset_end.astimezone(UTC)
    if (
        start < period_start
        or end >= period_end
        or record.start_time > start
        or record.end_time < end
    ):
        raise EvidenceRejected(f"OOS {source_type} archive does not cover the manifest window")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = [name for name in archive.namelist() if not name.endswith("/")]
    except (OSError, zipfile.BadZipFile) as exc:
        raise EvidenceRejected(f"OOS {source_type} archive cannot be inspected") from exc
    if members != [archive_path.stem + ".csv"]:
        raise EvidenceRejected(f"OOS {source_type} ZIP member does not match its archive identity")


def _verify_oos_raw_sources(
    root: Path,
    source_paths_payload: Any,
    manifest: ResearchDatasetManifest,
    expected_events: tuple[Any, ...],
) -> None:
    """Re-fetch official Binance sources and rebuild the exact OOS event dataset."""

    source_records = {record.source_type: record for record in manifest.source_records}
    source_records[manifest.exchange_info_source.source_type] = manifest.exchange_info_source
    required_sources = {
        "KLINES_1M",
        "MARK_PRICE_KLINES_1M",
        "BOOK_TICKER",
        "FUNDING_RATES",
        "EXCHANGE_INFO",
    }
    if (
        not isinstance(source_paths_payload, dict)
        or set(source_paths_payload) != required_sources
        or set(source_records) != required_sources
    ):
        raise EvidenceNotRun(
            "OOS raw Binance source files are missing; manifest hashes alone are not provenance"
        )

    source_paths: dict[str, Path] = {}
    source_hashes: dict[str, str] = {}
    for source_type in sorted(required_sources):
        source_path = _resolve_evidence_file(
            root, source_paths_payload[source_type], max_bytes=MAX_DATASET_BYTES
        )
        source_paths[source_type] = source_path
        actual_hash = sha256_file(source_path)
        if actual_hash != source_records[source_type].sha256:
            raise EvidenceRejected(f"OOS {source_type} raw source hash does not match its manifest")
        source_hashes[source_type] = actual_hash

    published_hashes: dict[str, str] = {}
    for source_type in ("KLINES_1M", "MARK_PRICE_KLINES_1M", "BOOK_TICKER"):
        record = source_records[source_type]
        _validate_vision_archive_identity(
            source_type,
            record,
            source_paths[source_type],
            dataset_start=manifest.start_time,
            dataset_end=manifest.end_time,
        )
        checksum_bytes = _read_verified_binance_public_url(
            record.url + ".CHECKSUM",
            expected_host="data.binance.vision",
            max_bytes=MAX_PUBLIC_CHECKSUM_BYTES,
        )
        checksum_text = checksum_bytes.decode("ascii", errors="strict")
        checksum_matches = re.findall(r"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", checksum_text)
        if len(checksum_matches) != 1:
            raise EvidenceRejected(f"OOS {source_type} published checksum is malformed")
        published_hash = checksum_matches[0].lower()
        if published_hash != source_hashes[source_type]:
            raise EvidenceRejected(f"OOS {source_type} differs from Binance's published checksum")
        published_hashes[source_type] = published_hash

    funding_url = urlsplit(source_records["FUNDING_RATES"].url)
    try:
        funding_query = parse_qs(funding_url.query, strict_parsing=True)
        funding_start = int(funding_query["startTime"][0])
        funding_end = int(funding_query["endTime"][0])
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceRejected("OOS funding source URL is not bounded by an explicit time window") from exc
    if (
        funding_url.hostname != "fapi.binance.com"
        or funding_url.path != "/fapi/v1/fundingRate"
        or set(funding_query) - {"symbol", "startTime", "endTime", "limit"}
        or funding_query.get("symbol") != [PROMOTION_SYMBOL]
        or funding_start < 0
        or funding_end <= funding_start
    ):
        raise EvidenceRejected("OOS funding source is not a bounded Mainnet ETHUSDC query")
    manifest_start_ms = int(manifest.start_time.astimezone(UTC).timestamp() * 1000)
    manifest_end_ms = int(manifest.end_time.astimezone(UTC).timestamp() * 1000)
    if funding_start > manifest_start_ms or funding_end < manifest_end_ms:
        raise EvidenceRejected("OOS funding source bounds do not cover the complete manifest window")
    funding_bytes = _read_verified_binance_public_url(
        source_records["FUNDING_RATES"].url,
        expected_host="fapi.binance.com",
        max_bytes=MAX_PUBLIC_JSON_BYTES,
    )
    if _sha256(funding_bytes) != source_hashes["FUNDING_RATES"]:
        raise EvidenceRejected("OOS funding response differs from the Binance public API")

    exchange_info_url = urlsplit(source_records["EXCHANGE_INFO"].url)
    if (
        exchange_info_url.hostname != "fapi.binance.com"
        or exchange_info_url.path != "/fapi/v1/exchangeInfo"
        or exchange_info_url.query
    ):
        raise EvidenceRejected("OOS exchange-info URL is not the Binance USD-M public endpoint")
    exchange_info_bytes = _read_verified_binance_public_url(
        source_records["EXCHANGE_INFO"].url,
        expected_host="fapi.binance.com",
        max_bytes=MAX_PUBLIC_JSON_BYTES,
    )
    if _sha256(exchange_info_bytes) != source_hashes["EXCHANGE_INFO"]:
        raise EvidenceRejected("OOS exchange-info capture differs from the Binance public API")
    try:
        exchange_info = json.loads(exchange_info_bytes.decode("utf-8"))
        actual_rules = parse_replay_symbol_rules_from_exchange_info(
            exchange_info, manifest.symbol
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise EvidenceRejected("OOS exchange-info response cannot reproduce captured symbol rules") from exc
    if (actual_rules,) != tuple(manifest.symbol_rules):
        raise EvidenceRejected("OOS symbol rules do not match the re-fetched Binance exchange-info")

    with tempfile.TemporaryDirectory(prefix="blessing-oos-checksums-") as temporary:
        checksum_paths: dict[str, Path] = {}
        for source_type, digest in published_hashes.items():
            checksum_path = Path(temporary) / f"{source_type}.CHECKSUM"
            checksum_path.write_text(f"{digest}  {source_paths[source_type].name}\n", encoding="ascii")
            checksum_paths[source_type] = checksum_path
        try:
            rebuilt_events, rebuilt_sources = load_vision_replay_inputs(
                symbol=manifest.symbol,
                venue="BINANCE_MAINNET",
                kline_archive=source_paths["KLINES_1M"],
                mark_price_archive=source_paths["MARK_PRICE_KLINES_1M"],
                book_ticker_archive=source_paths["BOOK_TICKER"],
                funding_json=source_paths["FUNDING_RATES"],
                kline_url=source_records["KLINES_1M"].url,
                mark_price_url=source_records["MARK_PRICE_KLINES_1M"].url,
                book_ticker_url=source_records["BOOK_TICKER"].url,
                funding_url=source_records["FUNDING_RATES"].url,
                kline_checksum=checksum_paths["KLINES_1M"],
                mark_price_checksum=checksum_paths["MARK_PRICE_KLINES_1M"],
                book_ticker_checksum=checksum_paths["BOOK_TICKER"],
                start_time=manifest.start_time,
                end_time=manifest.end_time + timedelta(milliseconds=1),
            )
        except Exception as exc:
            raise EvidenceRejected("OOS raw Binance sources cannot rebuild the replay dataset") from exc
    rebuilt_by_type = {record.source_type: record for record in rebuilt_sources}
    if any(rebuilt_by_type.get(name) != source_records[name] for name in rebuilt_by_type):
        raise EvidenceRejected("OOS source rows, time coverage, or raw-source hashes do not reproduce")
    if historical_events_sha256(rebuilt_events) != historical_events_sha256(expected_events):
        raise EvidenceRejected("OOS raw Binance archives do not rebuild the exact replay events")


def _config_models(
    cohort: str, config_payload: dict[str, Any]
) -> tuple[ReplayExecutionConfig, ...]:
    try:
        if cohort == "SHADOW":
            if (
                set(config_payload) != {"kind", "config"}
                or config_payload.get("kind") != "SHADOW_REPLAY"
            ):
                raise EvidenceRejected("Shadow configuration has an unsupported shape")
            return (ReplayExecutionConfig.model_validate(config_payload["config"]),)
        if (
            set(config_payload) != {"kind", "windowConfig", "variants"}
            or config_payload.get("kind") != "WALK_FORWARD"
        ):
            raise EvidenceRejected("OOS configuration has an unsupported shape")
        variants = tuple(
            ReplayParameterVariant.model_validate(item) for item in config_payload["variants"]
        )
        return tuple(item.config for item in variants)
    except (ValidationError, TypeError, KeyError) as exc:
        raise EvidenceRejected(f"{cohort} replay configuration is invalid") from exc


def _config_cost_model(config_payload: dict[str, Any]) -> EconomicCostModel:
    config = config_payload.get("config") if config_payload.get("kind") == "SHADOW_REPLAY" else None
    if config is not None:
        return ReplayExecutionConfig.model_validate(config).cost_model
    variants = [
        ReplayParameterVariant.model_validate(item) for item in config_payload.get("variants", [])
    ]
    if not variants:
        raise EvidenceNotRun("OOS configuration has no cost model")
    return variants[0].config.cost_model


def _config_symbol_rules(config_payload: dict[str, Any]) -> tuple[Any, ...]:
    if config_payload.get("kind") == "SHADOW_REPLAY":
        config = ReplayExecutionConfig.model_validate(config_payload["config"])
        return tuple(config.symbol_rules)
    variants = [
        ReplayParameterVariant.model_validate(item) for item in config_payload.get("variants", [])
    ]
    if not variants:
        return ()
    expected = tuple(variants[0].config.symbol_rules)
    if any(tuple(item.config.symbol_rules) != expected for item in variants[1:]):
        raise EvidenceRejected("OOS variants must share captured exchange symbol rules")
    return expected


def _validate_shadow_capture(
    capture: Any,
    *,
    expected_git_sha: str,
    expected_source_fingerprint: str,
    dataset_sha256: str,
    config_sha256: str,
    events: tuple[Any, ...],
) -> str:
    fields = {
        "schemaVersion",
        "generator",
        "captureId",
        "sourceGitSha",
        "sourceFingerprint",
        "datasetSha256",
        "configSha256",
        "eventCount",
        "startTime",
        "endTime",
        "orderSubmissionAttempts",
        "orderEndpointCalls",
        "decisionTraceSha256",
    }
    if not isinstance(capture, dict) or set(capture) != fields:
        raise EvidenceRejected("Shadow runtime capture has unsupported or missing fields")
    if capture.get("schemaVersion") != 1 or capture.get("generator") != "BLESSING_SHADOW_RUNTIME":
        raise EvidenceRejected("Shadow runtime capture schema or generator is unsupported")
    capture_id = capture.get("captureId")
    if not isinstance(capture_id, str) or not capture_id.strip() or len(capture_id) > 128:
        raise EvidenceRejected("Shadow runtime capture ID is invalid")
    if (
        not isinstance(capture.get("sourceGitSha"), str)
        or capture["sourceGitSha"].lower() != expected_git_sha.lower()
        or not isinstance(capture.get("sourceFingerprint"), str)
        or capture["sourceFingerprint"].lower() != expected_source_fingerprint.lower()
    ):
        raise EvidenceRejected("Shadow capture source identity does not match the reviewed runtime")
    if capture.get("datasetSha256") != dataset_sha256 or capture.get("configSha256") != config_sha256:
        raise EvidenceRejected("Shadow capture does not bind the reopened dataset and config")
    if capture.get("eventCount") != len(events):
        raise EvidenceRejected("Shadow capture event count does not match the replay dataset")
    if _parse_datetime(capture.get("startTime"), "SHADOW.startTime") != events[0].event_time:
        raise EvidenceRejected("Shadow capture start time does not match the dataset")
    if _parse_datetime(capture.get("endTime"), "SHADOW.endTime") != events[-1].event_time:
        raise EvidenceRejected("Shadow capture end time does not match the dataset")
    if capture.get("orderSubmissionAttempts") != 0 or capture.get("orderEndpointCalls") != 0:
        raise EvidenceRejected("Shadow capture records an order attempt or order endpoint call")
    return _require_hash(capture.get("decisionTraceSha256"), "SHADOW.decisionTraceSha256")


def _verify_cohort(
    root: Path,
    cohort_name: Literal["OOS", "SHADOW"],
    cohort_payload: Any,
    *,
    expected_git_sha: str,
    expected_source_fingerprint: str,
) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(cohort_payload, dict) or cohort_payload.get("cohort") != cohort_name:
        raise EvidenceNotRun(f"{cohort_name} cohort evidence is missing or mislabeled")
    if set(cohort_payload) != {"cohort", "provenance"}:
        raise EvidenceRejected(f"{cohort_name} cohort cannot contain caller-asserted metrics")
    provenance = cohort_payload.get("provenance")
    if not isinstance(provenance, dict):
        raise EvidenceNotRun(f"{cohort_name} provenance is missing")
    provenance_fields = {
        "generator",
        "sourceGitSha",
        "sourceFingerprint",
        "datasetPath",
        "datasetSha256",
        "configPath",
        "configSha256",
        "replayArtifactPath",
        "replayArtifactSha256",
    }
    if cohort_name == "OOS":
        provenance_fields.add("sourceFiles")
    else:
        provenance_fields.update({"runtimeCapturePath", "runtimeCaptureSha256"})
    if set(provenance) != provenance_fields:
        raise EvidenceRejected(f"{cohort_name} provenance has unsupported or missing fields")
    expected_generator = (
        "BLESSING_BACKTEST" if cohort_name == "OOS" else "BLESSING_SHADOW_RUNTIME"
    )
    if provenance.get("generator") != expected_generator:
        raise EvidenceRejected(f"{cohort_name} evidence generator is not trusted")
    if (
        not isinstance(provenance.get("sourceGitSha"), str)
        or provenance["sourceGitSha"].lower() != expected_git_sha.lower()
        or not isinstance(provenance.get("sourceFingerprint"), str)
        or provenance["sourceFingerprint"].lower() != expected_source_fingerprint.lower()
    ):
        raise EvidenceRejected(
            f"{cohort_name} source SHA/fingerprint does not match current runtime"
        )

    dataset_path = _resolve_evidence_file(
        root, provenance.get("datasetPath"), max_bytes=MAX_DATASET_BYTES
    )
    config_path = _resolve_evidence_file(
        root, provenance.get("configPath"), max_bytes=MAX_CONFIG_BYTES
    )
    replay_path = _resolve_evidence_file(
        root, provenance.get("replayArtifactPath"), max_bytes=MAX_REPLAY_BYTES
    )
    capture_path = (
        _resolve_evidence_file(
            root, provenance.get("runtimeCapturePath"), max_bytes=MAX_REPLAY_BYTES
        )
        if cohort_name == "SHADOW"
        else None
    )
    dataset_raw = dataset_path.read_bytes()
    config_payload, config_raw = _read_json(config_path, max_bytes=MAX_CONFIG_BYTES)
    artifact_payload, artifact_raw = _read_json(replay_path, max_bytes=MAX_REPLAY_BYTES)
    capture_payload: Any = None
    capture_raw: bytes | None = None
    if capture_path is not None:
        capture_payload, capture_raw = _read_json(capture_path, max_bytes=MAX_REPLAY_BYTES)

    for field, actual in (
        ("datasetSha256", _sha256(dataset_raw)),
        ("configSha256", _sha256(config_raw)),
        ("replayArtifactSha256", _sha256(artifact_raw)),
    ):
        expected = _require_hash(provenance.get(field), f"{cohort_name}.{field}")
        if expected != actual:
            raise EvidenceRejected(f"{cohort_name} {field} does not match reopened evidence bytes")
    if capture_raw is not None and _require_hash(
        provenance.get("runtimeCaptureSha256"), "SHADOW.runtimeCaptureSha256"
    ) != _sha256(capture_raw):
        raise EvidenceRejected("SHADOW runtimeCaptureSha256 does not match reopened capture bytes")
    if not isinstance(config_payload, dict) or not isinstance(artifact_payload, dict):
        raise EvidenceRejected(f"{cohort_name} config and replay artifact must be JSON objects")
    if _contains_illustrative_marker(config_payload) or _contains_illustrative_marker(
        artifact_payload
    ):
        raise EvidenceRejected(f"{cohort_name} contains illustrative or synthetic evidence")

    events = _load_events(dataset_path)
    dataset_hash = historical_events_sha256(events)
    config_hash = _sha256(config_raw)
    configs = _config_models(cohort_name, config_payload)
    if not configs:
        raise EvidenceNotRun(f"{cohort_name} contains no replay configuration")
    for config in configs:
        _validate_config(config, events)
    _validate_trusted_cost_policy(configs)

    if (
        artifact_payload.get("schemaVersion") != 1
        or artifact_payload.get("generator") != "BLESSING_BACKTEST"
        or artifact_payload.get("cohort") != cohort_name
    ):
        raise EvidenceRejected(f"{cohort_name} replay artifact schema is unsupported")
    artifact_fields = {
        "schemaVersion",
        "generator",
        "cohort",
        "sourceGitSha",
        "sourceFingerprint",
        "datasetSha256",
        "configSha256",
        "costPolicySha256",
        "deterministicReplaySha256",
    }
    if cohort_name == "OOS":
        artifact_fields.add("manifest")
    else:
        artifact_fields.add("runtimeCaptureSha256")
    if set(artifact_payload) != artifact_fields:
        raise EvidenceRejected(f"{cohort_name} replay artifact has unsupported or missing fields")
    if (
        not isinstance(artifact_payload.get("sourceGitSha"), str)
        or artifact_payload["sourceGitSha"].lower() != expected_git_sha.lower()
    ):
        raise EvidenceRejected(f"{cohort_name} replay artifact source SHA does not match")
    if (
        not isinstance(artifact_payload.get("sourceFingerprint"), str)
        or artifact_payload["sourceFingerprint"].lower() != expected_source_fingerprint.lower()
    ):
        raise EvidenceRejected(f"{cohort_name} replay artifact source fingerprint does not match")
    if artifact_payload.get("datasetSha256") != dataset_hash:
        raise EvidenceRejected(
            f"{cohort_name} replay artifact dataset fingerprint does not reproduce"
        )
    if artifact_payload.get("configSha256") != config_hash:
        raise EvidenceRejected(
            f"{cohort_name} replay artifact config fingerprint does not reproduce"
        )

    if cohort_name == "OOS":
        _validate_source_manifest(
            artifact_payload,
            expected_git_sha=expected_git_sha,
            events=events,
            config_payload=config_payload,
            config_sha256=config_hash,
        )
        if "sourceFiles" not in provenance:
            raise EvidenceNotRun(
                "OOS raw Binance source files are missing; checksums asserted only by the bundle are insufficient"
            )
        try:
            manifest = ResearchDatasetManifest.model_validate(artifact_payload.get("manifest"))
        except (ValidationError, TypeError) as exc:
            raise EvidenceRejected("OOS source manifest is invalid") from exc
        _verify_oos_raw_sources(root, provenance.get("sourceFiles"), manifest, events)
    else:
        if capture_raw is None or capture_payload is None:
            raise EvidenceNotRun("Shadow runtime capture is missing")
        if artifact_payload.get("runtimeCaptureSha256") != _sha256(capture_raw):
            raise EvidenceRejected("Shadow replay artifact does not bind runtime capture bytes")

    if artifact_payload.get("costPolicySha256") != TRUSTED_COST_POLICY_SHA256:
        raise EvidenceRejected("replay artifact does not bind the reviewed trusted cost policy")

    if cohort_name == "OOS":
        replayed = _replay_oos(events, config_payload)
        shadow_trace_hash = None
    else:
        shadow_config = configs[0]
        raw_result = run_replay(events, shadow_config)
        shadow_trace_hash = _shadow_decision_trace_sha256(raw_result)
        capture_trace_hash = _validate_shadow_capture(
            capture_payload,
            expected_git_sha=expected_git_sha,
            expected_source_fingerprint=expected_source_fingerprint,
            dataset_sha256=dataset_hash,
            config_sha256=config_hash,
            events=events,
        )
        if shadow_trace_hash != capture_trace_hash:
            raise EvidenceRejected("Shadow runtime decision trace does not match independent replay")
        replayed = _replay_shadow(events, shadow_config)
    claimed_replay_hash = _require_hash(
        artifact_payload.get("deterministicReplaySha256"),
        f"{cohort_name}.deterministicReplaySha256",
    )
    if claimed_replay_hash != replayed.replay_hash:
        raise EvidenceRejected(
            f"{cohort_name} deterministic replay digest does not match recomputation"
        )

    metrics, threshold_failures = _metrics(replayed)
    metrics["status"] = "PASS" if not threshold_failures else "FAIL"
    return metrics, threshold_failures


def _result(
    status: Literal["PASS", "FAIL", "NOT_RUN"],
    failures: list[str],
    *,
    bundle_sha256: str | None = None,
    total_closed_baskets: int = 0,
    cohorts: dict[str, Any] | None = None,
    source_git_sha: str | None = None,
    source_fingerprint: str | None = None,
) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": status,
        "passed": status == "PASS",
        "failures": failures,
        "bundleSha256": bundle_sha256,
        "totalClosedBaskets": total_closed_baskets,
        "cohorts": cohorts or {},
        "sourceGitSha": source_git_sha,
        "sourceFingerprint": source_fingerprint,
    }


def verify_promotion_bundle(
    evidence_root: str | Path,
    bundle_path: str,
    expected_git_sha: str,
    expected_source_fingerprint: str,
) -> dict[str, Any]:
    """Return structured status after re-fetching fixed allowlisted Binance public sources."""

    cohorts: dict[str, Any] = {}
    try:
        root = Path(evidence_root).resolve(strict=True)
        if not root.is_dir():
            raise EvidenceNotRun("evidence root is not a directory")
        git_sha = _require_hash(expected_git_sha.lower(), "expected Git SHA", GIT_SHA_RE)
        fingerprint = _require_hash(
            expected_source_fingerprint.lower(), "expected source fingerprint"
        )
        bundle_file = _resolve_evidence_file(root, bundle_path, max_bytes=MAX_BUNDLE_BYTES)
        bundle, raw = _read_json(bundle_file, max_bytes=MAX_BUNDLE_BYTES)
        bundle_hash = _sha256(raw)
        if not isinstance(bundle, dict) or _contains_illustrative_marker(bundle):
            raise EvidenceRejected("promotion bundle is not eligible evidence")
        if set(bundle) != {
            "schemaVersion",
            "generatedAt",
            "sourceGitSha",
            "sourceFingerprint",
            "cohorts",
        }:
            raise EvidenceRejected("promotion bundle has unsupported or missing fields")
        if bundle.get("schemaVersion") != BUNDLE_SCHEMA_VERSION:
            raise EvidenceRejected("promotion evidence schemaVersion must be 2")
        if (
            not isinstance(bundle.get("sourceGitSha"), str)
            or bundle["sourceGitSha"].lower() != git_sha
        ):
            raise EvidenceRejected("promotion bundle source SHA does not match reviewed runtime")
        if (
            not isinstance(bundle.get("sourceFingerprint"), str)
            or bundle["sourceFingerprint"].lower() != fingerprint
        ):
            raise EvidenceRejected(
                "promotion bundle fingerprint does not match current Local runtime"
            )
        generated_at = _parse_datetime(bundle.get("generatedAt"), "generatedAt")
        if generated_at > datetime.now(UTC):
            raise EvidenceRejected("promotion bundle generatedAt is in the future")
        cohort_payloads = bundle.get("cohorts")
        if not isinstance(cohort_payloads, dict):
            raise EvidenceNotRun("promotion bundle has no OOS/Shadow cohorts")
        if set(cohort_payloads) != set(COHORT_NAMES):
            raise EvidenceRejected("promotion bundle must contain exactly OOS and SHADOW cohorts")

        cohort_failures: list[str] = []
        for name in COHORT_NAMES:
            try:
                metrics, failures = _verify_cohort(
                    root,
                    name,  # type: ignore[arg-type]
                    cohort_payloads.get(name),
                    expected_git_sha=git_sha,
                    expected_source_fingerprint=fingerprint,
                )
                cohorts[name] = metrics
                cohort_failures.extend(f"{name}: {failure}" for failure in failures)
            except EvidenceNotRun as exc:
                cohorts[name] = {"status": "NOT_RUN", "failures": [str(exc)]}
                cohort_failures.append(f"{name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - untrusted evidence must yield a structured fail-closed result
                cohorts[name] = {"status": "FAIL", "failures": [str(exc)]}
                cohort_failures.append(f"{name}: {exc}")

        total = sum(
            int(cohorts.get(name, {}).get("closedBaskets", 0))
            for name in COHORT_NAMES
            if isinstance(cohorts.get(name), dict)
            and cohorts[name].get("status") != "NOT_RUN"
        )
        if total < THRESHOLDS["totalClosedBaskets"]:
            cohort_failures.append(
                "OOS and Shadow must contain at least 50 replay-verified closed baskets in total"
            )
        if any(cohorts.get(name, {}).get("status") == "NOT_RUN" for name in COHORT_NAMES):
            return _result(
                "NOT_RUN",
                cohort_failures,
                bundle_sha256=bundle_hash,
                total_closed_baskets=total,
                cohorts=cohorts,
                source_git_sha=git_sha,
                source_fingerprint=fingerprint,
            )
        if cohort_failures or any(
            cohorts.get(name, {}).get("status") != "PASS" for name in COHORT_NAMES
        ):
            return _result(
                "FAIL",
                cohort_failures,
                bundle_sha256=bundle_hash,
                total_closed_baskets=total,
                cohorts=cohorts,
                source_git_sha=git_sha,
                source_fingerprint=fingerprint,
            )
        return _result(
            "PASS",
            [],
            bundle_sha256=bundle_hash,
            total_closed_baskets=total,
            cohorts=cohorts,
            source_git_sha=git_sha,
            source_fingerprint=fingerprint,
        )
    except EvidenceNotRun as exc:
        return _result("NOT_RUN", [str(exc)], cohorts=cohorts)
    except Exception as exc:  # noqa: BLE001 - CLI boundary must never leak an unstructured traceback
        return _result("FAIL", [str(exc)], cohorts=cohorts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local OOS/Shadow replay verifier with allowlisted Binance public GET verification"
    )
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--bundle-path", required=True)
    parser.add_argument("--expected-git-sha", required=True)
    parser.add_argument("--expected-source-fingerprint", required=True)
    args = parser.parse_args(argv)
    result = verify_promotion_bundle(
        args.evidence_root,
        args.bundle_path,
        args.expected_git_sha,
        args.expected_source_fingerprint,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
