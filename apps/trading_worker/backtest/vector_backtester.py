"""Research-only vector feature extraction with explicit data provenance."""

import logging
import os
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


def validate_research_provenance(frame: Any) -> str:
    """Require an explicit Binance public read-only provenance column."""
    if frame is None or not hasattr(frame, "columns"):
        raise ValueError("Research dataset is unavailable")
    if "data_source" not in frame.columns:
        raise ValueError("Research dataset is missing data_source provenance")
    values = frame.get_column("data_source").drop_nulls().unique().to_list()
    if not values or any(value not in ALLOWED_RESEARCH_SOURCES for value in values):
        raise ValueError(
            "Research dataset provenance must be Binance public read-only data"
        )
    if len(values) != 1:
        raise ValueError("Research dataset must have one consistent data_source")
    return str(values[0])


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
        required_columns = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = sorted(required_columns.difference(frame.columns))
        if missing:
            raise ValueError(f"Research dataset is missing columns: {', '.join(missing)}")
        if frame.height == 0:
            raise ValueError("Research dataset has no rows")

        self.conn.execute(
            "CREATE OR REPLACE VIEW market_data AS SELECT * FROM read_parquet(?)",
            [file_path],
        )
        query = """
        WITH forward_returns AS (
            SELECT
                timestamp,
                data_source,
                close,
                MAX(high) OVER (
                    ORDER BY timestamp ROWS BETWEEN 1 FOLLOWING AND 1440 FOLLOWING
                ) AS max_price_24h,
                MIN(low) OVER (
                    ORDER BY timestamp ROWS BETWEEN 1 FOLLOWING AND 1440 FOLLOWING
                ) AS min_price_24h
            FROM market_data
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
