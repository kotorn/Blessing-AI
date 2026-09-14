import json
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import pytest

from apps.trading_worker.backtest.economic import EconomicCostModel
from apps.trading_worker.backtest.event_dataset import (
    FundingRateObservation,
    build_historical_events,
)
from apps.trading_worker.backtest.evidence_artifact import (
    ResearchSourceRecord,
    build_manifest,
    build_replay_evidence_artifact,
    verify_replay_evidence_artifact,
    write_replay_evidence_artifact,
)
from apps.trading_worker.backtest.replay import ReplayExecutionConfig, ReplaySymbolRules
from apps.trading_worker.backtest.run_research_replay import main as run_research_replay

START_MS = 1_700_000_000_000
START = datetime.fromtimestamp(START_MS / 1000, tz=UTC)


def _kline(index: int, close: str):
    open_time = START_MS + index * 60_000
    return [
        open_time,
        close,
        close,
        close,
        close,
        "12",
        open_time + 59_999,
        "1200",
        10,
        "6",
        "600",
        "0",
    ]


def _mark(index: int, value: str):
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


def _book(index: int, update_id: int):
    event_time = START_MS + index * 60_000 + 59_999
    return [update_id, "100", "2", "100.2", "2", event_time, event_time]


def _config() -> ReplayExecutionConfig:
    return ReplayExecutionConfig(
        initial_capital=Decimal(1000),
        cost_model=EconomicCostModel(
            maker_fee_rate=Decimal("0.0002"),
            taker_fee_rate=Decimal("0.0005"),
        ),
        market_slippage_bps=Decimal(2),
        funding_interval_sec=60,
        enabled_strategies=("shock",),
        symbol_rules=(
            ReplaySymbolRules(
                symbol="BTCUSDT",
                tick_size=Decimal("0.1"),
                step_size=Decimal("0.001"),
                min_quantity=Decimal("0.001"),
                max_quantity=Decimal(1000),
                min_notional=Decimal(5),
            ),
        ),
    )


def _events():
    return build_historical_events(
        [_kline(0, "100"), _kline(1, "102"), _kline(2, "99")],
        [_mark(0, "100"), _mark(1, "102"), _mark(2, "99")],
        [_book(0, 1), _book(1, 2), _book(2, 3)],
        [
            FundingRateObservation(
                symbol="BTCUSDT",
                funding_time=START + timedelta(minutes=2),
                funding_rate=Decimal("0.001"),
            )
        ],
        symbol="BTCUSDT",
        venue="BINANCE_TESTNET",
        data_source="BINANCE_PUBLIC_TESTNET_READ_ONLY",
    )


def _manifest(events, config):
    window_start = events[0].event_time
    window_end = events[-1].event_time
    source_records = tuple(
        ResearchSourceRecord(
            source_type=source_type,
            url="https://testnet.binancefuture.com/fapi/v1/fundingRate",
            sha256="a" * 64,
            row_count=3 if source_type != "FUNDING_RATES" else 1,
            start_time=window_start,
            end_time=window_end,
        )
        for source_type in (
            "KLINES_1M",
            "MARK_PRICE_KLINES_1M",
            "BOOK_TICKER",
            "FUNDING_RATES",
        )
    )
    return build_manifest(
        dataset_id="fixture-btcusdt-1m",
        symbol="BTCUSDT",
        venue="BINANCE_TESTNET",
        code_sha="a" * 40,
        source_records=source_records,
        exchange_info_source=ResearchSourceRecord(
            source_type="EXCHANGE_INFO",
            url="https://testnet.binancefuture.com/fapi/v1/exchangeInfo",
            sha256="b" * 64,
            row_count=1,
            start_time=window_start,
            end_time=window_end,
        ),
        cost_model=config.cost_model,
        symbol_rules=config.symbol_rules,
    )


def test_artifact_replays_and_recomputes_net_economics(tmp_path):
    events = _events()
    config = _config()
    artifact = build_replay_evidence_artifact(
        events, config, manifest=_manifest(events, config)
    )

    assert artifact.verification_status == "VERIFIED_REPLAY"
    assert artifact.replay.economic_result is not None
    assert artifact.replay.economic_result.trading_fees > 0
    assert artifact.replay.economic_result.slippage_cost > 0
    assert artifact.replay.open_position_at_end == 0
    assert verify_replay_evidence_artifact(artifact) == artifact

    path = tmp_path / "replay-evidence.json"
    assert write_replay_evidence_artifact(artifact, path) == artifact.artifact_sha256
    verified = verify_replay_evidence_artifact(path)
    assert verified.model_dump(mode="json") == artifact.model_dump(mode="json")


