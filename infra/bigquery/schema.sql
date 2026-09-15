-- =====================================================================================
-- BLESSING AI v0.2 — BIGQUERY WARM ANALYTICAL LAKEHOUSE DDL
-- Project ID: gen-lang-client-0730128480
-- Target Console: https://console.cloud.google.com/bigquery?project=gen-lang-client-0730128480&ws=!1m0
--
-- Architectural Specification: Blessing-AI-v0.2-Plan.md Section 5.2
-- Rules:
-- 1. All tables strictly partitioned by date.
-- 2. Clustered by high-cardinality query keys to minimize scanned bytes (<10 GB limit).
-- 3. Decoupled from the live trading execution loop (zero impact on low-latency path).
-- =====================================================================================

-- -------------------------------------------------------------------------------------
-- 1. DATASETS DEFINITION
-- -------------------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS `gen-lang-client-0730128480.market_data`
OPTIONS (
  location = 'US',
  description = 'Blessing AI v0.2 high-frequency and multi-resolution OHLCV & orderbook telemetry'
);

CREATE SCHEMA IF NOT EXISTS `gen-lang-client-0730128480.signals`
OPTIONS (
  location = 'US',
  description = 'Blessing AI v0.2 multi-strategy opportunity scores, intents, and regime state transitions'
);

CREATE SCHEMA IF NOT EXISTS `gen-lang-client-0730128480.risk`
OPTIONS (
  location = 'US',
  description = 'Portfolio risk governor snapshots, exposure recovery actions, and margin safety states'
);

CREATE SCHEMA IF NOT EXISTS `gen-lang-client-0730128480.backtests`
OPTIONS (
  location = 'US',
  description = 'Historical backtesting replay runs, parameter grid results, and Deflated Sharpe evaluations'
);

