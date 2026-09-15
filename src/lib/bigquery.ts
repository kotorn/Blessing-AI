import { getFirebaseIdToken } from './firebase';

/**
 * Blessing AI v0.2 — BigQuery Analytical Lakehouse Service
 * Target Project: gen-lang-client-0730128480
 * Workspace URL: https://console.cloud.google.com/bigquery?project=gen-lang-client-0730128480&ws=!1m0
 */

export const BIGQUERY_PROJECT_ID = 'gen-lang-client-0730128480';
export const BIGQUERY_CONSOLE_URL = 'https://console.cloud.google.com/bigquery?project=gen-lang-client-0730128480&ws=!1m0';
export const MAX_SCAN_BYTES_LIMIT = 10 * 1024 * 1024 * 1024; // 10 GB (COST_MODEL.md)

export interface BigQueryDatasetMeta {
  datasetId: string;
  description: string;
  location: string;
  tables: Array<{
    tableId: string;
    description: string;
    partitionField: string;
    clusterFields: string[];
    rowCountEstimate?: number;
    sizeMbEstimate?: number;
  }>;
}

export interface BigQueryDryRunResult {
  valid: boolean;
  totalBytesProcessed: number;
  totalBytesProcessedFormatted: string;
  estimatedCostUsd: number;
  withinFreeTier: boolean;
  exceedsSafetyCap: boolean;
  statementType?: string;
  message?: string;
  data_source?: 'BIGQUERY';
  evidence_status?: 'UNVERIFIED' | 'VERIFIED';
  verified?: boolean;
}

export interface BigQueryQueryResult {
  columns: string[];
  rows: any[];
  totalRows: number;
  returnedRows?: number;
  hasMore?: boolean;
  nextPageToken?: string;
  maxResultRows?: number;
  bytesProcessedFormatted: string;
  executionTimeMs: number;
  cacheHit: boolean;
  data_source?: 'BIGQUERY';
  evidence_status?: 'UNVERIFIED' | 'VERIFIED';
  verified?: boolean;
  note?: string;
  bytesProcessed?: number;
  projectId?: string;
}

export const PRESET_BIGQUERY_QUERIES = [
  {
    id: 'regime_alpha_attribution',
    title: '1. Strategy Alpha Attribution by Market Regime',
    description: 'Calculates win rate, opportunity scores, and net exposure allocation partitioned across regimes',
    sql: `-- Strategy Performance Attribution across 7 Market Regimes
SELECT
  regime,
  strategy_id,
  COUNT(1) as total_signals,
  ROUND(AVG(opportunity_score), 3) as avg_opp_score,
  ROUND(AVG(confidence), 3) as avg_confidence,
  ROUND(SUM(target_exposure_delta), 4) as net_exposure_allocated,
  COUNTIF(veto_reason IS NOT NULL) as vetoed_signals_count
FROM
  \`gen-lang-client-0730128480.signals.strategy_decisions\`
WHERE
  timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY
  regime, strategy_id
ORDER BY
  regime, avg_opp_score DESC;`,
  },
  {
    id: 'risk_margin_stress',
    title: '2. Portfolio Risk & Margin Stress Tracking',
    description: 'Audits Margin Utilization, Effective Leverage, and Risk Governor State Transitions over time',
    sql: `-- Portfolio Health, Margin Utilization & Drawdown Telemetry
SELECT
  TIMESTAMP_TRUNC(timestamp, HOUR) as hour_bucket,
  risk_state,
  ROUND(AVG(margin_utilization_pct), 2) as avg_margin_util_pct,
  ROUND(MAX(margin_utilization_pct), 2) as peak_margin_util_pct,
  ROUND(AVG(effective_leverage), 2) as avg_leverage,
  ROUND(MAX(portfolio_drawdown_pct), 2) as peak_drawdown_pct,
  ROUND(AVG(equity), 2) as avg_equity_usdt,
  MAX(max_grid_depth_reached) as peak_grid_depth
FROM
  \`gen-lang-client-0730128480.risk.portfolio_snapshots\`
WHERE
  timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
GROUP BY
  hour_bucket, risk_state
ORDER BY
  hour_bucket DESC;`,
  },
  {
    id: 'basis_funding_analysis',
    title: '3. Basis Carry & Volatility Analysis',
    description: 'Spot vs Perpetual Basis Z-score, Annualized Funding Drag, and Rolling ATR Volatility',
    sql: `-- Spot vs Futures Basis Z-Score and Funding Rate Arbitrage Telemetry
SELECT
  TIMESTAMP_TRUNC(timestamp, DAY) as trade_day,
  symbol,
  ROUND(AVG(basis_zscore), 2) as avg_basis_zscore,
  ROUND(AVG(funding_rate * 100 * 3 * 365), 2) as annualized_funding_pct,
  ROUND(AVG(realized_vol_24h), 2) as avg_realized_vol_pct,
  ROUND(AVG(atr_14), 2) as avg_atr_usdt
FROM
  \`gen-lang-client-0730128480.market_data.ohlcv_bars\`
WHERE
  timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
  AND resolution = '1h'
GROUP BY
  trade_day, symbol
ORDER BY
  trade_day DESC, symbol;`,
  },
  {
    id: 'backtest_deflated_sharpe',
    title: '4. Backtest Overfitting Audit (Deflated Sharpe)',
    description: 'Evaluates multiple-testing bias, realistic transaction/slippage drag, and Deflated Sharpe Ratio',
    sql: `-- Backtest Runs Evaluated with Deflated Sharpe Ratio (DSR) & Fee Drag
SELECT
  experiment_id,
  strategy_id,
  model_version,
  ROUND(sharpe_ratio, 2) as nominal_sharpe,
  ROUND(deflated_sharpe_ratio, 2) as deflated_sharpe,
  ROUND(profit_factor, 2) as profit_factor,
  ROUND(max_drawdown_pct, 2) as max_dd_pct,
  ROUND(total_commission_cost + total_slippage_cost + total_funding_cost, 2) as total_drag_usd
FROM
  \`gen-lang-client-0730128480.backtests.experiment_runs\`
WHERE
  created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 90 DAY)
ORDER BY
  deflated_sharpe DESC;`,
  },
];