def test_artifact_hash_and_replay_result_cannot_be_tampered():
    events = _events()
    config = _config()
    artifact = build_replay_evidence_artifact(
        events, config, manifest=_manifest(events, config)
    )
    tampered_replay = artifact.replay.model_copy(
        update={"final_equity": artifact.replay.final_equity + Decimal(1)}
    )
    tampered = artifact.model_copy(update={"replay": tampered_replay})
    with pytest.raises(ValueError, match="artifact SHA-256"):
        verify_replay_evidence_artifact(tampered)


def test_research_replay_cli_wires_checked_archives_to_verified_artifact(tmp_path):
    def write_archive(name, header, rows):
        archive = tmp_path / f"{name}.zip"
        payload = "\n".join(
            [",".join(header), *(",".join(str(value) for value in row) for row in rows)]
        )
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.writestr(f"{name}.csv", payload)
        digest = sha256(archive.read_bytes()).hexdigest()
        checksum = tmp_path / f"{name}.CHECKSUM"
        checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
        return archive, checksum

    kline_archive, kline_checksum = write_archive(
        "BTCUSDT-1m-kline",
        ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote", "count"],
        [_kline(0, "100"), _kline(1, "102"), _kline(2, "99")],
    )
    mark_archive, mark_checksum = write_archive(
        "BTCUSDT-1m-mark",
        ["open_time", "open", "high", "low", "close", "volume", "close_time"],
        [_mark(0, "100"), _mark(1, "102"), _mark(2, "99")],
    )
    book_archive, book_checksum = write_archive(
        "BTCUSDT-bookTicker",
        ["update_id", "best_bid_price", "best_bid_qty", "best_ask_price", "best_ask_qty", "transaction_time", "event_time"],
        [_book(0, 1), _book(1, 2), _book(2, 3)],
    )
    funding_path = tmp_path / "funding.json"
    funding_path.write_text(
        json.dumps(
            [
                {
                    "symbol": "BTCUSDT",
                    "fundingRate": "0.001",
                    "fundingTime": START_MS + 120_000,
                }
            ]
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(_config().model_dump(mode="json")), encoding="utf-8"
    )
    exchange_info_path = tmp_path / "exchangeInfo.json"
    exchange_info_path.write_text(
        json.dumps(
            {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "orderTypes": ["MARKET"],
                        "filters": [
                            {
                                "filterType": "PRICE_FILTER",
                                "tickSize": "0.1",
                            },
                            {
                                "filterType": "LOT_SIZE",
                                "minQty": "0.001",
                                "maxQty": "1000",
                                "stepSize": "0.001",
                            },
                            {
                                "filterType": "MIN_NOTIONAL",
                                "notional": "5",
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "cli-artifact.json"
    public_url = "https://testnet.binancefuture.com/fapi/v1/fundingRate"

    assert (
        run_research_replay(
            [
                "--symbol",
                "BTCUSDT",
                "--venue",
                "BINANCE_TESTNET",
                "--dataset-id",
                "fixture-cli",
                "--build-sha",
                "a" * 40,
                "--config-json",
                str(config_path),
                "--exchange-info-json",
                str(exchange_info_path),
                "--exchange-info-url",
                public_url,
                "--kline-archive",
                str(kline_archive),
                "--kline-checksum",
                str(kline_checksum),
                "--kline-url",
                public_url,
                "--mark-price-archive",
                str(mark_archive),
                "--mark-price-checksum",
                str(mark_checksum),
                "--mark-price-url",
                public_url,
                "--book-ticker-archive",
                str(book_archive),
                "--book-ticker-checksum",
                str(book_checksum),
                "--book-ticker-url",
                public_url,
                "--funding-json",
                str(funding_path),
                "--funding-url",
                public_url,
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    verified = verify_replay_evidence_artifact(output_path)
    assert verified.manifest.dataset_id == "fixture-cli"
    assert (
        verified.manifest.source_records[0].sha256
        == sha256(kline_archive.read_bytes()).hexdigest()
    )
