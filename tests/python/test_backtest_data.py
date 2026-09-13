from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from apps.trading_worker.backtest.data_downloader import (
    BinanceDataDownloader,
    _parse_kline_row,
)
from apps.trading_worker.backtest.economic import (
    BacktestTrade,
    EconomicCostModel,
    WalkForwardConfig,
    cost_trade,
    evaluate_trades,
    walk_forward_splits,
)
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
    assert closed["timestamp"].tzinfo == UTC


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


def _research_trade(index: int, *, gross_pnl: str = "10") -> BacktestTrade:
    return BacktestTrade(
        trade_id=f"T-{index}",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
        symbol="BTCUSDT",
        strategy_id="research_fixture_input",
        data_source="BINANCE_PUBLIC_TESTNET_READ_ONLY",
        gross_pnl=Decimal(gross_pnl),
        funding_pnl=Decimal(-1),
        entry_notional=Decimal(1000),
        exit_notional=Decimal(1000),
        entry_maker=True,
        exit_maker=False,
        entry_spread_bps=Decimal(4),
        exit_spread_bps=Decimal(4),
        entry_slippage_bps=Decimal(2),
        exit_slippage_bps=Decimal(2),
    )


def test_economic_cost_accounting_deducts_each_explicit_component():
    model = EconomicCostModel(
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
    )

    result = cost_trade(_research_trade(1), model)

    assert result.trading_fees == Decimal("0.7")
    assert result.spread_cost == Decimal("0.4")
    assert result.slippage_cost == Decimal("0.4")
    assert result.funding_pnl == Decimal(-1)
    assert result.net_pnl == Decimal("7.5")


def test_economic_backtest_result_is_not_launch_evidence():
    model = EconomicCostModel(
        maker_fee_rate=Decimal("0.0002"),
        taker_fee_rate=Decimal("0.0005"),
    )

    result = evaluate_trades(
        [_research_trade(1), _research_trade(2, gross_pnl="20")],
        initial_capital=Decimal(1000),
        cost_model=model,
    )

    assert result.net_pnl == Decimal("25.0")
    assert result.positive_net_expectancy is True
    assert result.evidence_status == "RESEARCH_CALCULATION_ONLY"
    assert result.launch_eligible is False


def test_walk_forward_split_has_purge_and_embargo_gaps():
    records = [_research_trade(index) for index in range(14)]
    folds = walk_forward_splits(
        records,
        WalkForwardConfig(train_size=3, test_size=2, purge_size=1, embargo_size=1),
    )

    assert len(folds) == 2
    assert (folds[0].train_start, folds[0].train_end) == (0, 3)
    assert (folds[0].test_start, folds[0].test_end) == (4, 6)
    assert (folds[1].train_start, folds[1].train_end) == (7, 10)
    assert (folds[1].test_start, folds[1].test_end) == (11, 13)
    assert folds[0].train_end < folds[0].test_start
    assert folds[0].test_end + 1 == folds[1].train_start


def test_walk_forward_rejects_out_of_order_records():
    first = _research_trade(1)
    second = _research_trade(2).model_copy(
        update={"timestamp": datetime(2025, 12, 31, tzinfo=UTC)}
    )

    with pytest.raises(ValueError, match="chronologically"):
        walk_forward_splits(
            [first, second],
            WalkForwardConfig(train_size=1, test_size=1),
        )