-- -------------------------------------------------------------------------------------
-- 2. MARKET DATA: OHLCV BARS
-- Partitioned by DATE(timestamp), Clustered by symbol, resolution
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `gen-lang-client-0730128480.market_data.ohlcv_bars`
(
  bar_id STRING NOT NULL,              -- Stable producer identity for idempotent read-back
  timestamp TIMESTAMP NOT NULL,
  symbol STRING NOT NULL,            -- e.g. 'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT'
  venue STRING NOT NULL,             -- 'binance_spot' or 'binance_usdm'
  resolution STRING NOT NULL,        -- '1s', '1m', '5m', '15m', '1h', '4h', '1d'
  open BIGNUMERIC(38, 18) NOT NULL,
  high BIGNUMERIC(38, 18) NOT NULL,
  low BIGNUMERIC(38, 18) NOT NULL,
  close BIGNUMERIC(38, 18) NOT NULL,
  volume BIGNUMERIC(38, 18) NOT NULL,
  quote_volume BIGNUMERIC(38, 18) NOT NULL,
  trade_count INT64,
  taker_buy_base_volume BIGNUMERIC(38, 18),
  taker_buy_quote_volume BIGNUMERIC(38, 18),
  vwap BIGNUMERIC(38, 18),
  atr_14 BIGNUMERIC(38, 18),
  realized_vol_24h BIGNUMERIC(38, 18),
  basis_zscore BIGNUMERIC(38, 18),
  funding_rate BIGNUMERIC(38, 18),
  open_interest BIGNUMERIC(38, 18),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
PARTITION BY DATE(timestamp)
CLUSTER BY symbol, resolution
OPTIONS (
  description = 'Normalized historical and live OHLCV bars with rolling volatility and basis telemetry',
  require_partition_filter = TRUE
);

-- -------------------------------------------------------------------------------------
-- 3. SIGNALS: STRATEGY DECISIONS & INTENTS
-- Partitioned by DATE(timestamp), Clustered by strategy_id, symbol, regime
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `gen-lang-client-0730128480.signals.strategy_decisions`
(
  timestamp TIMESTAMP NOT NULL,
  decision_id STRING NOT NULL,
  symbol STRING NOT NULL,
  strategy_id STRING NOT NULL,       -- 'STRUCTURAL_GRID', 'TREND_FOLLOWING', 'SHOCK_MOMENTUM', 'FUNDING_CARRY'
  regime STRING NOT NULL,            -- 'R0_STRONG_MR', 'R1_RANGE', 'R2_WEAK_TREND', 'R3_STRONG_TREND', 'R4_BREAKOUT', 'R5_VOL_SHOCK', 'R6_CRISIS'
  opportunity_score FLOAT64 NOT NULL, -- Continuous 0.0 to 1.0 (No rigid boolean filters)
  confidence FLOAT64 NOT NULL,
  target_exposure_delta BIGNUMERIC(38, 18) NOT NULL,
  direction STRING NOT NULL,         -- 'LONG', 'SHORT', 'FLAT'
  grid_depth INT64,
  hedge_multiplier FLOAT64,
  signal_features JSON,              -- Stored feature vector for CatBoost/LightGBM model training
  veto_reason STRING,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
PARTITION BY DATE(timestamp)
CLUSTER BY strategy_id, symbol, regime
OPTIONS (
  description = 'Strategy intents and opportunity scores before Meta Allocator and Risk Governor arbitration',
  require_partition_filter = TRUE
);

-- -------------------------------------------------------------------------------------
-- 4. RISK: PORTFOLIO RISK GOVERNOR SNAPSHOTS
-- Partitioned by DATE(timestamp), Clustered by risk_state
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `gen-lang-client-0730128480.risk.portfolio_snapshots`
(
  timestamp TIMESTAMP NOT NULL,
  snapshot_id STRING NOT NULL,
  risk_state STRING NOT NULL,        -- 'NORMAL', 'CAUTION', 'NO_NEW_GRID', 'RECOVERY_ONLY', 'DELEVERAGE', 'EMERGENCY'
  equity BIGNUMERIC(38, 18) NOT NULL,
  balance BIGNUMERIC(38, 18) NOT NULL,
  used_margin BIGNUMERIC(38, 18) NOT NULL,
  free_margin BIGNUMERIC(38, 18) NOT NULL,
  margin_utilization_pct FLOAT64 NOT NULL,
  effective_leverage FLOAT64 NOT NULL,
  portfolio_drawdown_pct FLOAT64 NOT NULL,
  daily_pnl BIGNUMERIC(38, 18) NOT NULL,
  kill_switch_active BOOLEAN NOT NULL,
  gross_exposure_usd BIGNUMERIC(38, 18) NOT NULL,
  net_delta_btc BIGNUMERIC(38, 18) NOT NULL,
  net_delta_eth BIGNUMERIC(38, 18) NOT NULL,
  active_baskets_count INT64 NOT NULL,
  max_grid_depth_reached INT64 NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
PARTITION BY DATE(timestamp)
CLUSTER BY risk_state
OPTIONS (
  description = 'Periodic portfolio health, risk states, and margin utilization snapshots',
  require_partition_filter = TRUE
);

-- -------------------------------------------------------------------------------------
-- 5. BACKTESTS: EXPERIMENT RUNS & VALIDATION
-- Partitioned by DATE(created_at), Clustered by strategy_id, model_version
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `gen-lang-client-0730128480.backtests.experiment_runs`
(
  experiment_id STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  strategy_id STRING NOT NULL,
  model_version STRING NOT NULL,
  start_time TIMESTAMP NOT NULL,
  end_time TIMESTAMP NOT NULL,
  symbols ARRAY<STRING>,
  initial_capital BIGNUMERIC(38, 18) NOT NULL,
  final_equity BIGNUMERIC(38, 18) NOT NULL,
  total_trades INT64 NOT NULL,
  win_rate_pct FLOAT64 NOT NULL,
  sharpe_ratio FLOAT64 NOT NULL,
  deflated_sharpe_ratio FLOAT64,     -- Deflated Sharpe Ratio to control backtest overfitting
  sortino_ratio FLOAT64,
  max_drawdown_pct FLOAT64 NOT NULL,
  profit_factor FLOAT64 NOT NULL,
  total_funding_cost BIGNUMERIC(38, 18),
  total_slippage_cost BIGNUMERIC(38, 18),
  total_commission_cost BIGNUMERIC(38, 18),
  parameters JSON,
  notes STRING
)
PARTITION BY DATE(created_at)
CLUSTER BY strategy_id, model_version
OPTIONS (
  description = 'Backtest experiment runs with realistic fee/slippage modeling and overfitting metrics',
  require_partition_filter = TRUE
);
