from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256

import pytest

from apps.trading_worker.backtest.event_dataset import (
    FundingRateObservation,
    build_historical_events,
    historical_events_sha256,
    parse_funding_rate_record,
    parse_vision_book_ticker_row,
    read_historical_events_jsonl,
    sha256_file,
    validate_historical_events,
    verify_sha256_checksum,
    verify_vision_archive_checksum,
    write_historical_events_jsonl,
)
from domain.enums import MarketType

START_MS = 1_700_000_000_000


def _kline(index: int, *, trade_count: int = 10):
    open_time = START_MS + index * 60_000
    return [
        open_time,
        "100",
        "101",
        "99",
        "100.5",
        "12",
        open_time + 59_999,
        "1200",
        trade_count,
        "6",
        "600",
        "0",
    ]


def _mark(index: int, value: str = "100.2"):
    open_time = START_MS + index * 60_000
    return [
        open_time,
        value,
        value,
        value,
        value,
        "0",
        open_time + 59_999,
        "0",
        "0",
        "0",
        "0",
        "0",
    ]


def _book(index: int, update_id: int, *, event_offset_ms: int = 59_999):
    return [
        update_id,
        "100.4",
        "2",
        "100.6",
        "3",
        START_MS + index * 60_000 + event_offset_ms,
        START_MS + index * 60_000 + event_offset_ms,
    ]


def test_book_ticker_parser_accepts_archive_header_and_preserves_event_time():
    assert parse_vision_book_ticker_row(
        [
            "update_id",
            "best_bid_price",
            "best_bid_qty",
            "best_ask_price",
            "best_ask_qty",
            "transaction_time",
            "event_time",
        ]
    ) is None
    parsed = parse_vision_book_ticker_row(_book(0, 5))
    assert parsed is not None
    assert parsed.update_id == 5
    assert parsed.event_time == datetime.fromtimestamp(
        (START_MS + 59_999) / 1000, tz=UTC
    )


def test_funding_parser_requires_symbol_and_uses_published_funding_time():
    parsed = parse_funding_rate_record(
        {
            "symbol": "BTCUSDT",
            "fundingRate": "-0.0005",
            "fundingTime": START_MS,
        },
        expected_symbol="BTCUSDT",
    )
    assert parsed.funding_rate == Decimal("-0.0005")
    assert parsed.funding_time == datetime.fromtimestamp(START_MS / 1000, tz=UTC)

    with pytest.raises(ValueError, match="missing symbol"):
        parse_funding_rate_record(
            {"fundingRate": "0.001", "fundingTime": START_MS},
            expected_symbol="BTCUSDT",
        )

    with pytest.raises(ValueError, match="fundingRate"):
        parse_funding_rate_record(
            {"symbol": "BTCUSDT", "fundingTime": START_MS},
            expected_symbol="BTCUSDT",
        )


def test_event_assembler_requires_real_mark_book_and_funding_alignment():
    events = build_historical_events(
        [_kline(0), _kline(1, trade_count=0)],
        [_mark(0), _mark(1, "100.3")],
        [_book(1, 2), _book(0, 1)],  # Intentionally out of order; parser sorts it.
        [
            FundingRateObservation(
                symbol="BTCUSDT",
                funding_time=datetime.fromtimestamp(START_MS / 1000, tz=UTC),
                funding_rate=Decimal("0.001"),
            )
        ],
        symbol="btcusdt",
    )

    assert len(events) == 2
    assert events[0].market_type == MarketType.USDM_FUTURES
    assert events[0].funding_event is True
    assert events[0].funding_rate == Decimal("0.001")
    assert events[1].trade_count == 0
    assert events[0].best_bid == Decimal("100.4")
    assert events[1].mark_price == Decimal("100.3")


def test_event_assembler_rejects_future_or_stale_book_observations():
    with pytest.raises(ValueError, match="at or before"):
        build_historical_events(
            [_kline(0)],
            [_mark(0)],
            [_book(1, 1)],
            symbol="BTCUSDT",
        )

    with pytest.raises(ValueError, match="stale"):
        build_historical_events(
            [_kline(0), _kline(1)],
            [_mark(0), _mark(1)],
            [_book(0, 1, event_offset_ms=0)],
            symbol="BTCUSDT",
            max_book_age_sec=1,
        )


def test_event_assembler_rejects_missing_mark_and_conflicting_book_update():
    with pytest.raises(ValueError, match="mark-price"):
        build_historical_events([_kline(0)], [], [_book(0, 1)], symbol="BTCUSDT")

    conflicting = _book(0, 1)
    conflicting[1] = "100.3"
    with pytest.raises(ValueError, match="conflicting duplicate"):
        build_historical_events(
            [_kline(0)],
            [_mark(0)],
            [_book(0, 1), conflicting],
            symbol="BTCUSDT",
        )


def test_event_jsonl_round_trip_preserves_hash_and_rejects_trailing_extra(tmp_path):
    events = build_historical_events(
        [_kline(0), _kline(1)],
        [_mark(0), _mark(1)],
        [_book(0, 1), _book(1, 2)],
        symbol="BTCUSDT",
    )
    path = tmp_path / "events.jsonl"
    content_hash = write_historical_events_jsonl(events, path)
    loaded = read_historical_events_jsonl(path)
    assert loaded == events
    assert historical_events_sha256(loaded) == content_hash

    with path.open("a", encoding="utf-8") as output_file:
        output_file.write('{"event_id":"bad","unexpected":true}\n')
    with pytest.raises(ValueError):
        read_historical_events_jsonl(path)


def test_dataset_hash_manifest_and_symbol_homogeneity_are_enforced(tmp_path):
    events = build_historical_events(
        [_kline(0), _kline(1)],
        [_mark(0), _mark(1)],
        [_book(0, 1), _book(1, 2)],
        symbol="BTCUSDT",
    )
    mixed = events[1].model_copy(
        update={"event_id": "ETHUSDT-1m-1700000060000", "symbol": "ETHUSDT"}
    )
    with pytest.raises(ValueError, match="one symbol"):
        validate_historical_events((events[0], mixed))

    archive = tmp_path / "source.zip"
    archive.write_bytes(b"research-archive")
    digest = sha256(b"research-archive").hexdigest()
    checksum = tmp_path / "source.CHECKSUM"
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    assert sha256_file(archive) == digest
    assert verify_sha256_checksum(archive, digest) == digest
    assert verify_vision_archive_checksum(archive, checksum) == digest

    with pytest.raises(ValueError, match="does not match"):
        verify_sha256_checksum(archive, "0" * 64)
