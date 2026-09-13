from datetime import datetime, timezone

import pytest

from apps.trading_worker.backtest.data_downloader import _parse_kline_row
from apps.trading_worker.backtest.data_downloader import BinanceDataDownloader
from apps.trading_worker.backtest.vector_backtester import validate_research_provenance


def kline(open_time: int, close_time: int, *, high="101", low="99"):
    return [
        open_time,
        "100",
        high,
        low,
        "100.5",
        "12.0",
        close_time,
        "1200",
        4,
        "6",
        "600",
        "0",
    ]


def test_kline_parser_drops_unfinished_candle_and_keeps_real_provenance_slot():
    now_ms = 1_700_000_000_000
    closed = _parse_kline_row(
        kline(now_ms - 120_000, now_ms - 60_001),
        symbol="BTCUSDT",
        interval="1m",
        end_time_ms=now_ms,
    )
    unfinished = _parse_kline_row(
        kline(now_ms - 60_000, now_ms),
        symbol="BTCUSDT",
        interval="1m",
        end_time_ms=now_ms,
    )

    assert closed is not None
    assert closed["symbol"] == "BTCUSDT"
    assert closed["data_source"] == ""
    assert unfinished is None
    assert closed["timestamp"].tzinfo == timezone.utc


def test_kline_parser_rejects_invalid_ohlc():
    with pytest.raises(ValueError, match="OHLC"):
        _parse_kline_row(
            kline(1_699_999_880_000, 1_699_999_939_999, high="99"),
            symbol="BTCUSDT",
            interval="1m",
            end_time_ms=1_700_000_000_000,
        )


def test_vector_provenance_rejects_missing_or_synthetic_sources():
    pl = pytest.importorskip("polars")
    missing = pl.DataFrame({"close": [100.0]})
    synthetic = pl.DataFrame({"data_source": ["SIMULATED"], "close": [100.0]})
    verified = pl.DataFrame(
        {"data_source": ["BINANCE_PUBLIC_MAINNET_READ_ONLY"], "close": [100.0]}
    )

    with pytest.raises(ValueError, match="missing data_source"):
        validate_research_provenance(missing)
    with pytest.raises(ValueError, match="public read-only"):
        validate_research_provenance(synthetic)
    assert validate_research_provenance(verified) == "BINANCE_PUBLIC_MAINNET_READ_ONLY"


class FakeKlineResponse:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        return None

    async def json(self):
        return self.payload


class FakeKlineSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def get(self, url, *, params):
        self.calls.append((url, params))
        return FakeKlineResponse(self.payload)


@pytest.mark.asyncio
async def test_mocked_downloader_writes_closed_rows_with_public_provenance(tmp_path):
    pl = pytest.importorskip("polars")
    end_time_ms = 1_700_000_000_000
    session = FakeKlineSession(
        [kline(end_time_ms - 120_000, end_time_ms - 60_001)]
    )
    downloader = BinanceDataDownloader(
        data_dir=str(tmp_path),
        base_url="https://testnet.binancefuture.com",
        request_delay_sec=0,
        session_factory=lambda: session,
    )

    output_path = await downloader.download_klines_to_parquet(
        "btcusdt", interval="1m", days=1, end_time_ms=end_time_ms
    )

    frame = pl.read_parquet(output_path)
    assert frame.height == 1
    assert frame.get_column("symbol").to_list() == ["BTCUSDT"]
    assert frame.get_column("data_source").to_list() == [
        "BINANCE_PUBLIC_TESTNET_READ_ONLY"
    ]
    assert session.calls[0][1]["limit"] == 1500
    assert session.calls[0][1]["symbol"] == "BTCUSDT"
