"""Build replay-ready events from independent Binance public-data observations.

This module is research-only.  A kline close is not a tradable quote, so the
assembler requires a separate mark-price kline and a real historical
book-ticker observation for every replay event.  Funding observations are
attached by event time with an explicit tolerance.  Missing, stale, future,
or conflicting observations fail closed; no price or liquidity field is
invented.

The module does not contain an exchange client and never authorizes execution.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import zipfile
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .replay import HistoricalMarketEvent


def _normalise_symbol(value: Any) -> str:
    symbol = str(value).strip().upper()
    if not symbol or not symbol.isalnum():
        raise ValueError("symbol must contain only letters and digits")
    return symbol


def _epoch_to_datetime(value: Any, field_name: str) -> tuple[int, datetime]:
    """Parse integer epoch milliseconds or microseconds without float rounding."""

    try:
        numeric = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer epoch timestamp") from exc
    if not numeric.is_finite() or numeric <= 0 or numeric != numeric.to_integral_value():
        raise ValueError(f"{field_name} must be a positive integer epoch timestamp")
    raw = int(numeric)
    # Binance public archives use milliseconds for USD-M futures; accepting
    # microseconds keeps the provenance parser explicit across archive
    # generations without converting a timestamp through a binary float.
    divisor = 1_000_000 if raw >= 100_000_000_000_000 else 1_000
    seconds, remainder = divmod(raw, divisor)
    microseconds = remainder if divisor == 1_000_000 else remainder * 1_000
    try:
        timestamp = datetime.fromtimestamp(seconds, tz=UTC).replace(
            microsecond=microseconds
        )
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError(f"{field_name} is outside the supported datetime range") from exc
    return raw, timestamp


def _decimal(value: Any, field_name: str, *, positive: bool = True) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be Decimal-compatible") from exc
    if not parsed.is_finite() or (parsed <= 0 if positive else parsed < 0):
        qualifier = "positive" if positive else "finite and non-negative"
        raise ValueError(f"{field_name} must be {qualifier}")
    return parsed


def _integer(value: Any, field_name: str, *, minimum: int = 0) -> int:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc
    if not parsed.is_finite() or parsed != parsed.to_integral_value():
        raise ValueError(f"{field_name} must be an integer")
    result = int(parsed)
    if result < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    return result


def _signed_decimal(value: Any, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be Decimal-compatible") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return parsed


class KlineObservation(BaseModel):
    """Closed USD-M kline observation from Binance public data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    open_time_ms: int = Field(gt=0)
    close_time_ms: int = Field(gt=0)
    event_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trade_count: int = Field(ge=0)

    @field_validator("event_time")
    @classmethod
    def require_utc_event_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("event_time must include timezone information")
        return value.astimezone(UTC)

    @field_validator("open", "high", "low", "close")
    @classmethod
    def require_positive_prices(cls, value: Decimal) -> Decimal:
        return _decimal(value, "price")

    @field_validator("volume")
    @classmethod
    def require_nonnegative_volume(cls, value: Decimal) -> Decimal:
        return _decimal(value, "volume", positive=False)

    @model_validator(mode="after")
    def validate_ohlc(self) -> KlineObservation:
        if self.close_time_ms < self.open_time_ms:
            raise ValueError("close_time_ms must not precede open_time_ms")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("kline OHLC relationship is invalid")
        if self.high < self.low:
            raise ValueError("kline high must be greater than or equal to low")
        return self


