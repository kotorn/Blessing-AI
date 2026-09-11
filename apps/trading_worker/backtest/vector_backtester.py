import logging
import os

try:
    import duckdb
    import polars as pl
except ImportError:
    duckdb = None
    pl = None

logger = logging.getLogger("blessing.backtest.vector_engine")

class VectorBacktestEngine:
    """
    DuckDB-powered backtesting engine. Extremely fast vectorized operations
    used to calculate grid expansion PnL over historical Parquet files and 
    extract labels to train the ML Safety Scorer.
    """
    def __init__(self, data_dir: str = "./data/historical"):
        self.data_dir = data_dir
        if duckdb:
            self.conn = duckdb.connect(database=':memory:')
            
    def run_grid_safety_extraction(self, symbol: str) -> bool:
        if not duckdb:
            logger.error("DuckDB not installed. Cannot run vector backtest.")
            return False
            
        file_path = os.path.join(self.data_dir, f"{symbol}_1m_historical.parquet")
        if not os.path.exists(file_path):
            logger.error("Data file not found: %s", file_path)
            return False
            
        logger.info("Running Vector Backtest for ML Feature Extraction on %s...", symbol)
        
        # 1. Load Parquet into DuckDB
        self.conn.execute(f"CREATE OR REPLACE VIEW market_data AS SELECT * FROM read_parquet('{file_path}')")
        
        # 2. Extract Labels: A "safe" grid is defined as one that does not exceed 3% MAE (Maximum Adverse Excursion) 
        # before achieving a 1% profit target within 24 hours.
        query = """
        WITH forward_returns AS (
            SELECT 
                timestamp,
                close,
                MAX(high) OVER (ORDER BY timestamp ROWS BETWEEN 1 FOLLOWING AND 1440 FOLLOWING) as max_price_24h,
                MIN(low) OVER (ORDER BY timestamp ROWS BETWEEN 1 FOLLOWING AND 1440 FOLLOWING) as min_price_24h
            FROM market_data
        )
        SELECT 
            timestamp,
            close,
            (max_price_24h - close) / close AS forward_max_up_pct,
            (close - min_price_24h) / close AS forward_max_down_pct,
            CASE 
                WHEN (close - min_price_24h) / close > 0.03 THEN 0.0 -- Unsafe (hit MAE)
                WHEN (max_price_24h - close) / close > 0.01 THEN 1.0 -- Safe (hit Target)
                ELSE 0.5 -- Neutral/Timeout
            END as target_label
        FROM forward_returns
        """
        
        # 3. Pull results into Polars for CatBoost training
        df_result = self.conn.execute(query).pl()
        
        safe_count = df_result.filter(pl.col("target_label") == 1.0).height
        unsafe_count = df_result.filter(pl.col("target_label") == 0.0).height
        
        logger.info("Extraction complete! Safe instances: %d | Unsafe instances: %d", safe_count, unsafe_count)
        return True

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    engine = VectorBacktestEngine()
    engine.run_grid_safety_extraction("BTCUSDT")
