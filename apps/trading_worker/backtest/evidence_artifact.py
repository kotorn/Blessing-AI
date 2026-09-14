"""Verifiable research artifacts for public-data replay.

This module is deliberately outside the execution worker.  It binds a replay
to the exact input events, source archive digests, captured exchange rules,
economic cost model, and source-code SHA.  Verification replays the events and
recomputes the economic result; a caller-supplied positive number is never
trusted as evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .economic import EconomicCostModel, evaluate_trades
from .event_dataset import (
    build_historical_events,
    historical_events_sha256,
    iter_vision_csv_archive,
    parse_funding_rate_record,
    parse_vision_book_ticker_row,
    parse_vision_kline_row,
    parse_vision_mark_price_row,
    sha256_file,
    validate_historical_events,
    verify_vision_archive_checksum,
)
from .replay import (
    HistoricalMarketEvent,
    ReplayExecutionConfig,
    ReplayResult,
    ReplaySymbolRules,
    run_replay,
)

RESEARCH_ARTIFACT_VERSION = "1"
_SOURCE_TYPES = frozenset(
    {"KLINES_1M", "MARK_PRICE_KLINES_1M", "BOOK_TICKER", "FUNDING_RATES"}
)
_ALL_SOURCE_TYPES = _SOURCE_TYPES | {"EXCHANGE_INFO"}
_PUBLIC_SOURCE_HOSTS = frozenset(
    {"data.binance.vision", "fapi.binance.com", "testnet.binancefuture.com"}
)
_SHA256_PATTERN = r"^[0-9a-fA-F]{64}$"
_CODE_SHA_PATTERN = r"^[0-9a-fA-F]{40,64}$"


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value.model_dump(mode="json") if isinstance(value, BaseModel) else value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _utc_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(UTC)


class ResearchSourceRecord(BaseModel):
    """Immutable provenance for one downloaded public source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: str
    url: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64, pattern=_SHA256_PATTERN)
    row_count: int = Field(gt=0)
    start_time: datetime
    end_time: datetime

    @field_validator("source_type")
    @classmethod
    def require_known_source_type(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in _ALL_SOURCE_TYPES:
            raise ValueError(f"unsupported research source type: {normalized}")
        return normalized

    @field_validator("url")
    @classmethod
    def require_public_https_url(cls, value: str) -> str:
        normalized = value.strip()
        parsed = urlparse(normalized)
        if parsed.scheme != "https" or parsed.hostname not in _PUBLIC_SOURCE_HOSTS:
            raise ValueError("research source URL must be an allowed Binance public HTTPS URL")
        return normalized

    @field_validator("sha256")
    @classmethod
    def normalize_sha256(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("start_time", "end_time")
    @classmethod
    def normalize_source_time(cls, value: datetime, info) -> datetime:
        return _utc_datetime(value, info.field_name)

    @model_validator(mode="after")
    def validate_source_window(self) -> ResearchSourceRecord:
        if self.end_time < self.start_time:
            raise ValueError("source end_time must not precede start_time")
        return self


class ResearchDatasetManifest(BaseModel):
    """Binding manifest for a complete, replayable public-data dataset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_version: Literal["1"] = RESEARCH_ARTIFACT_VERSION
    dataset_id: str = Field(min_length=1)
    symbol: str = Field(min_length=2)
    venue: Literal["BINANCE_MAINNET", "BINANCE_TESTNET"]
    market_type: Literal["USDM_FUTURES"] = "USDM_FUTURES"
    interval: Literal["1m"] = "1m"
    code_sha: str = Field(min_length=40, max_length=64, pattern=_CODE_SHA_PATTERN)
    source_records: tuple[ResearchSourceRecord, ...] = Field(min_length=4)
    exchange_info_source: ResearchSourceRecord
    cost_model: EconomicCostModel
    symbol_rules: tuple[ReplaySymbolRules, ...] = Field(min_length=1)
    event_count: int = Field(gt=0)
    start_time: datetime
    end_time: datetime
    dataset_sha256: str = Field(min_length=64, max_length=64, pattern=_SHA256_PATTERN)
    config_sha256: str = Field(min_length=64, max_length=64, pattern=_SHA256_PATTERN)

    @field_validator("dataset_id")
    @classmethod
    def normalize_dataset_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("dataset_id must not be empty")
        return normalized

    @field_validator("symbol")
    @classmethod
    def normalize_manifest_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized.isalnum():
            raise ValueError("symbol must contain only letters and digits")
        return normalized

    @field_validator("code_sha", "dataset_sha256", "config_sha256")
    @classmethod
    def normalize_manifest_hash(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("start_time", "end_time")
    @classmethod
    def normalize_manifest_time(cls, value: datetime, info) -> datetime:
        return _utc_datetime(value, info.field_name)

    @model_validator(mode="after")
    def validate_manifest_bindings(self) -> ResearchDatasetManifest:
        if self.end_time < self.start_time:
            raise ValueError("dataset end_time must not precede start_time")
        source_types = [record.source_type for record in self.source_records]
        if len(set(source_types)) != len(source_types) or set(source_types) != _SOURCE_TYPES:
            raise ValueError("manifest must contain exactly one record for every required source")
        if self.exchange_info_source.source_type != "EXCHANGE_INFO":
            raise ValueError("manifest exchange_info_source must be an EXCHANGE_INFO record")
        rules = [rule.symbol for rule in self.symbol_rules]
        if self.symbol not in rules:
            raise ValueError("manifest is missing exchange rules for its symbol")
        if any(record.start_time > self.end_time or record.end_time < self.start_time for record in self.source_records):
            raise ValueError("source coverage does not overlap the dataset window")
        if (
            self.exchange_info_source.start_time > self.end_time
            or self.exchange_info_source.end_time < self.start_time
        ):
            raise ValueError("exchange-info source does not overlap the dataset window")
        return self


class ReplayEvidenceArtifact(BaseModel):
    """Self-contained replay output with a verifiable content hash."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_version: Literal["1"] = RESEARCH_ARTIFACT_VERSION
    artifact_sha256: str = Field(min_length=64, max_length=64, pattern=_SHA256_PATTERN)
    manifest_sha256: str = Field(min_length=64, max_length=64, pattern=_SHA256_PATTERN)
    manifest: ResearchDatasetManifest
    config: ReplayExecutionConfig
    events: tuple[HistoricalMarketEvent, ...] = Field(min_length=1)
    replay: ReplayResult
    verification_status: Literal["VERIFIED_REPLAY"] = "VERIFIED_REPLAY"
    evidence_status: Literal["RESEARCH_ONLY"] = "RESEARCH_ONLY"
    launch_eligible: Literal[False] = False


def _artifact_hash_payload(artifact: ReplayEvidenceArtifact) -> dict[str, Any]:
    payload = artifact.model_dump(mode="json")
    payload["artifact_sha256"] = "0" * 64
    return payload


def _source_record(
    source_type: str,
    *,
    url: str,
    path: str | Path,
    row_count: int,
    observations: Sequence[Any],
    time_field: str = "event_time",
) -> ResearchSourceRecord:
    times = [getattr(observation, time_field) for observation in observations]
    if not times:
        raise ValueError(f"{source_type} source contains no observations")
    return ResearchSourceRecord(
        source_type=source_type,
        url=url,
        sha256=sha256_file(path),
        row_count=row_count,
        start_time=min(times),
        end_time=max(times),
    )


def _parsed_rows(rows: Sequence[Sequence[Any]], parser: Any) -> list[Any]:
    parsed: list[Any] = []
    for row in rows:
        observation = parser(row)
        if observation is not None:
            parsed.append(observation)
    return parsed


def _exchange_decimal(
    filter_data: Mapping[str, Any], names: Sequence[str], label: str
) -> Decimal:
    raw = next(
        (filter_data.get(name) for name in names if filter_data.get(name) not in (None, "")),
        None,
    )
    if raw is None:
        raise ValueError(f"exchange-info {label} is missing")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"exchange-info {label} is invalid") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"exchange-info {label} must be finite and positive")
    return value