class MarkPriceObservation(BaseModel):
    """Mark-price kline close aligned to a source kline open timestamp."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    open_time_ms: int = Field(gt=0)
    event_time: datetime
    mark_price: Decimal

    @field_validator("event_time")
    @classmethod
    def require_utc_event_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("event_time must include timezone information")
        return value.astimezone(UTC)

    @field_validator("mark_price")
    @classmethod
    def require_positive_mark(cls, value: Decimal) -> Decimal:
        return _decimal(value, "mark_price")


class BookTickerObservation(BaseModel):
    """One historical top-of-book observation from Binance bookTicker data."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    update_id: int = Field(ge=0)
    best_bid: Decimal
    bid_qty: Decimal
    best_ask: Decimal
    ask_qty: Decimal
    transaction_time: datetime
    event_time: datetime

    @field_validator("best_bid", "bid_qty", "best_ask", "ask_qty")
    @classmethod
    def require_positive_book_values(cls, value: Decimal) -> Decimal:
        return _decimal(value, "book value")

    @field_validator("transaction_time", "event_time")
    @classmethod
    def require_utc_book_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("book timestamps must include timezone information")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_book(self) -> BookTickerObservation:
        if self.best_bid > self.best_ask:
            raise ValueError("book best bid must be less than or equal to best ask")
        return self


