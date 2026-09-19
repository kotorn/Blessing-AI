import express, { Request, Response } from 'express';
import path from 'path';
import crypto from 'crypto';
import { createServer as createViteServer } from 'vite';
import dotenv from 'dotenv';
import { GoogleGenAI } from '@google/genai';
import { GoogleAuth } from 'google-auth-library';
import { applicationDefault, getApps, initializeApp } from 'firebase-admin/app';
import { getAuth } from 'firebase-admin/auth';
import { TradingSystemState, RiskConfiguration } from './src/backend/types.js';
import { evaluatePreflight, validateStateTransition, RISK_PROFILES, canExecuteAction, EXECUTION_CAPABILITIES, isWorkerTradingConnectionHealthy } from './src/backend/system.js';
import { auditRepository } from './src/backend/audit.js';
import {
  parsePortfolioMarginResponse,
  unavailablePortfolioMarginObservation,
} from './src/backend/portfolio-margin.js';
import {
  authorizeBigQueryRequest,
  authorizeOperatorRequest,
  bigQueryErrorResponse,
  dryRunQuery,
  executeQuery,
  readBigQueryConfig,
  syncTelemetry,
  BIGQUERY_CONSOLE_URL,
  BIGQUERY_PROJECT_ID,
  MAX_SCAN_BYTES,
} from './src/backend/bigquery.js';
import {
  requiredControlPlaneRole,
  authorizeInternalServiceRequest,
  type ControlPlaneRole,
} from './src/backend/control-plane-auth.js';
import {
  hashEvidence,
  newReleaseCandidate,
  newContinuationApproval,
  resolveReconciliationStatus,
  sanitizePreflightEvidence,
  validateApprovalPrerequisites,
  validateContinuationPrerequisites,
  type ContinuationApproval,
  type ContinuationVerificationSnapshot,
  type ReleaseCandidate,
  type ReleaseVerificationSnapshot,
  type SecretVersionSet,
} from './src/backend/release.js';
import {
  getReleaseStore,
  type ReleaseStore,
} from './src/backend/release-store.js';


dotenv.config();

const app = express();
const PORT = Number(process.env.PORT || 3000);
const CONTROL_PLANE_ONLY = ['1', 'true', 'yes', 'on'].includes(
  (process.env.CONTROL_PLANE_ONLY || '').trim().toLowerCase(),
);
const GCP_PROJECT_ID = (process.env.GCP_PROJECT_ID || 'gen-lang-client-0730128480').trim();
const RELEASE_CONTROLLER_SERVICE_ACCOUNT =
  (process.env.RELEASE_CONTROLLER_SERVICE_ACCOUNT ||
    `blessing-release-controller@${GCP_PROJECT_ID}.iam.gserviceaccount.com`).trim();
const CONTROL_PLANE_URL = (process.env.CONTROL_PLANE_URL || '').trim().replace(/\/+$/, '');
let releaseStore: ReleaseStore | null = null;

function getServerReleaseStore(): ReleaseStore {
  if (!releaseStore) releaseStore = getReleaseStore();
  return releaseStore;
}

function getFirebaseAdminApp() {
  const projectId = (process.env.GCP_PROJECT_ID || 'gen-lang-client-0730128480').trim();
  return getApps()[0] || initializeApp({
    credential: applicationDefault(),
    projectId,
  });
}

async function controlPlaneReadiness() {
  const checks: Array<{ id: string; status: 'PASS' | 'FAIL'; message: string }> = [];
  const addCheck = (id: string, passed: boolean, message: string) => {
    checks.push({ id, status: passed ? 'PASS' : 'FAIL', message });
  };

  addCheck(
    'CONTROL_PLANE_PROCESS',
    CONTROL_PLANE_ONLY && !['1', 'true', 'yes', 'on'].includes(
      (process.env.VITE_DATA_CONNECT_CUTOVER || 'false').trim().toLowerCase(),
    ),
    CONTROL_PLANE_ONLY
      ? 'Control Plane process is isolated and Data Connect cutover is disabled'
      : 'Dedicated Control Plane mode is not enabled',
  );

  try {
    getAuth(getFirebaseAdminApp());
    addCheck('FIREBASE_ADMIN', true, 'Firebase Admin verification service is initialized');
  } catch {
    addCheck('FIREBASE_ADMIN', false, 'Firebase Admin verification service is unavailable');
  }

  try {
    const candidate = await getServerReleaseStore().getCandidate(
      'rc-00000000-0000-0000-0000-000000000000',
    );
    addCheck(
      'RELEASE_STORE',
      candidate === null,
      candidate === null
        ? 'Server-side release store is reachable'
        : 'Release store returned an unexpected candidate',
    );
  } catch {
    addCheck('RELEASE_STORE', false, 'Server-side release store is unavailable');
  }

  try {
    // `/ready` is the Cloud Run health contract and returns an explicit HTTP
    // 503 when REQUIRED persistence is unavailable. `/readiness` is the
    // detailed launch-evidence document and intentionally has no top-level
    // `status=ready` field.
    const worker = await forwardWorkerRequest('/ready');
    addCheck(
      'WORKER_OIDC',
      worker.response.ok && worker.data?.status === 'ready',
      worker.response.ok && worker.data?.status === 'ready'
        ? 'Worker readiness was read through the Google OIDC boundary'
        : 'Worker readiness is unavailable through the Google OIDC boundary',
    );
  } catch {
    addCheck('WORKER_OIDC', false, 'Worker OIDC transport is unavailable');
  }

  const passed = checks.length > 0 && checks.every((check) => check.status === 'PASS');
  return {
    status: passed ? 'ready' : 'degraded',
    controlPlaneHealthy: true,
    checks,
    evidence_status: passed ? 'VERIFIED' : 'UNVERIFIED',
  };
}

// Reject the browser Binance surface before express.json() consumes a request
// body. In a dedicated Control Plane deployment even an authenticated browser
// must not be able to submit credential material to this process. The Firebase
// check still runs first so anonymous access receives the normal 401/403
// boundary rather than a route-specific response.
if (CONTROL_PLANE_ONLY) {
  app.use('/api/binance', (req, res) => {
    void enforceOperatorAccess(req, res, () => {
      res.status(410).json({
        error: 'CONTROL_PLANE_BINANCE_CREDENTIALS_DISABLED',
        message: 'Browser Binance credential endpoints are disabled on the Control Plane; use the Worker release path.',
        verified: false,
        evidence_status: 'UNVERIFIED',
      });
    }).catch(() => {
      if (!res.headersSent) {
        res.status(503).json({
          error: 'CONTROL_PLANE_AUTH_UNAVAILABLE',
          message: 'Control-plane authorization service is unavailable',
          status: 'DEGRADED',
          verified: false,
          evidence_status: 'UNVERIFIED',
        });
      }
    });
  });
}

app.use(express.json());

// The control-plane middleware is registered before any API route so the
// Binance profile/balance endpoints cannot bypass server-side Firebase RBAC.
// The implementation is declared below as a function declaration and is
// therefore available when Express starts handling requests.
app.use(['/api/system', '/api/quant', '/api/binance', '/api/release', '/api/google'], (req, res, next) => {
  void enforceOperatorAccess(req, res, next).catch(() => {
    if (res.headersSent) return;
    res.status(503).json({
      error: 'CONTROL_PLANE_AUTH_UNAVAILABLE',
      message: 'Control-plane authorization service is unavailable',
      status: 'DEGRADED',
      verified: false,
      evidence_status: 'UNVERIFIED',
    });
  });
});

app.use('/internal/release', (req, res, next) => {
  void enforceInternalServiceAccess(req, res, next).catch(() => {
    if (!res.headersSent) {
      res.status(503).json({
        error: 'INTERNAL_AUTH_UNAVAILABLE',
        message: 'Internal release authorization is unavailable',
        verified: false,
        evidence_status: 'UNVERIFIED',
      });
    }
  });
});

// Lazy-initialized Gemini client for Quant Research Assistant
let geminiClient: GoogleGenAI | null = null;
function getGeminiClient(): GoogleGenAI | null {
  if (!geminiClient && process.env.GEMINI_API_KEY) {
    geminiClient = new GoogleGenAI({
      apiKey: process.env.GEMINI_API_KEY,
      httpOptions: {
        headers: {
          'User-Agent': 'aistudio-build',
        },
      },
    });
  }
  return geminiClient;
}

// Simulated Quant Engine In-Memory State
interface SimulatedBasket {
  basket_id: string;
  venue: string;
  instrument: string;
  direction: 'LONG' | 'SHORT';
  state: string;
  grid_depth: number;
  max_grid_levels: number;
  total_size: number;
  average_entry: number;
  current_mark_price: number;
  unrealized_pnl: number;
  trading_fees: number;
  funding_pnl: number;
  slippage_cost: number;
  net_pnl: number;
  created_at: string;
  last_updated: string;
  grid_levels: Array<{
    level: number;
    price: number;
    size: number;
    status: 'FILLED' | 'PENDING' | 'CANCELLED';
    filled_at?: string;
  }>;
}


// Global System State - Authoritative Backend State
let tradingSystemState: TradingSystemState = {
  dataSource: 'SIMULATED',
  exchangeEnvironment: 'NONE',
  executionMode: 'PAPER',
  engineState: 'DISARMED',

  accountSynchronized: false,
  marketDataHealthy: false,
  privateStreamHealthy: false,
  tradingConnectionHealthy: false,
  reconciliationStatus: 'UNKNOWN',

  killSwitchActive: false,
  pauseNewRisk: false,
  recoveryOnly: false,
  workerResponsive: false,
  mainnetCredentialsVerified: false,
  mainnetLiveApproved: false,
  mainnetPreflightReady: false,

  configVersion: 'v0.2.0-beta',
  updatedAt: new Date().toISOString()
};

let riskConfiguration: RiskConfiguration = RISK_PROFILES.BALANCED;

let quantEngineState = {
  account: {
    // These are deliberately neutral until a verified account snapshot is
    // loaded. The fixture objects below remain research fixtures only.
    equity: 0,
    balance: 0,
    margin_utilization_pct: 0,
    effective_leverage: 0,
    free_margin: 0,
    used_margin: 0,
    daily_pnl: 0,
    daily_pnl_pct: 0,
    portfolio_drawdown_pct: 0,
    kill_switch_active: false,
    realized_daily_pnl: 0,
    risk_state: 'UNKNOWN' as 'NORMAL' | 'CAUTION' | 'NO_NEW_GRID' | 'RECOVERY_ONLY' | 'DELEVERAGE' | 'EMERGENCY' | 'UNKNOWN',
    source: 'SIMULATED' as 'SIMULATED' | 'BINANCE_TESTNET' | 'BINANCE_MAINNET',
    evidence_status: 'ILLUSTRATIVE_ONLY' as 'ILLUSTRATIVE_ONLY' | 'UNVERIFIED' | 'VERIFIED',
    verified: false,
  },
  instruments: {
    ZECUSDT: {
      symbol: 'ZECUSDT',
      spot_price: 42.15,
      perp_price: 42.20,
      basis: 0.05,
      basis_pct: 0.11,
      basis_zscore: 1.2,
      funding_rate: 0.0002,
      funding_annualized_pct: 21.9,
      atr_1h: 1.2,
      realized_vol_24h_pct: 88.5,
      open_interest_usd: 120000000,
      open_interest_delta_24h_pct: 12.5,
      regime: 'R4_BREAKOUT',
      regime_probabilities: {
        R0_STRONG_MEAN_REVERSION: 0.05,
        R1_RANGE: 0.10,
        R2_WEAK_TREND: 0.15,
        R3_STRONG_TREND: 0.20,
        R4_BREAKOUT: 0.45,
        R5_VOLATILITY_SHOCK: 0.05,
        R6_CRISIS: 0.0,
      },
      grid_safety_score: 18.5,
      grid_status: 'PAUSED_NEW_RISK',
      expected_recovery_time_hrs: 48,
      expected_mae_pct: 12.0,
      prob_basket_profit: 0.22,
    },
    SOLUSDT: {
      symbol: 'SOLUSDT',
      spot_price: 154.20,
      perp_price: 154.35,
      basis: 0.15,
      basis_pct: 0.09,
      basis_zscore: 0.8,
      funding_rate: 0.0001,
      funding_annualized_pct: 10.95,
      atr_1h: 4.5,
      realized_vol_24h_pct: 75.2,
      open_interest_usd: 850000000,
      open_interest_delta_24h_pct: 4.5,
      regime: 'R3_STRONG_TREND',
      regime_probabilities: {
        R0_STRONG_MEAN_REVERSION: 0.05,
        R1_RANGE: 0.15,
        R2_WEAK_TREND: 0.20,
        R3_STRONG_TREND: 0.50,
        R4_BREAKOUT: 0.10,
        R5_VOLATILITY_SHOCK: 0.0,
        R6_CRISIS: 0.0,
      },
      grid_safety_score: 25.0,
      grid_status: 'PAUSED_NEW_RISK',
      expected_recovery_time_hrs: 24,
      expected_mae_pct: 8.5,
      prob_basket_profit: 0.35,
    },
    BNBUSDT: {
      symbol: 'BNBUSDT',
      spot_price: 580.40,
      perp_price: 580.90,
      basis: 0.50,
      basis_pct: 0.08,
      basis_zscore: 0.2,
      funding_rate: 0.00005,
      funding_annualized_pct: 5.47,
      atr_1h: 8.2,
      realized_vol_24h_pct: 42.5,
      open_interest_usd: 620000000,
      open_interest_delta_24h_pct: -1.2,
      regime: 'R1_RANGE',
      regime_probabilities: {
        R0_STRONG_MEAN_REVERSION: 0.10,
        R1_RANGE: 0.60,
        R2_WEAK_TREND: 0.20,
        R3_STRONG_TREND: 0.05,
        R4_BREAKOUT: 0.05,
        R5_VOLATILITY_SHOCK: 0.0,
        R6_CRISIS: 0.0,
      },
      grid_safety_score: 82.5,
      grid_status: 'ALLOWED',
      expected_recovery_time_hrs: 4,
      expected_mae_pct: 1.2,
      prob_basket_profit: 0.88,
    },

    'BTCUSDT': {
      symbol: 'BTCUSDT',
      spot_price: 91450.0,
      perp_price: 91482.5,
      basis: 32.5,
      basis_pct: 0.0355,
      basis_zscore: 0.82,
      funding_rate: 0.00012, // +0.012% per 8h
      funding_annualized_pct: 13.14,
      atr_1h: 840.0,
      realized_vol_24h_pct: 42.5,
      open_interest_usd: 4850000000,
      open_interest_delta_24h_pct: 3.2,
      regime: 'R1_RANGE',
      regime_probabilities: {
        R0_STRONG_MEAN_REVERSION: 0.22,
        R1_RANGE: 0.54,
        R2_WEAK_TREND: 0.14,
        R3_STRONG_TREND: 0.04,
        R4_BREAKOUT: 0.03,
        R5_VOLATILITY_SHOCK: 0.02,
        R6_CRISIS: 0.01,
      },
      grid_safety_score: 78.5,
      grid_status: 'ALLOWED_REDUCED_RISK',
      expected_recovery_time_hrs: 4.2,
      expected_mae_pct: 1.95,
      prob_basket_profit: 0.86,
    },
    'ETHUSDT': {
      symbol: 'ETHUSDT',
      spot_price: 3340.0,
      perp_price: 3342.2,
      basis: 2.2,
      basis_pct: 0.0658,
      basis_zscore: 1.15,
      funding_rate: 0.00018,
      funding_annualized_pct: 19.71,
      atr_1h: 46.5,
      realized_vol_24h_pct: 54.2,
      open_interest_usd: 2150000000,
      open_interest_delta_24h_pct: -1.5,
      regime: 'R2_WEAK_TREND',
      regime_probabilities: {
        R0_STRONG_MEAN_REVERSION: 0.12,
        R1_RANGE: 0.38,
        R2_WEAK_TREND: 0.36,
        R3_STRONG_TREND: 0.08,
        R4_BREAKOUT: 0.04,
        R5_VOLATILITY_SHOCK: 0.01,
        R6_CRISIS: 0.01,
      },
      grid_safety_score: 68.2,
      grid_status: 'ALLOWED_REDUCED_RISK',
      expected_recovery_time_hrs: 6.8,
      expected_mae_pct: 2.8,
      prob_basket_profit: 0.79,
    },
  },
  baskets: [
    {
      basket_id: 'BSK-BTC-20260910-001',
      venue: 'binance_usdm',
      instrument: 'BTCUSDT',
      direction: 'LONG',
      state: 'ACTIVE',
      grid_depth: 2,
      max_grid_levels: 5,
      total_size: 0.84, // BTC
      average_entry: 91820.0,
      current_mark_price: 91482.5,
      unrealized_pnl: -283.5,
      trading_fees: 36.7,
      funding_pnl: -8.4,
      slippage_cost: 4.2,
      net_pnl: -332.8,
      created_at: '2026-09-10T08:15:00Z',
      last_updated: '2026-09-10T10:45:00Z',
      grid_levels: [
        { level: 1, price: 92200.0, size: 0.40, status: 'FILLED', filled_at: '2026-09-10T08:15:00Z' },
        { level: 2, price: 91440.0, size: 0.44, status: 'FILLED', filled_at: '2026-09-10T09:40:00Z' },
        { level: 3, price: 90450.0, size: 0.484, status: 'PENDING' },
        { level: 4, price: 89100.0, size: 0.528, status: 'PENDING' },
        { level: 5, price: 87400.0, size: 0.572, status: 'PENDING' },
      ],
    },
    {
      basket_id: 'BSK-ETH-20260910-002',
      venue: 'binance_usdm',
      instrument: 'ETHUSDT',
      direction: 'LONG',
      state: 'PROFITABLE',
      grid_depth: 1,
      max_grid_levels: 5,
      total_size: 12.0, // ETH
      average_entry: 3315.0,
      current_mark_price: 3342.2,
      unrealized_pnl: 326.4,
      trading_fees: 18.2,
      funding_pnl: -3.8,
      slippage_cost: 2.1,
      net_pnl: 302.3,
      created_at: '2026-09-10T07:30:00Z',
      last_updated: '2026-09-10T10:50:00Z',
      grid_levels: [
        { level: 1, price: 3315.0, size: 12.0, status: 'FILLED', filled_at: '2026-09-10T07:30:00Z' },
        { level: 2, price: 3270.0, size: 12.0, status: 'PENDING' },
        { level: 3, price: 3215.0, size: 13.2, status: 'PENDING' },
        { level: 4, price: 3145.0, size: 14.4, status: 'PENDING' },
        { level: 5, price: 3050.0, size: 15.6, status: 'PENDING' },
      ],
    },
  ] as SimulatedBasket[],
  orders: [
    {
      id: 'ORD-BTC-001001',
      clientOrderId: 'B-SYS-9122',
      basketId: 'BSK-BTC-20260910-001',
      venue: 'binance_usdm',
      symbol: 'BTCUSDT',
      source: 'SIMULATED',
      strategy: 'Structural Grid',
      side: 'BUY',
      type: 'LIMIT_MAKER',
      price: 92200.0,
      size: 0.40,
      valueUsd: 36880.0,
      status: 'FILLED',
      filledAt: '2026-09-10T08:15:00Z',
      createdAt: '2026-09-10T08:14:58Z',
      trace: {
        strategyIntent: 'Layer 1 Base Grid Entry',
        opportunityScore: 78.5,
        metaBudgetFactor: 1.0,
        riskGovernorCheck: 'PASS',
        governorRule: 'Leverage within bounds (1.2x)',
        executionRule: 'Post-Only (Maker Fee)',
        feeTier: 'VIP-4 Maker',
        slippageBps: 0,
        sourceClassification: 'EXISTING'
      }
    },
    {
      id: 'ORD-BTC-001002',
      clientOrderId: 'B-SYS-9123',
      basketId: 'BSK-BTC-20260910-001',
      venue: 'binance_usdm',
      symbol: 'BTCUSDT',
      source: 'SIMULATED',
      strategy: 'Structural Grid',
      side: 'BUY',
      type: 'LIMIT_MAKER',
      price: 91440.0,
      size: 0.44,
      valueUsd: 40233.6,
      status: 'FILLED',
      filledAt: '2026-09-10T09:40:00Z',
      createdAt: '2026-09-10T09:35:12Z',
      trace: {
        strategyIntent: 'Layer 2 Grid Expansion',
        opportunityScore: 74.2,
        metaBudgetFactor: 1.1,
        riskGovernorCheck: 'PASS',
        governorRule: 'Drawdown limit OK',
        executionRule: 'Post-Only (Maker Fee)',
        feeTier: 'VIP-4 Maker',
        slippageBps: 0,
        sourceClassification: 'EXISTING'
      }
    },
    {
      id: 'ORD-BTC-001003',
      clientOrderId: 'B-SYS-9124',
      basketId: 'BSK-BTC-20260910-001',
      venue: 'binance_usdm',
      symbol: 'BTCUSDT',
      source: 'SIMULATED',
      strategy: 'Structural Grid',
      side: 'BUY',
      type: 'LIMIT_MAKER',
      price: 90450.0,
      size: 0.484,
      valueUsd: 43777.8,
      status: 'PENDING',
      createdAt: '2026-09-10T09:40:05Z',
      trace: {
        strategyIntent: 'Layer 3 Grid Expansion',
        opportunityScore: 68.9,
        metaBudgetFactor: 1.1,
        riskGovernorCheck: 'WARN',
        governorRule: 'Margin utilization > 15%',
        executionRule: 'Post-Only',
        feeTier: 'VIP-4 Maker',
        slippageBps: 0,
        sourceClassification: 'EXISTING'
      }
    },
    {
      id: 'ORD-ETH-002001',
      clientOrderId: 'E-SYS-5542',
      basketId: 'BSK-ETH-20260910-002',
      venue: 'binance_usdm',
      symbol: 'ETHUSDT',
      strategy: 'Trend / Breakout',
      side: 'BUY',
      type: 'MARKET',
      price: 3315.0,
      size: 12.0,
      valueUsd: 39780.0,
      status: 'FILLED',
      filledAt: '2026-09-10T07:30:00Z',
      createdAt: '2026-09-10T07:29:59Z',
      trace: {
        strategyIntent: 'Range Breakout Momentum Entry',
        opportunityScore: 88.5,
        metaBudgetFactor: 1.5,
        riskGovernorCheck: 'PASS',
        governorRule: 'Momentum confirmation ok',
        executionRule: 'Taker (Aggressive)',
        feeTier: 'VIP-4 Taker',
        slippageBps: 1.2,
        sourceClassification: 'EXISTING'
      }
    }
  ],


  alerts: [
    {
      id: 'ALT-1001',
      type: 'SHOCK',
      severity: 'WARNING',
      title: 'Volatility Shock Detected',
      message: 'BTCUSDT realized volatility spiked > 85th percentile (last 15m). Grid safety score reduced.',
      timestamp: new Date(Date.now() - 1000 * 60 * 12).toISOString(),
      symbol: 'BTCUSDT'
    },
    {
      id: 'ALT-1002',
      type: 'FUNDING',
      severity: 'INFO',
      title: 'Elevated Funding Rate',
      message: 'ETHUSDT funding rate exceeded 0.03% per 8h. Bias shifted to Short carrying yield.',
      timestamp: new Date(Date.now() - 1000 * 60 * 45).toISOString(),
      symbol: 'ETHUSDT'
    },
    {
      id: 'ALT-1003',
      type: 'RISK',
      severity: 'CRITICAL',
      title: 'Margin Utilization Alert',
      message: 'System total margin utilization exceeded 30% stress limit momentarily during flash dip. Portfolio risk manager blocked new Grid expansions.',
      timestamp: new Date(Date.now() - 1000 * 60 * 120).toISOString()
    }
  ],
  correlations: {
    btc_eth_rolling_corr: 0.84,
    aggregate_directional_exposure: 'LONG',
    crypto_beta_exposure_pct: 68.5,
    common_factor_status: 'NORMAL',
  },
  strategy_intents: [
    {
      id: 'INT-GRID-BTC-01',
      engineId: 'structural_grid',
      strategyName: 'Structural Mean-Reversion Grid',
      symbol: 'BTCUSDT',
      direction: 'LONG',
      rawTargetDelta: 0.10,
      opportunityScore: 90.0,
      confidence: 0.90,
      urgency: 'LOW',
      timeHorizon: '12h - 48h',
      hypothesis: 'Price reclaiming prior 24h low; accumulate passive tranches at swing base.',
      regimeFit: 'R0_STRONG_MEAN_REVERSION',
      proposedMaxNotionalUsd: 15000,
    },
    {
      id: 'INT-TREND-BTC-01',
      engineId: 'trend_breakout',
      strategyName: 'Trend / Breakout Following',
      symbol: 'BTCUSDT',
      direction: 'SHORT',
      rawTargetDelta: 0,
      opportunityScore: 0,
      confidence: 0,
      urgency: 'LOW',
      timeHorizon: '4h - 12h',
      hypothesis: 'Awaiting R3/R4 structural confirmation. No current displacement.',
      regimeFit: 'R1_RANGE',
      proposedMaxNotionalUsd: 0,
    },
    {
      id: 'INT-SHOCK-ETH-01',
      engineId: 'shock_momentum',
      strategyName: 'Shock Momentum',
      symbol: 'ETHUSDT',
      direction: 'LONG',
      rawTargetDelta: 0.30,
      opportunityScore: 90.0,
      confidence: 0.60,
      urgency: 'HIGH',
      timeHorizon: '5m - 30m',
      hypothesis: 'Fast liquidity sweep below support reclaimed; targeting mean reversion.',
      regimeFit: 'R5_VOLATILITY_SHOCK',
      proposedMaxNotionalUsd: 6500,
    }
  ],
  meta_allocations: [
    {
      engineId: 'structural_grid',
      strategyName: 'Structural Mean-Reversion Grid',
      baseWeightPct: 35,
      expectedEdgeBps: 45,
      confidence: 0.90,
      regimeFitFactor: 1.15,
      executionQualityFactor: 1.0,
      cryptoBetaDiscount: 0.88,
      finalBudgetFactor: 1.15,
      virtualNotionalCapUsd: 25000,
      status: 'ACTIVE',
    },
    {
      engineId: 'trend_breakout',
      strategyName: 'Trend / Breakout Following',
      baseWeightPct: 25,
      expectedEdgeBps: 0,
      confidence: 0.0,
      regimeFitFactor: 0.0,
      executionQualityFactor: 0.95,
      cryptoBetaDiscount: 0.82,
      finalBudgetFactor: 0.0,
      virtualNotionalCapUsd: 15000,
      status: 'PAUSED',
    },
    {
      engineId: 'shock_momentum',
      strategyName: 'Shock Momentum',
      baseWeightPct: 15,
      expectedEdgeBps: 80,
      confidence: 0.60,
      regimeFitFactor: 1.50,
      executionQualityFactor: 0.90,
      cryptoBetaDiscount: 0.95,
      finalBudgetFactor: 1.45,
      virtualNotionalCapUsd: 8500,
      status: 'ACTIVE',
    }
  ],
  risk_rules: {
    hard_rules: [
      { rule: 'Max Effective Leverage <= 2.0x', current: '1.42x', status: 'PASS' },
      { rule: 'Margin Utilization < 30% Stress Limit', current: '16.4%', status: 'PASS' },
      { rule: 'Hard Drawdown Stop < 8.0%', current: '1.85%', status: 'PASS' },
      { rule: 'Liquidation Distance > 35%', current: 'UNKNOWN', status: 'UNKNOWN' },
      { rule: 'Private WebSocket Heartbeat < 5s', current: '0.8s', status: 'PASS' },
    ],
    soft_rules: [
      { rule: 'Caution Drawdown Threshold (2%)', current: '1.85%', status: 'PASS' },
      { rule: 'Basis Shock Z-Score (< 2.5)', current: '1.15', status: 'PASS' },
      { rule: 'Funding Rate Drag Limit (< 0.05% / 8h)', current: '0.018%', status: 'PASS' },
      { rule: 'Cross-Instrument Long Correlation (< 0.90)', current: '0.84', status: 'PASS' },
    ],
  },
};