def _exchange_bool(
    filter_data: Mapping[str, Any], name: str, *, default: bool = True
) -> bool:
    raw = filter_data.get(name)
    if raw in (None, ""):
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"exchange-info {name} is invalid")


def parse_replay_symbol_rules_from_exchange_info(
    payload: Mapping[str, Any], symbol: str
) -> ReplaySymbolRules:
    """Convert a captured exchangeInfo symbol into the replay rule subset."""

    if not isinstance(payload, Mapping) or not isinstance(payload.get("symbols"), list):
        raise TypeError("exchange-info payload must contain a symbols list")
    normalized_symbol = str(symbol).strip().upper()
    matches = [
        item
        for item in payload["symbols"]
        if isinstance(item, Mapping)
        and str(item.get("symbol", "")).strip().upper() == normalized_symbol
    ]
    if len(matches) != 1:
        raise ValueError("exchange-info must contain exactly one requested symbol")
    symbol_data = matches[0]
    if str(symbol_data.get("status", "")).strip().upper() != "TRADING":
        raise ValueError("exchange-info symbol is not TRADING")
    order_types = {
        str(order_type).strip().upper()
        for order_type in symbol_data.get("orderTypes", [])
    }
    if "MARKET" not in order_types:
        raise ValueError("exchange-info symbol does not support MARKET orders")
    filters = symbol_data.get("filters")
    if not isinstance(filters, list):
        raise TypeError("exchange-info symbol filters must be a list")
    by_type: dict[str, Mapping[str, Any]] = {}
    for item in filters:
        if not isinstance(item, Mapping):
            raise TypeError("exchange-info symbol filter must be an object")
        filter_type = str(item.get("filterType", "")).strip().upper()
        if filter_type:
            by_type[filter_type] = item

    price_filter = by_type.get("PRICE_FILTER")
    lot_filter = by_type.get("LOT_SIZE")
    if price_filter is None or lot_filter is None:
        raise ValueError("exchange-info is missing PRICE_FILTER or LOT_SIZE")
    tick_size = _exchange_decimal(price_filter, ("tickSize",), "tickSize")
    lot_min = _exchange_decimal(lot_filter, ("minQty",), "LOT_SIZE minQty")
    lot_max = _exchange_decimal(lot_filter, ("maxQty",), "LOT_SIZE maxQty")
    lot_step = _exchange_decimal(lot_filter, ("stepSize",), "LOT_SIZE stepSize")

    market_filter = by_type.get("MARKET_LOT_SIZE")
    min_quantity = lot_min
    max_quantity = lot_max
    if market_filter is not None:
        market_min = _exchange_decimal(market_filter, ("minQty",), "MARKET_LOT_SIZE minQty")
        market_max = _exchange_decimal(market_filter, ("maxQty",), "MARKET_LOT_SIZE maxQty")
        market_step = _exchange_decimal(
            market_filter, ("stepSize",), "MARKET_LOT_SIZE stepSize"
        )
        if market_step != lot_step:
            raise ValueError("LOT_SIZE and MARKET_LOT_SIZE step sizes differ")
        min_quantity = max(lot_min, market_min)
        max_quantity = min(lot_max, market_max)

    minimum_notionals: list[Decimal] = []
    maximum_notionals: list[Decimal] = []
    min_filter = by_type.get("MIN_NOTIONAL")
    if min_filter is not None and _exchange_bool(min_filter, "applyToMarket"):
        minimum_notionals.append(_exchange_decimal(min_filter, ("notional", "minNotional"), "MIN_NOTIONAL"))
    notional_filter = by_type.get("NOTIONAL")
    if notional_filter is not None:
        if _exchange_bool(notional_filter, "applyMinToMarket"):
            minimum_notionals.append(
                _exchange_decimal(notional_filter, ("minNotional", "notional"), "NOTIONAL minNotional")
            )
        if _exchange_bool(notional_filter, "applyMaxToMarket"):
            maximum_notionals.append(
                _exchange_decimal(notional_filter, ("maxNotional",), "NOTIONAL maxNotional")
            )
    if not minimum_notionals:
        raise ValueError("exchange-info is missing a market minimum notional")
    max_notional = min(maximum_notionals) if maximum_notionals else None
    return ReplaySymbolRules(
        symbol=normalized_symbol,
        tick_size=tick_size,
        step_size=lot_step,
        min_quantity=min_quantity,
        max_quantity=max_quantity,
        min_notional=max(minimum_notionals),
        max_notional=max_notional,
    )