class FundingRateObservation(BaseModel):
    """A funding settlement observation from the public funding-rate endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    funding_time: datetime
    funding_rate: Decimal

    @field_validator("symbol")
    @classmethod
    def normalize_funding_symbol(cls, value: str) -> str:
        return _normalise_symbol(value)

    @field_validator("funding_time")
    @classmethod
    def require_utc_funding_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("funding_time must include timezone information")
        return value.astimezone(UTC)

    @field_validator("funding_rate")
    @classmethod
    def require_finite_funding_rate(cls, value: Decimal) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("funding_rate must be Decimal-compatible") from exc
        if not parsed.is_finite():
            raise ValueError("funding_rate must be finite")
        return parsed


def _header_row(row: Sequence[Any], *names: str) -> bool:
    if not isinstance(row, (list, tuple)) or not row:
        return False
    first = str(row[0]).strip().lower().replace(" ", "_")
    return first in {name.lower().replace(" ", "_") for name in names}


def parse_vision_kline_row(row: Sequence[Any]) -> KlineObservation | None:
    """Parse a Binance Vision USD-M 1m kline row; return None for its header."""

    if _header_row(row, "open_time", "open time"):
        return None
    if not isinstance(row, (list, tuple)) or len(row) < 9:
        raise ValueError("Binance Vision kline row is malformed")
    open_time_ms, _ = _epoch_to_datetime(row[0], "open_time")
    close_time_ms, event_time = _epoch_to_datetime(row[6], "close_time")
    return KlineObservation(
        open_time_ms=open_time_ms,
        close_time_ms=close_time_ms,
        event_time=event_time,
        open=_decimal(row[1], "open"),
        high=_decimal(row[2], "high"),
        low=_decimal(row[3], "low"),
        close=_decimal(row[4], "close"),
        volume=_decimal(row[5], "volume", positive=False),
        trade_count=_integer(row[8], "trade_count", minimum=0),
    )


def parse_vision_mark_price_row(row: Sequence[Any]) -> MarkPriceObservation | None:
    """Parse a Binance Vision markPriceKlines row using its close as the mark."""

    if _header_row(row, "open_time", "open time"):
        return None
    if not isinstance(row, (list, tuple)) or len(row) < 7:
        raise ValueError("Binance Vision mark-price row is malformed")
    open_time_ms, _ = _epoch_to_datetime(row[0], "mark_open_time")
    close_time_ms, event_time = _epoch_to_datetime(row[6], "mark_close_time")
    if close_time_ms < open_time_ms:
        raise ValueError("mark-price close time must not precede open time")
    return MarkPriceObservation(
        open_time_ms=open_time_ms,
        event_time=event_time,
        mark_price=_decimal(row[4], "mark_price"),
    )


def parse_vision_book_ticker_row(
    row: Sequence[Any],
) -> BookTickerObservation | None:
    """Parse the seven-column Vision bookTicker CSV row.

    Binance's public archive has historically contained out-of-order rows;
    callers must sort the resulting observations by event time and update ID
    before using them for a causal join.
    """

    if _header_row(row, "update_id", "update id"):
        return None
    if not isinstance(row, (list, tuple)) or len(row) < 7:
        raise ValueError("Binance Vision bookTicker row is malformed")
    _, transaction_time = _epoch_to_datetime(row[5], "transaction_time")
    _, event_time = _epoch_to_datetime(row[6], "event_time")
    return BookTickerObservation(
        update_id=_integer(row[0], "update_id", minimum=0),
        best_bid=_decimal(row[1], "best_bid"),
        bid_qty=_decimal(row[2], "bid_qty"),
        best_ask=_decimal(row[3], "best_ask"),
        ask_qty=_decimal(row[4], "ask_qty"),
        transaction_time=transaction_time,
        event_time=event_time,
    )


def parse_funding_rate_record(
    record: Mapping[str, Any], *, expected_symbol: str
) -> FundingRateObservation:
    """Parse one public ``fundingRate`` response without accepting omissions."""

    raw_symbol = record.get("symbol")
    if raw_symbol in (None, ""):
        raise ValueError("funding-rate record is missing symbol")
    symbol = _normalise_symbol(raw_symbol)
    expected = _normalise_symbol(expected_symbol)
    if symbol != expected:
        raise ValueError("funding-rate symbol does not match the dataset symbol")
    _, funding_time = _epoch_to_datetime(record.get("fundingTime"), "fundingTime")
    return FundingRateObservation(
        symbol=symbol,
        funding_time=funding_time,
        funding_rate=_signed_decimal(record.get("fundingRate"), "fundingRate"),
    )


def iter_vision_csv_archive(archive_path: str | Path) -> Iterable[list[str]]:
    """Yield rows from a single-file Binance Vision ZIP without trusting paths."""

    path = Path(archive_path)
    with zipfile.ZipFile(path) as archive:
        members = [
            info
            for info in archive.infolist()
            if not info.is_dir() and not info.filename.endswith("/")
        ]
        if len(members) != 1:
            raise ValueError("Binance Vision archive must contain exactly one data file")
        member = members[0]
        if Path(member.filename).name != member.filename:
            raise ValueError("Binance Vision archive contains an unsafe member path")
        with archive.open(member, "r") as raw_file:
            import io

            with io.TextIOWrapper(raw_file, encoding="utf-8", newline="") as text_file:
                yield from csv.reader(text_file)


def sha256_file(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    """Hash a downloaded archive without loading it into memory."""

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    digest = hashlib.sha256()
    with Path(path).open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256_checksum(
    archive_path: str | Path, expected_sha256: str
) -> str:
    """Verify one archive against a SHA-256 digest from its source manifest."""

    match = re.fullmatch(r"\s*([0-9a-fA-F]{64})\s*", str(expected_sha256))
    if match is None:
        raise ValueError("expected_sha256 must be exactly 64 hexadecimal characters")
    expected = match.group(1).lower()
    actual = sha256_file(archive_path)
    if actual != expected:
        raise ValueError("archive SHA-256 does not match the source manifest")
    return actual


def verify_vision_archive_checksum(
    archive_path: str | Path, checksum_path: str | Path
) -> str:
    """Verify a Binance Vision archive using its downloaded ``.CHECKSUM`` file."""

    checksum_text = Path(checksum_path).read_text(encoding="utf-8")
    match = re.search(r"(?i)(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", checksum_text)
    if match is None:
        raise ValueError("checksum file does not contain one SHA-256 digest")
    return verify_sha256_checksum(archive_path, match.group(0))


def _validate_options(max_book_age_sec: float, funding_tolerance_sec: float) -> None:
    for name, value in (
        ("max_book_age_sec", max_book_age_sec),
        ("funding_tolerance_sec", funding_tolerance_sec),
    ):
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be finite and positive") from exc
        if not math.isfinite(parsed) or parsed <= 0:
            raise ValueError(f"{name} must be finite and positive")


def _unique_klines(rows: Sequence[Sequence[Any]]) -> list[KlineObservation]:
    by_open_time: dict[int, KlineObservation] = {}
    for row in rows:
        parsed = parse_vision_kline_row(row)
        if parsed is None:
            continue
        previous = by_open_time.get(parsed.open_time_ms)
        if previous is not None and previous != parsed:
            raise ValueError("conflicting duplicate kline observations")
        by_open_time[parsed.open_time_ms] = parsed
    result = sorted(by_open_time.values(), key=lambda item: item.open_time_ms)
    if not result:
        raise ValueError("kline source contains no data rows")
    for previous, current in pairwise(result):
        if current.open_time_ms - previous.open_time_ms != 60_000:
            raise ValueError("kline source contains a missing or non-1m interval")
    return result


def _unique_mark_prices(rows: Sequence[Sequence[Any]]) -> dict[int, MarkPriceObservation]:
    by_open_time: dict[int, MarkPriceObservation] = {}
    for row in rows:
        parsed = parse_vision_mark_price_row(row)
        if parsed is None:
            continue
        previous = by_open_time.get(parsed.open_time_ms)
        if previous is not None and previous != parsed:
            raise ValueError("conflicting duplicate mark-price observations")
        by_open_time[parsed.open_time_ms] = parsed
    if not by_open_time:
        raise ValueError("mark-price source contains no data rows")
    return by_open_time


def _sorted_book_tickers(rows: Sequence[Sequence[Any]]) -> list[BookTickerObservation]:
    by_update_id: dict[int, BookTickerObservation] = {}
    for row in rows:
        parsed = parse_vision_book_ticker_row(row)
        if parsed is None:
            continue
        previous = by_update_id.get(parsed.update_id)
        if previous is not None and previous != parsed:
            raise ValueError("conflicting duplicate bookTicker update ID")
        by_update_id[parsed.update_id] = parsed
    result = sorted(by_update_id.values(), key=lambda item: (item.event_time, item.update_id))
    if not result:
        raise ValueError("bookTicker source contains no data rows")
    return result


def _attach_funding(
    events: Sequence[KlineObservation],
    funding_rates: Sequence[FundingRateObservation],
    *,
    symbol: str,
    tolerance_sec: float,
) -> dict[int, FundingRateObservation]:
    event_times = [event.event_time for event in events]
    attached: dict[int, FundingRateObservation] = {}
    tolerance = float(tolerance_sec)
    for funding in sorted(funding_rates, key=lambda item: item.funding_time):
        if funding.symbol != symbol:
            raise ValueError("funding-rate symbol does not match the dataset symbol")
        index = bisect_left(event_times, funding.funding_time)
        if index >= len(event_times):
            raise ValueError("funding settlement is after the event dataset")
        age = (event_times[index] - funding.funding_time).total_seconds()
        if age < 0 or age > tolerance:
            raise ValueError("funding settlement cannot be causally aligned to an event")
        if index in attached:
            raise ValueError("multiple funding settlements map to one replay event")
        attached[index] = funding
    return attached


def validate_historical_events(events: Sequence[HistoricalMarketEvent]) -> tuple[HistoricalMarketEvent, ...]:
    """Validate deterministic order and unique IDs before hashing or writing."""

    normalized = tuple(events)
    if not normalized:
        raise ValueError("historical event dataset must not be empty")
    first = normalized[0]
    ids: set[str] = set()
    for previous, current in pairwise(normalized):
        if current.event_time <= previous.event_time:
            raise ValueError("historical events must be strictly chronological")
        if (current.event_time - previous.event_time).total_seconds() != 60:
            raise ValueError(
                "historical event timestamps must be contiguous 1-minute intervals"
            )
    for event in normalized:
        if (
            event.symbol != first.symbol
            or event.venue != first.venue
            or event.market_type != first.market_type
            or event.data_source != first.data_source
        ):
            raise ValueError("historical events must use one symbol, venue, and data source")
        if event.event_id in ids:
            raise ValueError(f"duplicate historical event_id: {event.event_id}")
        ids.add(event.event_id)
    return normalized


def build_historical_events(
    kline_rows: Sequence[Sequence[Any]],
    mark_price_rows: Sequence[Sequence[Any]],
    book_ticker_rows: Sequence[Sequence[Any]],
    funding_rates: Sequence[FundingRateObservation] = (),
    *,
    symbol: str,
    venue: str = "BINANCE_MAINNET",
    data_source: str = "BINANCE_PUBLIC_MAINNET_READ_ONLY",
    max_book_age_sec: float = 60.0,
    funding_tolerance_sec: float = 60.0,
) -> tuple[HistoricalMarketEvent, ...]:
    """Causally join real public observations into replay events.

    The event timestamp is the closed kline's close time.  The selected quote
    is the latest bookTicker observation at or before that timestamp, and it
    must not exceed ``max_book_age_sec``.  Mark prices are joined by the exact
    kline open timestamp.  Funding is attached to the first event at or after
    the published settlement time within the explicit tolerance.
    """

    _validate_options(max_book_age_sec, funding_tolerance_sec)
    normalized_symbol = _normalise_symbol(symbol)
    klines = _unique_klines(kline_rows)
    marks = _unique_mark_prices(mark_price_rows)
    books = _sorted_book_tickers(book_ticker_rows)
    funding_by_index = _attach_funding(
        klines,
        funding_rates,
        symbol=normalized_symbol,
        tolerance_sec=funding_tolerance_sec,
    )
    book_times = [item.event_time for item in books]
    built: list[HistoricalMarketEvent] = []
    for index, kline in enumerate(klines):
        mark = marks.get(kline.open_time_ms)
        if mark is None:
            raise ValueError(
                f"missing mark-price observation for kline {kline.open_time_ms}"
            )
        if mark.event_time > kline.event_time:
            raise ValueError("mark-price observation is after its kline event")
        book_index = bisect_right(book_times, kline.event_time) - 1
        if book_index < 0:
            raise ValueError("bookTicker has no observation at or before the first event")
        book = books[book_index]
        book_age = (kline.event_time - book.event_time).total_seconds()
        if book_age < 0 or book_age > float(max_book_age_sec):
            raise ValueError(
                f"bookTicker observation is stale for kline {kline.open_time_ms}"
            )
        funding = funding_by_index.get(index)
        built.append(
            HistoricalMarketEvent(
                event_id=f"{normalized_symbol}-1m-{kline.open_time_ms}",
                event_time=kline.event_time,
                symbol=normalized_symbol,
                venue=venue,
                market_type="USDM_FUTURES",
                open=kline.open,
                high=kline.high,
                low=kline.low,
                close=kline.close,
                volume=kline.volume,
                trade_count=kline.trade_count,
                best_bid=book.best_bid,
                best_ask=book.best_ask,
                bid_qty=book.bid_qty,
                ask_qty=book.ask_qty,
                mark_price=mark.mark_price,
                funding_rate=funding.funding_rate if funding else None,
                funding_event=funding is not None,
                data_source=data_source,
            )
        )
    return validate_historical_events(built)


def historical_events_sha256(events: Sequence[HistoricalMarketEvent]) -> str:
    """Return a stable content hash for the exact replay input rows."""

    normalized = validate_historical_events(events)
    payload = json.dumps(
        [event.model_dump(mode="json") for event in normalized],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def write_historical_events_jsonl(
    events: Sequence[HistoricalMarketEvent], path: str | Path
) -> str:
    """Write exact replay events and return their content hash.

    JSONL is deliberately used as an interchange format here so Decimal
    values remain strings and a later Parquet conversion cannot silently
    reduce their precision.  The returned hash is the value to record in a
    research manifest.
    """

    normalized = validate_historical_events(events)
    output_path = Path(path)
    with output_path.open("w", encoding="utf-8", newline="\n") as output_file:
        for event in normalized:
            output_file.write(
                json.dumps(
                    event.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    return historical_events_sha256(normalized)


def read_historical_events_jsonl(path: str | Path) -> tuple[HistoricalMarketEvent, ...]:
    """Read and revalidate a JSONL replay dataset without accepting extras."""

    events: list[HistoricalMarketEvent] = []
    with Path(path).open("r", encoding="utf-8") as input_file:
        for line_number, line in enumerate(input_file, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL event at line {line_number}") from exc
            events.append(HistoricalMarketEvent.model_validate(payload))
    return validate_historical_events(events)