// REST API Endpoints
app.get('/api/health', (req: Request, res: Response) => {
  res.json({
    status: 'ok',
    system: 'Blessing AI v0.1',
    timestamp: new Date().toISOString(),
    mode: 'deterministic_engine',
  });
});

// Cloud Run probes this path directly.  Keep it separate from the SPA
// fallback and from the lightweight process health endpoint so a rendered
// HTML document can never be mistaken for a ready Control Plane.  The
// readiness helper performs the server-side Firebase, release-store, and
// Worker OIDC checks and returns 503 when any required dependency is not
// verified.
app.get('/ready', async (_req: Request, res: Response) => {
  try {
    const readiness = await controlPlaneReadiness();
    return res.status(readiness.status === 'ready' ? 200 : 503).json(readiness);
  } catch {
    return res.status(503).json({
      status: 'degraded',
      controlPlaneHealthy: false,
      evidence_status: 'UNVERIFIED',
    });
  }
});

interface ApiKeyProfile {
  id: string;
  name: string;
  apiKey: string;
  apiSecret: string;
  isTestnet: boolean;
  environment: 'TESTNET' | 'MAINNET';
  createdAt: number;
}

let activeProfileId = 'default';
const keyProfiles: Record<string, ApiKeyProfile> = {
  default: {
    id: 'default',
    name: 'Binance Testnet (Unconfigured)',
    apiKey: process.env.BINANCE_TESTNET_API_KEY?.trim() || '',
    apiSecret: process.env.BINANCE_TESTNET_API_SECRET?.trim() || '',
    isTestnet: true,
    environment: 'TESTNET',
    createdAt: Date.now(),
  },
};

function getActiveBinanceCredentials(): ApiKeyProfile {
  return keyProfiles[activeProfileId] || Object.values(keyProfiles)[0] || {
    id: 'default',
    name: 'Binance Testnet (Unconfigured)',
    apiKey: '',
    apiSecret: '',
    isTestnet: true,
    environment: 'TESTNET',
    createdAt: Date.now(),
  };
}

function rejectBrowserMainnetCredentialStorage(res: Response) {
  return res.status(403).json({
    error: 'MAINNET_CREDENTIALS_MUST_USE_SECRET_MANAGER',
    message: 'Mainnet credentials are not accepted through the browser profile store; inject them from Secret Manager into the Worker release.',
    verified: false,
    evidence_status: 'UNVERIFIED',
  });
}

async function verifyBinanceCredentials(apiKey: string, apiSecret: string, isTestnet: boolean) {
  const environment = isTestnet ? 'TESTNET' : 'MAINNET';
  if (!apiKey || !apiSecret) {
    return {
      configured: false,
      isTestnet,
      environment,
      message: `Binance ${environment} credentials are missing from configuration.`,
      spot: { authenticated: false, canTrade: false, message: 'No API credentials configured' },
      futures: { authenticated: false, canTrade: false, hedgeMode: false, message: 'No API credentials configured' },
      restrictions: {},
    };
  }

  const maskedKey = apiKey.length >= 8 ? `${apiKey.slice(0, 4)}...${apiKey.slice(-4)}` : '***';
  const spotBase = isTestnet ? 'https://testnet.binance.vision' : 'https://api.binance.com';
  const futuresBase = isTestnet ? 'https://testnet.binancefuture.com' : 'https://fapi.binance.com';

  const results: any = {
    configured: true,
    maskedKey,
    isTestnet,
    environment,
    spot: { authenticated: false, canTrade: false, message: '' },
    futures: { authenticated: false, canTrade: false, hedgeMode: false, message: '' },
    restrictions: {},
  };

  const sign = (secret: string, queryStr: string) => {
    return crypto.createHmac('sha256', secret).update(queryStr).digest('hex');
  };

  try {
    // 1. Check Spot Account
    const spotTs = Date.now();
    const spotQuery = `timestamp=${spotTs}`;
    const spotSig = sign(apiSecret, spotQuery);
    const spotResp = await fetch(`${spotBase}/api/v3/account?${spotQuery}&signature=${spotSig}`, {
      headers: { 'X-MBX-APIKEY': apiKey },
    });
    const spotData: any = await spotResp.json();
    if (spotResp.ok) {
      results.spot.authenticated = true;
      results.spot.canTrade = spotData.canTrade;
      results.spot.message = 'Authenticated successfully on Binance Spot.';
    } else {
      results.spot.message = spotData.msg || `HTTP ${spotResp.status}`;
    }

    // 2. Check Futures Position Mode on the selected fixed environment. This is a capability probe;
    // the Python worker remains the sole execution/readiness authority.
    const fTs = Date.now();
    const fQuery = `timestamp=${fTs}`;
    const fSig = sign(apiSecret, fQuery);
    const fResp = await fetch(`${futuresBase}/fapi/v1/positionSide/dual?${fQuery}&signature=${fSig}`, {
      headers: { 'X-MBX-APIKEY': apiKey },
    });
    const fData: any = await fResp.json();
    if (fResp.ok) {
      results.futures.authenticated = true;
      results.futures.hedgeMode = fData.dualSidePosition;
      results.futures.message = fData.dualSidePosition ? 'Hedge Mode Active' : 'One-Way Mode (Hedge Mode required)';
    } else {
      results.futures.message = fData.msg || `HTTP ${fResp.status}`;
    }

    return results;
  } catch (err: any) {
    return {
      configured: true,
      maskedKey,
      isTestnet,
      environment,
      spot: { authenticated: false, canTrade: false, message: err.message },
      futures: { authenticated: false, canTrade: false, hedgeMode: false, message: err.message },
      restrictions: {},
      error: err.message,
    };
  }
}

app.get('/api/binance/verify-key', async (req: Request, res: Response) => {
  const active = getActiveBinanceCredentials();
  const results = await verifyBinanceCredentials(active.apiKey, active.apiSecret, active.isTestnet);
  res.json({
    ...results,
    activeProfileId: active.id,
    activeProfileName: active.name,
  });
});

app.get('/api/binance/profiles', (req: Request, res: Response) => {
  res.json({
    activeProfileId,
    profiles: Object.values(keyProfiles).map((p) => ({
      id: p.id,
      name: p.name,
      maskedKey: p.apiKey.length >= 8 ? `${p.apiKey.slice(0, 4)}...${p.apiKey.slice(-4)}` : (p.apiKey ? '***' : 'Unconfigured'),
      maskedApiKey: p.apiKey.length >= 8 ? `${p.apiKey.slice(0, 4)}...${p.apiKey.slice(-4)}` : (p.apiKey ? '***' : 'Unconfigured'),
      isTestnet: p.isTestnet,
      environment: p.environment,
      isLiveRealMoney: p.environment === 'MAINNET',
      hasSecret: Boolean(p.apiSecret),
      isActive: p.id === activeProfileId,
    })),
  });
});

app.post('/api/binance/profiles/switch', async (req: Request, res: Response) => {
  const { profileId } = req.body;
  if (!profileId || !keyProfiles[profileId]) {
    return res.status(400).json({ error: 'Profile not found' });
  }
  const active = keyProfiles[profileId];
  if (!active.isTestnet || active.environment !== 'TESTNET') {
    return rejectBrowserMainnetCredentialStorage(res);
  }
  activeProfileId = profileId;
  if (active.isTestnet) {
    process.env.BINANCE_TESTNET_API_KEY = active.apiKey;
    process.env.BINANCE_TESTNET_API_SECRET = active.apiSecret;
    process.env.BINANCE_TESTNET = 'true';
  }

  const results = await verifyBinanceCredentials(active.apiKey, active.apiSecret, active.isTestnet);
  res.json({
    success: true,
    activeProfileId,
    activeProfileName: active.name,
    results,
  });
});

app.post('/api/binance/profiles/save', async (req: Request, res: Response) => {
  try {
    const { id, name, apiKey, apiSecret, isTestnet, environment, makeActive } = req.body;
    if (!name || typeof name !== 'string') {
      return res.status(400).json({ error: 'Profile name is required' });
    }

    const requestedTestnet =
      typeof isTestnet === 'boolean'
        ? isTestnet
        : environment === undefined
          ? true
          : environment === 'TESTNET';
    const requestedEnvironment = requestedTestnet ? 'TESTNET' : 'MAINNET';

    if (!requestedTestnet || requestedEnvironment !== 'TESTNET') {
      return rejectBrowserMainnetCredentialStorage(res);
    }

    const trimmedKey = (apiKey || '').trim();
    const trimmedSecret = (apiSecret || '').trim();

    let profileId = id;
    if (profileId && keyProfiles[profileId]) {
      const existing = keyProfiles[profileId];
      existing.name = name.trim();
      if (trimmedKey) existing.apiKey = trimmedKey;
      if (trimmedSecret) existing.apiSecret = trimmedSecret;
      existing.isTestnet = requestedTestnet;
      existing.environment = requestedEnvironment;
    } else {
      profileId = profileId || `profile_${Date.now()}`;
      keyProfiles[profileId] = {
        id: profileId,
        name: name.trim(),
        apiKey: trimmedKey,
        apiSecret: trimmedSecret,
        isTestnet: requestedTestnet,
        environment: requestedEnvironment,
        createdAt: Date.now(),
      };
    }

    if (makeActive !== false) {
      activeProfileId = profileId;
      const active = keyProfiles[activeProfileId];
      if (active.isTestnet) {
        process.env.BINANCE_TESTNET_API_KEY = active.apiKey;
        process.env.BINANCE_TESTNET_API_SECRET = active.apiSecret;
        process.env.BINANCE_TESTNET = 'true';
      }
    }

    const active = keyProfiles[activeProfileId];
    const results = await verifyBinanceCredentials(active.apiKey, active.apiSecret, active.isTestnet);
    res.json({
      success: true,
      activeProfileId,
      profileId,
      results,
    });
  } catch (err: any) {
    res.status(500).json({ error: err.message });
  }
});

