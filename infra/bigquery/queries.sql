-- =====================================================================================
-- BLESSING AI v0.2 — BIGQUERY QUANTITATIVE LAKEHOUSE QUERIES
-- Project ID: gen-lang-client-0730128480
-- Target Console: https://console.cloud.google.com/bigquery?project=gen-lang-client-0730128480&ws=!1m0
-- =====================================================================================

-- -------------------------------------------------------------------------------------
-- QUERY 1: Strategy Performance Attribution by Market Regime
-- Evaluates which Alpha engines generate positive expectation across market states
-- -------------------------------------------------------------------------------------
SELECT
  regime,
  strategy_id,
  COUNT(1) as total_signals,
  ROUND(AVG(opportunity_score), 3) as avg_opportunity_score,
  ROUND(AVG(confidence), 3) as avg_confidence,
  ROUND(SUM(target_exposure_delta), 4) as net_exposure_allocated,
  COUNTIF(veto_reason IS NOT NULL) as vetoed_signals_count
FROM
  `gen-lang-client-0730128480.signals.strategy_decisions`
WHERE
  DATE(timestamp) >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
GROUP BY
  regime, strategy_id
ORDER BY
  regime, avg_opportunity_score DESC;

-- -------------------------------------------------------------------------------------
-- QUERY 2: Portfolio Risk & Margin Stress Tracking
-- Monitors Margin Utilization, Effective Leverage, and Risk Governor State Transitions
-- -------------------------------------------------------------------------------------
SELECT
  TIMESTAMP_TRUNC(timestamp, HOUR) as hour_bucket,
  risk_state,
  ROUND(AVG(margin_utilization_pct), 2) as avg_margin_utilization,
  ROUND(MAX(margin_utilization_pct), 2) as max_margin_utilization,
  ROUND(AVG(effective_leverage), 2) as avg_leverage,
  ROUND(MAX(portfolio_drawdown_pct), 2) as peak_drawdown_pct,
  ROUND(AVG(equity), 2) as avg_equity,
  MAX(max_grid_depth_reached) as peak_grid_depth
FROM
  `gen-lang-client-0730128480.risk.portfolio_snapshots`
WHERE
  DATE(timestamp) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
GROUP BY
  hour_bucket, risk_state
ORDER BY
  hour_bucket DESC;

-- -------------------------------------------------------------------------------------
-- QUERY 3: Basis Carry & Volatility Analysis
-- Compares Spot vs Perpetual Basis Z-score against Funding Rate
-- -------------------------------------------------------------------------------------
SELECT
  DATE(timestamp) as trade_date,
  symbol,
  ROUND(AVG(basis_zscore), 2) as avg_basis_zscore,
  ROUND(AVG(funding_rate * 100 * 3 * 365), 2) as annualized_funding_pct,
  ROUND(AVG(realized_vol_24h), 2) as avg_realized_vol_pct,
  ROUND(AVG(atr_14), 2) as avg_atr
FROM
  `gen-lang-client-0730128480.market_data.ohlcv_bars`
WHERE
  DATE(timestamp) >= DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY)
  AND resolution = '1h'
GROUP BY
  trade_date, symbol
ORDER BY
  trade_date DESC, symbol;

-- -------------------------------------------------------------------------------------
-- QUERY 4: Backtest Overfitting Audit (Deflated Sharpe Ratio)
-- Evaluates backtest runs with complexity penalization
-- -------------------------------------------------------------------------------------
SELECT
  experiment_id,
  strategy_id,
  model_version,
  ROUND(sharpe_ratio, 2) as nominal_sharpe,
  ROUND(deflated_sharpe_ratio, 2) as deflated_sharpe,
  ROUND(profit_factor, 2) as profit_factor,
  ROUND(max_drawdown_pct, 2) as max_dd_pct,
  ROUND(total_commission_cost + total_slippage_cost + total_funding_cost, 2) as total_execution_drag_usd
FROM
  `gen-lang-client-0730128480.backtests.experiment_runs`
WHERE
  DATE(created_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
ORDER BY
  deflated_sharpe_ratio DESC;