def build_exchange_info_source(
    path: str | Path,
    *,
    url: str,
    start_time: datetime,
    end_time: datetime,
) -> ResearchSourceRecord:
    """Create a hash-bound exchange-info provenance record."""

    return ResearchSourceRecord(
        source_type="EXCHANGE_INFO",
        url=url,
        sha256=sha256_file(path),
        row_count=1,
        start_time=start_time,
        end_time=end_time,
    )


def build_replay_evidence_artifact(
    events: Sequence[HistoricalMarketEvent],
    config: ReplayExecutionConfig,
    *,
    manifest: ResearchDatasetManifest,
) -> ReplayEvidenceArtifact:
    """Run and bind a deterministic replay to an immutable manifest."""

    normalized_events = validate_historical_events(events)
    if not config.force_close_at_end:
        raise ValueError("research evidence requires an explicit end-of-sample close")
    if any(event.symbol != manifest.symbol for event in normalized_events):
        raise ValueError("manifest symbol does not match replay events")
    expected_source = (
        "BINANCE_PUBLIC_TESTNET_READ_ONLY"
        if manifest.venue == "BINANCE_TESTNET"
        else "BINANCE_PUBLIC_MAINNET_READ_ONLY"
    )
    if any(event.venue != manifest.venue or event.data_source != expected_source for event in normalized_events):
        raise ValueError("manifest venue/data source does not match replay events")
    if config.rules_for(manifest.symbol).symbol != manifest.symbol:
        raise ValueError("config does not contain the manifest symbol rules")
    if tuple(config.symbol_rules) != tuple(manifest.symbol_rules):
        raise ValueError("manifest exchange rules do not match replay config")

    replay = run_replay(normalized_events, config)
    if replay.open_position_at_end != 0:
        raise ValueError("replay evidence must end flat")
    dataset_hash = historical_events_sha256(normalized_events)
    if replay.dataset_sha256 != dataset_hash:
        raise ValueError("replay dataset hash does not match canonical event hash")
    updated_manifest = manifest.model_copy(
        update={
            "event_count": len(normalized_events),
            "start_time": normalized_events[0].event_time,
            "end_time": normalized_events[-1].event_time,
            "dataset_sha256": dataset_hash,
            "config_sha256": replay.config_sha256,
        }
    )
    manifest_hash = _canonical_hash(updated_manifest)
    artifact = ReplayEvidenceArtifact(
        artifact_sha256="0" * 64,
        manifest_sha256=manifest_hash,
        manifest=updated_manifest,
        config=config,
        events=normalized_events,
        replay=replay,
    )
    return artifact.model_copy(update={"artifact_sha256": _canonical_hash(_artifact_hash_payload(artifact))})