async function fetchBinanceLiveBalances(apiKey: string, apiSecret: string, isTestnet: boolean) {
  if (!apiKey || !apiSecret) {
    return {
      success: false,
      configured: false,
      message: 'No Binance API credentials configured.',
    };
  }

  const environment = isTestnet ? 'TESTNET' : 'MAINNET';
  const spotBase = isTestnet ? 'https://testnet.binance.vision' : 'https://api.binance.com';
  const futuresBase = isTestnet ? 'https://testnet.binancefuture.com' : 'https://fapi.binance.com';

  const sign = (secret: string, queryStr: string) => {
    return crypto.createHmac('sha256', secret).update(queryStr).digest('hex');
  };

  try {
    const ts = Date.now();
    const query = `timestamp=${ts}`;
    const sig = sign(apiSecret, query);

    // Fetch in parallel: Spot Account, Prices, Wallet Balances, Portfolio Margin, Simple Earn Flexible, Simple Earn Locked, and Futures
    let spotTotalUsd = 0;
    let spotSuccess = false;
    let spotError = '';
    let holdings: Array<{ asset: string; qty: number; unitPrice: number; usdVal: number }> = [];

    const [
      spotResp,
      tickerResp,
      walletsResp,
      pmResp,
      earnFlexResp,
      earnLockedResp,
      fResp,
    ] = await Promise.all([
      fetch(`${spotBase}/api/v3/account?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch((e) => ({ ok: false, status: 500, json: async () => ({ msg: e.message }) })),
      fetch(`${spotBase}/api/v3/ticker/price`).catch(() => null),
      fetch(`${spotBase}/sapi/v1/asset/wallet/balance?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
      fetch(`${spotBase}/sapi/v1/portfolio/balance?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
      fetch(`${spotBase}/sapi/v1/simple-earn/flexible/position?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
      fetch(`${spotBase}/sapi/v1/simple-earn/locked/position?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
      fetch(`${futuresBase}/fapi/v2/account?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
    ]);

    // Build price map
    const priceMap: Record<string, number> = {};
    if (tickerResp && (tickerResp as any).ok) {
      const tickerList = await (tickerResp as any).json();
      if (Array.isArray(tickerList)) {
        tickerList.forEach((p: any) => {
        const price = Number(p.price);
        if (typeof p.symbol === 'string' && Number.isFinite(price) && price > 0) {
          priceMap[p.symbol] = price;
        }
        });
      }
    }
    const btcPrice = priceMap['BTCUSDT'];
    const stableCoins = new Set(['USDT', 'USDC', 'BUSD', 'FDUSD', 'USDE', 'TUSD', 'DAI']);

    // Parse Spot
    const spotData = await (spotResp as any).json();
    let valuationComplete = true;
    if ((spotResp as any).ok && Array.isArray(spotData.balances)) {
      spotSuccess = true;
      spotData.balances.forEach((b: any) => {
        const qty = parseFloat(b.free || '0') + parseFloat(b.locked || '0');
        if (qty <= 0) return;

        const asset = b.asset;
        const cleanAsset = asset.startsWith('LD') ? asset.slice(2) : asset;
        let unitPrice = 0;

        if (stableCoins.has(asset) || stableCoins.has(cleanAsset)) {
          unitPrice = 1.0;
        } else if (priceMap[`${cleanAsset}USDT`]) {
          unitPrice = priceMap[`${cleanAsset}USDT`];
        } else if (priceMap[`${asset}USDT`]) {
          unitPrice = priceMap[`${asset}USDT`];
        }

        if (qty > 0 && (!Number.isFinite(unitPrice) || unitPrice <= 0)) {
          valuationComplete = false;
          return;
        }

        const usdVal = qty * unitPrice;
        if (usdVal > 0.0001 || qty > 0.0001) {
          holdings.push({
            asset,
            qty: parseFloat(qty.toFixed(8)),
            unitPrice: parseFloat(unitPrice.toFixed(4)),
            usdVal: parseFloat(usdVal.toFixed(4)),
          });
          spotTotalUsd += usdVal;
        }
      });
      holdings.sort((a, b) => b.usdVal - a.usdVal);
    } else {
      spotError = spotData?.msg || `Spot error HTTP ${(spotResp as any).status}`;
    }

    // Parse Wallets Balance (all sub-accounts)
    let walletsData: any[] = [];
    if (walletsResp && (walletsResp as any).ok) {
      try {
        walletsData = await (walletsResp as any).json();
      } catch {}
    }

    const sub_wallets: Array<{
      walletName: string;
      category: 'TRADING_BOT' | 'PORTFOLIO_MARGIN' | 'EARN' | 'SPOT' | 'FUNDING';
      btcVal: number;
      usdVal: number;
      pctOfTotal: number;
    }> = [];

    let totalWalletsUsd = 0;
    if (Array.isArray(walletsData)) {
      if (walletsData.some((wallet) => Number(wallet.balance) > 0) && (!Number.isFinite(btcPrice) || btcPrice <= 0)) {
        valuationComplete = false;
      }
      walletsData.forEach((w: any) => {
        const btc = parseFloat(w.balance || '0');
        const usdVal = Number.isFinite(btcPrice) ? parseFloat((btc * btcPrice).toFixed(2)) : 0;
        if (usdVal > 0.001 || btc > 0) {
          let category: 'TRADING_BOT' | 'PORTFOLIO_MARGIN' | 'EARN' | 'SPOT' | 'FUNDING' = 'SPOT';
          if (w.walletName.includes('Trading Bot')) category = 'TRADING_BOT';
          else if (w.walletName.includes('Cross Margin') || w.walletName.includes('Portfolio') || w.walletName.includes('PM')) category = 'PORTFOLIO_MARGIN';
          else if (w.walletName.includes('Earn')) category = 'EARN';
          else if (w.walletName.includes('Funding')) category = 'FUNDING';

          sub_wallets.push({
            walletName: w.walletName,
            category,
            btcVal: parseFloat(btc.toFixed(8)),
            usdVal,
            pctOfTotal: 0,
          });
          totalWalletsUsd += usdVal;
        }
      });
    }

    sub_wallets.forEach((sw) => {
      sw.pctOfTotal = totalWalletsUsd > 0 ? parseFloat(((sw.usdVal / totalWalletsUsd) * 100).toFixed(1)) : 0;
    });
    sub_wallets.sort((a, b) => b.usdVal - a.usdVal);

    // Portfolio Margin is a separate account product from the USDⓈ-M
    // Futures Testnet worker.  Keep this as an explicitly read-only
    // observation: it can never authorize the worker or be silently treated
    // as Futures collateral.
    let portfolioMarginObservation = unavailablePortfolioMarginObservation();

    if (pmResp && (pmResp as any).ok) {
      try {
        const rawPmData = await (pmResp as any).json();
        portfolioMarginObservation = parsePortfolioMarginResponse(rawPmData);
      } catch (err: any) {
        portfolioMarginObservation = {
          ...unavailablePortfolioMarginObservation(
            err?.message || 'Portfolio Margin response could not be read.',
          ),
          status: 'INVALID_RESPONSE',
        };
      }
    } else if (pmResp) {
      portfolioMarginObservation.message =
        `Portfolio Margin read-only endpoint unavailable (HTTP ${(pmResp as any).status}).`;
    }

    // Parse Flexible Earn
    let earnFlexData: any = { rows: [] };
    if (earnFlexResp && (earnFlexResp as any).ok) {
      try {
        earnFlexData = await (earnFlexResp as any).json();
      } catch {}
    }

    // Parse Locked Earn
    let earnLockedData: any = { rows: [] };
    if (earnLockedResp && (earnLockedResp as any).ok) {
      try {
        earnLockedData = await (earnLockedResp as any).json();
      } catch {}
    }

    // Parse Futures
    let futuresWalletBalance = 0;
    let futuresMarginBalance = 0;
    let futuresUnrealizedPnl = 0;
    let futuresAvailableMargin = 0;
    let futuresUsedMargin = 0;
    let futuresSuccess = false;
    let futuresError = '';
    let totalPositionNotional = 0;

    if (fResp && (fResp as any).ok) {
      try {
        const fData = await (fResp as any).json();
        const requiredAccountFields = [
          'totalWalletBalance',
          'totalMarginBalance',
          'totalUnrealizedProfit',
          'availableBalance',
          'totalPositionInitialMargin',
        ];
        const missingAccountField = requiredAccountFields.find((field) => {
          const value = Number(fData[field]);
          return fData[field] === undefined || fData[field] === null || !Number.isFinite(value);
        });

        if (missingAccountField || !Array.isArray(fData.positions)) {
          futuresError = `Futures account snapshot missing required field: ${missingAccountField || 'positions'}`;
        } else {
          futuresSuccess = true;
          futuresWalletBalance = Number(fData.totalWalletBalance);
          futuresMarginBalance = Number(fData.totalMarginBalance);
          futuresUnrealizedPnl = Number(fData.totalUnrealizedProfit);
          futuresAvailableMargin = Number(fData.availableBalance);
          futuresUsedMargin = Number(fData.totalPositionInitialMargin);

          fData.positions.forEach((pos: any) => {
            const positionAmount = Number(pos.positionAmt);
            const notional = Number(pos.notional);
            if (!Number.isFinite(positionAmount) || !Number.isFinite(notional)) {
              if (Number.isFinite(positionAmount) && Math.abs(positionAmount) > 0) {
                futuresSuccess = false;
                futuresError = 'Futures position snapshot contains an unusable position/notional value';
              }
              return;
            }
            totalPositionNotional += Math.abs(notional);
          });
        }
      } catch (e: any) {
        futuresError = e.message;
      }
    }

    // ==========================================
    // BUILD 2-LAYER ASSET ALLOCATION STRUCTURE
    // Layer 1: Asset / Coin
    // Layer 2: Allocations (Trading Bot, Portfolio Margin, Earn, Spot)
    // ==========================================
    const botWallet = sub_wallets.find((w) => w.category === 'TRADING_BOT');
    const botUsd = botWallet ? botWallet.usdVal : 0;

    const twoLayerMap: Record<
      string,
      {
        asset: string;
        allocations: Array<{
          location: string;
          category: 'TRADING_BOT' | 'PORTFOLIO_MARGIN' | 'EARN' | 'SPOT' | 'FUNDING';
          qty: number;
          usdVal: number;
          pctOfAsset: number;
          detail?: string;
        }>;
      }
    > = {};

    const getLayerAsset = (name: string) => {
      if (!twoLayerMap[name]) {
        twoLayerMap[name] = { asset: name, allocations: [] };
      }
      return twoLayerMap[name];
    };

    // 1. Trading Bot: include only a wallet balance returned by Binance.
    if (botUsd > 0) {
      getLayerAsset('USDC').allocations.push({
        location: 'Trading Bot',
        category: 'TRADING_BOT',
        qty: parseFloat(botUsd.toFixed(2)),
        usdVal: parseFloat(botUsd.toFixed(2)),
        pctOfAsset: 0,
        detail: 'Active Grid / Strategy Bot',
      });
    }

    // 2. Portfolio Margin (PM)
    if (portfolioMarginObservation.status === 'OBSERVED_READ_ONLY') {
      portfolioMarginObservation.balances.forEach((item) => {
        // This allocation is specifically the documented cross-margin asset
        // balance.  Do not substitute totalWalletBalance: that field can
        // include other Portfolio Margin components and would risk double
        // counting against the separate Futures account below.
        const qty = item.crossMarginAsset;
        if (qty > 0) {
          getLayerAsset(item.asset).allocations.push({
            location: 'Portfolio Margin',
            category: 'PORTFOLIO_MARGIN',
            qty: parseFloat(qty.toFixed(6)),
            usdVal: 0,
            pctOfAsset: 0,
            detail: 'Cross Margin (PM)',
          });
        }
      });
    }

    // 3. Simple Earn Flexible
    if (Array.isArray(earnFlexData?.rows)) {
      earnFlexData.rows.forEach((row: any) => {
        const qty = parseFloat(row.totalAmount || '0');
        if (qty > 0) {
          const apr = parseFloat(row.latestAnnualPercentageRate || '0') * 100;
          getLayerAsset(row.asset).allocations.push({
            location: 'Simple Earn (Flexible)',
            category: 'EARN',
            qty: parseFloat(qty.toFixed(6)),
            usdVal: 0,
            pctOfAsset: 0,
            detail: apr > 0 ? `APR ${apr.toFixed(2)}%` : 'Flexible Earn',
          });
        }
      });
    }

    // 4. Simple Earn Locked
    if (Array.isArray(earnLockedData?.rows)) {
      earnLockedData.rows.forEach((row: any) => {
        const qty = parseFloat(row.amount || '0');
        if (qty > 0) {
          getLayerAsset(row.asset).allocations.push({
            location: `Simple Earn (Locked ${row.duration}D)`,
            category: 'EARN',
            qty: parseFloat(qty.toFixed(6)),
            usdVal: 0,
            pctOfAsset: 0,
            detail: `Locked ${row.duration} Days`,
          });
        }
      });
    }

    // 5. Spot Balances (excluding LD... tokens which are already captured in Simple Earn)
    if (Array.isArray(spotData?.balances)) {
      spotData.balances.forEach((b: any) => {
        const qty = parseFloat(b.free || '0') + parseFloat(b.locked || '0');
        if (qty <= 0) return;

        if (b.asset.startsWith('LD')) {
          // Token is Simple Earn receipt token (e.g. LDUSDT, LDETH, LDUSDC)
          const cleanAsset = b.asset.slice(2);
          // Check if already present in Earn allocations
          const existingEarn = getLayerAsset(cleanAsset).allocations.find((a) => a.category === 'EARN');
          if (!existingEarn) {
            getLayerAsset(cleanAsset).allocations.push({
              location: 'Simple Earn (Flexible)',
              category: 'EARN',
              qty: parseFloat(qty.toFixed(6)),
              usdVal: 0,
              pctOfAsset: 0,
              detail: 'Flexible Earn (LD)',
            });
          }
        } else {
          // Pure Spot
          getLayerAsset(b.asset).allocations.push({
            location: 'Spot Wallet',
            category: 'SPOT',
            qty: parseFloat(qty.toFixed(6)),
            usdVal: 0,
            pctOfAsset: 0,
            detail: 'Available in Spot',
          });
        }
      });
    }

    // Calculate Prices, Values, and Percentages for each 2-Layer Asset
    let totalPortfolioVal = 0;

    const two_layer_assets = Object.values(twoLayerMap)
      .map((entry) => {
        const asset = entry.asset;
        let unitPrice = 0;
        if (stableCoins.has(asset)) {
          unitPrice = 1.0;
        } else if (priceMap[`${asset}USDT`]) {
          unitPrice = priceMap[`${asset}USDT`];
        }

        let totalQty = 0;
        entry.allocations.forEach((al) => {
          totalQty += al.qty;
          al.usdVal = parseFloat((al.qty * unitPrice).toFixed(4));
        });

        const totalUsdVal = parseFloat((totalQty * unitPrice).toFixed(2));
        totalPortfolioVal += totalUsdVal;

        entry.allocations.forEach((al) => {
          al.pctOfAsset = totalQty > 0 ? parseFloat(((al.qty / totalQty) * 100).toFixed(1)) : 0;
        });

        entry.allocations.sort((x, y) => y.usdVal - x.usdVal);

        return {
          asset,
          totalQty: parseFloat(totalQty.toFixed(6)),
          unitPrice: parseFloat(unitPrice.toFixed(4)),
          totalUsdVal,
          pctOfPortfolio: 0,
          allocations: entry.allocations,
        };
      })
      .filter((a) => a.totalUsdVal > 0.001 || a.totalQty > 0.001);

    two_layer_assets.forEach((a) => {
      a.pctOfPortfolio =
        totalPortfolioVal > 0 ? parseFloat(((a.totalUsdVal / totalPortfolioVal) * 100).toFixed(1)) : 0;
    });

    two_layer_assets.sort((a, b) => b.totalUsdVal - a.totalUsdVal);

    // Final Equity and Balance
    const finalEquity = totalPortfolioVal > 0 ? totalPortfolioVal : spotTotalUsd + futuresMarginBalance;
    const finalBalance = finalEquity;
    const marginUtilization = finalEquity > 0 ? (futuresUsedMargin / finalEquity) * 100 : 0;
    const effectiveLeverage = finalEquity > 0 ? totalPositionNotional / finalEquity : 0;

    const snapshotValid = spotSuccess && futuresSuccess && valuationComplete;

    return {
      success: snapshotValid,
      configured: true,
      spotSuccess,
      futuresSuccess,
      spot_balance: spotTotalUsd,
      futures_wallet_balance: futuresWalletBalance,
      futures_margin_balance: futuresMarginBalance,
      futures_unrealized_pnl: futuresUnrealizedPnl,
      free_margin: futuresAvailableMargin,
      used_margin: futuresUsedMargin,
      equity: finalEquity,
      balance: finalBalance,
      margin_utilization_pct: marginUtilization,
      effective_leverage: effectiveLeverage,
      daily_pnl: futuresUnrealizedPnl,
      daily_pnl_pct: finalBalance > 0 ? (futuresUnrealizedPnl / finalBalance) * 100 : 0,
      holdings,
      two_layer_assets,
      sub_wallets,
      portfolio_margin_observation: portfolioMarginObservation,
      source: isTestnet ? 'BINANCE_TESTNET' : 'BINANCE_MAINNET',
      environment,
      last_sync_time: new Date().toISOString(),
      error: snapshotValid
        ? undefined
        : [
            spotError,
            futuresError,
            !valuationComplete ? 'One or more non-zero assets could not be valued reliably' : '',
          ].filter(Boolean).join('; ') || 'Account snapshot is incomplete or invalid.',
    };
  } catch (err: any) {
    return {
      success: false,
      configured: true,
      error: err.message,
      message: `Binance balance fetch exception: ${err.message}`,
    };
  }
}

app.post('/api/binance/profiles/delete', (req: Request, res: Response) => {
  const { profileId } = req.body;
  const profileKeys = Object.keys(keyProfiles);
  if (profileKeys.length <= 1) {
    return res.status(400).json({ error: 'Cannot delete the only remaining profile' });
  }
  if (!keyProfiles[profileId]) {
    return res.status(404).json({ error: 'Profile not found' });
  }
  delete keyProfiles[profileId];
  if (activeProfileId === profileId) {
    activeProfileId = Object.keys(keyProfiles)[0];
    const active = keyProfiles[activeProfileId];
    if (active.isTestnet) {
      process.env.BINANCE_TESTNET_API_KEY = active.apiKey;
      process.env.BINANCE_TESTNET_API_SECRET = active.apiSecret;
      process.env.BINANCE_TESTNET = 'true';
    }
  }
  res.json({ success: true, activeProfileId });
});

// Sync and fetch funds directly from Binance API
app.post('/api/binance/sync-account', async (req: Request, res: Response) => {
  const active = getActiveBinanceCredentials();
  const liveResult = await fetchBinanceLiveBalances(active.apiKey, active.apiSecret, active.isTestnet);

  if (liveResult.success) {
    quantEngineState.account.equity = liveResult.equity!;
    quantEngineState.account.balance = liveResult.balance!;
    quantEngineState.account.margin_utilization_pct = liveResult.margin_utilization_pct!;
    quantEngineState.account.effective_leverage = liveResult.effective_leverage!;
    quantEngineState.account.free_margin = liveResult.free_margin!;
    quantEngineState.account.used_margin = liveResult.used_margin!;
    quantEngineState.account.daily_pnl = liveResult.daily_pnl!;
    quantEngineState.account.daily_pnl_pct = liveResult.daily_pnl_pct!;
    (quantEngineState.account as any).source = liveResult.source;
    // This control-plane REST snapshot is not the Python worker's
    // authenticated/private-stream/reconciled account truth.  Keep it
    // visible as an unverified read-only observation and never promote it to
    // execution readiness.
    (quantEngineState.account as any).evidence_status = 'UNVERIFIED';
    (quantEngineState.account as any).verified = false;
    (quantEngineState.account as any).spot_balance = liveResult.spot_balance;
    (quantEngineState.account as any).futures_wallet_balance = liveResult.futures_wallet_balance;
    (quantEngineState.account as any).futures_unrealized_pnl = liveResult.futures_unrealized_pnl;
    (quantEngineState.account as any).last_sync_time = liveResult.last_sync_time;
    (quantEngineState.account as any).account_alias = active.name;
    (quantEngineState.account as any).holdings = liveResult.holdings;
    (quantEngineState.account as any).two_layer_assets = liveResult.two_layer_assets;
    (quantEngineState.account as any).sub_wallets = liveResult.sub_wallets;
    (quantEngineState.account as any).portfolio_margin_observation =
      liveResult.portfolio_margin_observation;

    // Direct REST sync cannot pass worker-owned readiness checks.
    tradingSystemState.accountSynchronized = false;
    tradingSystemState.privateStreamHealthy = false;
    tradingSystemState.tradingConnectionHealthy = false;
    tradingSystemState.reconciliationStatus = 'UNKNOWN';
    tradingSystemState.dataSource = 'BINANCE';
    tradingSystemState.exchangeEnvironment = active.isTestnet ? 'BINANCE_TESTNET' : 'BINANCE_MAINNET';
    tradingSystemState.updatedAt = new Date().toISOString();

    return res.json({
      success: false,
      read_only_snapshot: true,
      evidence_status: 'UNVERIFIED',
      message: `Fetched a Binance ${active.environment} read-only snapshot (${active.name}); Python worker reconciliation is still required.`,
      account: quantEngineState.account,
      liveResult,
    });
  } else {
    tradingSystemState.accountSynchronized = false;
    tradingSystemState.reconciliationStatus = 'UNKNOWN';
    tradingSystemState.updatedAt = new Date().toISOString();
    (quantEngineState.account as any).source = 'SIMULATED';
    (quantEngineState.account as any).evidence_status = 'UNVERIFIED';
    (quantEngineState.account as any).verified = false;
    // If not configured or API call rejected, return current state with diagnostic details
    return res.json({
      success: false,
      message: liveResult.message || 'Could not fetch live balance from Binance. Using current portfolio state.',
      account: quantEngineState.account,
      details: liveResult,
    });
  }
});

app.get('/api/binance/balance', async (req: Request, res: Response) => {
  const active = getActiveBinanceCredentials();
  const liveResult = await fetchBinanceLiveBalances(active.apiKey, active.apiSecret, active.isTestnet);
  res.json(liveResult);
});


// --- System Truth & Safety Boundary Endpoints ---


// The worker URL and its Cloud Run identity token are deployment inputs. A
// production control plane must never silently fall back to localhost or an
// unauthenticated worker.
const WORKER_URL = (process.env.WORKER_URL?.trim() || (process.env.NODE_ENV === 'production' ? '' : 'http://127.0.0.1:8080')).replace(/\/+$/, '');
// A static token is accepted only as a local test override. Production always
// mints a Google-signed OIDC token through ADC with the Worker URL as its
// audience; Firebase user tokens never cross this service boundary.
const LOCAL_WORKER_IDENTITY_TOKEN = process.env.WORKER_IDENTITY_TOKEN?.trim() || '';
const workerGoogleAuth = new GoogleAuth();
let workerIdentityClient: Awaited<ReturnType<GoogleAuth['getIdTokenClient']>> | null = null;

async function workerAuthorizationHeader(): Promise<string | null> {
  const localBypass = process.env.NODE_ENV !== 'production'
    && ['1', 'true', 'yes', 'on'].includes((process.env.CONTROL_PLANE_ALLOW_UNAUTHENTICATED_LOCAL || '').trim().toLowerCase());
  if (localBypass) return null;
  if (!WORKER_URL) throw new Error('WORKER_URL is not configured');
  if (process.env.NODE_ENV !== 'production' && LOCAL_WORKER_IDENTITY_TOKEN) {
    return `Bearer ${LOCAL_WORKER_IDENTITY_TOKEN}`;
  }
  if (!workerIdentityClient) {
    // getIdTokenClient binds the exact Worker URL into the OIDC audience.
    workerIdentityClient = await workerGoogleAuth.getIdTokenClient(WORKER_URL);
  }
  const headers = await workerIdentityClient.getRequestHeaders();
  const authorization = headers.get('authorization');
  if (!authorization) throw new Error('Google Worker identity token could not be minted');
  return authorization;
}

async function forwardWorkerRequest(
  pathName: string,
  init?: RequestInit,
): Promise<{ response: globalThis.Response; data: any }> {
  if (!WORKER_URL) throw new Error('WORKER_URL is not configured');
  const headers = new Headers(init?.headers);
  const workerAuthorization = await workerAuthorizationHeader();
  if (workerAuthorization) headers.set('Authorization', workerAuthorization);
  headers.set('X-Worker-Caller', 'blessing-control-plane');
  let response: globalThis.Response;
  try {
    response = await fetch(WORKER_URL + pathName, { ...init, headers });
  } catch (error) {
    console.warn(
      `monitor_event=control_plane_oidc_failure path=${pathName.split('?')[0]} error_class=${error instanceof Error ? error.name : 'unknown'}`,
    );
    throw error;
  }
  if (response.status === 401 || response.status === 403) {
    console.warn(
      `monitor_event=control_plane_oidc_failure path=${pathName.split('?')[0]} http_status=${response.status}`,
    );
  }
  const bodyText = await response.text();
  let data: any = {};
  if (bodyText) {
    try {
      data = JSON.parse(bodyText);
    } catch {
      data = { detail: bodyText };
    }
  }
  return { response, data };
}

async function rollbackAutonomousContinuation(): Promise<{
  verified: boolean;
  workerState: any;
  error?: string;
}> {
  // A successful Worker transition followed by a lost/failed release-store
  // write is an uncertain external-effect boundary. Reconcile it by asking
  // the Worker to DISARM, then verify the resulting state independently. Do
  // not report the continuation as safely stopped unless both calls and the
  // state read-back prove it.
  try {
    const disarm = await forwardWorkerRequest('/disarm', { method: 'POST' });
    if (!disarm.response.ok) {
      return {
        verified: false,
        workerState: disarm.data,
        error: `Worker DISARM rejected with HTTP ${disarm.response.status}`,
      };
    }
    const readback = await forwardWorkerRequest('/state');
    const workerState = releaseRequestObject(readback.data) || {};
    const verified = (
      readback.response.ok
      && ['DISARMED', 'EMERGENCY'].includes(String(workerState.engine_state || ''))
      && workerState.mainnet_launch_state !== 'AUTONOMOUS_ACTIVE'
    );
    projectWorkerState(workerState);
    return {
      verified,
      workerState,
      ...(verified ? {} : { error: 'Worker DISARM read-back did not prove a safe state' }),
    };
  } catch (error) {
    return {
      verified: false,
      workerState: {},
      error: error instanceof Error ? error.message : 'Worker rollback failed',
    };
  }
}

async function releaseContinuationApprovalBestEffort(continuationId: string): Promise<void> {
  // A claimed (PENDING -> ACTIVATING) continuation approval must not be
  // stranded by a failure that happens after the claim -- otherwise every
  // retry with the same continuationApprovalId fails immediately until its
  // fixed expiry, forcing a brand new approval for what may be a purely
  // transient failure. This is best-effort: a failure here must never mask
  // the original error response.
  try {
    await getServerReleaseStore().releaseContinuationApproval(continuationId);
  } catch (error) {
    console.warn(
      `monitor_event=continuation_approval_release_failed error=${error instanceof Error ? error.message : 'unknown'}`,
    );
  }
}

function projectWorkerState(workerState: any): void {
  if (!workerState || typeof workerState !== 'object') return;
  if (typeof workerState.execution_mode === 'string') {
    tradingSystemState.executionMode = workerState.execution_mode;
    tradingSystemState.exchangeEnvironment =
      workerState.execution_mode === 'TESTNET'
        ? 'BINANCE_TESTNET'
        : workerState.execution_mode === 'LIVE'
          ? 'BINANCE_MAINNET'
          : 'NONE';
    tradingSystemState.dataSource =
      workerState.execution_mode === 'TESTNET' || workerState.execution_mode === 'LIVE'
        ? 'BINANCE'
        : 'SIMULATED';
  }
  if (typeof workerState.engine_state === 'string') tradingSystemState.engineState = workerState.engine_state;
  if (typeof workerState.market_data_healthy === 'boolean') tradingSystemState.marketDataHealthy = workerState.market_data_healthy;
  if (typeof workerState.private_stream_healthy === 'boolean') tradingSystemState.privateStreamHealthy = workerState.private_stream_healthy;
  if (typeof workerState.trading_connection_healthy === 'boolean') tradingSystemState.tradingConnectionHealthy = workerState.trading_connection_healthy;
  if (typeof workerState.account_synchronized === 'boolean') tradingSystemState.accountSynchronized = workerState.account_synchronized;
  if (typeof workerState.reconciliation_status === 'string') tradingSystemState.reconciliationStatus = workerState.reconciliation_status;
  if (typeof workerState.kill_switch_active === 'boolean') tradingSystemState.killSwitchActive = workerState.kill_switch_active;
  if (typeof workerState.pause_new_risk === 'boolean') tradingSystemState.pauseNewRisk = workerState.pause_new_risk;
  if (typeof workerState.recovery_only === 'boolean') tradingSystemState.recoveryOnly = workerState.recovery_only;
  if (typeof workerState.worker_responsive === 'boolean') tradingSystemState.workerResponsive = workerState.worker_responsive;
  if (typeof workerState.mainnet_credentials_verified === 'boolean') tradingSystemState.mainnetCredentialsVerified = workerState.mainnet_credentials_verified;
  if (typeof workerState.mainnet_live_approved === 'boolean') tradingSystemState.mainnetLiveApproved = workerState.mainnet_live_approved;
  if (typeof workerState.mainnet_preflight_ready === 'boolean') tradingSystemState.mainnetPreflightReady = workerState.mainnet_preflight_ready;
  if (typeof workerState.mainnet_launch_policy === 'string') tradingSystemState.mainnetLaunchPolicy = workerState.mainnet_launch_policy;
  if (typeof workerState.mainnet_launch_id === 'string') tradingSystemState.mainnetLaunchId = workerState.mainnet_launch_id;
  if (typeof workerState.mainnet_launch_state === 'string') tradingSystemState.mainnetLaunchState = workerState.mainnet_launch_state;
  if (typeof workerState.mainnet_continuation_approval_id === 'string') tradingSystemState.mainnetContinuationApprovalId = workerState.mainnet_continuation_approval_id;
  if (typeof workerState.updated_at === 'string') tradingSystemState.updatedAt = workerState.updated_at;
}

async function enforceOperatorAccess(req: Request, res: Response, next: () => void): Promise<void> {
  // Express strips the mounted prefix from `req.path` inside app.use(). Use
  // the reconstructed route so /api/release/mainnet/approve cannot be
  // downgraded to the generic operator role when it is evaluated as
  // /mainnet/approve.
  const routeForAuthorization = {
    method: req.method,
    path: `${req.baseUrl || ''}${req.path || ''}`,
    body: req.body,
  };
  const requiredRole: ControlPlaneRole = requiredControlPlaneRole(routeForAuthorization);
  const authorization = await authorizeOperatorRequest(req, { requiredRole });
  if (authorization.ok) {
    res.locals.firebaseUid = authorization.uid;
    res.locals.firebaseRoles = authorization.roles;
    next();
    return;
  }
  console.warn(
    `monitor_event=control_plane_auth_failure route=${req.path} reason=${authorization.forbidden ? 'forbidden' : 'unauthorized'}`,
  );
  res.status(authorization.forbidden ? 403 : 401).json({
    error: authorization.forbidden ? 'CONTROL_PLANE_AUTH_FORBIDDEN' : 'CONTROL_PLANE_AUTH_REQUIRED',
    message: authorization.error || `Authenticated ${requiredRole} access is required`,
    requiredRole,
    status: 'DEGRADED',
    verified: false,
    evidence_status: 'UNVERIFIED',
  });
}

async function enforceInternalServiceAccess(req: Request, res: Response, next: () => void): Promise<void> {
  const authorization = await authorizeInternalServiceRequest(req, {
    audience: CONTROL_PLANE_URL,
    allowedServiceAccounts: [RELEASE_CONTROLLER_SERVICE_ACCOUNT],
  });
  if (authorization.ok) {
    next();
    return;
  }
  console.warn(
    `monitor_event=control_plane_oidc_failure route=${req.path} reason=${authorization.error === 'Google service identity is required' ? 'missing_identity' : 'invalid_identity'}`,
  );
  res.status(authorization.error === 'Google service identity is required' ? 401 : 403).json({
    error: 'INTERNAL_RELEASE_AUTH_DENIED',
    message: authorization.error || 'Authorized release-controller identity is required',
    verified: false,
    evidence_status: 'UNVERIFIED',
  });
}

function releaseRequestObject(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function secretVersionsFromState(value: unknown): SecretVersionSet {
  const versions = releaseRequestObject(value) || {};
  return {
    sql: String(versions.sql || ''),
    apiKey: String(versions.apiKey || ''),
    apiSecret: String(versions.apiSecret || ''),
  };
}

function secretVersionsMatch(expected: SecretVersionSet, actual: SecretVersionSet): boolean {
  return expected.sql === actual.sql
    && expected.apiKey === actual.apiKey
    && expected.apiSecret === actual.apiSecret;
}

function hasCredentialLikeKey(value: unknown, allowSecretVersionMetadata = false): boolean {
  if (!value || typeof value !== 'object') return false;
  if (Array.isArray(value)) return value.some((child) => hasCredentialLikeKey(child, allowSecretVersionMetadata));
  return Object.entries(value as Record<string, unknown>).some(([key, child]) => {
    if (allowSecretVersionMetadata && key === 'secretVersions') {
      const versions = releaseRequestObject(child);
      if (!versions) return true;
      return Object.entries(versions).some(([versionKey, version]) =>
        !['sql', 'apiKey', 'apiSecret'].includes(versionKey)
        || typeof version !== 'string'
        || !/^[1-9][0-9]*$/.test(version),
      );
    }
    return /api[-_ ]?(key|secret)|password|token|dsn|private[-_ ]?key/i.test(key)
      || hasCredentialLikeKey(child, allowSecretVersionMetadata);
  });
}

function releaseCandidateInputFromRequest(value: unknown): Parameters<typeof newReleaseCandidate>[0] {
  const body = releaseRequestObject(value);
  if (!body) throw new Error('Release candidate request must be an object');
  if (hasCredentialLikeKey(body, true)) {
    throw new Error('Release candidate payload must not contain credentials or tokens');
  }
  const versions = releaseRequestObject(body.secretVersions);
  if (!versions) throw new Error('secretVersions is required');
  return {
    repoSha: String(body.repoSha || ''),
    imageDigest: String(body.imageDigest || ''),
    workerRevision: String(body.workerRevision || ''),
    secretVersions: {
      sql: String(versions.sql || ''),
      apiKey: String(versions.apiKey || ''),
      apiSecret: String(versions.apiSecret || ''),
    },
    preflightEvidenceHash: String(body.preflightEvidenceHash || ''),
    repoGateEvidenceHash: String(body.repoGateEvidenceHash || ''),
    cloudGateEvidenceHash: String(body.cloudGateEvidenceHash || ''),
    expiresAt: String(body.expiresAt || ''),
    nonce: String(body.nonce || ''),
  };
}

function releaseEvidenceHashForPreflight(value: unknown): {
  evidence: ReturnType<typeof sanitizePreflightEvidence>;
  evidenceHash: string;
} {
  const evidence = sanitizePreflightEvidence(value);
  return { evidence, evidenceHash: hashEvidence(evidence) };
}

async function runReleasePreflight(candidateId?: string): Promise<{
  evidence: ReturnType<typeof sanitizePreflightEvidence>;
  evidenceHash: string;
}> {
  const forwarded = await forwardWorkerRequest('/preflight/read-only', { method: 'POST' });
  if (!forwarded.response.ok) throw new Error('Worker rejected read-only Mainnet preflight');
  const result = releaseEvidenceHashForPreflight(forwarded.data);
  if (candidateId) {
    const candidate = await getServerReleaseStore().getCandidate(candidateId);
    if (!candidate) throw new Error('Release candidate not found');
    await getServerReleaseStore().updatePreflight(candidateId, result.evidence, result.evidenceHash);
  }
  return result;
}

async function currentReleaseVerification(
  candidate: ReleaseCandidate,
  preflight: ReturnType<typeof sanitizePreflightEvidence>,
): Promise<ReleaseVerificationSnapshot> {
  const [stateResponse, readinessResponse] = await Promise.all([
    forwardWorkerRequest('/state'),
    forwardWorkerRequest('/readiness'),
  ]);
  if (!stateResponse.response.ok || !readinessResponse.response.ok) {
    throw new Error('Worker read-back is unavailable');
  }
  const state = releaseRequestObject(stateResponse.data) || {};
  const readiness = releaseRequestObject(readinessResponse.data) || {};
  const persistence = releaseRequestObject(readiness.persistence) || {};
  const configuredDigest = (process.env.WORKER_IMAGE_DIGEST || '').trim();
  const configuredRevision = (process.env.WORKER_REVISION || '').trim();
  const workerImageDigest = String(state.worker_image_digest || '').trim();
  const workerRevision = String(state.worker_revision || '').trim();
  const workerBindingsMatch = (
    (!configuredDigest || configuredDigest === workerImageDigest)
    && (!configuredRevision || configuredRevision === workerRevision)
  );
  const preflightReconciliationStatus = resolveReconciliationStatus(
    preflight.checks,
    state.reconciliation_status,
  );
  const preflightHasRequiredEvidence = preflight.checks.length > 0
    && preflight.checks.every((check) => !check.required || check.status === 'PASS');
  return {
    currentImageDigest: workerBindingsMatch ? workerImageDigest : '',
    currentWorkerRevision: workerBindingsMatch ? workerRevision : '',
    currentExecutionMode: String(state.execution_mode || ''),
    currentMainnetLiveApproved: state.mainnet_live_approved === true,
    currentEngineState: String(state.engine_state || ''),
    currentOrderSubmissionAttempts: Number.isInteger(Number(state.order_submission_attempts))
      ? Number(state.order_submission_attempts)
      : -1,
    currentSecretVersions: secretVersionsFromState(state.secret_versions),
    preflightPassed: preflight.preflightPassed
      && preflightHasRequiredEvidence
      && preflight.orderSubmissionAttempts === 0
      && preflight.orderEndpointAttempts === 0,
    preflightObservedAt: preflight.observedAt,
    reconciliationStatus: preflightReconciliationStatus,
    persistenceDurable: persistence.durable === true,
    dataConnectCutover: ['1', 'true', 'yes', 'on'].includes((process.env.VITE_DATA_CONNECT_CUTOVER || 'false').trim().toLowerCase()),
    killSwitchActive: state.kill_switch_active === true,
  };
}

async function verifyReleaseCandidate(candidateId: string): Promise<{
  candidate: ReleaseCandidate;
  evidence: ReturnType<typeof sanitizePreflightEvidence>;
  evidenceHash: string;
  snapshot: ReleaseVerificationSnapshot;
  failures: string[];
}> {
  const store = getServerReleaseStore();
  const candidate = await store.getCandidate(candidateId);
  if (!candidate) throw new Error('Release candidate not found');
  const preflightResult = await runReleasePreflight(candidateId);
  const refreshed = await store.getCandidate(candidateId);
  if (!refreshed) throw new Error('Release candidate disappeared during verification');
  const snapshot = await currentReleaseVerification(refreshed, preflightResult.evidence);
  const failures = validateApprovalPrerequisites(refreshed, snapshot);
  await store.putEvidence({
    candidateId,
    kind: 'VERIFY',
    generatedAt: new Date().toISOString(),
    evidenceHash: hashEvidence({ snapshot, failures }),
    payload: { snapshot, failures },
  });
  return {
    candidate: refreshed,
    evidence: preflightResult.evidence,
    evidenceHash: preflightResult.evidenceHash,
    snapshot,
    failures,
  };
}

function sanitizeContinuationReadiness(value: unknown): {
  executionMode: 'LIVE';
  continuationOnly: true;
  continuationReady: boolean;
  launchId: string;
  launchPolicy: string;
  launchState: string;
  mainnetLiveApproved: boolean;
  engineState: string;
  submittedOrders: number;
  reservedOrders: number;
  preflightPassed: boolean;
  preflightOrderSubmissionAttempts: number;
  preflightOrderEndpointAttempts: number;
  secretVersions: SecretVersionSet;
  persistenceDurable: boolean;
  preflight: ReturnType<typeof sanitizePreflightEvidence>;
  checks: Array<{ id: string; name: string; required: boolean; status: 'PASS' | 'FAIL'; message: string }>;
  observedAt: string;
} {
  const raw = releaseRequestObject(value) || {};
  const rawChecks = Array.isArray(raw.checks) ? raw.checks : [];
  const checks = rawChecks.flatMap((item) => {
    if (!item || typeof item !== 'object') return [];
    const check = item as Record<string, unknown>;
    if (check.status !== 'PASS' && check.status !== 'FAIL') return [];
    const id = String(check.id || '').trim().slice(0, 80);
    const name = String(check.name || '').trim().slice(0, 160);
    if (!id || !name) return [];
    const message = String(check.message || '')
      .replace(/(authorization|api[-_ ]?key|api[-_ ]?secret|password|token|dsn)\s*[:=]\s*[^,;\s]+/gi, '$1=<redacted>')
      .slice(0, 500);
    return [{
      id,
      name,
      required: check.required !== false,
      status: check.status as 'PASS' | 'FAIL',
      message,
    }];
  });
  const preflight = sanitizePreflightEvidence(raw.preflight);
  const intOr = (valueToParse: unknown, fallback = -1) => {
    const number = Number(valueToParse);
    return Number.isInteger(number) && number >= 0 ? number : fallback;
  };
  return {
    executionMode: 'LIVE',
    continuationOnly: true,
    continuationReady: raw.continuationReady === true,
    launchId: String(raw.launchId || '').trim(),
    launchPolicy: String(raw.launchPolicy || '').trim(),
    launchState: String(raw.launchState || '').trim(),
    mainnetLiveApproved: raw.mainnetLiveApproved === true,
    engineState: String(raw.engineState || '').trim(),
    submittedOrders: intOr(raw.submittedOrders, 0),
    reservedOrders: intOr(raw.reservedOrders, 0),
    preflightPassed: raw.preflightPassed === true,
    preflightOrderSubmissionAttempts: intOr(raw.preflightOrderSubmissionAttempts),
    preflightOrderEndpointAttempts: intOr(raw.preflightOrderEndpointAttempts),
    secretVersions: secretVersionsFromState(raw.secretVersions),
    persistenceDurable: raw.persistenceDurable === true,
    preflight,
    checks,
    observedAt: String(raw.observedAt || '').trim(),
  };
}

async function runContinuationReadiness(launchId: string): Promise<{
  evidence: ReturnType<typeof sanitizeContinuationReadiness>;
  evidenceHash: string;
}> {
  const normalized = String(launchId || '').trim();
  if (!/^launch-[A-Za-z0-9-]{8,127}$/.test(normalized)) {
    throw new Error('Invalid continuation launch id');
  }
  const forwarded = await forwardWorkerRequest(
    `/continuation/readiness?launch_id=${encodeURIComponent(normalized)}`,
    { method: 'POST' },
  );
  if (!forwarded.response.ok) throw new Error('Worker rejected continuation readiness');
  const evidence = sanitizeContinuationReadiness(forwarded.data);
  return { evidence, evidenceHash: hashEvidence(evidence) };
}

function continuationVerificationSnapshot(
  evidence: ReturnType<typeof sanitizeContinuationReadiness>,
  workerState: Record<string, unknown>,
): ContinuationVerificationSnapshot {
  const workerImageDigest = String(workerState.worker_image_digest || '').trim();
  const workerRevision = String(workerState.worker_revision || '').trim();
  const preflightHasRequiredEvidence = evidence.preflight.checks.length > 0
    && evidence.preflight.checks.every((check) => !check.required || check.status === 'PASS');
  const preflightPassed = evidence.preflightPassed
    && preflightHasRequiredEvidence
    && evidence.preflightOrderSubmissionAttempts === 0
    && evidence.preflightOrderEndpointAttempts === 0;
  return {
    currentImageDigest: workerImageDigest,
    currentWorkerRevision: workerRevision,
    currentExecutionMode: String(workerState.execution_mode || ''),
    currentMainnetLiveApproved: workerState.mainnet_live_approved === true,
    currentEngineState: String(workerState.engine_state || ''),
    currentLaunchId: String(workerState.mainnet_launch_id || ''),
    currentLaunchPolicy: String(workerState.mainnet_launch_policy || ''),
    currentLaunchState: String(workerState.mainnet_launch_state || ''),
    currentContinuationApprovalId: typeof workerState.mainnet_continuation_approval_id === 'string'
      ? workerState.mainnet_continuation_approval_id
      : undefined,
    currentSubmittedOrders: evidence.submittedOrders,
    currentSecretVersions: secretVersionsFromState(workerState.secret_versions),
    preflightPassed,
    preflightObservedAt: evidence.observedAt,
    preflightOrderEndpointAttempts: evidence.preflightOrderEndpointAttempts,
    preflightOrderSubmissionAttempts: evidence.preflightOrderSubmissionAttempts,
    reconciliationStatus: resolveReconciliationStatus(
      evidence.preflight.checks,
      workerState.reconciliation_status,
    ),
    persistenceDurable: evidence.persistenceDurable,
    dataConnectCutover: ['1', 'true', 'yes', 'on'].includes(
      (process.env.VITE_DATA_CONNECT_CUTOVER || 'false').trim().toLowerCase(),
    ),
    killSwitchActive: workerState.kill_switch_active === true,
  };
}

function requireWorkerBoolean(data: any, field: string): boolean | null {
  return typeof data?.[field] === 'boolean' ? data[field] : null;
}

const SIMULATED_EVIDENCE = {
  data_source: 'SIMULATED' as const,
  evidence_status: 'ILLUSTRATIVE_ONLY' as 'ILLUSTRATIVE_ONLY' | 'UNVERIFIED' | 'VERIFIED',
  verified: false,
};

function quantStateForUi() {
  const account = quantEngineState.account;
  const accountEnvironment = account.source;
  const environmentMatchesMode =
    (accountEnvironment === 'BINANCE_TESTNET' &&
      tradingSystemState.exchangeEnvironment === 'BINANCE_TESTNET' &&
      tradingSystemState.executionMode === 'TESTNET') ||
    (accountEnvironment === 'BINANCE_MAINNET' &&
      tradingSystemState.exchangeEnvironment === 'BINANCE_MAINNET' &&
      tradingSystemState.executionMode === 'LIVE' &&
      tradingSystemState.mainnetLiveApproved === true &&
      tradingSystemState.mainnetPreflightReady === true);
  const accountIsVerified =
    account.verified === true &&
    (accountEnvironment === 'BINANCE_TESTNET' || accountEnvironment === 'BINANCE_MAINNET') &&
    environmentMatchesMode &&
    tradingSystemState.workerResponsive === true &&
    tradingSystemState.accountSynchronized === true &&
    tradingSystemState.tradingConnectionHealthy === true &&
    tradingSystemState.privateStreamHealthy === true &&
    tradingSystemState.reconciliationStatus === 'IN_SYNC' &&
    tradingSystemState.killSwitchActive === false;

  return {
    ...quantEngineState,
    account: {
      ...account,
      evidence_status: accountIsVerified
        ? 'VERIFIED'
        : account.evidence_status === 'UNVERIFIED'
          ? 'UNVERIFIED'
          : 'ILLUSTRATIVE_ONLY',
      verified: accountIsVerified,
    },
    // The server-side fixture is never an exchange execution feed. Keep every
    // derived object visibly simulated until the Python worker supplies it.
    instruments: Object.fromEntries(
      Object.entries(quantEngineState.instruments).map(([symbol, instrument]) => [
        symbol,
        { ...instrument, ...SIMULATED_EVIDENCE },
      ]),
    ),
    baskets: quantEngineState.baskets.map((basket) => ({ ...basket, ...SIMULATED_EVIDENCE })),
    orders: quantEngineState.orders.map((order) => ({ ...order, ...SIMULATED_EVIDENCE })),
    alerts: quantEngineState.alerts.map((alert) => ({ ...alert, ...SIMULATED_EVIDENCE })),
    strategy_intents: quantEngineState.strategy_intents.map((intent) => ({
      ...intent,
      ...SIMULATED_EVIDENCE,
    })),
    meta_allocations: quantEngineState.meta_allocations.map((allocation) => ({
      ...allocation,
      ...SIMULATED_EVIDENCE,
    })),
    risk_rules: [
      ...quantEngineState.risk_rules.hard_rules,
      ...quantEngineState.risk_rules.soft_rules,
    ].map((rule) => ({
      ...rule,
      current: 'UNKNOWN',
      status: 'UNKNOWN' as const,
      ...SIMULATED_EVIDENCE,
    })),
    correlations: {
      ...quantEngineState.correlations,
      btc_eth_rolling_corr: null,
      crypto_beta_exposure_pct: null,
      common_factor_status: 'UNKNOWN',
      ...SIMULATED_EVIDENCE,
    },
  };
}

app.get('/api/system/state', async (req, res) => {
  try {
    const workerStateResp = await forwardWorkerRequest('/state');
    if (!workerStateResp.response.ok) throw new Error('Worker not OK');
    const workerState = workerStateResp.data;

    const workerCapsResp = await forwardWorkerRequest('/capabilities');
    if (!workerCapsResp.response.ok) throw new Error('Worker capabilities not OK');
    const workerCaps = workerCapsResp.data;
    
    // Sync environment and health from the worker's canonical response.
    projectWorkerState(workerState);
    tradingSystemState.engineState = workerState.engine_state;
    tradingSystemState.executionMode = workerState.execution_mode;
    tradingSystemState.pauseNewRisk = workerState.pause_new_risk;
    tradingSystemState.recoveryOnly = workerState.recovery_only;
    tradingSystemState.killSwitchActive = workerState.kill_switch_active;
    tradingSystemState.updatedAt = workerState.updated_at;
    // The Python worker owns the canonical health verdict. A READY transport
    // label alone must not imply execution health while stream/auth/
    // reconciliation checks are still false.
    tradingSystemState.tradingConnectionHealthy = isWorkerTradingConnectionHealthy(workerState);
    if (typeof workerState.market_data_healthy === 'boolean') {
      tradingSystemState.marketDataHealthy = workerState.market_data_healthy;
    }
    if (typeof workerState.private_stream_healthy === 'boolean') {
      tradingSystemState.privateStreamHealthy = workerState.private_stream_healthy;
    }
    if (typeof workerState.account_synchronized === 'boolean') {
      tradingSystemState.accountSynchronized = workerState.account_synchronized;
    }
    if (workerState.reconciliation_status) {
      tradingSystemState.reconciliationStatus = workerState.reconciliation_status;
    }
    if (workerState.heartbeat_at) {
      tradingSystemState.heartbeatAt = workerState.heartbeat_at;
      const hbTime = new Date(workerState.heartbeat_at).getTime();
      const ageMs = Date.now() - hbTime;
      // Worker is considered responsive if heartbeat was recorded within the last 10 seconds
      tradingSystemState.workerResponsive = !isNaN(hbTime) && ageMs >= 0 && ageMs < 10000;
    } else {
      tradingSystemState.workerResponsive = false;
    }
    
    res.json({
      ...tradingSystemState,
      capabilities: workerCaps,
      workerState, // pass raw state to frontend for debug if needed
    });
  } catch (err) {
    // Never retain stale exchange health after the sole execution authority
    // disappears.  The UI receives an explicit unavailable/degraded state and
    // every exchange-dependent capability is fail-closed.
    tradingSystemState = {
      ...tradingSystemState,
      dataSource: 'SIMULATED',
      exchangeEnvironment: 'NONE',
      executionMode: 'PAPER',
      engineState: 'DEGRADED',
      accountSynchronized: false,
      marketDataHealthy: false,
      privateStreamHealthy: false,
      tradingConnectionHealthy: false,
      reconciliationStatus: 'UNKNOWN',
      workerResponsive: false,
      mainnetCredentialsVerified: false,
      mainnetLiveApproved: false,
      mainnetPreflightReady: false,
      updatedAt: new Date().toISOString(),
    };
    res.json({
      ...tradingSystemState,
      capabilities: EXECUTION_CAPABILITIES,
      error: 'Worker unreachable',
      workerResponsive: false,
    });
  }
});

app.get('/api/system/preflight', async (req, res) => {
  const mode = req.query.executionMode || 'PAPER';
  try {
    const resp = await forwardWorkerRequest('/preflight?execution_mode=' + encodeURIComponent(String(mode)));
    if (!resp.response.ok) throw new Error('Worker preflight not OK');
    const preflight = resp.data;

    const capsResp = await forwardWorkerRequest('/capabilities');
    if (!capsResp.response.ok) throw new Error('Worker capabilities not OK');
    const caps = capsResp.data;
    
    res.json({
      ...preflight,
      capabilities: caps
    });
  } catch (err) {
    res.json({
      executionMode: mode,
      canArm: false,
      checks: [{ id: 'CHK-WORKER', name: 'Worker Connectivity', required: true, status: 'FAIL', message: 'Unreachable' }],
      capabilities: EXECUTION_CAPABILITIES
    });
  }
});

// This is an operator-only, non-arming Mainnet observation. It uses the
// Worker's Google-authenticated service boundary and never forwards a browser
// Firebase token to Cloud Run. The Worker must leave its engine state and
// active configuration unchanged and must report zero order submissions.
app.post('/api/system/preflight/read-only', async (req, res) => {
  try {
    const forwarded = await forwardWorkerRequest('/preflight/read-only', { method: 'POST' });
    if (!forwarded.response.ok) {
      return res.status(forwarded.response.status).json({
        error: 'WORKER_REJECTED_READ_ONLY_PREFLIGHT',
        detail: forwarded.data,
      });
    }
    return res.json(forwarded.data);
  } catch (err) {
    return res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  }
});

// Release approval is a server-side, one-time Firebase trading_admin action.
// It records approval only; a separate Release Controller consumes the record
// and performs an independently verified Cloud Run deployment.
app.post('/api/release/mainnet/approve', async (req: Request, res: Response) => {
  const candidateId = typeof req.body?.candidateId === 'string' ? req.body.candidateId.trim() : '';
  if (!candidateId) return res.status(400).json({ error: 'RELEASE_CANDIDATE_ID_REQUIRED' });
  try {
    const verification = await verifyReleaseCandidate(candidateId);
    if (verification.failures.length) {
      return res.status(409).json({
        error: 'RELEASE_GATE_NOT_PASSED',
        candidateId,
        failures: verification.failures,
        evidence_status: 'UNVERIFIED',
        approved: false,
      });
    }
    const authorizationUid = res.locals.firebaseUid;
    if (typeof authorizationUid !== 'string' || !authorizationUid) {
      return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
    }
    const approved = await getServerReleaseStore().approveCandidate(
      candidateId,
      authorizationUid,
      verification.snapshot,
    );
    return res.json({
      approved: true,
      candidateId: approved.candidateId,
      approvalId: approved.approvalId,
      status: approved.status,
      expiresAt: approved.expiresAt,
      imageDigest: approved.imageDigest,
      workerRevision: approved.workerRevision,
      launchPolicy: approved.launchPolicy,
      orderSubmissionAttempts: approved.orderSubmissionAttempts,
      evidence_status: 'VERIFIED',
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Release approval failed';
    const status = message.includes('not found') ? 404 : 503;
    return res.status(status).json({ error: 'RELEASE_APPROVAL_FAILED', message });
  }
});

// The second approval is deliberately separate from the initial release
// approval. It records evidence for the staged first order but never changes
// Worker state or the MAINNET_LIVE_APPROVED deployment flag.
app.post('/api/release/mainnet/continuation/approve', async (req: Request, res: Response) => {
  const body = releaseRequestObject(req.body) || {};
  if (hasCredentialLikeKey(body)) {
    return res.status(400).json({ error: 'CONTINUATION_PAYLOAD_CONTAINS_CREDENTIALS' });
  }
  const candidateId = typeof body.candidateId === 'string' ? body.candidateId.trim() : '';
  const launchId = typeof body.launchId === 'string' ? body.launchId.trim() : '';
  if (!/^rc-[0-9a-f-]{36}$/i.test(candidateId)) {
    return res.status(400).json({ error: 'RELEASE_CANDIDATE_ID_REQUIRED' });
  }
  if (!/^launch-[A-Za-z0-9-]{8,127}$/.test(launchId)) {
    return res.status(400).json({ error: 'LAUNCH_ID_REQUIRED' });
  }
  const requesterUid = res.locals.firebaseUid;
  if (typeof requesterUid !== 'string' || !requesterUid) {
    return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
  }
  try {
    const store = getServerReleaseStore();
    const candidate = await store.getCandidate(candidateId);
    if (!candidate) return res.status(404).json({ error: 'RELEASE_CANDIDATE_NOT_FOUND' });
    if (candidate.status !== 'CONSUMED' || !candidate.approvalId) {
      return res.status(409).json({
        error: 'INITIAL_RELEASE_APPROVAL_NOT_CONSUMED',
        message: 'Continuation requires the consumed staged-release approval',
        evidence_status: 'UNVERIFIED',
      });
    }
    const consumed = await store.getConsumedApproval(candidate.approvalId);
    if (!consumed || consumed.launchPolicy !== 'STAGED_FIRST_ORDER') {
      return res.status(409).json({ error: 'INITIAL_RELEASE_APPROVAL_INVALID', evidence_status: 'UNVERIFIED' });
    }

    const readinessResult = await runContinuationReadiness(launchId);
    const stateResponse = await forwardWorkerRequest('/state');
    if (!stateResponse.response.ok) throw new Error('Worker state read-back is unavailable');
    const workerState = releaseRequestObject(stateResponse.data) || {};
    const snapshot = continuationVerificationSnapshot(readinessResult.evidence, workerState);
    const now = new Date();
    const approval = newContinuationApproval({
      candidateId,
      launchId,
      initialApprovalId: consumed.approvalId,
      imageDigest: consumed.imageDigest,
      workerRevision: consumed.workerRevision,
      secretVersions: consumed.secretVersions,
      firstOrderEvidenceHash: readinessResult.evidenceHash,
      preflightObservedAt: readinessResult.evidence.observedAt,
      nonce: crypto.randomBytes(16).toString('hex'),
      requesterUid,
      expiresAt: new Date(now.getTime() + 60 * 60 * 1000).toISOString(),
    }, now);
    const failures = validateContinuationPrerequisites(approval, snapshot, now);
    if (failures.length) {
      return res.status(409).json({
        error: 'CONTINUATION_GATE_NOT_PASSED',
        candidateId,
        launchId,
        failures,
        evidence_status: 'UNVERIFIED',
        approved: false,
      });
    }
    await store.createContinuationApproval(approval);
    await store.putEvidence({
      candidateId,
      kind: 'CONTINUATION_APPROVAL',
      generatedAt: now.toISOString(),
      evidenceHash: hashEvidence({ approval, snapshot }),
      payload: {
        continuationId: approval.continuationId,
        candidateId,
        launchId,
        firstOrderEvidenceHash: approval.firstOrderEvidenceHash,
        reconciliationStatus: approval.reconciliationStatus,
        requesterUid,
        expiresAt: approval.expiresAt,
      },
    });
    return res.status(201).json({
      approved: true,
      continuationId: approval.continuationId,
      candidateId: approval.candidateId,
      launchId: approval.launchId,
      initialApprovalId: approval.initialApprovalId,
      imageDigest: approval.imageDigest,
      workerRevision: approval.workerRevision,
      status: approval.status,
      expiresAt: approval.expiresAt,
      firstOrderEvidenceHash: approval.firstOrderEvidenceHash,
      reconciliationStatus: approval.reconciliationStatus,
      evidence_status: 'VERIFIED',
      executionActivated: false,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Continuation approval failed';
    return res.status(message.includes('not found') ? 404 : 503).json({
      error: 'CONTINUATION_APPROVAL_FAILED',
      message,
      approved: false,
      executionActivated: false,
      evidence_status: 'UNVERIFIED',
    });
  }
});

app.get('/api/release/mainnet/continuation/:continuationId', async (req: Request, res: Response) => {
  try {
    const approval = await getServerReleaseStore().getContinuationApproval(String(req.params.continuationId));
    if (!approval) return res.status(404).json({ error: 'CONTINUATION_APPROVAL_NOT_FOUND' });
    return res.json({
      continuationId: approval.continuationId,
      candidateId: approval.candidateId,
      launchId: approval.launchId,
      initialApprovalId: approval.initialApprovalId,
      imageDigest: approval.imageDigest,
      workerRevision: approval.workerRevision,
      secretVersions: approval.secretVersions,
      firstOrderEvidenceHash: approval.firstOrderEvidenceHash,
      reconciliationStatus: approval.reconciliationStatus,
      preflightObservedAt: approval.preflightObservedAt,
      status: approval.status,
      createdAt: approval.createdAt,
      expiresAt: approval.expiresAt,
      evidence_status: 'VERIFIED',
    });
  } catch {
    return res.status(503).json({ error: 'RELEASE_STORE_UNAVAILABLE', evidence_status: 'UNVERIFIED' });
  }
});

app.get('/api/release/mainnet/:candidateId', async (req: Request, res: Response) => {
  try {
    const candidate = await getServerReleaseStore().getCandidate(String(req.params.candidateId));
    if (!candidate) return res.status(404).json({ error: 'RELEASE_CANDIDATE_NOT_FOUND' });
    return res.json({
      candidateId: candidate.candidateId,
      status: candidate.status,
      executionMode: candidate.executionMode,
      symbol: candidate.symbol,
      imageDigest: candidate.imageDigest,
      workerRevision: candidate.workerRevision,
      secretVersions: candidate.secretVersions,
      launchPolicy: candidate.launchPolicy,
      expiresAt: candidate.expiresAt,
      createdAt: candidate.createdAt,
      approvalId: candidate.status === 'PENDING_APPROVAL' ? undefined : candidate.approvalId,
      orderSubmissionAttempts: candidate.orderSubmissionAttempts,
      workerDisarmed: candidate.workerDisarmed,
      viteDataConnectCutover: candidate.viteDataConnectCutover,
      evidence_status: 'VERIFIED',
    });
  } catch {
    return res.status(503).json({ error: 'RELEASE_STORE_UNAVAILABLE', evidence_status: 'UNVERIFIED' });
  }
});

app.get('/api/system/readiness', async (req, res) => {
  try {
    const resp = await forwardWorkerRequest('/readiness');
    if (!resp.response.ok) throw new Error('Worker readiness not OK');
    const data = resp.data;
    res.json(data);
  } catch (err) {
    res.json({
      PAPER_READY: false,
      TESTNET_READ_ONLY_READY: false,
      TESTNET_MANUAL_READY: false,
      TESTNET_AUTONOMOUS_READY: false,
      SMALL_LIVE_READY: false
    });
  }
});

app.post('/api/system/arm', async (req, res) => {
  const { executionMode, riskProfile, instruments, strategies, enforcePreflight, releaseApprovalId, launchPolicy } = req.body;

  const requestedConfig = {
    executionMode: executionMode || 'PAPER',
    instruments: instruments || [],
    strategies: strategies || { grid: false, trend: false, shock: false, carry: false },
    riskProfile: riskProfile || 'BALANCED',
    enforcePreflight: executionMode === 'LIVE' ? true : Boolean(enforcePreflight),
    releaseApprovalId: typeof releaseApprovalId === 'string' ? releaseApprovalId : undefined,
    launchPolicy: launchPolicy || 'STAGED_FIRST_ORDER',
  };

  try {
    if (requestedConfig.executionMode === 'LIVE') {
      // A browser may submit an approval id, but it cannot create or assert an
      // approval. The server must resolve the id to a consumed Firestore
      // record before the Worker can see an ARM request.
      if (
        typeof requestedConfig.releaseApprovalId !== 'string'
        || !/^approval-[0-9a-f-]{36}$/i.test(requestedConfig.releaseApprovalId)
      ) {
        return res.status(400).json({
          error: 'RELEASE_APPROVAL_ID_REQUIRED',
          message: 'LIVE ARM requires a server-consumed release approval',
          evidence_status: 'UNVERIFIED',
        });
      }

      let consumedApproval;
      try {
        consumedApproval = await getServerReleaseStore().getConsumedApproval(
          requestedConfig.releaseApprovalId,
        );
      } catch {
        return res.status(503).json({
          error: 'RELEASE_STORE_UNAVAILABLE',
          message: 'The server could not verify the release approval',
          evidence_status: 'UNVERIFIED',
        });
      }
      if (!consumedApproval) {
        return res.status(409).json({
          error: 'RELEASE_APPROVAL_NOT_CONSUMED',
          message: 'LIVE ARM requires a valid, one-time approval consumed by the release controller',
          evidence_status: 'UNVERIFIED',
        });
      }

      const requestedInstruments = Array.isArray(requestedConfig.instruments)
        ? requestedConfig.instruments.map((symbol) => String(symbol).trim().toUpperCase())
        : [];
      if (
        consumedApproval.executionMode !== 'LIVE'
        || consumedApproval.symbol !== 'ETHUSDC'
        || consumedApproval.launchPolicy !== 'STAGED_FIRST_ORDER'
        || consumedApproval.viteDataConnectCutover !== false
        || consumedApproval.orderSubmissionAttempts !== 0
        || consumedApproval.workerDisarmed !== true
        || requestedInstruments.length !== 1
        || requestedInstruments[0] !== consumedApproval.symbol
      ) {
        return res.status(409).json({
          error: 'RELEASE_APPROVAL_SCOPE_MISMATCH',
          message: 'The consumed approval does not authorize this LIVE ARM request',
          evidence_status: 'UNVERIFIED',
        });
      }

      const configuredDigest = (process.env.WORKER_IMAGE_DIGEST || '').trim();
      const configuredRevision = (process.env.WORKER_REVISION || '').trim();
      if (
        (configuredDigest && configuredDigest !== consumedApproval.imageDigest)
        || (configuredRevision && configuredRevision !== consumedApproval.workerRevision)
      ) {
        return res.status(409).json({
          error: 'RELEASE_APPROVAL_RUNTIME_MISMATCH',
          message: 'The consumed approval does not match the configured Worker revision',
          evidence_status: 'UNVERIFIED',
        });
      }

      let workerStateResponse;
      try {
        workerStateResponse = await forwardWorkerRequest('/state');
      } catch {
        return res.status(503).json({
          error: 'WORKER_UNREACHABLE',
          message: 'The Worker state could not be verified before LIVE ARM',
          evidence_status: 'UNVERIFIED',
        });
      }
      const workerState = releaseRequestObject(workerStateResponse.data) || {};
      if (
        !workerStateResponse.response.ok
        || workerState.execution_mode !== 'LIVE'
        || workerState.mainnet_live_approved !== true
        || workerState.engine_state !== 'DISARMED'
        || Number(workerState.order_submission_attempts) !== 0
        || String(workerState.worker_image_digest || '').trim() !== consumedApproval.imageDigest
        || String(workerState.worker_revision || '').trim() !== consumedApproval.workerRevision
      ) {
        return res.status(409).json({
          error: 'WORKER_NOT_LIVE_DISARMED',
          message: 'Worker must be LIVE-approved, DISARMED, and unused before ARM',
          evidence_status: 'UNVERIFIED',
        });
      }

      // Forward only the id resolved from the server-side consumed record.
      requestedConfig.releaseApprovalId = consumedApproval.approvalId;
      requestedConfig.instruments = requestedInstruments;
    }

    const previousState = tradingSystemState.engineState;
    const forwarded = await forwardWorkerRequest('/arm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(requestedConfig)
    });

    if (!forwarded.response.ok) {
      return res.status(forwarded.response.status).json({ error: 'WORKER_REJECTED_ARM', detail: forwarded.data });
    }

    projectWorkerState(forwarded.data);
    // Pick risk profile
    const profileKey = requestedConfig.riskProfile as keyof typeof RISK_PROFILES;
    riskConfiguration = RISK_PROFILES[profileKey] || RISK_PROFILES.BALANCED;
    tradingSystemState.engineState = forwarded.data.engine_state || 'ARMED';
    tradingSystemState.executionMode = requestedConfig.executionMode as any;
    tradingSystemState.activeConfiguration = {
      executionMode: requestedConfig.executionMode as any,
      instruments: requestedConfig.instruments,
      strategies: requestedConfig.strategies,
      riskProfile: requestedConfig.riskProfile as any,
      riskConfiguration,
      configVersion: tradingSystemState.configVersion,
      armedAt: new Date().toISOString()
    };
    
    await auditRepository.logEvent({
      eventType: 'ENGINE_ARMED',
      previousState,
      newState: 'ARMED',
      executionMode: requestedConfig.executionMode,
      reason: 'ARM requested by user and worker accepted',
      metadata: { requestedConfig }
    });
    
    res.json(forwarded.data);
  } catch (err) {
    res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  }
});

// Continuation is a distinct trading_admin action. The browser supplies only
// opaque identifiers and strategy configuration; the Control Plane resolves
// the server-side approval and forwards a Google-authenticated request to the
// Worker. No endpoint here can set MAINNET_LIVE_APPROVED itself.
app.post('/api/system/continue', async (req: Request, res: Response) => {
  const body = releaseRequestObject(req.body) || {};
  if (hasCredentialLikeKey(body)) {
    return res.status(400).json({ error: 'CONTINUATION_PAYLOAD_CONTAINS_CREDENTIALS' });
  }
  const continuationApprovalId = typeof body.continuationApprovalId === 'string'
    ? body.continuationApprovalId.trim()
    : '';
  const launchId = typeof body.launchId === 'string' ? body.launchId.trim() : '';
  if (!/^continuation-[0-9a-f-]{36}$/i.test(continuationApprovalId)) {
    return res.status(400).json({ error: 'CONTINUATION_APPROVAL_ID_REQUIRED' });
  }
  if (!/^launch-[A-Za-z0-9-]{8,127}$/.test(launchId)) {
    return res.status(400).json({ error: 'LAUNCH_ID_REQUIRED' });
  }

  const rawStrategies = releaseRequestObject(body.strategies);
  const strategies = {
    grid: rawStrategies?.grid === true,
    trend: rawStrategies?.trend === true,
    shock: rawStrategies?.shock === true,
    carry: rawStrategies?.carry === true,
  };
  if (!Object.values(strategies).some(Boolean)) {
    return res.status(400).json({ error: 'CONTINUATION_STRATEGY_REQUIRED' });
  }
  const riskProfile = String(body.riskProfile || 'CONSERVATIVE').trim().toUpperCase();
  if (!['CONSERVATIVE', 'BALANCED', 'AGGRESSIVE'].includes(riskProfile)) {
    return res.status(400).json({ error: 'INVALID_RISK_PROFILE' });
  }
  if (body.executionMode !== 'LIVE' || body.enforcePreflight !== true) {
    return res.status(400).json({
      error: 'INVALID_CONTINUATION_CONFIGURATION',
      message: 'Autonomous continuation requires executionMode=LIVE and enforcePreflight=true',
    });
  }
  const instruments = Array.isArray(body.instruments)
    ? body.instruments.map((symbol) => String(symbol).trim().toUpperCase()).filter(Boolean)
    : ['ETHUSDC'];
  if (instruments.length !== 1 || instruments[0] !== 'ETHUSDC') {
    return res.status(400).json({ error: 'CONTINUATION_SYMBOL_MUST_BE_ETHUSDC' });
  }

  try {
    const store = getServerReleaseStore();
    let approval = await store.getContinuationApproval(continuationApprovalId);
    if (!approval) return res.status(404).json({ error: 'CONTINUATION_APPROVAL_NOT_FOUND' });

    const candidate = await store.getCandidate(approval.candidateId);
    if (!candidate || candidate.status !== 'CONSUMED' || candidate.approvalId !== approval.initialApprovalId) {
      return res.status(409).json({ error: 'INITIAL_RELEASE_APPROVAL_INVALID', evidence_status: 'UNVERIFIED' });
    }
    const consumed = await store.getConsumedApproval(approval.initialApprovalId);
    if (!consumed || consumed.imageDigest !== approval.imageDigest || consumed.workerRevision !== approval.workerRevision) {
      return res.status(409).json({ error: 'CONTINUATION_SCOPE_MISMATCH', evidence_status: 'UNVERIFIED' });
    }

    const stateForIdempotency = await forwardWorkerRequest('/state');
    const existingWorkerState = releaseRequestObject(stateForIdempotency.data) || {};
    const alreadyActive = stateForIdempotency.response.ok
      && existingWorkerState.execution_mode === 'LIVE'
      && existingWorkerState.engine_state === 'ARMED'
      && existingWorkerState.mainnet_launch_state === 'AUTONOMOUS_ACTIVE'
      && existingWorkerState.mainnet_continuation_approval_id === continuationApprovalId
      && existingWorkerState.mainnet_live_approved === true
      && String(existingWorkerState.worker_image_digest || '').trim() === approval.imageDigest
      && String(existingWorkerState.worker_revision || '').trim() === approval.workerRevision;
    if (alreadyActive) {
      if (approval.status !== 'CONSUMED') {
        approval = await store.consumeContinuationApproval(continuationApprovalId);
      }
      projectWorkerState(existingWorkerState);
      return res.json({
        ...existingWorkerState,
        continuationApprovalId,
        launchId,
        status: 'AUTONOMOUS_ACTIVE',
        idempotent: true,
        evidence_status: 'VERIFIED',
      });
    }
    if (approval.status === 'CONSUMED' || approval.status === 'EXPIRED') {
      console.warn(
        `monitor_event=release_approval_replay approval_kind=continuation status=${approval.status}`,
      );
      return res.status(409).json({ error: 'CONTINUATION_APPROVAL_REPLAYED', evidence_status: 'UNVERIFIED' });
    }
    approval = await store.claimContinuationApproval(continuationApprovalId);

    const readinessResult = await runContinuationReadiness(launchId);
    const latestStateResponse = await forwardWorkerRequest('/state');
    if (!latestStateResponse.response.ok) throw new Error('Worker state read-back is unavailable');
    const latestState = releaseRequestObject(latestStateResponse.data) || {};
    const snapshot = continuationVerificationSnapshot(readinessResult.evidence, latestState);
    const failures = validateContinuationPrerequisites(approval, snapshot);
    if (approval.launchId !== launchId) failures.push('launch id does not match continuation approval');
    if (failures.length) {
      await releaseContinuationApprovalBestEffort(approval.continuationId);
      return res.status(409).json({
        error: 'CONTINUATION_GATE_NOT_PASSED',
        failures,
        evidence_status: 'UNVERIFIED',
      });
    }

    const forwarded = await forwardWorkerRequest('/continue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        executionMode: 'LIVE',
        instruments: ['ETHUSDC'],
        strategies,
        riskProfile,
        enforcePreflight: true,
        continuationApprovalId: approval.continuationId,
        launchId: approval.launchId,
        initialApprovalId: approval.initialApprovalId,
      }),
    });
    if (!forwarded.response.ok) {
      const rollback = await rollbackAutonomousContinuation();
      await releaseContinuationApprovalBestEffort(approval.continuationId);
      return res.status(forwarded.response.status).json({
        error: 'WORKER_REJECTED_CONTINUATION',
        detail: forwarded.data,
        executionActivated: !rollback.verified,
        rollbackVerified: rollback.verified,
        rollbackError: rollback.error,
        evidence_status: 'UNVERIFIED',
      });
    }
    const workerState = releaseRequestObject(forwarded.data) || {};
    if (
      workerState.execution_mode !== 'LIVE'
      || workerState.engine_state !== 'ARMED'
      || workerState.mainnet_live_approved !== true
      || workerState.mainnet_launch_state !== 'AUTONOMOUS_ACTIVE'
      || workerState.mainnet_continuation_approval_id !== approval.continuationId
      || String(workerState.worker_image_digest || '').trim() !== approval.imageDigest
      || String(workerState.worker_revision || '').trim() !== approval.workerRevision
    ) {
      const rollback = await rollbackAutonomousContinuation();
      await releaseContinuationApprovalBestEffort(approval.continuationId);
      return res.status(409).json({
        error: 'WORKER_AUTONOMOUS_READBACK_FAILED',
        message: 'Worker did not prove the approved autonomous continuation state',
        executionActivated: !rollback.verified,
        rollbackVerified: rollback.verified,
        rollbackError: rollback.error,
        evidence_status: 'UNVERIFIED',
      });
    }
    let consumedContinuation: ContinuationApproval;
    try {
      consumedContinuation = await store.consumeContinuationApproval(approval.continuationId);
    } catch (error) {
      const rollback = await rollbackAutonomousContinuation();
      // Safe even if consumeContinuationApproval failed because a
      // concurrent request already legitimately consumed it -- release is a
      // no-op for any status other than ACTIVATING.
      await releaseContinuationApprovalBestEffort(approval.continuationId);
      console.warn(
        `monitor_event=autonomous_continuation_failure phase=consume rollback_verified=${rollback.verified}`,
      );
      return res.status(503).json({
        error: 'CONTINUATION_APPROVAL_CONSUME_FAILED',
        message: 'Worker activation was not independently committed; rollback verification is required.',
        executionActivated: !rollback.verified,
        rollbackVerified: rollback.verified,
        rollbackError: rollback.error,
        evidence_status: 'UNVERIFIED',
      });
    }
    projectWorkerState(workerState);
    await auditRepository.logEvent({
      eventType: 'AUTONOMOUS_CONTINUATION_ACTIVATED',
      previousState: tradingSystemState.engineState,
      newState: 'ARMED',
      executionMode: 'LIVE',
      reason: 'Independent continuation approval and Worker read-back passed',
      metadata: {
        launchId: approval.launchId,
        continuationId: approval.continuationId,
        imageDigest: approval.imageDigest,
        workerRevision: approval.workerRevision,
      },
    });
    return res.json({
      ...workerState,
      continuationId: consumedContinuation.continuationId,
      launchId: consumedContinuation.launchId,
      status: 'AUTONOMOUS_ACTIVE',
      evidence_status: 'VERIFIED',
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Autonomous continuation failed';
    console.warn(
      `monitor_event=autonomous_continuation_failure error_class=${error instanceof Error ? error.name : 'unknown'}`,
    );
    return res.status(message.includes('not found') ? 404 : 503).json({
      error: 'AUTONOMOUS_CONTINUATION_FAILED',
      message,
      executionActivated: false,
      evidence_status: 'UNVERIFIED',
    });
  }
});

app.post('/api/system/disarm', async (req, res) => {
  try {
    const previousState = tradingSystemState.engineState;
    const forwarded = await forwardWorkerRequest('/disarm', { method: 'POST' });
    if (!forwarded.response.ok) {
      return res.status(forwarded.response.status).json({ error: 'WORKER_REJECTED_DISARM', detail: forwarded.data });
    }
    tradingSystemState.engineState = 'DISARMED';
    
    await auditRepository.logEvent({
      eventType: 'ENGINE_DISARMED',
      previousState,
      newState: 'DISARMED',
      executionMode: tradingSystemState.executionMode,
      reason: 'Manual DISARM requested and worker accepted'
    });
    
    res.json(forwarded.data);
  } catch (err) {
    res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  }
});

app.post('/api/system/pause-new-risk', async (req, res) => {
  try {
    const forwarded = await forwardWorkerRequest('/pause-new-risk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req.body)
    });
    if (!forwarded.response.ok) {
      return res.status(forwarded.response.status).json({ error: 'WORKER_REJECTED_PAUSE', detail: forwarded.data });
    }
    const active = requireWorkerBoolean(forwarded.data, 'active');
    if (active === null) return res.status(502).json({ error: 'INVALID_WORKER_RESPONSE', detail: forwarded.data });
    tradingSystemState.pauseNewRisk = active;
    tradingSystemState.engineState = active ? 'PAUSED_NEW_RISK' : (tradingSystemState.activeConfiguration ? 'ARMED' : 'DISARMED');
    res.json(forwarded.data);
  } catch (err) {
    res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  }
});

app.post('/api/system/recovery-only', async (req, res) => {
  try {
    const forwarded = await forwardWorkerRequest('/recovery-only', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req.body)
    });
    if (!forwarded.response.ok) {
      return res.status(forwarded.response.status).json({ error: 'WORKER_REJECTED_RECOVERY', detail: forwarded.data });
    }
    const active = requireWorkerBoolean(forwarded.data, 'active');
    if (active === null) return res.status(502).json({ error: 'INVALID_WORKER_RESPONSE', detail: forwarded.data });
    tradingSystemState.recoveryOnly = active;
    tradingSystemState.engineState = active ? 'RECOVERY_ONLY' : (tradingSystemState.activeConfiguration ? 'ARMED' : 'DISARMED');
    res.json(forwarded.data);
  } catch (err) {
    res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  }
});

app.post('/api/system/kill-switch', async (req, res) => {
  if (typeof req.body?.active !== 'boolean') {
    return res.status(400).json({ error: 'INVALID_KILL_SWITCH_REQUEST', message: 'active must be a boolean' });
  }
  try {
    const forwarded = await forwardWorkerRequest('/kill-switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req.body)
    });
    if (!forwarded.response.ok) {
      return res.status(forwarded.response.status).json({ error: 'WORKER_REJECTED_KILL_SWITCH', detail: forwarded.data });
    }
    const isActive = requireWorkerBoolean(forwarded.data, 'kill_switch_active');
    if (isActive === null) return res.status(502).json({ error: 'INVALID_WORKER_RESPONSE', detail: forwarded.data });
    tradingSystemState.killSwitchActive = isActive;
    if (isActive) tradingSystemState.engineState = 'EMERGENCY';
    else if (forwarded.data.status === 'CONFIRMED') tradingSystemState.engineState = 'DISARMED';
    res.json(forwarded.data);
  } catch (err) {
    res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  }
});

app.post('/api/system/reconcile', async (req, res) => {
  try {
    const forwarded = await forwardWorkerRequest('/reconcile', { method: 'POST' });
    if (!forwarded.response.ok) {
      return res.status(forwarded.response.status).json({ error: 'WORKER_REJECTED_RECONCILE', detail: forwarded.data });
    }
    const data = forwarded.data;
    tradingSystemState.reconciliationStatus = data.status;
    tradingSystemState.accountSynchronized = data.status === 'IN_SYNC';
    res.json(data);
  } catch (err) {
    res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  }
});

// ---------------------------------------------------------------------------
// Internal release-controller protocol. These routes require a Google-signed
// OIDC token from the fixed Release Controller service account. They never
// accept Firebase tokens, Binance credentials, SQL passwords, or deployment
// access tokens in request bodies.
// ---------------------------------------------------------------------------
app.post('/internal/release/candidate', async (req: Request, res: Response) => {
  try {
    const body = releaseRequestObject(req.body) || {};
    const candidate = newReleaseCandidate(releaseCandidateInputFromRequest(body), undefined, {
      repoGateOutput: body.repoGateOutput,
      cloudGateOutput: body.cloudGateOutput,
    });
    let workerStateResponse;
    try {
      workerStateResponse = await forwardWorkerRequest('/state');
    } catch {
      return res.status(503).json({
        error: 'WORKER_RUNTIME_UNAVAILABLE',
        message: 'Worker state could not be read before creating the release candidate',
        evidence_status: 'UNVERIFIED',
      });
    }
    const workerState = releaseRequestObject(workerStateResponse.data) || {};
    if (
      !workerStateResponse.response.ok
      || workerState.execution_mode !== 'LIVE'
      || workerState.mainnet_live_approved !== false
      || workerState.engine_state !== 'DISARMED'
      || Number(workerState.order_submission_attempts) !== 0
      || String(workerState.worker_image_digest || '').trim() !== candidate.imageDigest
      || String(workerState.worker_revision || '').trim() !== candidate.workerRevision
      || !secretVersionsMatch(candidate.secretVersions, secretVersionsFromState(workerState.secret_versions))
    ) {
      return res.status(409).json({
        error: 'RELEASE_CANDIDATE_RUNTIME_MISMATCH',
        message: 'Candidate must match the current LIVE-disarmed Worker revision before approval',
        evidence_status: 'UNVERIFIED',
      });
    }
    await getServerReleaseStore().createCandidate(candidate);
    return res.status(201).json({
      candidateId: candidate.candidateId,
      status: candidate.status,
      executionMode: candidate.executionMode,
      symbol: candidate.symbol,
      imageDigest: candidate.imageDigest,
      workerRevision: candidate.workerRevision,
      secretVersions: candidate.secretVersions,
      launchPolicy: candidate.launchPolicy,
      expiresAt: candidate.expiresAt,
      orderSubmissionAttempts: candidate.orderSubmissionAttempts,
      workerDisarmed: candidate.workerDisarmed,
      viteDataConnectCutover: candidate.viteDataConnectCutover,
      evidence_status: 'VERIFIED',
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Invalid release candidate';
    return res.status(400).json({ error: 'INVALID_RELEASE_CANDIDATE', message });
  }
});

app.get('/internal/release/candidate/:candidateId', async (req: Request, res: Response) => {
  try {
    const candidate = await getServerReleaseStore().getCandidate(String(req.params.candidateId));
    if (!candidate) return res.status(404).json({ error: 'RELEASE_CANDIDATE_NOT_FOUND' });
    return res.json(candidate);
  } catch {
    return res.status(503).json({ error: 'RELEASE_STORE_UNAVAILABLE', evidence_status: 'UNVERIFIED' });
  }
});

app.post('/internal/release/preflight', async (req: Request, res: Response) => {
  const body = releaseRequestObject(req.body) || {};
  const candidateId = typeof body.candidateId === 'string' ? body.candidateId.trim() : '';
  try {
    const result = await runReleasePreflight(candidateId || undefined);
    return res.json({
      ...result.evidence,
      evidenceHash: result.evidenceHash,
      candidateId: candidateId || undefined,
      evidence_status: result.evidence.preflightPassed && result.evidence.orderSubmissionAttempts === 0 && result.evidence.orderEndpointAttempts === 0
        ? 'VERIFIED'
        : 'UNVERIFIED',
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Mainnet preflight failed';
    return res.status(503).json({
      error: 'MAINNET_PREFLIGHT_FAILED',
      message,
      preflightPassed: false,
      orderSubmissionAttempts: -1,
      orderEndpointAttempts: -1,
      evidence_status: 'UNVERIFIED',
    });
  }
});

app.post('/internal/release/continuation-readiness', async (req: Request, res: Response) => {
  const body = releaseRequestObject(req.body) || {};
  if (hasCredentialLikeKey(body)) {
    return res.status(400).json({ error: 'CONTINUATION_PAYLOAD_CONTAINS_CREDENTIALS', evidence_status: 'UNVERIFIED' });
  }
  const launchId = typeof body.launchId === 'string' ? body.launchId.trim() : '';
  if (!launchId) return res.status(400).json({ error: 'LAUNCH_ID_REQUIRED', evidence_status: 'UNVERIFIED' });
  try {
    const result = await runContinuationReadiness(launchId);
    return res.status(result.evidence.continuationReady ? 200 : 409).json({
      ...result.evidence,
      evidenceHash: result.evidenceHash,
      evidence_status: result.evidence.continuationReady ? 'VERIFIED' : 'UNVERIFIED',
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Continuation readiness failed';
    return res.status(503).json({
      error: 'CONTINUATION_READINESS_FAILED',
      message,
      continuationReady: false,
      evidence_status: 'UNVERIFIED',
    });
  }
});

app.post('/internal/release/verify', async (req: Request, res: Response) => {
  const candidateId = typeof req.body?.candidateId === 'string' ? req.body.candidateId.trim() : '';
  if (!candidateId) return res.status(400).json({ error: 'RELEASE_CANDIDATE_ID_REQUIRED' });
  try {
    const verification = await verifyReleaseCandidate(candidateId);
    return res.status(verification.failures.length ? 409 : 200).json({
      candidateId,
      verified: verification.failures.length === 0,
      failures: verification.failures,
      evidenceHash: verification.evidenceHash,
      snapshot: verification.snapshot,
      preflight: verification.evidence,
      evidence_status: verification.failures.length ? 'UNVERIFIED' : 'VERIFIED',
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Release verification failed';
    return res.status(message.includes('not found') ? 404 : 503).json({
      error: 'RELEASE_VERIFICATION_FAILED',
      message,
      verified: false,
      evidence_status: 'UNVERIFIED',
    });
  }
});

app.post('/internal/release/runtime', async (_req: Request, res: Response) => {
  try {
    const [stateResponse, readinessResponse] = await Promise.all([
      forwardWorkerRequest('/state'),
      forwardWorkerRequest('/readiness'),
    ]);
    if (!stateResponse.response.ok || !readinessResponse.response.ok) {
      return res.status(503).json({ error: 'WORKER_RUNTIME_READBACK_UNAVAILABLE', verified: false });
    }
    const state = releaseRequestObject(stateResponse.data) || {};
    const readiness = releaseRequestObject(readinessResponse.data) || {};
    const persistence = releaseRequestObject(readiness.persistence) || {};
    return res.json({
      state: {
        executionMode: state.execution_mode,
        engineState: state.engine_state,
        workerImageDigest: state.worker_image_digest,
        workerRevision: state.worker_revision,
        secretVersions: secretVersionsFromState(state.secret_versions),
        mainnetLiveApproved: state.mainnet_live_approved === true,
        mainnetLaunchId: state.mainnet_launch_id,
        mainnetLaunchPolicy: state.mainnet_launch_policy,
        mainnetLaunchState: state.mainnet_launch_state,
        mainnetContinuationApprovalId: state.mainnet_continuation_approval_id,
        orderSubmissionAttempts: Number(state.order_submission_attempts || 0),
        privateStreamHealthy: state.private_stream_healthy === true,
        killSwitchActive: state.kill_switch_active === true,
        reconciliationStatus: state.reconciliation_status,
      },
      persistence: {
        mode: persistence.mode,
        durable: persistence.durable === true,
        pendingOutbox: persistence.pending_outbox,
        failedWrites: persistence.failed_writes,
      },
      evidence_status: 'VERIFIED',
    });
  } catch {
    return res.status(503).json({ error: 'WORKER_RUNTIME_READBACK_FAILED', verified: false });
  }
});

// Release Controller readiness is deliberately internal and authenticated. It
// proves the Control Plane process, Firebase Admin verifier, server-side
// release store, and the Worker OIDC/readiness path independently of any
// browser token or exchange credential.
app.post('/internal/release/readiness', async (_req: Request, res: Response) => {
  const readiness = await controlPlaneReadiness();
  return res.status(readiness.status === 'ready' ? 200 : 503).json(readiness);
});

// Consuming an approval is intentionally the only release-store transition
// exposed to the controller after Firebase approval. It returns a proof for a
// deployment step; it does not set MAINNET_LIVE_APPROVED or ARM the Worker.
app.post('/internal/release/consume', async (req: Request, res: Response) => {
  const candidateId = typeof req.body?.candidateId === 'string' ? req.body.candidateId.trim() : '';
  if (!candidateId) return res.status(400).json({ error: 'RELEASE_CANDIDATE_ID_REQUIRED' });
  try {
    const store = getServerReleaseStore();
    const candidate = await store.getCandidate(candidateId);
    if (!candidate) return res.status(404).json({ error: 'RELEASE_CANDIDATE_NOT_FOUND' });
    const workerStateResponse = await forwardWorkerRequest('/state');
    const workerState = releaseRequestObject(workerStateResponse.data) || {};
    if (
      !workerStateResponse.response.ok
      || workerState.execution_mode !== 'LIVE'
      || workerState.mainnet_live_approved !== false
      || workerState.engine_state !== 'DISARMED'
      || Number(workerState.order_submission_attempts) !== 0
      || String(workerState.worker_image_digest || '').trim() !== candidate.imageDigest
      || String(workerState.worker_revision || '').trim() !== candidate.workerRevision
      || !secretVersionsMatch(candidate.secretVersions, secretVersionsFromState(workerState.secret_versions))
      || ['1', 'true', 'yes', 'on'].includes((process.env.VITE_DATA_CONNECT_CUTOVER || 'false').trim().toLowerCase())
    ) {
      console.warn('monitor_event=release_approval_replay reason=runtime_mismatch');
      return res.status(409).json({
        error: 'RELEASE_APPROVAL_RUNTIME_MISMATCH',
        consumed: false,
        executionActivated: false,
        evidence_status: 'UNVERIFIED',
      });
    }
    let approval;
    if (candidate.status === 'CONSUMED' && candidate.approvalId) {
      approval = await store.getConsumedApproval(candidate.approvalId);
      if (!approval) throw new Error('Release candidate approval cannot be resolved');
    } else {
      approval = await store.consumeApproval(candidateId);
    }
    return res.json({ ...approval, consumed: true, executionActivated: false, evidence_status: 'VERIFIED' });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Approval consumption failed';
    return res.status(message.includes('not found') ? 404 : 409).json({
      error: 'RELEASE_APPROVAL_NOT_CONSUMABLE',
      message,
      consumed: false,
      executionActivated: false,
      evidence_status: 'UNVERIFIED',
    });
  }
});

app.get('/api/quant/state', (req: Request, res: Response) => {
  const state = quantStateForUi();
  res.json({
    ...state,
    evidence_status: 'ILLUSTRATIVE_ONLY',
    execution_authority: 'PYTHON_TRADING_WORKER',
    exposure_recovery: {
      status: 'UNKNOWN',
      data_source: 'SIMULATED',
      evidence_status: 'ILLUSTRATIVE_ONLY',
      verified: false,
      assessment: null,
    },
    correlation_btc_eth: null,
    crypto_beta_exposure_pct: null,
    liquidation_distance_pct: null,
    liquidation_safety: 'UNKNOWN',
  });
});

app.post('/api/quant/risk/kill-switch', async (req: Request, res: Response) => {
  if (typeof req.body?.active !== 'boolean') {
    return res.status(400).json({ error: 'INVALID_KILL_SWITCH_REQUEST', message: 'active must be a boolean' });
  }
  const ksActive = req.body.active;
  try {
    const forwarded = await forwardWorkerRequest('/kill-switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: ksActive }),
    });
    if (!forwarded.response.ok) {
      return res.status(forwarded.response.status).json({ error: 'WORKER_REJECTED_KILL_SWITCH', detail: forwarded.data });
    }
    const actualActive = requireWorkerBoolean(forwarded.data, 'kill_switch_active');
    if (actualActive === null) return res.status(502).json({ error: 'INVALID_WORKER_RESPONSE', detail: forwarded.data });
    tradingSystemState.killSwitchActive = actualActive;
    if (actualActive) tradingSystemState.engineState = 'EMERGENCY';
    else if (forwarded.data.status === 'CONFIRMED') tradingSystemState.engineState = 'DISARMED';
    tradingSystemState.updatedAt = new Date().toISOString();
    quantEngineState.account.kill_switch_active = actualActive;
    quantEngineState.account.risk_state = actualActive ? 'EMERGENCY' : 'NORMAL';
    return res.json({ ...forwarded.data, risk_state: quantEngineState.account.risk_state });
  } catch (err) {
    return res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  }
});

app.post('/api/quant/basket/expand', (req: Request, res: Response) => {
  if (tradingSystemState.executionMode !== 'PAPER') {
    return res.status(400).json({
      error: 'SIMULATED_BASKET_MUTATION_NOT_PERMITTED',
      message: 'Simulated basket mutations are not permitted in exchange execution modes.',
    });
  }
  if (!canExecuteAction(tradingSystemState.engineState, 'INCREASE_RISK')) {
    return res.status(403).json({ error: 'ACTION_BLOCKED_BY_SYSTEM_STATE', engineState: tradingSystemState.engineState, action: 'INCREASE_RISK' });
  }
  const { basket_id } = req.body;
  const basket = quantEngineState.baskets.find((b) => b.basket_id === basket_id);
  if (!basket) {
    return res.status(404).json({ error: 'Basket not found' });
  }
  if (basket.grid_depth < basket.max_grid_levels) {
    basket.grid_depth += 1;
    const nextLvl = basket.grid_levels[basket.grid_depth - 1];
    if (nextLvl) {
      nextLvl.status = 'FILLED';
      nextLvl.filled_at = new Date().toISOString();
    }
    basket.state = 'GRID_EXPANDING';
    basket.total_size = Number((basket.total_size * 1.3).toFixed(4));
    basket.average_entry = Number((basket.average_entry * 0.985).toFixed(2));
    basket.last_updated = new Date().toISOString();
  }
  res.json({ status: 'success', basket });
});

app.post('/api/quant/basket/recovery', (req: Request, res: Response) => {
  if (tradingSystemState.executionMode !== 'PAPER') {
    return res.status(400).json({
      error: 'SIMULATED_BASKET_MUTATION_NOT_PERMITTED',
      message: 'Simulated basket mutations are not permitted in exchange execution modes.',
    });
  }
  if (!canExecuteAction(tradingSystemState.engineState, 'RECOVERY')) {
    return res.status(403).json({ error: 'ACTION_BLOCKED_BY_SYSTEM_STATE', engineState: tradingSystemState.engineState, action: 'RECOVERY' });
  }
  const { basket_id } = req.body;
  const basket = quantEngineState.baskets.find((b) => b.basket_id === basket_id);
  if (!basket) {
    return res.status(404).json({ error: 'Basket not found' });
  }
  basket.state = 'RECOVERY';
  basket.last_updated = new Date().toISOString();
  res.json({ status: 'success', basket });
});

app.post('/api/quant/basket/close', (req: Request, res: Response) => {
  if (tradingSystemState.executionMode !== 'PAPER') {
    return res.status(400).json({
      error: 'SIMULATED_BASKET_MUTATION_NOT_PERMITTED',
      message: 'Simulated basket mutations are not permitted in exchange execution modes.',
    });
  }
  if (!canExecuteAction(tradingSystemState.engineState, 'CLOSE')) {
    return res.status(403).json({ error: 'ACTION_BLOCKED_BY_SYSTEM_STATE', engineState: tradingSystemState.engineState, action: 'CLOSE' });
  }
  const { basket_id } = req.body;
  const basket = quantEngineState.baskets.find((b) => b.basket_id === basket_id);
  if (!basket) {
    return res.status(404).json({ error: 'Basket not found' });
  }
  basket.state = 'CLOSED';
  basket.last_updated = new Date().toISOString();
  quantEngineState.account.realized_daily_pnl = (quantEngineState.account.realized_daily_pnl || 0) + basket.net_pnl;
  quantEngineState.account.equity += basket.net_pnl;
  res.json({ status: 'success', basket });
});

app.post('/api/quant/basket/action', (req: Request, res: Response) => {
  if (tradingSystemState.executionMode !== 'PAPER') {
    return res.status(400).json({
      error: 'SIMULATED_BASKET_MUTATION_NOT_PERMITTED',
      message: 'Simulated basket mutations are not permitted in exchange execution modes.',
    });
  }
  const { basket_id, action } = req.body;
  
  let riskClass = 'NEW_RISK';
  if (action === 'CLOSE_ALL') riskClass = 'CLOSE';
  if (action === 'ENABLE_AUTO_RECOVERY') riskClass = 'RECOVERY';
  if (!canExecuteAction(tradingSystemState.engineState, riskClass as any)) {
    return res.status(403).json({ error: 'ACTION_BLOCKED_BY_SYSTEM_STATE', engineState: tradingSystemState.engineState, action: riskClass });
  }
  const basket = quantEngineState.baskets.find((b) => b.basket_id === basket_id);
  if (!basket) {
    return res.status(404).json({ error: 'Basket not found' });
  }

  if (action === 'EXPAND_GRID') {
    if (basket.grid_depth < basket.max_grid_levels) {
      basket.grid_depth += 1;
      const nextLvl = basket.grid_levels[basket.grid_depth - 1];
      if (nextLvl) {
        nextLvl.status = 'FILLED';
        nextLvl.filled_at = new Date().toISOString();
      }
      basket.state = 'GRID_EXPANDING';
      basket.total_size = Number((basket.total_size * 1.3).toFixed(4));
      basket.average_entry = Number((basket.average_entry * 0.985).toFixed(2));
      basket.last_updated = new Date().toISOString();
    }
  } else if (action === 'CLOSE_PROFIT') {
    basket.state = 'CLOSED';
    basket.last_updated = new Date().toISOString();
    quantEngineState.account.realized_daily_pnl = (quantEngineState.account.daily_pnl || 0) + basket.net_pnl;
    quantEngineState.account.equity += basket.net_pnl;
  } else if (action === 'ENTER_RECOVERY') {
    basket.state = 'RECOVERY';
    basket.last_updated = new Date().toISOString();
  } else if (action === 'EMERGENCY_FLATTEN') {
    basket.state = 'EMERGENCY_EXIT';
    basket.last_updated = new Date().toISOString();
  }

  res.json({ status: 'success', basket });
});

app.post('/api/quant/killswitch', async (req: Request, res: Response) => {
  // Alias for backward compatibility
  if (typeof req.body?.active !== 'boolean') {
    return res.status(400).json({ error: 'INVALID_KILL_SWITCH_REQUEST', message: 'active must be a boolean' });
  }
  const ksActive = req.body.active;
  const targetState = ksActive ? 'EMERGENCY' : 'DISARMED';
  if (!validateStateTransition(tradingSystemState.engineState, targetState)) {
    return res.status(409).json({ error: 'INVALID_STATE_TRANSITION', currentState: tradingSystemState.engineState, requestedState: targetState });
  }
  try {
    const forwarded = await forwardWorkerRequest('/kill-switch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: ksActive }),
    });
    if (!forwarded.response.ok) {
      return res.status(forwarded.response.status).json({ error: 'WORKER_REJECTED_KILL_SWITCH', detail: forwarded.data });
    }
    const actualActive = requireWorkerBoolean(forwarded.data, 'kill_switch_active');
    if (actualActive === null) return res.status(502).json({ error: 'INVALID_WORKER_RESPONSE', detail: forwarded.data });
    const prevState = tradingSystemState.engineState;
    tradingSystemState.killSwitchActive = actualActive;
    if (actualActive) tradingSystemState.engineState = 'EMERGENCY';
    else if (forwarded.data.status === 'CONFIRMED') tradingSystemState.engineState = 'DISARMED';
    await auditRepository.logEvent({
      eventType: actualActive ? 'KILL_SWITCH_ENGAGED' : 'KILL_SWITCH_RELEASED',
      previousState: prevState,
      newState: tradingSystemState.engineState,
      executionMode: tradingSystemState.executionMode,
      reason: actualActive ? 'Kill switch engaged' : 'Kill switch release verified by worker',
    });
    tradingSystemState.updatedAt = new Date().toISOString();
    quantEngineState.account.kill_switch_active = actualActive;
    quantEngineState.account.risk_state = actualActive ? 'EMERGENCY' : 'NORMAL';
    return res.json({ ...forwarded.data, risk_state: quantEngineState.account.risk_state });
  } catch (err) {
    return res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  }
});

// Event-driven Historical Replay / Stress Scenario Simulation.  The current
// endpoint is a deterministic UI fixture, not a data-backed backtest runner;
// its metrics must never be treated as launch or profitability evidence.
app.post('/api/quant/backtest/run', (req: Request, res: Response) => {
  const { scenario = 'COVID_CRASH_2020', initial_capital = 100000, max_grid_levels = 5, regime_filter = true } = req.body;

  const scenarios: Record<string, any> = {
    'COVID_CRASH_2020': {
      name: 'March 2020 Flash Liquidity Crisis (-50% in 48h)',
      total_bars: 2880,
      total_baskets: 42,
      win_baskets: 38,
      failed_baskets: 4,
      net_profit: 14280.0,
      roi_pct: 14.28,
      max_equity_drawdown_pct: 6.82,
      max_balance_drawdown_pct: 2.15,
      equity_balance_divergence_pct: 4.67,
      ulcer_index: 2.14,
      expected_shortfall_99_pct: 3.42,
      time_under_water_hrs: 38.5,
      worst_basket_pnl: -1840.0,
      longest_recovery_hrs: 24.5,
      max_grid_depth_reached: 4,
      grid_depth_p95: 3.2,
      emergency_exits: 1,
      total_funding_cost: 142.0,
      total_trading_fees: 395.0,
      slippage_cost: 110.0,
      profit_to_floating_dd_ratio: 2.09,
    },
    'LUNA_DEPEG_2022': {
      name: 'May 2022 Terra/LUNA Depeg & Cascade (-60% Trend)',
      total_bars: 4320,
      total_baskets: 58,
      win_baskets: 53,
      failed_baskets: 5,
      net_profit: 18450.0,
      roi_pct: 18.45,
      max_equity_drawdown_pct: 7.15,
      max_balance_drawdown_pct: 3.2,
      equity_balance_divergence_pct: 3.95,
      ulcer_index: 2.65,
      expected_shortfall_99_pct: 4.10,
      time_under_water_hrs: 52.0,
      worst_basket_pnl: -2200.0,
      longest_recovery_hrs: 34.0,
      max_grid_depth_reached: 5,
      grid_depth_p95: 4.1,
      emergency_exits: 2,
      total_funding_cost: 210.0,
      total_trading_fees: 520.0,
      slippage_cost: 180.0,
      profit_to_floating_dd_ratio: 2.58,
    },
    'FTX_COLLAPSE_2022': {
      name: 'Nov 2022 FTX Insolvency & Contagion (-30% Shock)',
      total_bars: 3600,
      total_baskets: 49,
      win_baskets: 47,
      failed_baskets: 2,
      net_profit: 16200.0,
      roi_pct: 16.20,
      max_equity_drawdown_pct: 5.4,
      max_balance_drawdown_pct: 1.8,
      equity_balance_divergence_pct: 3.6,
      ulcer_index: 1.88,
      expected_shortfall_99_pct: 2.9,
      time_under_water_hrs: 28.0,
      worst_basket_pnl: -980.0,
      longest_recovery_hrs: 18.2,
      max_grid_depth_reached: 4,
      grid_depth_p95: 2.9,
      emergency_exits: 0,
      total_funding_cost: 95.0,
      total_trading_fees: 410.0,
      slippage_cost: 85.0,
      profit_to_floating_dd_ratio: 3.0,
    },
    'BULL_EXPANSION_2024': {
      name: 'Q1-Q2 2024 Strong Trend & Volatility Expansion (+85%)',
      total_bars: 8640,
      total_baskets: 135,
      win_baskets: 131,
      failed_baskets: 4,
      net_profit: 48900.0,
      roi_pct: 48.9,
      max_equity_drawdown_pct: 4.2,
      max_balance_drawdown_pct: 1.4,
      equity_balance_divergence_pct: 2.8,
      ulcer_index: 1.25,
      expected_shortfall_99_pct: 2.1,
      time_under_water_hrs: 14.5,
      worst_basket_pnl: -750.0,
      longest_recovery_hrs: 12.0,
      max_grid_depth_reached: 3,
      grid_depth_p95: 2.2,
      emergency_exits: 0,
      total_funding_cost: 320.0,
      total_trading_fees: 1150.0,
      slippage_cost: 220.0,
      profit_to_floating_dd_ratio: 11.6,
    },
  };

  const normKey = (scenario || '').toUpperCase();
  const resData =
    scenarios[normKey] ||
    (normKey.includes('COVID') ? scenarios['COVID_CRASH_2020'] : null) ||
    (normKey.includes('LUNA') ? scenarios['LUNA_DEPEG_2022'] : null) ||
    (normKey.includes('FTX') ? scenarios['FTX_COLLAPSE_2022'] : null) ||
    (normKey.includes('BULL') ? scenarios['BULL_EXPANSION_2024'] : null) ||
    scenarios['COVID_CRASH_2020'];

  const evidence = {
    data_source: 'SIMULATED',
    evidence_status: 'ILLUSTRATIVE_ONLY',
    verified: false,
    net_economic_pnl_verified: false,
    execution_cost_model_status: 'NOT_VERIFIED',
    launch_eligible: false,
  };
  const metrics = { ...resData, ...evidence };

  res.json({
    ...metrics,
    scenario,
    initial_capital,
    max_grid_levels,
    regime_filter,
    metrics,
  });
});

// Quant AI Assistant (Research / Analysis Only - Section 33)
app.post('/api/quant/ai/research', async (req: Request, res: Response) => {
  const query = req.body.query || req.body.prompt;
  const context = {
    ...(req.body.context && typeof req.body.context === 'object' ? req.body.context : {}),
    portfolio_equity: 'UNKNOWN',
    risk_state: 'UNKNOWN',
    drawdown_pct: 'UNKNOWN',
    effective_leverage: 'UNKNOWN',
    data_source: 'SIMULATED',
    evidence_status: 'ILLUSTRATIVE_ONLY',
    execution_authority: 'PYTHON_TRADING_WORKER',
  };
  if (!query) {
    return res.status(400).json({ error: 'Query prompt is required' });
  }

  const ai = getGeminiClient();
  if (!ai) {
    return res.json({
      analysis: `### [Blessing AI Copilot: Research-Only Diagnostic]\n\n**Evaluation Context**: Current account, market, and execution evidence is UNKNOWN. No live-state or positive-expectancy claim is made.\n\n**Query**: ${String(query)}\n\n- The Python Trading Worker is the sole execution authority.\n- This response cannot arm the worker, override Risk Governor decisions, or submit orders.\n- Load a timestamped backtest/OOS or Testnet evidence artifact before drawing performance conclusions.`,
      model: 'deterministic_quant_engine',
      data_source: 'SIMULATED',
      evidence_status: 'ILLUSTRATIVE_ONLY',
      verified: false,
      execution_authority: 'PYTHON_TRADING_WORKER',
      timestamp: new Date().toISOString(),
    });
    /* Legacy fixture response intentionally disabled. */
    /*
    return res.json({
      analysis: `### [Blessing AI Copilot: Quant Architecture Review]\n\n**Evaluation Context**: Live state evaluated for risk invariant compliance (Portfolio: $${Number(context?.portfolio_equity || quantEngineState.account.equity).toLocaleString()}, Risk State: ${context?.risk_state || quantEngineState.account.risk_state}).\n\n1. **Mathematical Invariant Verification**:\n   - Grid Volume Multiplier series is strictly anti-martingale ($L_1: 1.0, L_2: 1.0, L_3: 1.1, L_4: 1.2, L_5: 1.3$). Maximum grid depth is hardware-locked at $L_5$.\n   - Effective Leverage cap ($\le 2.0\\times$) is fully satisfied by the Portfolio Risk Governor.\n\n2. **Regime & Volatility Calibration**:\n   - Dynamic step distances adapt continuously via rolling 1h ATR ($\sigma_{1h}$) multiplied by regime severity factor.\n   - High Basis Z-Score ($Z > 2.5$) and extreme negative funding drag act as fail-closed execution brakes.\n\n3. **Failure Mode Mitigations**:\n   - In the event of a one-way liquidity cascade, state advances through tiered drawdown gates (Caution 2% $\\to$ No New Grid 4% $\\to$ Recovery Only 6% $\\to$ Emergency Exit 8%).\n\n*(Connect \`GEMINI_API_KEY\` in Settings > Secrets to activate real-time dynamic Gemini 3.8 Flash generative research).*`,
      model: 'deterministic_quant_engine',
      timestamp: new Date().toISOString(),
    });
  */
  }

  try {
    const prompt = `You are the Principal Quant Research Advisor and Algorithmic Trading Architect for Blessing AI v0.1.
Adhere strictly to Section 33 of the specification:
- LLMs help with: research, strategy analysis, parameter sensitivity, backtest diagnostics, anomaly investigation, log diagnosis, and risk governance evaluations.
- ABSOLUTE MANDATE: You CANNOT place live orders or override deterministic Risk Governor rules.

User Context:
${JSON.stringify(context || quantEngineState, null, 2)}

User Request:
${query}

Provide a deep, rigorous, mathematically disciplined Senior Quant Engineer response in clear English and Thai terminology. Include mathematical justification, failure modes, trade-offs, and parameter boundaries where relevant.`;

    let textResponse = '';
    let providerError = false;
    try {
      const response = await ai.models.generateContent({
        model: 'gemini-2.5-flash',
        contents: prompt,
      });
      textResponse = response.text || '';
    } catch (apiErr: any) {
      console.warn('Gemini 2.5 Flash temporarily unavailable, using Quant Engine fallback:', apiErr.message);
      providerError = true;
      textResponse = `### [Blessing AI Quant Advisory — Research Diagnostic]\n\n**Evaluated Query**: ${query}\n**System State**: Risk Level: ${context?.risk_state || 'NORMAL'}, Active Drawdown: ${context?.drawdown_pct || '1.85'}%, Leverage: ${context?.effective_leverage || '1.42'}x.\n\n1. **Mathematical Validation**:\n   - Grid step scaling factor $k_{atr} = 1.25$ provides sufficient variance absorption under current regime parameters.\n   - Progression formula $S_i = S_0 \\times (1 + \\alpha)^{i-1}$ satisfies bounded adverse excursion limits.\n\n2. **Stress & Correlation Guardrails**:\n   - Cross-instrument rolling correlation is monitored against the $0.90$ threshold to mitigate synthetic unhedged directional concentration.\n   - Hard liquidation buffer maintains $>35\\%$ minimum safety margin.\n\n3. **Recommendation**:\n   - Retain anti-martingale volume cap at $L_5$.\n   - Continue monitoring Basis Z-score and 8-hour funding rates before opening secondary basket layers.`;
    }

    if (providerError) {
      textResponse = `### [Blessing AI Copilot: Research-Only Diagnostic]\n\nThe configured research provider is unavailable. Account, market, and execution evidence remain UNKNOWN; no execution decision was made.\n\n**Query**: ${String(query)}\n\nThe Python Trading Worker remains the sole execution authority, and this analysis cannot submit, modify, or cancel an order.`;
    }

    res.json({
      analysis: textResponse,
      model: 'gemini-2.5-flash',
      data_source: context.data_source,
      evidence_status: context.evidence_status,
      verified: false,
      execution_authority: 'PYTHON_TRADING_WORKER',
      timestamp: new Date().toISOString(),
    });
  } catch (err: any) {
    console.error('Gemini Quant API error:', err);
    res.json({
      analysis: 'Quant research evaluation is unavailable; no execution decision was made.',
      model: 'deterministic_quant_engine',
      data_source: 'SIMULATED',
      evidence_status: 'ILLUSTRATIVE_ONLY',
      verified: false,
      execution_authority: 'PYTHON_TRADING_WORKER',
      timestamp: new Date().toISOString(),
    });
  }
});

// ==========================================
// Google Cloud & Google Products Automated Wiring
// Project ID: gen-lang-client-0730128480
// User: kotorn@gmail.com | Region: asia-southeast1
// ==========================================
// GCP_PROJECT_ID is configured once near the application bootstrap so the
// Control Plane and the cloud product metadata cannot drift apart.
const GCP_REGION = 'asia-southeast1';
const GOOGLE_USER = 'kotorn@gmail.com';

const googleProductsState = [
  {
    id: 'bigquery',
    name: 'Google BigQuery',
    category: 'ANALYTICS',
    status: 'CONFIGURATION_DECLARED_NOT_VERIFIED',
    projectId: GCP_PROJECT_ID,
    resourceIdentifier: `${GCP_PROJECT_ID}.[market_data, signals, risk, backtests]`,
    region: 'US / asia-southeast1',
    description: 'Partitioned analytical research lakehouse for multi-horizon OHLCV, opportunity scores, and backtests.',
    consoleUrl: `https://console.cloud.google.com/bigquery?project=${GCP_PROJECT_ID}&ws=!1m0`,
    connectionParams: {
      projectId: GCP_PROJECT_ID,
      location: 'US',
      datasets: ['market_data', 'signals', 'risk', 'backtests'],
      maxScanBytesLimitGb: 10.0,
      monthlyFreeTierGb: 1000,
    },
    latencyMs: 42,
    lastVerified: null,
  },
  {
    id: 'cloud_storage',
    name: 'Google Cloud Storage (GCS)',
    category: 'STORAGE',
    status: 'CONFIGURATION_DECLARED_NOT_VERIFIED',
    projectId: GCP_PROJECT_ID,
    resourceIdentifier: `gs://blessing-ai-data-${GCP_PROJECT_ID}`,
    region: GCP_REGION,
    description: 'Cold storage bucket for 5-minute Parquet batches, orderbook snapshots, and ML model weights.',
    consoleUrl: `https://console.cloud.google.com/storage/browser/blessing-ai-data-${GCP_PROJECT_ID}?project=${GCP_PROJECT_ID}`,
    connectionParams: {
      bucket: `blessing-ai-data-${GCP_PROJECT_ID}`,
      storageClass: 'STANDARD',
      parquetPrefix: 'parquet/raw_ticks/',
      modelsPrefix: 'models/catboost_v1.4/',
      lifecycleRuleDays: 90,
    },
    latencyMs: 38,
    lastVerified: null,
  },
  {
    id: 'cloud_sql',
    name: 'Google Cloud SQL (PostgreSQL 17)',
    category: 'DATABASE',
    status: 'CONFIGURATION_DECLARED_NOT_VERIFIED',
    projectId: GCP_PROJECT_ID,
    resourceIdentifier: `${GCP_PROJECT_ID}:${GCP_REGION}:blessing-sql-primary`,
    region: GCP_REGION,
    description: 'HOT transactional database for active baskets, open orders, and immutable Risk Governor states.',
    consoleUrl: `https://console.cloud.google.com/sql/instances/blessing-sql-primary/overview?project=${GCP_PROJECT_ID}`,
    connectionParams: {
      instanceId: 'blessing-sql-primary',
      tier: 'LOWEST_COST_SHARED_CORE_PENDING_VERIFICATION',
      engineVersion: 'POSTGRES_17_PENDING_REGIONAL_CAPABILITY_CHECK',
      storageGb: 10,
      database: 'blessing_trading',
      dataConnectDatabase: 'blessing_app',
      user: 'blessing_worker',
      port: 5432,
      haMode: 'NONE',
      deletionProtection: true,
      backupPolicy: 'NON_HA_PENDING_VERIFICATION',
      connectionMode: 'CLOUD_SQL_UNIX_SOCKET',
    },
    latencyMs: 14,
    lastVerified: null,
  },
  {
    id: 'secret_manager',
    name: 'Google Secret Manager',
    category: 'SECURITY',
    status: 'CONFIGURATION_DECLARED_NOT_VERIFIED',
    projectId: GCP_PROJECT_ID,
    resourceIdentifier: `projects/${GCP_PROJECT_ID}/secrets/*`,
    region: 'global',
    description: 'Hardware-backed secret store for Binance API keys, PostgreSQL credentials, and Telegram tokens.',
    consoleUrl: `https://console.cloud.google.com/security/secret-manager?project=${GCP_PROJECT_ID}`,
    connectionParams: {
      projectId: GCP_PROJECT_ID,
      secretKeys: ['binance-api-key', 'binance-api-secret', 'postgres-password', 'telegram-bot-token'],
      autoRotationDays: 90,
    },
    latencyMs: 65,
    lastVerified: null,
  },
  {
    id: 'cloud_run',
    name: 'Google Cloud Run',
    category: 'COMPUTE',
    status: 'CONFIGURATION_DECLARED_NOT_VERIFIED',
    projectId: GCP_PROJECT_ID,
    resourceIdentifier: `${GCP_REGION}/blessing-trading-worker`,
    region: GCP_REGION,
    description: 'Serverless execution container for asynchronous daemon trading worker and web cockpit.',
    consoleUrl: `https://console.cloud.google.com/run?project=${GCP_PROJECT_ID}`,
    connectionParams: {
      service: 'blessing-trading-worker',
      cpu: '1',
      memory: '1Gi',
      concurrency: 1,
      minInstances: 1,
      maxInstances: 1,
      executionMode: 'PAPER_BY_DEFAULT',
      liveApproval: 'EXPLICIT_RELEASE_ONLY',
    },
    latencyMs: 9,
    lastVerified: null,
  },
  {
    id: 'firebase',
    name: 'Firebase (Firestore & Auth)',
    category: 'DATABASE',
    status: 'CONFIGURATION_DECLARED_NOT_VERIFIED',
    projectId: GCP_PROJECT_ID,
    resourceIdentifier: `ai-studio-blessingai-3ae78e47-476e-4c0a-8ff4-fafa3b8cc364`,
    region: GCP_REGION,
    description: 'Cloud document database for audit logs, real-time basket replication, and Google OAuth.',
    consoleUrl: `https://console.firebase.google.com/project/${GCP_PROJECT_ID}/firestore`,
    connectionParams: {
      firestoreDb: 'ai-studio-blessingai-3ae78e47-476e-4c0a-8ff4-fafa3b8cc364',
      collections: ['baskets', 'risk_states', 'audit_logs', 'strategy_configs', 'google_connections'],
      authProvider: 'Google Identity Services (GSI)',
    },
    latencyMs: 22,
    lastVerified: null,
  },
  {
    id: 'google_workspace',
    name: 'Google Workspace (Drive & Sheets)',
    category: 'WORKSPACE',
    status: 'CONFIGURATION_DECLARED_NOT_VERIFIED',
    projectId: GCP_PROJECT_ID,
    resourceIdentifier: `${GOOGLE_USER} / Blessing AI v0.2 Quant Lakehouse`,
    region: 'global',
    description: 'Direct spreadsheet exporter for real-time risk summaries, basket tracking, and Google Drive audits.',
    consoleUrl: `https://drive.google.com`,
    connectionParams: {
      authorizedAccount: GOOGLE_USER,
      driveFolder: 'Blessing AI v0.2 Quant Lakehouse',
      sheetsSpreadsheetName: 'Blessing AI v0.2 - Live Baskets & Risk Telemetry',
      exportFormat: 'Google Sheets (Native)',
    },
    latencyMs: 78,
    lastVerified: null,
  },
  {
    id: 'gemini_ai',
    name: 'Google Gemini Generative AI',
    category: 'AI',
    status: 'CONFIGURATION_DECLARED_NOT_VERIFIED',
    projectId: GCP_PROJECT_ID,
    resourceIdentifier: 'gemini-2.5-flash / gemini-3.8-flash',
    region: 'global',
    description: 'Quant copilot for log telemetry analysis, multi-regime calibration insights, and risk auditing.',
    consoleUrl: `https://aistudio.google.com`,
    connectionParams: {
      defaultModel: 'gemini-2.5-flash',
      reasoningModel: 'gemini-3.8-flash',
      serverSideProxy: true,
      executionLoopDecoupled: true,
    },
    latencyMs: 140,
    lastVerified: null,
  },
];

const unverifiedGoogleProduct = (product: typeof googleProductsState[number]) => ({
  ...product,
  status: 'CONFIGURATION_DECLARED_NOT_VERIFIED',
  evidence_status: 'ILLUSTRATIVE_ONLY',
  verified: false,
  lastVerified: null,
});

app.get('/api/google/products', (req: Request, res: Response) => {
  res.json({
    projectId: GCP_PROJECT_ID,
    region: GCP_REGION,
    userEmail: GOOGLE_USER,
    status: 'CONFIGURATION_DECLARED_NOT_VERIFIED',
    evidence_status: 'ILLUSTRATIVE_ONLY',
    verified: false,
    products: googleProductsState.map(unverifiedGoogleProduct),
  });
});

app.post('/api/google/test-connection', async (req: Request, res: Response) => {
  const { productId } = req.body;
  const product = googleProductsState.find((p) => p.id === productId);
  if (!product) {
    return res.status(404).json({ error: `Google product ${productId} not found` });
  }

  res.json({
    success: false,
    productId: product.id,
    productName: product.name,
    status: 'UNVERIFIED',
    evidence_status: 'ILLUSTRATIVE_ONLY',
    verified: false,
    message: `No live Google connector probe is configured for ${product.name}; configuration metadata only.`,
  });
});

app.post('/api/google/sync-all', (req: Request, res: Response) => {
  res.json({
    success: false,
    status: 'UNVERIFIED',
    evidence_status: 'ILLUSTRATIVE_ONLY',
    verified: false,
    message: 'Google product synchronization is not verified in this local runtime.',
    products: googleProductsState.map(unverifiedGoogleProduct),
  });
});

// BigQuery analytical lakehouse API. Responses are marked VERIFIED only after
// the Google client has performed a live read-back. Local failures are
// explicit DEGRADED responses; no fixture is allowed to look like cloud data.
async function requireBigQueryAccess(
  req: Request,
  res: Response,
  options: { requireTelemetryProducer?: boolean } = {},
): Promise<boolean> {
  const authorization = await authorizeBigQueryRequest(req, options);
  if (authorization.ok) return true;
  res.status(authorization.forbidden ? 403 : 401).json({
    error: authorization.forbidden ? 'BIGQUERY_PRODUCER_FORBIDDEN' : 'BIGQUERY_AUTH_REQUIRED',
    message: authorization.error || 'Authenticated access is required',
    status: 'DEGRADED',
    data_source: 'BIGQUERY',
    verified: false,
    evidence_status: 'UNVERIFIED',
  });
  return false;
}

app.get('/api/bigquery/config', async (req: Request, res: Response) => {
  if (!(await requireBigQueryAccess(req, res))) return;
  try {
    res.json(await readBigQueryConfig());
  } catch (error) {
    const failure = bigQueryErrorResponse(error);
    res.status(failure.status).json(failure.body);
  }
});

app.post('/api/bigquery/dry-run', async (req: Request, res: Response) => {
  if (!(await requireBigQueryAccess(req, res))) return;
  try {
    res.json(await dryRunQuery(req.body?.query));
  } catch (error) {
    const failure = bigQueryErrorResponse(error);
    res.status(failure.status).json(failure.body);
  }
});

app.post('/api/bigquery/query', async (req: Request, res: Response) => {
  if (!(await requireBigQueryAccess(req, res))) return;
  try {
    res.json(await executeQuery(req.body?.query));
  } catch (error) {
    const failure = bigQueryErrorResponse(error);
    res.status(failure.status).json(failure.body);
  }
});

app.post('/api/bigquery/sync-telemetry', async (req: Request, res: Response) => {
  // An empty browser buffer is a safe no-op and only needs ordinary Firebase
  // identity. Non-empty writes are restricted to the worker producer claim.
  const rows = req.body?.rows;
  const requireTelemetryProducer = !Array.isArray(rows) || rows.length > 0;
  if (!(await requireBigQueryAccess(req, res, { requireTelemetryProducer }))) return;
  try {
    res.json(await syncTelemetry(rows, req.body?.datasetId, req.body?.tableId));
  } catch (error) {
    const failure = bigQueryErrorResponse(error);
    res.status(failure.status).json(failure.body);
  }
});

// Explicit 404 catch-all for /api routes to prevent falling through to Vite HTML fallback
app.all('/api/*', (req: Request, res: Response) => {
  res.status(404).json({
    error: `API endpoint not found: ${req.method} ${req.originalUrl}`,
    status: 404,
  });
});

// Vite middleware in dev or static files in production
async function startServer() {
  if (process.env.NODE_ENV !== 'production') {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: 'spa',
    });
    app.use(vite.middlewares);
  } else {
    const distPath = path.join(process.cwd(), 'dist');
    app.use(express.static(distPath));
    app.get('*', (req: Request, res: Response) => {
      res.sendFile(path.join(distPath, 'index.html'));
    });
  }

  app.listen(PORT, '0.0.0.0', () => {
    console.log(`Blessing AI v0.1 Server listening on http://0.0.0.0:${PORT}`);

    // Initial background sync from Binance
    const active = getActiveBinanceCredentials();
    if (active.apiKey && active.apiSecret) {
      fetchBinanceLiveBalances(active.apiKey, active.apiSecret, active.isTestnet)
        .then((live) => {
          if (live.success) {
            quantEngineState.account.equity = live.equity!;
            quantEngineState.account.balance = live.balance!;
            quantEngineState.account.margin_utilization_pct = live.margin_utilization_pct!;
            quantEngineState.account.effective_leverage = live.effective_leverage!;
            quantEngineState.account.free_margin = live.free_margin!;
            quantEngineState.account.used_margin = live.used_margin!;
            quantEngineState.account.daily_pnl = live.daily_pnl!;
            quantEngineState.account.daily_pnl_pct = live.daily_pnl_pct!;
            (quantEngineState.account as any).source = live.source;
            // The control-plane boot probe is a read-only observation.  It
            // does not prove the Python worker's signed account truth,
            // private stream health, or authoritative reconciliation.
            (quantEngineState.account as any).evidence_status = 'UNVERIFIED';
            (quantEngineState.account as any).verified = false;
            (quantEngineState.account as any).spot_balance = live.spot_balance;
            (quantEngineState.account as any).futures_wallet_balance = live.futures_wallet_balance;
            (quantEngineState.account as any).futures_unrealized_pnl = live.futures_unrealized_pnl;
            (quantEngineState.account as any).last_sync_time = live.last_sync_time;
            (quantEngineState.account as any).account_alias = active.name;
            (quantEngineState.account as any).holdings = live.holdings;
            (quantEngineState.account as any).two_layer_assets = live.two_layer_assets;
            (quantEngineState.account as any).sub_wallets = live.sub_wallets;
            tradingSystemState.accountSynchronized = false;
            tradingSystemState.privateStreamHealthy = false;
            tradingSystemState.tradingConnectionHealthy = false;
            tradingSystemState.reconciliationStatus = 'UNKNOWN';
            tradingSystemState.dataSource = 'BINANCE';
            tradingSystemState.exchangeEnvironment = 'BINANCE_TESTNET';
            tradingSystemState.updatedAt = new Date().toISOString();
            console.log(`[Binance] Boot sync: read-only Testnet snapshot observed; worker reconciliation is still required (equity = $${live.equity?.toFixed(2)})`);
          }
        })
        .catch((e) => console.warn('[Binance] Initial balance sync failed:', e.message));
    }
  });
}

startServer().catch((err) => {
  console.error('Failed to start server:', err);
});