/**
 * Fetch BigQuery Lakehouse Configuration & Metadata
 */
async function firebaseAuthHeaders(): Promise<Record<string, string>> {
  const token = await getFirebaseIdToken();
  if (!token) throw new Error('Firebase sign-in is required for BigQuery access');
  return { Authorization: `Bearer ${token}` };
}

export async function getBigQueryConfig(firebaseIdToken?: string | null) {
  const token = firebaseIdToken || await getFirebaseIdToken();
  if (!token) throw new Error('Firebase sign-in is required for BigQuery access');
  const resp = await fetch('/api/bigquery/config', { headers: { Authorization: `Bearer ${token}` } });
  if (!resp.ok) {
    throw new Error('Failed to fetch BigQuery configuration');
  }
  return await resp.json();
}

/**
 * Execute a Dry Run to estimate scanned bytes and enforce the 10GB scan safety cap
 */
export async function executeDryRun(query: string, firebaseIdToken?: string | null): Promise<BigQueryDryRunResult> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  Object.assign(headers, firebaseIdToken ? { Authorization: `Bearer ${firebaseIdToken}` } : await firebaseAuthHeaders());

  const resp = await fetch('/api/bigquery/dry-run', {
    method: 'POST',
    headers,
    body: JSON.stringify({ query }),
  });

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || 'Dry run evaluation failed');
  }

  return await resp.json();
}

/**
 * Execute query against BigQuery lakehouse
 */
export async function executeQuery(query: string, firebaseIdToken?: string | null): Promise<BigQueryQueryResult> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  Object.assign(headers, firebaseIdToken ? { Authorization: `Bearer ${firebaseIdToken}` } : await firebaseAuthHeaders());

  const resp = await fetch('/api/bigquery/query', {
    method: 'POST',
    headers,
    body: JSON.stringify({ query }),
  });

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || 'Query execution failed');
  }

  return await resp.json();
}

/**
 * Trigger an explicit telemetry flush request. An omitted browser-side buffer
 * is represented as an empty batch, which is a safe no-op rather than a fake
 * telemetry payload; the worker producer can supply real rows separately.
 */
export async function flushRingBufferToBigQuery(firebaseIdToken?: string | null) {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  Object.assign(headers, firebaseIdToken ? { Authorization: `Bearer ${firebaseIdToken}` } : await firebaseAuthHeaders());

  const resp = await fetch('/api/bigquery/sync-telemetry', {
    method: 'POST',
    headers,
    body: JSON.stringify({ rows: [] }),
  });

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    throw new Error(err.error || 'Failed to sync telemetry to BigQuery');
  }

  return await resp.json();
}