def verify_replay_evidence_artifact(
    artifact: ReplayEvidenceArtifact | Mapping[str, Any] | str | Path,
) -> ReplayEvidenceArtifact:
    """Re-read, replay, and independently verify an evidence artifact."""

    if isinstance(artifact, (str, Path)):
        payload = json.loads(Path(artifact).read_text(encoding="utf-8"))
        candidate = ReplayEvidenceArtifact.model_validate(payload)
    elif isinstance(artifact, ReplayEvidenceArtifact):
        candidate = artifact
    else:
        candidate = ReplayEvidenceArtifact.model_validate(artifact)

    expected_artifact_hash = _canonical_hash(_artifact_hash_payload(candidate))
    if candidate.artifact_sha256.lower() != expected_artifact_hash:
        raise ValueError("replay evidence artifact SHA-256 is invalid")
    if candidate.manifest_sha256 != _canonical_hash(candidate.manifest):
        raise ValueError("replay evidence manifest SHA-256 is invalid")

    events = validate_historical_events(candidate.events)
    if len(events) != candidate.manifest.event_count:
        raise ValueError("manifest event_count does not match embedded events")
    event_hash = historical_events_sha256(events)
    if event_hash != candidate.manifest.dataset_sha256:
        raise ValueError("manifest dataset SHA-256 is invalid")
    if event_hash != candidate.replay.dataset_sha256:
        raise ValueError("replay dataset SHA-256 is invalid")
    if candidate.replay.config_sha256 != _canonical_hash(candidate.config):
        raise ValueError("replay config SHA-256 is invalid")
    if candidate.manifest.config_sha256 != candidate.replay.config_sha256:
        raise ValueError("manifest config SHA-256 is invalid")
    if candidate.config.cost_model != candidate.manifest.cost_model:
        raise ValueError("manifest cost model does not match replay config")
    if tuple(candidate.config.symbol_rules) != tuple(candidate.manifest.symbol_rules):
        raise ValueError("manifest exchange rules do not match replay config")

    expected_replay = run_replay(events, candidate.config)
    # ExchangeFill intentionally keeps event timestamps as ``Any`` for
    # compatibility with the live ledger.  Compare canonical JSON rather
    # than Python object identity so an artifact round trip is equivalent.
    if _canonical_hash(expected_replay) != _canonical_hash(candidate.replay):
        raise ValueError("embedded replay result does not reproduce from events and config")
    if candidate.replay.economic_result is not None:
        recomputed_economics = evaluate_trades(
            candidate.replay.trades,
            initial_capital=candidate.config.initial_capital,
            cost_model=candidate.config.cost_model,
        )
        if recomputed_economics != candidate.replay.economic_result:
            raise ValueError("embedded net economics do not reproduce independently")
    if candidate.replay.open_position_at_end != 0:
        raise ValueError("verified replay evidence must end flat")
    return candidate


