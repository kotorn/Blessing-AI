"""Research-only vector feature extraction with explicit data provenance."""

import logging
import math
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

try:
    import duckdb
    import polars as pl
except ImportError:  # pragma: no cover - exercised in minimal environments
    duckdb = None
    pl = None


logger = logging.getLogger("blessing.backtest.vector_engine")

ALLOWED_RESEARCH_SOURCES = {
    "BINANCE_PUBLIC_MAINNET_READ_ONLY",
    "BINANCE_PUBLIC_TESTNET_READ_ONLY",
}
FORWARD_HORIZON_BARS = 1440


def _finite_market_value(value: Any, field: str, *, allow_zero: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Research dataset field {field} is not numeric") from exc
    if not math.isfinite(parsed) or (parsed < 0 if allow_zero else parsed <= 0):
        qualifier = "finite and non-negative" if allow_zero else "finite and positive"
        raise ValueError(f"Research dataset field {field} must be {qualifier}")
    return parsed


def _validate_market_timestamps(timestamps: Iterable[Any]) -> list[datetime]:
    normalized: list[datetime] = []
    for timestamp in timestamps:
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            raise ValueError(
                "Research dataset timestamps must be timezone-aware datetimes"
            )
        normalized.append(timestamp.astimezone(UTC))
    if any(right <= left for left, right in zip(normalized, normalized[1:])):
        raise ValueError("Research dataset timestamps must be strictly chronological")
    if any(
        (right - left).total_seconds() != 60
        for left, right in zip(normalized, normalized[1:])
    ):
        raise ValueError("Research dataset contains a missing or non-1m candle interval")
    return normalized


def validate_research_provenance(frame: Any) -> str:
    """Require an explicit Binance public read-only provenance column."""
    if frame is None or not hasattr(frame, "columns"):
        raise ValueError("Research dataset is unavailable")
    if "data_source" not in frame.columns:
        raise ValueError("Research dataset is missing data_source provenance")
    raw_values = frame.get_column("data_source").to_list()
    if any(value is None or not str(value).strip() for value in raw_values):
        raise ValueError("Research dataset has missing data_source provenance")
    values = list(dict.fromkeys(raw_values))
    if not values or any(value not in ALLOWED_RESEARCH_SOURCES for value in values):
        raise ValueError(
            "Research dataset provenance must be Binance public read-only data"
        )
    if len(values) != 1:
        raise ValueError("Research dataset must have one consistent data_source")
    return str(values[0])


def validate_research_market_frame(frame: Any) -> None:
    """Reject malformed or incomplete 1-minute data before forward labeling."""

    required_columns = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = sorted(required_columns.difference(frame.columns))
    if missing:
        raise ValueError(f"Research dataset is missing columns: {', '.join(missing)}")
    if frame.height <= FORWARD_HORIZON_BARS:
        raise ValueError(
            "Research dataset is shorter than the complete forward labeling horizon"
        )

    timestamps = _validate_market_timestamps(frame.get_column("timestamp").to_list())
    if len(timestamps) != frame.height:
        raise ValueError("Research dataset timestamp row count is inconsistent")

    rows = frame.select(["open", "high", "low", "close", "volume"]).to_dicts()
    for row_index, row in enumerate(rows):
        open_price = _finite_market_value(row["open"], "open")
        high_price = _finite_market_value(row["high"], "high")
        low_price = _finite_market_value(row["low"], "low")
        close_price = _finite_market_value(row["close"], "close")
        _finite_market_value(row["volume"], "volume", allow_zero=True)
        if high_price < max(open_price, close_price) or low_price > min(
            open_price, close_price
        ):
            raise ValueError(f"Research dataset OHLC relationship is invalid at row {row_index}")


class VectorBacktestEngine:
    """DuckDB feature extraction; output is never live execution evidence."""

    def __init__(self, data_dir: str = "./data/historical"):
        self.data_dir = data_dir
        self.conn = duckdb.connect(database=":memory:") if duckdb else None
        self.last_result = None
        self.last_data_source: str | None = None

    def _data_path(self, symbol: str) -> str:
        normalized = str(symbol).strip().upper()
        if not normalized or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for char in normalized
        ):
            raise ValueError("symbol is invalid")
        return os.path.join(self.data_dir, f"{normalized}_1m_historical.parquet")

    def run_grid_safety_extraction(self, symbol: str) -> bool:
        if duckdb is None or pl is None or self.conn is None:
            raise RuntimeError("DuckDB and Polars are required for research extraction")
        file_path = self._data_path(symbol)
        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)

        frame = pl.read_parquet(file_path)
        self.last_data_source = validate_research_provenance(frame)
        if frame.height == 0:
            raise ValueError("Research dataset has no rows")
        validate_research_market_frame(frame)

        # DuckDB does not allow a prepared parameter in the DDL statement that
        # creates a view over ``read_parquet``. Escape the already-resolved
        # local path before embedding it so the query remains safe for paths
        # containing quotes without falling back to an unsafe raw interpolation.
        escaped_file_path = file_path.replace("'", "''")
        self.conn.execute(
            "CREATE OR REPLACE VIEW market_data AS "
            f"SELECT * FROM read_parquet('{escaped_file_path}')"
        )
        query = """
        WITH ordered_market_data AS (
            SELECT
                *,
                ROW_NUMBER() OVER (ORDER BY timestamp) AS row_index,
                COUNT(*) OVER () AS total_rows
            FROM market_data
        ),
        forward_returns AS (
            SELECT
                timestamp,
                data_source,
                close,
                row_index,
                total_rows,
                MAX(high) OVER (
                    ORDER BY timestamp ROWS BETWEEN 1 FOLLOWING AND 1440 FOLLOWING
                ) AS max_price_24h,
                MIN(low) OVER (
                    ORDER BY timestamp ROWS BETWEEN 1 FOLLOWING AND 1440 FOLLOWING
                ) AS min_price_24h
            FROM ordered_market_data
        )
        SELECT
            timestamp,
            data_source,
            close,
            (max_price_24h - close) / close AS forward_max_up_pct,
            (close - min_price_24h) / close AS forward_max_down_pct,
            CASE
            WHEN (close - min_price_24h) / close > 0.03 THEN 0.0
            WHEN (max_price_24h - close) / close > 0.01 THEN 1.0
            ELSE 0.5
        END AS target_label
        FROM forward_returns
        WHERE close > 0
          AND row_index <= total_rows - 1440
        """
        self.last_result = self.conn.execute(query).pl()
        logger.info(
            "Research extraction complete for %s: %d rows, source=%s",
            symbol,
            self.last_result.height,
            self.last_data_source,
        )
        return True


if __name__ == "__main__":  # pragma: no cover - manual research utility
    logging.basicConfig(level=logging.INFO)
    VectorBacktestEngine().run_grid_safety_extraction("BTCUSDT")