def write_replay_evidence_artifact(
    artifact: ReplayEvidenceArtifact, path: str | Path
) -> str:
    """Verify and write an artifact, returning its content hash."""

    verified = verify_replay_evidence_artifact(artifact)
    output_path = Path(path)
    output_path.write_text(
        json.dumps(verified.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return verified.artifact_sha256


def load_vision_replay_inputs(
    *,
    symbol: str,
    venue: Literal["BINANCE_MAINNET", "BINANCE_TESTNET"],
    kline_archive: str | Path,
    mark_price_archive: str | Path,
    book_ticker_archive: str | Path,
    funding_json: str | Path,
    kline_url: str,
    mark_price_url: str,
    book_ticker_url: str,
    funding_url: str,
    kline_checksum: str | Path,
    mark_price_checksum: str | Path,
    book_ticker_checksum: str | Path,
    max_book_age_sec: float = 60.0,
    funding_tolerance_sec: float = 60.0,
) -> tuple[tuple[HistoricalMarketEvent, ...], tuple[ResearchSourceRecord, ...]]:
    """Load checked local Vision archives and assemble causal replay events.

    The function performs no download.  Archives and the funding response
    must be acquired separately from allowed public Binance endpoints, and
    each archive must be paired with its published ``.CHECKSUM`` file.
    """

    verify_vision_archive_checksum(kline_archive, kline_checksum)
    verify_vision_archive_checksum(mark_price_archive, mark_price_checksum)
    verify_vision_archive_checksum(book_ticker_archive, book_ticker_checksum)
    kline_rows = list(iter_vision_csv_archive(kline_archive))
    mark_rows = list(iter_vision_csv_archive(mark_price_archive))
    kline_observations = _parsed_rows(kline_rows, parse_vision_kline_row)
    mark_observations = _parsed_rows(mark_rows, parse_vision_mark_price_row)
    try:
        book_age_sec = float(max_book_age_sec)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_book_age_sec must be finite and positive") from exc
    if not math.isfinite(book_age_sec) or not book_age_sec > 0:
        raise ValueError("max_book_age_sec must be finite and positive")
    if not kline_observations:
        raise ValueError("kline source contains no data rows")
    # The daily bookTicker archive can be multi-gigabyte and is not
    # guaranteed to be ordered.  Stream it once and retain only observations
    # that can causally serve this kline window.  Sorting remains delegated to
    # build_historical_events after this bounded filter.
    book_start = kline_observations[0].event_time - timedelta(seconds=book_age_sec)
    book_end = kline_observations[-1].event_time
    book_rows: list[list[str]] = []
    book_observations: list[Any] = []
    book_row_count = 0
    for row in iter_vision_csv_archive(book_ticker_archive):
        observation = parse_vision_book_ticker_row(row)
        if observation is None:
            continue
        book_row_count += 1
        if book_start <= observation.event_time <= book_end:
            book_rows.append(row)
            book_observations.append(observation)
    try:
        funding_payload = json.loads(Path(funding_json).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("funding response is not valid JSON") from exc
    records = funding_payload.get("data") if isinstance(funding_payload, dict) else funding_payload
    if not isinstance(records, list) or not records:
        raise ValueError("funding response must contain a non-empty list")
    funding_observations = [
        parse_funding_rate_record(record, expected_symbol=symbol)
        for record in records
        if isinstance(record, Mapping)
    ]
    if len(funding_observations) != len(records):
        raise ValueError("funding response contains a non-object record")
    data_source = (
        "BINANCE_PUBLIC_TESTNET_READ_ONLY"
        if venue == "BINANCE_TESTNET"
        else "BINANCE_PUBLIC_MAINNET_READ_ONLY"
    )
    events = build_historical_events(
        kline_rows,
        mark_rows,
        book_rows,
        funding_observations,
        symbol=symbol,
        venue=venue,
        data_source=data_source,
        max_book_age_sec=max_book_age_sec,
        funding_tolerance_sec=funding_tolerance_sec,
    )
    sources = (
        _source_record(
            "KLINES_1M",
            url=kline_url,
            path=kline_archive,
            row_count=len(kline_observations),
            observations=kline_observations,
        ),
        _source_record(
            "MARK_PRICE_KLINES_1M",
            url=mark_price_url,
            path=mark_price_archive,
            row_count=len(mark_observations),
            observations=mark_observations,
        ),
        _source_record(
            "BOOK_TICKER",
            url=book_ticker_url,
            path=book_ticker_archive,
            row_count=book_row_count,
            observations=book_observations,
        ),
        _source_record(
            "FUNDING_RATES",
            url=funding_url,
            path=funding_json,
            row_count=len(funding_observations),
            observations=funding_observations,
            time_field="funding_time",
        ),
    )
    return events, sources


def build_manifest(
    *,
    dataset_id: str,
    symbol: str,
    venue: Literal["BINANCE_MAINNET", "BINANCE_TESTNET"],
    code_sha: str,
    source_records: Sequence[ResearchSourceRecord],
    exchange_info_source: ResearchSourceRecord,
    cost_model: EconomicCostModel,
    symbol_rules: Sequence[ReplaySymbolRules],
) -> ResearchDatasetManifest:
    """Create the pre-replay manifest; hashes and window are bound by builder."""

    return ResearchDatasetManifest(
        dataset_id=dataset_id,
        symbol=symbol,
        venue=venue,
        code_sha=code_sha,
        source_records=tuple(source_records),
        exchange_info_source=exchange_info_source,
        cost_model=cost_model,
        symbol_rules=tuple(symbol_rules),
        event_count=1,
        start_time=source_records[0].start_time,
        end_time=source_records[0].end_time,
        dataset_sha256="0" * 64,
        config_sha256="0" * 64,
    )
