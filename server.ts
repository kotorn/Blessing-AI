import express, { Request, Response } from 'express';
import { execFileSync } from 'node:child_process';
import path from 'path';
import crypto from 'crypto';
import { createServer as createViteServer } from 'vite';
import dotenv from 'dotenv';
import { GoogleGenAI } from '@google/genai';
import { GoogleAuth } from 'google-auth-library';
import { applicationDefault, getApps, initializeApp } from 'firebase-admin/app';
import { getAuth } from 'firebase-admin/auth';
import { getFirestore } from 'firebase-admin/firestore';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { PilotAcceptanceAcknowledgementError, runPilotOfflineAcceptance, type PilotOfflineCheck } from './src/backend/local-pilot-acceptance-runner.js';
import { FirestorePilotAcceptanceAudit } from './src/backend/local-pilot-acceptance-audit.js';
import { TradingSystemState, RiskConfiguration } from './src/backend/types.js';
import {  validateStateTransition, RISK_PROFILES, canExecuteAction, EXECUTION_CAPABILITIES, isWorkerTradingConnectionHealthy } from './src/backend/system.js';
import { auditRepository } from './src/backend/audit.js';
import {
  fetchBinanceLiveBalancesInternal,
  verifyBinanceCredentialsInternal,
} from './src/backend/binance-verify.js';
import {
  authorizeBigQueryRequest,
  authorizeOperatorRequest,
  bigQueryErrorResponse,
  dryRunQuery,
  executeQuery,
  readBigQueryConfig,
  syncTelemetry,
} from './src/backend/bigquery.js';
import {
  isEightDIncidentId,
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
import {
  LocalWorkerSupervisor,
  localPilotPaperWorkerIsDisarmed,
  localPilotWorkerStateMatchesCampaign,
  revokeLocalPilotWithWorkerGuard,
} from './src/backend/local-worker-supervisor.js';
import { accessPinnedLocalMainnetSecrets } from './src/backend/local-secret-manager.js';
import {
  assertExpectedLocalBinding,
  newLocalReleaseCandidate,
  type LocalReleaseBinding,
  type LocalReleaseCandidate,
} from './src/backend/local-release.js';
import { getLocalReleaseStore, type LocalReleaseStore } from './src/backend/local-release-store.js';
import {
  localContinuationBinding,
  localContinuationIdFor,
  newLocalContinuationApproval,
} from './src/backend/local-continuation.js';
import {
  getLocalContinuationStore,
  type LocalContinuationStore,
} from './src/backend/local-continuation-store.js';
import {
  computeLocalReleaseFingerprint,
  LOCAL_RUNTIME_TARGET,
} from './src/backend/local-release-runtime.js';
import { verifyLocalPromotionBundle } from './src/backend/local-promotion-evidence.js';
import {
  localLivePilotBinding,
  localLivePilotCanPrepare,
  localLivePilotCanStart,
  assertLocalLivePilotRecoveryReleaseAllowed,
  newLocalLivePilotCampaign,
  validateLocalLivePilotActor,
  type LocalLivePilotCampaign,
  type LocalLivePilotStrategyId,
} from './src/backend/local-live-pilot.js';
import { getLocalLivePilotStore } from './src/backend/local-live-pilot-firestore-store.js';
import type { LocalLivePilotStore } from './src/backend/local-live-pilot-store.js';
import { buildLocalPilotGitEnvironment, localLivePilotReadiness } from './src/backend/local-live-pilot-readiness.js';
import {
  attestPreparedLocalPilot,
  preparedLocalPilotMatches,
} from './src/backend/local-pilot-preparation.js';
import {
  localLivePilotPolicySha256,
  localLivePilotStrategySha256,
} from './src/backend/local-release-runtime.js';
import { localSecretSourceIdentity } from './src/backend/local-secret-manager.js';


dotenv.config();

const app = express();
const PORT = Number(process.env.PORT || 3000);
const LOCAL_ONLY = ['1', 'true', 'yes', 'on'].includes(
  (process.env.LOCAL_ONLY || '').trim().toLowerCase(),
);
const RUNTIME_TARGET = LOCAL_ONLY ? LOCAL_RUNTIME_TARGET : 'CLOUD_RUN';
const BIND_HOST = LOCAL_ONLY
  ? '127.0.0.1'
  : (process.env.BIND_HOST?.trim() || (process.env.NODE_ENV === 'production' ? '0.0.0.0' : '127.0.0.1'));
const CONTROL_PLANE_ONLY = ['1', 'true', 'yes', 'on'].includes(
  (process.env.CONTROL_PLANE_ONLY || '').trim().toLowerCase(),
);
const GCP_PROJECT_ID = (process.env.GCP_PROJECT_ID || 'gen-lang-client-0730128480').trim();
const RELEASE_CONTROLLER_SERVICE_ACCOUNT =
  (process.env.RELEASE_CONTROLLER_SERVICE_ACCOUNT ||
    `blessing-release-controller@${GCP_PROJECT_ID}.iam.gserviceaccount.com`).trim();
const CONTROL_PLANE_URL = (process.env.CONTROL_PLANE_URL || '').trim().replace(/\/+$/, '');
let releaseStore: ReleaseStore | null = null;
let localReleaseStore: LocalReleaseStore | null = null;
let localContinuationStore: LocalContinuationStore | null = null;
let localLivePilotStore: LocalLivePilotStore | null = null;
let localPilotTransitionBusy = false;
function reserveLocalPilotTransition(): boolean {
  if (localPilotTransitionBusy) return false;
  localPilotTransitionBusy = true;
  return true;
}
function rejectUnreadyLocalPilot(res: Response, adminUid?: string): boolean {
  const readiness = currentPilotCapabilityReadiness(adminUid);
  if (readiness.status === 'READY') return false;
  res.status(409).json({ error: 'LOCAL_PILOT_RUNTIME_NOT_READY', readiness, evidence_status: 'FAIL' });
  return true;
}

function currentPilotCapabilityReadiness(adminUid?: string) {
  try {
    const fingerprint = currentLocalFingerprint(
      (process.env.LOCAL_MAINNET_API_KEY_VERSION || '').trim(),
      (process.env.LOCAL_MAINNET_API_SECRET_VERSION || '').trim(),
    );
    return localLivePilotReadiness({
      root: process.cwd(),
      fingerprint,
      pilotPolicySha256: localLivePilotPolicySha256(),
      authenticatedServerAuthority: adminUid ? {
        adminUid,
        verifiedAt: new Date().toISOString(),
      } : undefined,
    });
  } catch {
    return localLivePilotReadiness();
  }
}

function getServerReleaseStore(): ReleaseStore {
  if (!releaseStore) releaseStore = getReleaseStore();
  return releaseStore;
}

function getServerLocalReleaseStore(): LocalReleaseStore {
  if (!localReleaseStore) localReleaseStore = getLocalReleaseStore();
  return localReleaseStore;
}

function getServerLocalContinuationStore(): LocalContinuationStore {
  if (!localContinuationStore) localContinuationStore = getLocalContinuationStore();
  return localContinuationStore;
}

function getServerLocalLivePilotStore(): LocalLivePilotStore {
  if (!localLivePilotStore) localLivePilotStore = getLocalLivePilotStore();
  return localLivePilotStore;
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
    if (LOCAL_ONLY) {
      const candidate = await getServerLocalReleaseStore().getCandidate(
        'local-rc-00000000-0000-4000-8000-000000000000',
      );
      addCheck(
        'LOCAL_RELEASE_STORE',
        candidate === null,
        candidate === null
          ? 'Firebase Local release store is reachable and isolated from Cloud Run approvals'
          : 'Local release store returned an unexpected candidate',
      );
    } else {
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
    }
  } catch {
    addCheck(
      LOCAL_ONLY ? 'LOCAL_RELEASE_STORE' : 'RELEASE_STORE',
      false,
      LOCAL_ONLY ? 'Firebase Local release store is unavailable' : 'Server-side release store is unavailable',
    );
  }

  try {
    // `/ready` is the Cloud Run health contract and returns an explicit HTTP
    // 503 when REQUIRED persistence is unavailable. `/readiness` is the
    // detailed launch-evidence document and intentionally has no top-level
    // `status=ready` field.
    const worker = await forwardWorkerRequest('/ready');
    const workerReady = worker.response.ok && worker.data?.status === 'ready';
    addCheck(
      LOCAL_ONLY ? 'LOCAL_WORKER_IDENTITY' : 'WORKER_OIDC',
      workerReady,
      workerReady
        ? LOCAL_ONLY
          ? 'Worker readiness was read through the local loopback identity boundary'
          : 'Worker readiness was read through the Google OIDC boundary'
        : LOCAL_ONLY
          ? 'Local Worker readiness is unavailable'
          : 'Worker readiness is unavailable through the Google OIDC boundary',
    );
  } catch {
    addCheck(
      LOCAL_ONLY ? 'LOCAL_WORKER_IDENTITY' : 'WORKER_OIDC',
      false,
      LOCAL_ONLY ? 'Local Worker identity transport is unavailable' : 'Worker OIDC transport is unavailable',
    );
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

// A Local runtime must never serve Cloud Run release-controller operations,
// even if a valid trading_admin or Google service identity reaches localhost.
// Local approvals use the separate /api/local/mainnet namespace and store.
app.use('/api/release/mainnet', (req, res, next) => {
  if (!LOCAL_ONLY) return next();
  return res.status(404).json({
    error: 'CLOUD_RELEASE_UNAVAILABLE_IN_LOCAL_RUNTIME',
    runtimeTarget: 'LOCAL',
    evidence_status: 'UNVERIFIED',
  });
});
app.use('/internal/release', (req, res, next) => {
  if (!LOCAL_ONLY) return next();
  return res.status(404).json({
    error: 'CLOUD_RELEASE_CONTROLLER_UNAVAILABLE_IN_LOCAL_RUNTIME',
    runtimeTarget: 'LOCAL',
    evidence_status: 'UNVERIFIED',
  });
});

// The control-plane middleware is registered before any API route so the
// Binance profile/balance endpoints cannot bypass server-side Firebase RBAC.
// The implementation is declared below as a function declaration and is
// therefore available when Express starts handling requests.
app.use([
  '/api/system',
  '/api/quant',
  '/api/binance',
  '/api/local',
  '/api/release',
  '/api/google',
  '/api/wealth',
  '/api/incidents',
  '/api/learning',
], (req, res, next) => {
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

const quantEngineState = {
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
    system: 'Blessing AI v0.2',
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

// Thin wrapper preserved for the source contract: route handlers and the
// startup boot-sync keep calling the same names; the implementation lives in
// src/backend/binance-verify.ts.
async function verifyBinanceCredentials(apiKey: string, apiSecret: string, isTestnet: boolean) {
  return verifyBinanceCredentialsInternal(apiKey, apiSecret, isTestnet);
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

// Thin wrapper preserved for the source contract: the /api/binance/sync-account
// and /api/binance/balance routes plus the startup boot-sync keep calling the
// same name; the implementation lives in src/backend/binance-verify.ts.
async function fetchBinanceLiveBalances(apiKey: string, apiSecret: string, isTestnet: boolean) {
  return fetchBinanceLiveBalancesInternal(apiKey, apiSecret, isTestnet);
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
    (quantEngineState.account as any).margin_balance = liveResult.margin_balance;
    (quantEngineState.account as any).margin_mode = liveResult.margin_mode;
    (quantEngineState.account as any).margin_level = liveResult.margin_level;
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
      success: true,
      read_only_snapshot: true,
      evidence_status: 'UNVERIFIED',
      message: `ดึงยอดเงินจาก Binance ${active.environment} สำเร็จ (${active.name})`,
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
    const notConfigured = !active.apiKey || !active.apiSecret;
    const message = notConfigured
      ? 'ยังไม่ได้ตั้งค่า Binance API Key กรุณากดปุ่ม Binance API เพื่อกรอก Key & Secret'
      : liveResult.message || liveResult.error || 'ไม่สามารถดึงยอดเงินสดจาก Binance ได้ ตรวจสอบ API Key หรือการเชื่อมต่อเครือข่าย';
    return res.json({
      success: false,
      configured: !notConfigured,
      message,
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
const LOCAL_RUN_ID = (process.env.LOCAL_RUN_ID || '').trim();
const localWorkerSupervisor = LOCAL_ONLY
  ? new LocalWorkerSupervisor({
    workerUrl: WORKER_URL,
    workerIdentityToken: LOCAL_WORKER_IDENTITY_TOKEN,
    runId: LOCAL_RUN_ID,
  })
  : null;
const workerGoogleAuth = new GoogleAuth();
let workerIdentityClient: Awaited<ReturnType<GoogleAuth['getIdTokenClient']>> | null = null;

async function workerAuthorizationHeader(): Promise<string | null> {
  if (!WORKER_URL) throw new Error('WORKER_URL is not configured');
  if ((process.env.NODE_ENV !== 'production' || LOCAL_ONLY) && LOCAL_WORKER_IDENTITY_TOKEN) {
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
    // Bounded so a hung Worker cannot stall operator routes indefinitely;
    // worker paths may include reconciliation, so the cap is generous.
    response = await fetch(WORKER_URL + pathName, {
      ...init,
      headers,
      signal: init?.signal ?? AbortSignal.timeout(30_000),
    });
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
  firstOrderClientOrderId: string | null;
  pendingOrderClientOrderId: string | null;
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
    firstOrderClientOrderId: String(raw.firstOrderClientOrderId || '').trim() || null,
    pendingOrderClientOrderId: String(raw.pendingOrderClientOrderId || '').trim() || null,
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
  } catch  {
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
  } catch  {
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
  } catch  {
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

function localBindingFromFingerprint(
  fingerprint: ReturnType<typeof computeLocalReleaseFingerprint>,
  promotionEvidenceSha256: string,
): LocalReleaseBinding {
  return {
    runtimeTarget: 'LOCAL',
    runId: fingerprint.runId,
    sourceFingerprint: fingerprint.sourceSha256,
    dependencyFingerprint: fingerprint.dependencySha256,
    migrationFingerprint: fingerprint.migrationSha256,
    promotionEvidenceSha256,
    apiKeyVersion: fingerprint.secretVersions.apiKey,
    apiSecretVersion: fingerprint.secretVersions.apiSecret,
    secretManagerProjectId: fingerprint.secretSource.secretManagerProjectId,
    apiKeySecretVersionResource: fingerprint.secretSource.apiKeySecretVersionResource,
    apiSecretSecretVersionResource: fingerprint.secretSource.apiSecretSecretVersionResource,
    policyVersion: fingerprint.riskPolicyVersion as LocalReleaseBinding['policyVersion'],
    policyHash: fingerprint.riskPolicySha256,
  };
}

function currentLocalFingerprint(apiKeyVersion: string, apiSecretVersion: string) {
  if (!LOCAL_ONLY || RUNTIME_TARGET !== 'LOCAL') throw new Error('LOCAL_RUNTIME_REQUIRED');
  return computeLocalReleaseFingerprint({
    root: process.cwd(),
    runId: LOCAL_RUN_ID,
    apiKeyVersion,
    apiSecretVersion,
    secretManagerProjectId: (process.env.LOCAL_SECRET_MANAGER_PROJECT_ID || GCP_PROJECT_ID).trim(),
  });
}

function assertCommittedPilotCandidate(): void {
  const status = execFileSync('git', ['status', '--porcelain', '--untracked-files=all'], {
    cwd: process.cwd(), encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    env: buildLocalPilotGitEnvironment(process.env),
  });
  if (status.trim()) throw new Error('LOCAL_PILOT_REQUIRES_REVIEWED_CLEAN_COMMIT');
}

function localPromotionStatus(fingerprint: ReturnType<typeof computeLocalReleaseFingerprint>) {
  return verifyLocalPromotionBundle(process.cwd(), fingerprint.gitSha, fingerprint.sourceSha256);
}

async function readLocalWorkerState() {
  const result = await forwardWorkerRequest('/state');
  if (!result.response.ok) throw new Error('LOCAL_WORKER_STATE_UNAVAILABLE');
  return releaseRequestObject(result.data) || {};
}

async function confirmRunningLocalPilotCampaign(campaignId: string): Promise<Record<string, unknown> | null> {
  const supervisor = localWorkerSupervisor?.status();
  if (!supervisor?.workerRunning) return null;
  if (supervisor.mode !== 'LIVE' || supervisor.pilotCampaignId !== campaignId) {
    throw new Error('LOCAL_PILOT_WORKER_CAMPAIGN_MISMATCH');
  }
  const workerState = await readLocalWorkerState();
  if (!localPilotWorkerStateMatchesCampaign(workerState, campaignId)) {
    throw new Error('LOCAL_PILOT_WORKER_CAMPAIGN_MISMATCH');
  }
  return workerState;
}

function localWorkerIsPaperDisarmed(state: Record<string, unknown>): boolean {
  return localPilotPaperWorkerIsDisarmed(state, localWorkerSupervisor?.status() ?? null, LOCAL_RUN_ID);
}

async function localPersistenceIsDurable(): Promise<boolean> {
  try {
    const result = await forwardWorkerRequest('/readiness');
    const readiness = releaseRequestObject(result.data) || {};
    const persistence = releaseRequestObject(readiness.persistence) || {};
    return result.response.ok
      && persistence.mode === 'REQUIRED'
      && persistence.durable === true
      && persistence.runtime_target === 'LOCAL'
      && persistence.database_provider === 'POSTGRES_LOCAL'
      && (persistence.database_host === '127.0.0.1' || persistence.database_host === 'host.docker.internal' || persistence.database_host === 'localhost')
      && Number(persistence.database_port) === 5433
      && persistence.database_identity_verified === true
      && Number(persistence.pending_outbox) === 0
      && Number(persistence.failed_writes) === 0;
  } catch {
    return false;
  }
}

async function localMainnetRiskLifecycleStatus(): Promise<{
  ready: boolean;
  missing: string[];
}> {
  try {
    const result = await forwardWorkerRequest('/readiness');
    const readiness = releaseRequestObject(result.data) || {};
    const missing = Array.isArray(readiness.local_mainnet_risk_lifecycle_missing)
      ? readiness.local_mainnet_risk_lifecycle_missing.map((item) => String(item))
      : [];
    return {
      ready: result.response.ok && readiness.local_mainnet_risk_lifecycle_ready === true,
      missing,
    };
  } catch {
    return { ready: false, missing: ['Worker readiness is unavailable'] };
  }
}

function localContinuationEvidenceHash(
  evidence: ReturnType<typeof sanitizeContinuationReadiness>,
  workerState: Record<string, unknown>,
  binding: LocalReleaseBinding,
): string {
  return hashEvidence({
    runtimeTarget: 'LOCAL',
    runId: binding.runId,
    candidateId: workerState.local_release_candidate_id || null,
    initialApprovalId: workerState.mainnet_release_approval_id || null,
    sourceFingerprint: binding.sourceFingerprint,
    dependencyFingerprint: binding.dependencyFingerprint,
    migrationFingerprint: binding.migrationFingerprint,
    policyVersion: binding.policyVersion,
    policyHash: binding.policyHash,
    launchId: evidence.launchId,
    launchPolicy: evidence.launchPolicy,
    launchState: evidence.launchState,
    submittedOrders: evidence.submittedOrders,
    reservedOrders: evidence.reservedOrders,
    firstOrderClientOrderId: evidence.firstOrderClientOrderId,
    pendingOrderClientOrderId: evidence.pendingOrderClientOrderId,
    engineState: evidence.engineState,
    mainnetLiveApproved: evidence.mainnetLiveApproved,
    persistenceDurable: evidence.persistenceDurable,
    preflightPassed: evidence.preflightPassed,
    preflightOrderSubmissionAttempts: evidence.preflightOrderSubmissionAttempts,
    preflightOrderEndpointAttempts: evidence.preflightOrderEndpointAttempts,
    reconciliationStatus: workerState.reconciliation_status || null,
    killSwitchActive: workerState.kill_switch_active === true,
    checks: evidence.checks.map((check) => ({ id: check.id, status: check.status })),
  });
}

function safeLocalLivePilotCampaign(campaign: LocalLivePilotCampaign) {
  const {
    nonce: _nonce,
    adminUid: _adminUid,
    approvedByUid: _approvedByUid,
    secretManagerProjectId: _secretManagerProjectId,
    apiKeyVersion: _apiKeyVersion,
    apiSecretVersion: _apiSecretVersion,
    ...safe
  } = campaign;
  return safe;
}

function pilotActor(uid: string) {
  const actor = { uid, role: 'trading_admin' as const };
  validateLocalLivePilotActor(actor);
  return actor;
}

const pilotAcceptanceInstanceId = crypto.randomUUID();
let pilotAcceptanceRunning = false;
app.get('/api/local/pilot/acceptance', async (_req: Request, res: Response) => {
  if (!LOCAL_ONLY) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  if (!res.locals.firebaseUid) return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
  try {
    assertCommittedPilotCandidate();
    const fingerprint = currentLocalFingerprint(
      (process.env.LOCAL_MAINNET_API_KEY_VERSION || '').trim(),
      (process.env.LOCAL_MAINNET_API_SECRET_VERSION || '').trim());
    const binding = { gitSha: fingerprint.gitSha, sourceSha256: fingerprint.sourceSha256,
      dependencySha256: fingerprint.dependencySha256, migrationSha256: fingerprint.migrationSha256,
      policySha256: localLivePilotPolicySha256() };
    const firebaseApp = getApps()[0] || initializeApp({ credential: applicationDefault(), projectId: GCP_PROJECT_ID });
    const audit = new FirestorePilotAcceptanceAudit(getFirestore(firebaseApp), pilotAcceptanceInstanceId,
      res.locals.firebaseUid);
    return res.json({ ...await audit.findRuns(binding), campaignAuthority: false, executionActivated: false });
  } catch {
    return res.status(503).json({ error: 'LOCAL_PILOT_ACCEPTANCE_UNKNOWN', evidence_status: 'UNVERIFIED' });
  }
});
app.get('/api/local/pilot/acceptance/:runId', async (req: Request, res: Response) => {
  if (!LOCAL_ONLY) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  if (!res.locals.firebaseUid) return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
  try {
    assertCommittedPilotCandidate();
    const fingerprint = currentLocalFingerprint(
      (process.env.LOCAL_MAINNET_API_KEY_VERSION || '').trim(),
      (process.env.LOCAL_MAINNET_API_SECRET_VERSION || '').trim(),
    );
    const expected = { gitSha: fingerprint.gitSha, sourceSha256: fingerprint.sourceSha256,
      dependencySha256: fingerprint.dependencySha256, migrationSha256: fingerprint.migrationSha256,
      policySha256: localLivePilotPolicySha256() };
    const firebaseApp = getApps()[0] || initializeApp({ credential: applicationDefault(), projectId: GCP_PROJECT_ID });
    const audit = new FirestorePilotAcceptanceAudit(getFirestore(firebaseApp), pilotAcceptanceInstanceId,
      res.locals.firebaseUid);
    const result = await audit.read(String(req.params.runId), expected);
    return res.json({ ...result, campaignAuthority: false, executionActivated: false });
  } catch {
    return res.status(503).json({ error: 'LOCAL_PILOT_ACCEPTANCE_UNKNOWN', evidence_status: 'UNVERIFIED' });
  }
});
app.post('/api/local/pilot/acceptance', async (req: Request, res: Response) => {
  if (!LOCAL_ONLY || !localWorkerSupervisor) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  const body = releaseRequestObject(req.body);
  if (!body || Object.keys(body).some((key) => key !== 'check')
    || !['TYPESCRIPT_TESTS', 'LINT', 'BUILD'].includes(String(body.check))) {
    return res.status(400).json({ error: 'LOCAL_PILOT_CHECK_NOT_ALLOWED' });
  }
  if (!res.locals.firebaseUid) return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
  if (pilotAcceptanceRunning) return res.status(409).json({ error: 'LOCAL_PILOT_ACCEPTANCE_BUSY' });
  pilotAcceptanceRunning = true;
  let scratch: string | undefined;
  try {
    assertCommittedPilotCandidate();
    if (!localWorkerIsPaperDisarmed(await readLocalWorkerState()) || !(await localPersistenceIsDurable())) {
      return res.status(409).json({ error: 'LOCAL_PILOT_REQUIRES_PAPER_DISARMED_DURABLE_RUNTIME' });
    }
    const versions = [(process.env.LOCAL_MAINNET_API_KEY_VERSION || '').trim(),
      (process.env.LOCAL_MAINNET_API_SECRET_VERSION || '').trim()] as const;
    const bindingNow = () => {
      const value = currentLocalFingerprint(...versions);
      return { gitSha: value.gitSha, sourceSha256: value.sourceSha256,
        dependencySha256: value.dependencySha256, migrationSha256: value.migrationSha256,
        policySha256: localLivePilotPolicySha256() };
    };
    const binding = bindingNow();
    const firebaseApp = getApps()[0] || initializeApp({ credential: applicationDefault(), projectId: GCP_PROJECT_ID });
    const audit = new FirestorePilotAcceptanceAudit(getFirestore(firebaseApp), pilotAcceptanceInstanceId,
      res.locals.firebaseUid);
    scratch = mkdtempSync(path.join(tmpdir(), 'blessing-acceptance-'));
    const result = await runPilotOfflineAcceptance({
      root: process.cwd(), isolatedHome: scratch, check: body.check as PilotOfflineCheck, binding, audit,
      assertBindingUnchanged: () => {
        assertCommittedPilotCandidate();
        if (JSON.stringify(bindingNow()) !== JSON.stringify(binding)) throw new Error('LOCAL_PILOT_FINGERPRINT_CHANGED');
      },
    });
    return res.json({ ...result, executionActivated: false, campaignAuthority: false });
  } catch (error) {
    if (error instanceof PilotAcceptanceAcknowledgementError) {
      return res.status(503).json({ error: error.message, runId: error.runId,
        evidence_status: 'UNKNOWN', executionActivated: false, campaignAuthority: false });
    }
    return res.status(503).json({ error: 'LOCAL_PILOT_ACCEPTANCE_FAILED', evidence_status: 'UNVERIFIED' });
  } finally {
    pilotAcceptanceRunning = false;
    if (scratch) rmSync(scratch, { recursive: true, force: true });
  }
});

app.post('/api/local/pilot/request', async (req: Request, res: Response) => {
  if (!LOCAL_ONLY || !localWorkerSupervisor) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  const body = releaseRequestObject(req.body) || {};
  if (hasCredentialLikeKey(body) || Object.keys(body).some((key) => key !== 'strategyId')) {
    return res.status(400).json({ error: 'LOCAL_PILOT_REQUEST_INVALID' });
  }
  const strategyId = String(body.strategyId || '').trim().toLowerCase() as LocalLivePilotStrategyId;
  if (!['grid', 'trend', 'shock', 'carry'].includes(strategyId)) {
    return res.status(400).json({ error: 'LOCAL_PILOT_STRATEGY_REQUIRED' });
  }
  const uid = res.locals.firebaseUid;
  if (typeof uid !== 'string' || !uid) return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
  const apiKeyVersion = (process.env.LOCAL_MAINNET_API_KEY_VERSION || '').trim();
  const apiSecretVersion = (process.env.LOCAL_MAINNET_API_SECRET_VERSION || '').trim();
  try {
    assertCommittedPilotCandidate();
    const fingerprint = currentLocalFingerprint(apiKeyVersion, apiSecretVersion);
    const workerState = await readLocalWorkerState();
    if (!localWorkerIsPaperDisarmed(workerState) || !(await localPersistenceIsDurable())) {
      return res.status(409).json({
        error: 'LOCAL_PILOT_REQUIRES_PAPER_DISARMED_DURABLE_RUNTIME',
        evidence_status: 'UNVERIFIED',
      });
    }
    const identity = localSecretSourceIdentity(
      (process.env.LOCAL_SECRET_MANAGER_PROJECT_ID || GCP_PROJECT_ID).trim(),
      apiKeyVersion,
      apiSecretVersion,
    );
    const campaign = newLocalLivePilotCampaign({
      campaignId: `pilot-${crypto.randomUUID()}`,
      runId: fingerprint.runId,
      adminUid: uid,
      role: 'trading_admin',
      gitSha: fingerprint.gitSha,
      sourceHash: fingerprint.sourceSha256,
      dependencyHash: fingerprint.dependencySha256,
      migrationHash: fingerprint.migrationSha256,
      strategyHash: localLivePilotStrategySha256(strategyId),
      riskPolicyHash: localLivePilotPolicySha256(),
      strategyId,
      secretManagerProjectId: identity.secretManagerProjectId,
      apiKeyVersion,
      apiSecretVersion,
      managementMode: 'QUICK',
    });
    const saved = await getServerLocalLivePilotStore().create({
      campaignId: campaign.campaignId,
      runId: campaign.runId,
      adminUid: campaign.adminUid,
      role: 'trading_admin',
      gitSha: campaign.gitSha,
      sourceHash: campaign.sourceHash,
      dependencyHash: campaign.dependencyHash,
      migrationHash: campaign.migrationHash,
      strategyHash: campaign.strategyHash,
      riskPolicyHash: campaign.riskPolicyHash,
      strategyId: campaign.strategyId,
      secretManagerProjectId: campaign.secretManagerProjectId,
      apiKeyVersion: campaign.apiKeyVersion,
      apiSecretVersion: campaign.apiSecretVersion,
      managementMode: campaign.managementMode,
    });
    return res.status(201).json({ ...safeLocalLivePilotCampaign(saved), evidence_status: 'VERIFIED' });
  } catch (error) {
    const reason = error instanceof Error ? error.message : 'LOCAL_PILOT_REQUEST_FAILED';
    return res.status(503).json({ error: 'LOCAL_PILOT_REQUEST_FAILED', reason, evidence_status: 'UNVERIFIED' });
  }
});

app.get('/api/local/pilot/readiness', async (_req: Request, res: Response) => {
  if (!LOCAL_ONLY) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  const uid = typeof res.locals.firebaseUid === 'string' ? res.locals.firebaseUid : undefined;
  const readiness = currentPilotCapabilityReadiness(uid);
  return res.json({
    readiness,
    evidence_status: readiness.status === 'READY' ? 'VERIFIED' : 'UNVERIFIED',
  });
});

app.get('/api/local/pilot/:campaignId', async (req: Request, res: Response) => {
  if (!LOCAL_ONLY) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  try {
    const campaign = await getServerLocalLivePilotStore().get(String(req.params.campaignId));
    if (!campaign) return res.status(404).json({ error: 'LOCAL_PILOT_NOT_FOUND' });
    const supervisor = localWorkerSupervisor?.status();
    const [workerState, workerReadiness, workerAccounting] = supervisor?.mode === 'LIVE'
      ? await Promise.all([
        forwardWorkerRequest('/state').catch(() => null),
        forwardWorkerRequest('/readiness').catch(() => null),
        forwardWorkerRequest('/local-pilot/accounting').catch(() => null),
      ])
      : [null, null, null];
    const workerVerified = workerState?.response.ok === true && workerReadiness?.response.ok === true;
    const pilotWorkerBound = workerVerified
      && supervisor?.pilotCampaignId === campaign.campaignId
      && supervisor.workerResponsiveness === 'RESPONSIVE'
      && workerState?.data?.pilot_campaign_id === campaign.campaignId
      && workerState?.data?.local_run_id === campaign.runId
      && workerState?.data?.local_source_fingerprint === campaign.sourceHash
      && workerState?.data?.local_supervisor_instance_id === supervisor?.supervisorInstanceId
      && workerState?.data?.execution_mode === 'LIVE'
      && workerState?.data?.mainnet_live_approved === true;
    const prepared = pilotWorkerBound
      && workerState?.data?.engine_state === 'DISARMED'
      && workerState?.data?.order_submission_attempts === 0
      && preparedLocalPilotMatches(campaign.preparation || null, {
        campaignId: campaign.campaignId,
        runId: campaign.runId,
        sourceFingerprint: campaign.sourceHash,
        approvalId: `local-approval-${campaign.campaignId.slice('pilot-'.length)}`,
        workerGeneration: supervisor?.workerGeneration ?? -1,
        supervisorInstanceId: supervisor?.supervisorInstanceId || '',
      });
    const accountingData = workerAccounting?.response.ok === true
      && workerAccounting.data?.evidence_status === 'VERIFIED'
      && workerAccounting.data?.campaign_id === campaign.campaignId
      ? workerAccounting.data
      : null;
    return res.json({
      ...safeLocalLivePilotCampaign(campaign),
      readiness: currentPilotCapabilityReadiness(typeof res.locals.firebaseUid === 'string' ? res.locals.firebaseUid : undefined),
      supervision: supervisor?.pilotCampaignId === campaign.campaignId ? {
        workerResponsiveness: supervisor.workerResponsiveness,
        workerStateObservedAt: supervisor.workerStateObservedAt,
        workerHeartbeatAt: supervisor.workerHeartbeatAt,
        pilotLifecycleMonitorStatus: supervisor.pilotLifecycleMonitorStatus,
      } : 'UNKNOWN',
      preparation: prepared ? {
        status: 'PASS', observedAt: campaign.preparation?.preflightObservedAt || null,
      } : { status: 'NOT_RUN', observedAt: null },
      runtime: pilotWorkerBound ? {
        executionMode: workerState?.data?.execution_mode || 'UNKNOWN',
        engineState: workerState?.data?.engine_state || 'UNKNOWN',
        mainnetLiveApproved: workerState?.data?.mainnet_live_approved === true,
        orderSubmissionAttempts: typeof workerState?.data?.order_submission_attempts === 'number'
          ? workerState.data.order_submission_attempts : 'UNKNOWN',
        readiness: workerReadiness?.data || 'UNKNOWN',
      } : 'UNKNOWN',
      accounting: {
        status: accountingData ? 'VERIFIED' : workerAccounting?.data?.status || 'UNKNOWN',
        netPnlUsdc: accountingData?.net_pnl_usdc ?? 'UNKNOWN',
        peakNetPnlUsdc: accountingData?.peak_net_pnl_usdc ?? 'UNKNOWN',
        drawdownUsdc: accountingData?.drawdown_usdc ?? 'UNKNOWN',
        realizedPnlUsdc: accountingData?.realized_pnl_usdc ?? 'UNKNOWN',
        unrealizedPnlUsdc: accountingData?.unrealized_pnl_usdc ?? 'UNKNOWN',
        feesUsdc: accountingData?.fees_usdc ?? 'UNKNOWN',
        fundingUsdc: accountingData?.funding_usdc ?? 'UNKNOWN',
        slippageUsdc: 'UNKNOWN',
        lastEventAt: accountingData?.last_event_at || null,
        reason: accountingData ? null : workerAccounting?.data?.reason || 'PILOT_ACCOUNTING_EVIDENCE_UNAVAILABLE',
      },
      evidence_status: pilotWorkerBound ? 'VERIFIED' : 'UNVERIFIED',
    });
  } catch {
    return res.status(503).json({ error: 'LOCAL_PILOT_STORE_UNAVAILABLE', evidence_status: 'UNVERIFIED' });
  }
});

app.post('/api/local/pilot/approve', async (req: Request, res: Response) => {
  if (!LOCAL_ONLY) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  const uid = res.locals.firebaseUid;
  if (typeof uid !== 'string' || !uid) return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
  if (rejectUnreadyLocalPilot(res, uid)) return;
  const body = releaseRequestObject(req.body) || {};
  if (hasCredentialLikeKey(body) || Object.keys(body).some((key) => key !== 'campaignId')) {
    return res.status(400).json({ error: 'LOCAL_PILOT_APPROVAL_INVALID' });
  }
  const campaignId = String(body.campaignId || '').trim();
  try {
    assertCommittedPilotCandidate();
    const store = getServerLocalLivePilotStore();
    const pending = await store.get(campaignId);
    if (!pending) return res.status(404).json({ error: 'LOCAL_PILOT_NOT_FOUND' });
    const fingerprint = currentLocalFingerprint(pending.apiKeyVersion, pending.apiSecretVersion);
    const currentBinding = {
      ...localLivePilotBinding(pending),
      gitSha: fingerprint.gitSha,
      sourceHash: fingerprint.sourceSha256,
      dependencyHash: fingerprint.dependencySha256,
      migrationHash: fingerprint.migrationSha256,
      strategyHash: localLivePilotStrategySha256(pending.strategyId),
      riskPolicyHash: localLivePilotPolicySha256(),
    };
    if (JSON.stringify(currentBinding) !== JSON.stringify(localLivePilotBinding(pending))) {
      return res.status(409).json({ error: 'LOCAL_PILOT_FINGERPRINT_CHANGED', evidence_status: 'UNVERIFIED' });
    }
    const workerState = await readLocalWorkerState();
    if (!localWorkerIsPaperDisarmed(workerState) || !(await localPersistenceIsDurable())) {
      return res.status(409).json({ error: 'LOCAL_PILOT_REQUIRES_PAPER_DISARMED_DURABLE_RUNTIME' });
    }
    const approved = await store.approve(campaignId, pilotActor(uid), localLivePilotBinding(pending));
    return res.json({ ...safeLocalLivePilotCampaign(approved), evidence_status: 'VERIFIED', executionActivated: false });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'LOCAL_PILOT_APPROVAL_FAILED';
    return res.status(/expired|binding|UID|pending/i.test(message) ? 409 : 503).json({
      error: 'LOCAL_PILOT_APPROVAL_FAILED',
      evidence_status: 'UNVERIFIED',
    });
  }
});

app.post('/api/local/pilot/prepare', async (req: Request, res: Response) => {
  if (!LOCAL_ONLY || !localWorkerSupervisor) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  const uid = res.locals.firebaseUid;
  if (typeof uid !== 'string' || !uid) return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
  if (rejectUnreadyLocalPilot(res, uid)) return;
  const body = releaseRequestObject(req.body) || {};
  if (hasCredentialLikeKey(body) || Object.keys(body).some((key) => key !== 'campaignId')) {
    return res.status(400).json({ error: 'LOCAL_PILOT_PREPARE_INVALID' });
  }
  if (!reserveLocalPilotTransition()) return res.status(409).json({ error: 'LOCAL_PILOT_TRANSITION_IN_PROGRESS' });
  const campaignId = String(body.campaignId || '').trim();
  const store = getServerLocalLivePilotStore();
  let campaign: LocalLivePilotCampaign | null = null;
  let workerReplacementStarted = false;
  try {
    assertCommittedPilotCandidate();
    campaign = await store.get(campaignId);
    if (!campaign) return res.status(404).json({ error: 'LOCAL_PILOT_NOT_FOUND' });
    if (campaign.adminUid !== uid || campaign.approvedByUid !== uid
      || !['APPROVED', 'ACTIVE'].includes(campaign.status)) {
      return res.status(409).json({ error: 'LOCAL_PILOT_APPROVAL_BINDING_INVALID' });
    }
    if (!localLivePilotCanPrepare(campaign)) {
      return res.status(409).json({ error: 'LOCAL_PILOT_APPROVAL_EXPIRED_OR_INVALID' });
    }
    const fingerprint = currentLocalFingerprint(campaign.apiKeyVersion, campaign.apiSecretVersion);
    const currentBinding = {
      ...localLivePilotBinding(campaign),
      gitSha: fingerprint.gitSha,
      sourceHash: fingerprint.sourceSha256,
      dependencyHash: fingerprint.dependencySha256,
      migrationHash: fingerprint.migrationSha256,
      strategyHash: localLivePilotStrategySha256(campaign.strategyId),
      riskPolicyHash: localLivePilotPolicySha256(),
    };
    if (JSON.stringify(currentBinding) !== JSON.stringify(localLivePilotBinding(campaign))) {
      return res.status(409).json({ error: 'LOCAL_PILOT_FINGERPRINT_CHANGED' });
    }
    if (!(await localPersistenceIsDurable())) {
      return res.status(409).json({ error: 'LOCAL_PILOT_DURABLE_POSTGRES_UNAVAILABLE' });
    }
    const workerBeforeStart = await readLocalWorkerState();
    if (!localWorkerIsPaperDisarmed(workerBeforeStart)
      || workerBeforeStart.order_submission_attempts !== 0) {
      return res.status(409).json({ error: 'LOCAL_PILOT_REQUIRES_UNUSED_PAPER_WORKER' });
    }

    // The only secret read in this route is after the persisted trading_admin approval.
    const identity = localSecretSourceIdentity(
      campaign.secretManagerProjectId,
      campaign.apiKeyVersion,
      campaign.apiSecretVersion,
    );
    const secrets = await accessPinnedLocalMainnetSecrets({
      projectId: campaign.secretManagerProjectId,
      apiKeyVersion: campaign.apiKeyVersion,
      apiSecretVersion: campaign.apiSecretVersion,
      identity,
      campaign: {
        campaignId: campaign.campaignId,
        status: campaign.status === 'ACTIVE' ? 'ACTIVE' : 'APPROVED',
        adminUid: campaign.adminUid,
        approvedByUid: campaign.approvedByUid || '',
        campaignExpiresAt: campaign.campaignExpiresAt || '',
        secretManagerProjectId: campaign.secretManagerProjectId,
        apiKeyVersion: campaign.apiKeyVersion,
        apiSecretVersion: campaign.apiSecretVersion,
      },
    });
    const approvalId = `local-approval-${campaign.campaignId.slice('pilot-'.length)}`;
    workerReplacementStarted = true;
    await localWorkerSupervisor.startApprovedPilotLive({
      approvalId,
      sourceFingerprint: fingerprint.sourceSha256,
      apiKey: secrets.apiKey,
      apiSecret: secrets.apiSecret,
      apiKeyVersion: secrets.apiKeyVersion,
      apiSecretVersion: secrets.apiSecretVersion,
      campaignId: campaign.campaignId,
      gitSha: campaign.gitSha,
      sourceHash: campaign.sourceHash,
      dependencyHash: campaign.dependencyHash,
      migrationHash: campaign.migrationHash,
      strategyHash: campaign.strategyHash,
      riskPolicyHash: campaign.riskPolicyHash,
      secretProjectId: campaign.secretManagerProjectId,
      strategyId: campaign.strategyId,
      expiresAt: campaign.campaignExpiresAt || '',
    });
    const preflight = await forwardWorkerRequest('/preflight/read-only', { method: 'POST' });
    if (!preflight.response.ok) throw new Error('LOCAL_PILOT_READ_ONLY_PREFLIGHT_FAILED');
    const finalState = await forwardWorkerRequest('/state');
    if (!finalState.response.ok) throw new Error('LOCAL_PILOT_WORKER_STATE_UNAVAILABLE');
    const supervisor = localWorkerSupervisor.status();
    if (supervisor.pilotCampaignId !== campaign.campaignId
      || supervisor.approvalId !== approvalId
      || supervisor.sourceFingerprint !== fingerprint.sourceSha256
      || supervisor.secretVersions?.apiKey !== campaign.apiKeyVersion
      || supervisor.secretVersions?.apiSecret !== campaign.apiSecretVersion) {
      throw new Error('LOCAL_PILOT_SUPERVISOR_BINDING_MISMATCH');
    }
    const preparation = attestPreparedLocalPilot({
      campaignId: campaign.campaignId,
      runId: campaign.runId,
      sourceFingerprint: fingerprint.sourceSha256,
      approvalId,
      workerGeneration: supervisor.workerGeneration,
      supervisorInstanceId: supervisor.supervisorInstanceId,
      workerState: finalState.data,
      preflight: preflight.data,
    });
    campaign = await store.recordPreparation(campaign.campaignId, localLivePilotBinding(campaign), preparation);
    return res.json({
      campaign: safeLocalLivePilotCampaign(campaign),
      preflight: { status: 'PASS', observedAt: preparation.preflightObservedAt, sha256: preparation.preflightSha256 },
      worker: { status: 'LIVE_DISARMED', mainnetLiveApproved: true, orderSubmissionAttempts: 0 },
      evidence_status: 'VERIFIED',
    });
  } catch (error) {
    if (workerReplacementStarted) {
      try {
        const state = campaign ? await confirmRunningLocalPilotCampaign(campaign.campaignId) : null;
        if (state?.engine_state === 'DISARMED' && state.order_submission_attempts === 0) {
          await localWorkerSupervisor.startPaper();
        }
      } catch { /* preserve the Worker for operator inspection when its state is ambiguous */ }
    }
    const message = error instanceof Error ? error.message : 'LOCAL_PILOT_PREPARE_FAILED';
    return res.status(503).json({ error: 'LOCAL_PILOT_PREPARE_FAILED', reason: message, evidence_status: 'UNVERIFIED' });
  } finally {
    localPilotTransitionBusy = false;
  }
});

app.post('/api/local/pilot/start', async (req: Request, res: Response) => {
  if (!LOCAL_ONLY || !localWorkerSupervisor) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  if (rejectUnreadyLocalPilot(res)) return;
  const body = releaseRequestObject(req.body) || {};
  if (hasCredentialLikeKey(body) || Object.keys(body).some((key) => key !== 'campaignId')) {
    return res.status(400).json({ error: 'LOCAL_PILOT_START_INVALID' });
  }
  const uid = res.locals.firebaseUid;
  if (typeof uid !== 'string' || !uid) return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
  if (!reserveLocalPilotTransition()) return res.status(409).json({ error: 'LOCAL_PILOT_TRANSITION_IN_PROGRESS' });
  let campaign: LocalLivePilotCampaign | null = null;
  let activated = false;
  let armAttempted = false;
  try {
    assertCommittedPilotCandidate();
    campaign = await getServerLocalLivePilotStore().get(String(body.campaignId || '').trim());
    if (!campaign) return res.status(404).json({ error: 'LOCAL_PILOT_NOT_FOUND' });
    if (campaign.adminUid !== uid || campaign.approvedByUid !== uid || !localLivePilotCanStart(campaign)) {
      return res.status(409).json({ error: 'LOCAL_PILOT_APPROVAL_BINDING_INVALID' });
    }
    const fingerprint = currentLocalFingerprint(campaign.apiKeyVersion, campaign.apiSecretVersion);
    const currentBinding = {
      ...localLivePilotBinding(campaign),
      gitSha: fingerprint.gitSha,
      sourceHash: fingerprint.sourceSha256,
      dependencyHash: fingerprint.dependencySha256,
      migrationHash: fingerprint.migrationSha256,
      strategyHash: localLivePilotStrategySha256(campaign.strategyId),
      riskPolicyHash: localLivePilotPolicySha256(),
    };
    if (JSON.stringify(currentBinding) !== JSON.stringify(localLivePilotBinding(campaign))) {
      return res.status(409).json({ error: 'LOCAL_PILOT_FINGERPRINT_CHANGED' });
    }
    if (!(await localPersistenceIsDurable())) {
      return res.status(409).json({ error: 'LOCAL_PILOT_DURABLE_POSTGRES_UNAVAILABLE' });
    }
    const supervisor = localWorkerSupervisor.status();
    const approvalId = `local-approval-${campaign.campaignId.slice('pilot-'.length)}`;
    if (supervisor.mode !== 'LIVE'
      || supervisor.workerResponsiveness !== 'RESPONSIVE'
      || supervisor.pilotCampaignId !== campaign.campaignId
      || supervisor.approvalId !== approvalId
      || supervisor.sourceFingerprint !== fingerprint.sourceSha256
      || supervisor.secretVersions?.apiKey !== campaign.apiKeyVersion
      || supervisor.secretVersions?.apiSecret !== campaign.apiSecretVersion
      || supervisor.workerGeneration === null
      || !preparedLocalPilotMatches(campaign.preparation || null, {
        campaignId: campaign.campaignId,
        runId: campaign.runId,
        sourceFingerprint: fingerprint.sourceSha256,
        approvalId,
        workerGeneration: supervisor.workerGeneration,
        supervisorInstanceId: supervisor.supervisorInstanceId,
      })) {
      return res.status(409).json({ error: 'LOCAL_PILOT_PREPARE_REQUIRED' });
    }
    const beforeArm = await readLocalWorkerState();
    if (beforeArm.engine_state !== 'DISARMED' || beforeArm.order_submission_attempts !== 0
      || beforeArm.mainnet_live_approved !== true
      || beforeArm.pilot_campaign_id !== campaign.campaignId
      || beforeArm.local_run_id !== campaign.runId
      || beforeArm.local_source_fingerprint !== campaign.sourceHash
      || beforeArm.local_supervisor_instance_id !== supervisor.supervisorInstanceId) {
      return res.status(409).json({ error: 'LOCAL_PILOT_PREPARED_WORKER_CHANGED' });
    }
    const preflight = await forwardWorkerRequest('/preflight/read-only', { method: 'POST' });
    const afterPreflight = await forwardWorkerRequest('/state');
    if (!preflight.response.ok || !afterPreflight.response.ok) {
      return res.status(409).json({ error: 'LOCAL_PILOT_READ_ONLY_PREFLIGHT_FAILED', evidence_status: 'FAIL' });
    }
    const refreshedPreparation = attestPreparedLocalPilot({
      campaignId: campaign.campaignId,
      runId: campaign.runId,
      sourceFingerprint: fingerprint.sourceSha256,
      approvalId,
      workerGeneration: supervisor.workerGeneration,
      supervisorInstanceId: supervisor.supervisorInstanceId,
      workerState: afterPreflight.data,
      preflight: preflight.data,
    });
    campaign = await getServerLocalLivePilotStore().recordPreparation(
      campaign.campaignId, localLivePilotBinding(campaign), refreshedPreparation,
    );
    // This CAS transition is immediately followed by the explicit ARM action.
    campaign = await getServerLocalLivePilotStore().activate(campaign.campaignId, localLivePilotBinding(campaign));
    activated = true;
    const strategies = {
      grid: campaign.strategyId === 'grid',
      trend: campaign.strategyId === 'trend',
      shock: campaign.strategyId === 'shock',
      carry: campaign.strategyId === 'carry',
    };
    armAttempted = true;
    const armed = await forwardWorkerRequest('/arm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        executionMode: 'LIVE', instruments: ['ETHUSDC'], strategies,
        riskProfile: 'CONSERVATIVE', enforcePreflight: true,
        releaseApprovalId: approvalId,
        launchPolicy: 'LIVE_RESEARCH_PILOT',
        pilotCampaignId: campaign.campaignId,
      }),
    });
    if (!armed.response.ok) throw new Error('LOCAL_PILOT_WORKER_ARM_REJECTED');
    const finalState = await forwardWorkerRequest('/state');
    if (!finalState.response.ok || finalState.data?.execution_mode !== 'LIVE'
      || !['ARMED', 'PAUSED_NEW_RISK'].includes(String(finalState.data?.engine_state))
      || finalState.data?.mainnet_live_approved !== true
      || finalState.data?.pilot_campaign_id !== campaign.campaignId
      || finalState.data?.local_run_id !== campaign.runId
      || finalState.data?.local_source_fingerprint !== campaign.sourceHash
      || finalState.data?.local_supervisor_instance_id !== supervisor.supervisorInstanceId
      || finalState.data?.mainnet_launch_policy !== 'LIVE_RESEARCH_PILOT'
      || !Number.isSafeInteger(finalState.data?.order_submission_attempts)
      || finalState.data?.order_submission_attempts < 0) {
      throw new Error('LOCAL_PILOT_ARM_READBACK_FAILED');
    }
    return res.json({
      campaign: safeLocalLivePilotCampaign(campaign),
      preflight: { status: 'PASS', observedAt: preflight.data?.observedAt || null },
      worker: {
        status: String(finalState.data.engine_state),
        orderSubmissionAttempts: finalState.data.order_submission_attempts ?? 'UNKNOWN',
      },
      evidence_status: 'VERIFIED',
    });
  } catch (error) {
    if (activated && campaign) {
      try {
        await getServerLocalLivePilotStore().enterCloseOnly(campaign.campaignId, localLivePilotBinding(campaign));
      } catch { /* retain fail-closed runtime for operator reconciliation */ }
    }
    if (armAttempted && campaign) {
      try {
        const state = await confirmRunningLocalPilotCampaign(campaign.campaignId);
        if (state && ['ARMED', 'PAUSED_NEW_RISK'].includes(String(state.engine_state))) {
          await forwardWorkerRequest('/recovery-only', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ active: true }),
          });
        }
      } catch { /* keep the live Worker for inspection; never replace an ambiguous process */ }
    }
    const message = error instanceof Error ? error.message : 'LOCAL_PILOT_START_FAILED';
    return res.status(503).json({ error: 'LOCAL_PILOT_START_FAILED', reason: message, evidence_status: 'UNVERIFIED' });
  } finally {
    localPilotTransitionBusy = false;
  }
});

app.post('/api/local/pilot/close-only', async (req: Request, res: Response) => {
  if (!LOCAL_ONLY) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  const body = releaseRequestObject(req.body) || {};
  if (hasCredentialLikeKey(body) || Object.keys(body).some((key) => key !== 'campaignId')) {
    return res.status(400).json({ error: 'LOCAL_PILOT_CLOSE_ONLY_INVALID' });
  }
  const uid = res.locals.firebaseUid;
  if (typeof uid !== 'string' || !uid) return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
  if (!reserveLocalPilotTransition()) return res.status(409).json({ error: 'LOCAL_PILOT_TRANSITION_IN_PROGRESS' });
  try {
    const store = getServerLocalLivePilotStore();
    const campaign = await store.get(String(body.campaignId || '').trim());
    if (!campaign) return res.status(404).json({ error: 'LOCAL_PILOT_NOT_FOUND' });
    if (campaign.adminUid !== uid) return res.status(403).json({ error: 'LOCAL_PILOT_CLOSER_UID_MISMATCH' });
    if (!['APPROVED', 'ACTIVE', 'EXPIRED'].includes(campaign.status)) {
      return res.status(409).json({ error: 'LOCAL_PILOT_CANNOT_ENTER_CLOSE_ONLY_FROM_STATUS' });
    }
    const supervisor = localWorkerSupervisor?.status();
    if (supervisor?.mode === 'LIVE') {
      await confirmRunningLocalPilotCampaign(campaign.campaignId);
      const closeOnly = await forwardWorkerRequest('/recovery-only', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ active: true }),
      });
      if (!closeOnly.response.ok || closeOnly.data?.active !== true) {
        return res.status(503).json({ error: 'LOCAL_PILOT_WORKER_CLOSE_ONLY_UNCONFIRMED', evidence_status: 'UNVERIFIED' });
      }
    }
    const closed = await store.enterCloseOnly(campaign.campaignId, localLivePilotBinding(campaign));
    return res.json({ ...safeLocalLivePilotCampaign(closed), evidence_status: 'VERIFIED' });
  } catch {
    return res.status(409).json({ error: 'LOCAL_PILOT_CLOSE_ONLY_FAILED', evidence_status: 'UNVERIFIED' });
  } finally {
    localPilotTransitionBusy = false;
  }
});

app.post('/api/local/pilot/revoke', async (req: Request, res: Response) => {
  if (!LOCAL_ONLY || !localWorkerSupervisor) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  const body = releaseRequestObject(req.body) || {};
  if (hasCredentialLikeKey(body) || Object.keys(body).some((key) => key !== 'campaignId')) {
    return res.status(400).json({ error: 'LOCAL_PILOT_REVOCATION_INVALID' });
  }
  const uid = res.locals.firebaseUid;
  if (typeof uid !== 'string' || !uid) return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
  if (!reserveLocalPilotTransition()) return res.status(409).json({ error: 'LOCAL_PILOT_TRANSITION_IN_PROGRESS' });
  let workerRecoveryConfirmed = false;
  try {
    const store = getServerLocalLivePilotStore();
    const campaign = await store.get(String(body.campaignId || '').trim());
    if (!campaign) return res.status(404).json({ error: 'LOCAL_PILOT_NOT_FOUND' });
    if (campaign.adminUid !== uid) return res.status(403).json({ error: 'LOCAL_PILOT_REVOCER_UID_MISMATCH' });
    if (campaign.status === 'COMPLETED') return res.status(409).json({ error: 'LOCAL_PILOT_ALREADY_COMPLETED' });
    const supervisor = localWorkerSupervisor.status();
    const revoked = await revokeLocalPilotWithWorkerGuard({
      campaign,
      actorUid: uid,
      worker: supervisor,
      requestRecoveryOnly: async () => {
        const closeOnly = await forwardWorkerRequest('/recovery-only', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ active: true }),
        });
        return { ok: closeOnly.response.ok, active: closeOnly.data?.active };
      },
      readWorkerState: readLocalWorkerState,
      commitRevocation: () => store.revoke(
        campaign.campaignId,
        pilotActor(uid),
        localLivePilotBinding(campaign),
      ),
      onWorkerRecoveryConfirmed: () => { workerRecoveryConfirmed = true; },
    });
    return res.json({ ...safeLocalLivePilotCampaign(revoked), evidence_status: 'VERIFIED' });
  } catch (error) {
    const message = error instanceof Error ? error.message : '';
    if (message === 'LOCAL_PILOT_WORKER_CLOSE_ONLY_UNCONFIRMED') {
      return res.status(503).json({ error: message, evidence_status: 'UNVERIFIED' });
    }
    if (message === 'LOCAL_PILOT_WORKER_CAMPAIGN_MISMATCH') {
      return res.status(409).json({ error: message, evidence_status: 'UNVERIFIED' });
    }
    return res.status(409).json({
      error: 'LOCAL_PILOT_REVOCATION_FAILED',
      workerRecoveryOnly: workerRecoveryConfirmed,
      evidence_status: 'UNVERIFIED',
    });
  } finally {
    localPilotTransitionBusy = false;
  }
});

async function inspectLocalContinuationContext(
  candidateId: string,
  launchId: string,
): Promise<{
  candidate: LocalReleaseCandidate;
  consumedApprovalId: string;
  binding: LocalReleaseBinding;
  evidenceHash: string;
  failures: string[];
}> {
  if (!LOCAL_ONLY || !localWorkerSupervisor) throw new Error('LOCAL_RUNTIME_REQUIRED');
  const store = getServerLocalReleaseStore();
  const candidate = await store.getCandidate(candidateId);
  if (!candidate) throw new Error('LOCAL_RELEASE_CANDIDATE_NOT_FOUND');
  if (candidate.status !== 'CONSUMED' || !candidate.approvalId) {
    throw new Error('LOCAL_INITIAL_RELEASE_APPROVAL_NOT_CONSUMED');
  }
  const consumed = await store.getConsumedApproval(candidate.approvalId);
  if (!consumed) throw new Error('LOCAL_INITIAL_RELEASE_APPROVAL_INVALID');

  const fingerprint = currentLocalFingerprint(candidate.apiKeyVersion, candidate.apiSecretVersion);
  const promotion = localPromotionStatus(fingerprint);
  const binding = localBindingFromFingerprint(fingerprint, promotion.bundleSha256 || '');
  assertExpectedLocalBinding(candidate, binding);
  const supervisor = localWorkerSupervisor.status();
  const [workerState, readinessResult, persistenceReady] = await Promise.all([
    readLocalWorkerState(),
    runContinuationReadiness(launchId),
    localPersistenceIsDurable(),
  ]);
  const evidence = readinessResult.evidence;
  const failures = [...promotion.failures];
  if (!promotion.passed) failures.push('Local promotion evidence is not currently verified');
  if (supervisor.mode !== 'LIVE'
    || !supervisor.workerRunning
    || supervisor.runId !== candidate.runId
    || supervisor.approvalId !== candidate.approvalId
    || supervisor.sourceFingerprint !== binding.sourceFingerprint
    || supervisor.secretVersions?.apiKey !== candidate.apiKeyVersion
    || supervisor.secretVersions?.apiSecret !== candidate.apiSecretVersion) {
    failures.push('Local Worker supervisor identity does not match the consumed release approval');
  }
  if (!persistenceReady || evidence.persistenceDurable !== true) {
    failures.push('Durable Local PostgreSQL persistence is not verified');
  }
  if (evidence.launchId !== launchId
    || evidence.launchPolicy !== 'STAGED_FIRST_ORDER'
    || evidence.launchState !== 'PAUSED_NEW_RISK'
    || evidence.submittedOrders !== 1
    || evidence.reservedOrders < 1
    || evidence.engineState !== 'PAUSED_NEW_RISK') {
    failures.push('Durable launch session is not paused after exactly one confirmed first risk-increasing order');
  }
  if (!evidence.firstOrderClientOrderId
    || !/^[A-Za-z0-9_-]{1,64}$/.test(evidence.firstOrderClientOrderId)
    || evidence.pendingOrderClientOrderId) {
    failures.push('First-order client ID is missing or an exchange submission remains ambiguous');
  }
  if (evidence.mainnetLiveApproved !== true
    || evidence.preflightPassed !== true
    || evidence.preflightOrderSubmissionAttempts !== 0
    || evidence.preflightOrderEndpointAttempts !== 0
    || !evidence.continuationReady
    || evidence.checks.some((check) => check.required && check.status !== 'PASS')) {
    failures.push('Fresh read-only continuation preflight is not fully passing with zero preflight order attempts');
  }
  if (workerState.execution_mode !== 'LIVE'
    || workerState.mainnet_live_approved !== true
    || workerState.mainnet_launch_id !== launchId
    || workerState.mainnet_launch_policy !== 'STAGED_FIRST_ORDER'
    || workerState.mainnet_launch_state !== 'PAUSED_NEW_RISK'
    || workerState.engine_state !== 'PAUSED_NEW_RISK'
    || workerState.reconciliation_status !== 'IN_SYNC'
    || workerState.kill_switch_active === true) {
    failures.push('Worker state does not prove the approved Local staged launch is paused and reconciled');
  }
  return {
    candidate,
    consumedApprovalId: consumed.approvalId,
    binding,
    evidenceHash: localContinuationEvidenceHash(evidence, workerState, binding),
    failures: [...new Set(failures)],
  };
}

function safeLocalCandidate(candidate: LocalReleaseCandidate) {
  return {
    candidateId: candidate.candidateId,
    runtimeTarget: candidate.runtimeTarget,
    runId: candidate.runId,
    status: candidate.status,
    sourceFingerprint: candidate.sourceFingerprint,
    dependencyFingerprint: candidate.dependencyFingerprint,
    migrationFingerprint: candidate.migrationFingerprint,
    promotionEvidenceSha256: candidate.promotionEvidenceSha256,
    policyVersion: candidate.policyVersion,
    policyHash: candidate.policyHash,
    secretVersions: {
      apiKey: candidate.apiKeyVersion,
      apiSecret: candidate.apiSecretVersion,
    },
    createdAt: candidate.createdAt,
    expiresAt: candidate.expiresAt,
  };
}

app.get('/api/local/runtime', async (_req: Request, res: Response) => {
  if (!LOCAL_ONLY) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  try {
    const [workerState, readiness] = await Promise.all([
      readLocalWorkerState(),
      forwardWorkerRequest('/readiness'),
    ]);
    const workerReadiness = releaseRequestObject(readiness.data) || {};
    const persistence = releaseRequestObject(workerReadiness.persistence) || {};
    const fingerprint = (() => {
      try {
        return currentLocalFingerprint(
          process.env.LOCAL_MAINNET_API_KEY_VERSION || '',
          process.env.LOCAL_MAINNET_API_SECRET_VERSION || '',
        );
      } catch {
        return null;
      }
    })();
    const promotion = fingerprint
      ? localPromotionStatus(fingerprint)
      : {
          passed: false,
          failures: ['Local release fingerprint is unavailable'],
          totalClosedBaskets: 0,
          cohorts: undefined,
        };
    return res.json({
      runtimeTarget: RUNTIME_TARGET,
      supervisor: localWorkerSupervisor?.status() || null,
      executionMode: workerState.execution_mode || 'UNKNOWN',
      engineState: workerState.engine_state || 'UNKNOWN',
      mainnetLiveApproved: workerState.mainnet_live_approved === true,
      orderSubmissionAttempts: Number(workerState.order_submission_attempts || 0),
      orderEndpointAttempts: Number(workerState.order_endpoint_attempts || 0),
      persistence: {
        mode: persistence.mode || 'UNKNOWN',
        ready: persistence.ready === true,
        durable: persistence.durable === true,
        runtimeTarget: persistence.runtime_target || 'UNKNOWN',
        databaseProvider: persistence.database_provider || 'UNKNOWN',
        databaseHost: persistence.database_host || null,
        databasePort: persistence.database_port || null,
        databaseIdentityVerified: persistence.database_identity_verified === true,
      },
      localMainnetRiskLifecycle: {
        ready: workerReadiness.local_mainnet_risk_lifecycle_ready === true,
        missing: Array.isArray(workerReadiness.local_mainnet_risk_lifecycle_missing)
          ? workerReadiness.local_mainnet_risk_lifecycle_missing
          : [],
      },
      promotionEvidence: {
        passed: promotion.passed,
        failures: promotion.failures,
        totalClosedBaskets: promotion.totalClosedBaskets || 0,
        cohorts: promotion.cohorts || null,
      },
      evidence_status: 'UNVERIFIED',
    });
  } catch {
    return res.status(503).json({
      error: 'LOCAL_RUNTIME_STATUS_UNAVAILABLE',
      runtimeTarget: 'LOCAL',
      evidence_status: 'UNVERIFIED',
    });
  }
});

app.post('/api/local/mainnet/candidate', async (req: Request, res: Response) => {
  if (!LOCAL_ONLY) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  const body = releaseRequestObject(req.body) || {};
  if (hasCredentialLikeKey(body) || Object.keys(body).length > 0) {
    return res.status(400).json({ error: 'LOCAL_CANDIDATE_BODY_MUST_BE_EMPTY' });
  }
  const apiKeyVersion = (process.env.LOCAL_MAINNET_API_KEY_VERSION || '').trim();
  const apiSecretVersion = (process.env.LOCAL_MAINNET_API_SECRET_VERSION || '').trim();
  try {
    const fingerprint = currentLocalFingerprint(apiKeyVersion, apiSecretVersion);
    const promotion = localPromotionStatus(fingerprint);
    if (!promotion.passed) {
      return res.status(409).json({
        error: 'LOCAL_PROMOTION_GATE_NOT_PASSED',
        failures: promotion.failures,
        totalClosedBaskets: promotion.totalClosedBaskets || 0,
        cohorts: promotion.cohorts || null,
        evidence_status: 'UNVERIFIED',
      });
    }
    const workerState = await readLocalWorkerState();
    const localRiskLifecycle = await localMainnetRiskLifecycleStatus();
    if (!localRiskLifecycle.ready) {
      return res.status(409).json({
        error: 'LOCAL_MAINNET_RISK_LIFECYCLE_UNAVAILABLE',
        missing: localRiskLifecycle.missing,
        message: 'Local release candidates stay blocked until durable basket-risk context and verified stop/target lifecycle are implemented.',
        evidence_status: 'UNVERIFIED',
      });
    }
    if (!localWorkerIsPaperDisarmed(workerState) || !(await localPersistenceIsDurable())) {
      return res.status(409).json({
        error: 'LOCAL_RUNTIME_NOT_PAPER_DISARMED_WITH_DURABLE_LOCAL_DATABASE',
        message: 'Candidate creation requires the current Local Worker to be PAPER/DISARMED and the Local PostgreSQL ledger to be durable.',
        evidence_status: 'UNVERIFIED',
      });
    }
    const binding = localBindingFromFingerprint(fingerprint, promotion.bundleSha256 || '');
    const candidate = newLocalReleaseCandidate({
      runId: binding.runId,
      sourceFingerprint: binding.sourceFingerprint,
      dependencyFingerprint: binding.dependencyFingerprint,
      migrationFingerprint: binding.migrationFingerprint,
      promotionEvidenceSha256: binding.promotionEvidenceSha256,
      apiKeyVersion: binding.apiKeyVersion,
      apiSecretVersion: binding.apiSecretVersion,
      secretManagerProjectId: binding.secretManagerProjectId,
      apiKeySecretVersionResource: binding.apiKeySecretVersionResource,
      apiSecretSecretVersionResource: binding.apiSecretSecretVersionResource,
      policyVersion: binding.policyVersion,
      policyHash: binding.policyHash,
      nonce: crypto.randomBytes(24).toString('base64url'),
    });
    await getServerLocalReleaseStore().createCandidate(candidate);
    return res.status(201).json({
      ...safeLocalCandidate(candidate),
      promotionEvidence: {
        bundleSha256: promotion.bundleSha256,
        totalClosedBaskets: promotion.totalClosedBaskets,
        cohorts: promotion.cohorts,
      },
      evidence_status: 'VERIFIED',
    });
  } catch {
    return res.status(503).json({ error: 'LOCAL_RELEASE_CANDIDATE_FAILED', evidence_status: 'UNVERIFIED' });
  }
});

app.get('/api/local/mainnet/:candidateId', async (req: Request, res: Response) => {
  if (!LOCAL_ONLY) return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  try {
    const candidate = await getServerLocalReleaseStore().getCandidate(String(req.params.candidateId));
    if (!candidate) return res.status(404).json({ error: 'LOCAL_RELEASE_CANDIDATE_NOT_FOUND' });
    return res.json(safeLocalCandidate(candidate));
  } catch {
    return res.status(503).json({ error: 'LOCAL_RELEASE_STORE_UNAVAILABLE', evidence_status: 'UNVERIFIED' });
  }
});

app.post('/api/local/mainnet/continuation/request', async (req: Request, res: Response) => {
  if (!LOCAL_ONLY || !localWorkerSupervisor) {
    return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  }
  const body = releaseRequestObject(req.body) || {};
  if (hasCredentialLikeKey(body)
    || Object.keys(body).some((key) => !['candidateId', 'launchId'].includes(key))) {
    return res.status(400).json({ error: 'LOCAL_CONTINUATION_PAYLOAD_INVALID' });
  }
  const candidateId = typeof body.candidateId === 'string' ? body.candidateId.trim() : '';
  const launchId = typeof body.launchId === 'string' ? body.launchId.trim() : '';
  if (!/^local-rc-[0-9a-f-]{36}$/i.test(candidateId)) {
    return res.status(400).json({ error: 'LOCAL_RELEASE_CANDIDATE_ID_REQUIRED' });
  }
  if (!/^launch-[A-Za-z0-9-]{8,127}$/.test(launchId)) {
    return res.status(400).json({ error: 'LAUNCH_ID_REQUIRED' });
  }
  const uid = res.locals.firebaseUid;
  if (typeof uid !== 'string' || !uid) return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
  try {
    const context = await inspectLocalContinuationContext(candidateId, launchId);
    if (context.failures.length) {
      return res.status(409).json({
        error: 'LOCAL_CONTINUATION_GATE_NOT_PASSED',
        failures: context.failures,
        evidence_status: 'UNVERIFIED',
        executionActivated: false,
      });
    }
    const approval = newLocalContinuationApproval({
      continuationId: localContinuationIdFor(candidateId, launchId),
      candidateId,
      initialApprovalId: context.consumedApprovalId,
      runId: context.binding.runId,
      launchId,
      sourceFingerprint: context.binding.sourceFingerprint,
      dependencyFingerprint: context.binding.dependencyFingerprint,
      migrationFingerprint: context.binding.migrationFingerprint,
      firstOrderEvidenceHash: context.evidenceHash,
      tradingAdminUid: uid,
      nonce: crypto.randomBytes(24).toString('base64url'),
      policyVersion: context.binding.policyVersion,
      policyHash: context.binding.policyHash,
    });
    const savedApproval = await getServerLocalContinuationStore().createApproval(approval);
    return res.status(201).json({
      runtimeTarget: 'LOCAL',
      continuationId: savedApproval.continuationId,
      candidateId,
      launchId,
      initialApprovalId: context.consumedApprovalId,
      firstOrderEvidenceHash: context.evidenceHash,
      status: savedApproval.status,
      expiresAt: savedApproval.expiresAt,
      evidence_status: 'VERIFIED',
      executionActivated: false,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Local continuation request failed';
    const status = message.includes('NOT_FOUND') ? 404 : 503;
    return res.status(status).json({
      error: 'LOCAL_CONTINUATION_REQUEST_FAILED',
      message,
      evidence_status: 'UNVERIFIED',
      executionActivated: false,
    });
  }
});

app.post('/api/local/mainnet/continuation/approve', async (req: Request, res: Response) => {
  if (!LOCAL_ONLY || !localWorkerSupervisor) {
    return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  }
  const body = releaseRequestObject(req.body) || {};
  if (hasCredentialLikeKey(body)
    || Object.keys(body).some((key) => key !== 'continuationId')) {
    return res.status(400).json({ error: 'LOCAL_CONTINUATION_APPROVAL_PAYLOAD_INVALID' });
  }
  const continuationId = typeof body.continuationId === 'string' ? body.continuationId.trim() : '';
  if (!/^local-continuation-[0-9a-f-]{36}$/i.test(continuationId)) {
    return res.status(400).json({ error: 'LOCAL_CONTINUATION_ID_REQUIRED' });
  }
  const uid = res.locals.firebaseUid;
  if (typeof uid !== 'string' || !uid) return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
  try {
    const store = getServerLocalContinuationStore();
    const pending = await store.getApproval(continuationId);
    if (!pending) return res.status(404).json({ error: 'LOCAL_CONTINUATION_NOT_FOUND' });
    if (pending.status !== 'PENDING_APPROVAL') {
      return res.status(409).json({ error: 'LOCAL_CONTINUATION_NOT_PENDING', status: pending.status });
    }
    const context = await inspectLocalContinuationContext(pending.candidateId, pending.launchId);
    if (context.failures.length || context.evidenceHash !== pending.firstOrderEvidenceHash) {
      return res.status(409).json({
        error: 'LOCAL_CONTINUATION_EVIDENCE_CHANGED',
        failures: context.failures.length ? context.failures : ['First-order evidence no longer matches the pending request'],
        evidence_status: 'UNVERIFIED',
        executionActivated: false,
      });
    }
    const approved = await store.approveContinuation(
      continuationId,
      { uid, role: 'trading_admin' },
      localContinuationBinding(pending),
    );
    return res.json({
      runtimeTarget: 'LOCAL',
      continuationId,
      approvalId: approved.approvalId,
      candidateId: approved.candidateId,
      launchId: approved.launchId,
      status: approved.status,
      expiresAt: approved.expiresAt,
      firstOrderEvidenceHash: approved.firstOrderEvidenceHash,
      evidence_status: 'VERIFIED',
      executionActivated: false,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Local continuation approval failed';
    const status = message.includes('not found') ? 404 : /binding|UID|expired|pending/i.test(message) ? 409 : 503;
    return res.status(status).json({
      error: 'LOCAL_CONTINUATION_APPROVAL_FAILED',
      evidence_status: 'UNVERIFIED',
      executionActivated: false,
    });
  }
});

app.post('/api/local/mainnet/approve', async (_req: Request, res: Response) => {
  if (!LOCAL_ONLY || !localWorkerSupervisor) {
    return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
  }
  return res.status(409).json({
    error: 'LOCAL_PILOT_REQUIRED',
    message: 'Use the campaign-bound Local Pilot request, approval, and prepare flow.',
    evidence_status: 'NOT_RUN',
  });
});

app.get('/api/system/readiness', async (req, res) => {
  try {
    const resp = await forwardWorkerRequest('/readiness');
    if (!resp.response.ok) throw new Error('Worker readiness not OK');
    const data = resp.data;
    res.json(data);
  } catch  {
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
    if (requestedConfig.executionMode === 'LIVE' && LOCAL_ONLY) {
      if (
        typeof requestedConfig.releaseApprovalId !== 'string'
        || !/^local-approval-[0-9a-f-]{36}$/i.test(requestedConfig.releaseApprovalId)
      ) {
        return res.status(400).json({
          error: 'LOCAL_RELEASE_APPROVAL_ID_REQUIRED',
          evidence_status: 'UNVERIFIED',
        });
      }
      let consumed;
      try {
        consumed = await getServerLocalReleaseStore().getConsumedApproval(
          requestedConfig.releaseApprovalId,
        );
      } catch {
        return res.status(503).json({ error: 'LOCAL_RELEASE_STORE_UNAVAILABLE', evidence_status: 'UNVERIFIED' });
      }
      if (!consumed) {
        return res.status(409).json({
          error: 'LOCAL_RELEASE_APPROVAL_NOT_CONSUMED',
          evidence_status: 'UNVERIFIED',
        });
      }
      const requestedInstruments = Array.isArray(requestedConfig.instruments)
        ? requestedConfig.instruments.map((symbol) => String(symbol).trim().toUpperCase())
        : [];
      if (
        consumed.runtimeTarget !== 'LOCAL'
        || consumed.runId !== LOCAL_RUN_ID
        || requestedConfig.launchPolicy !== 'STAGED_FIRST_ORDER'
        || requestedConfig.enforcePreflight !== true
        || requestedInstruments.length !== 1
        || requestedInstruments[0] !== 'ETHUSDC'
      ) {
        return res.status(409).json({ error: 'LOCAL_RELEASE_APPROVAL_SCOPE_MISMATCH', evidence_status: 'UNVERIFIED' });
      }
      try {
        const candidate = await getServerLocalReleaseStore().getCandidate(consumed.candidateId);
        if (!candidate || candidate.approvalId !== consumed.approvalId || candidate.status !== 'CONSUMED') {
          return res.status(409).json({ error: 'LOCAL_RELEASE_CANDIDATE_INVALID', evidence_status: 'UNVERIFIED' });
        }
        const current = currentLocalFingerprint(consumed.apiKeyVersion, consumed.apiSecretVersion);
        const promotion = localPromotionStatus(current);
        if (!promotion.passed) {
          return res.status(409).json({
            error: 'LOCAL_PROMOTION_GATE_NOT_PASSED',
            failures: promotion.failures,
            evidence_status: 'UNVERIFIED',
          });
        }
        assertExpectedLocalBinding(candidate, localBindingFromFingerprint(current, promotion.bundleSha256 || ''));
        const workerState = await readLocalWorkerState();
        const supervisor = localWorkerSupervisor?.status();
        if (
          workerState.execution_mode !== 'LIVE'
          || workerState.engine_state !== 'DISARMED'
          || workerState.mainnet_live_approved !== true
          || Number(workerState.order_submission_attempts || 0) !== 0
          || supervisor?.mode !== 'LIVE'
          || supervisor.approvalId !== consumed.approvalId
          || supervisor.sourceFingerprint !== consumed.sourceFingerprint
          || supervisor.secretVersions?.apiKey !== consumed.apiKeyVersion
          || supervisor.secretVersions?.apiSecret !== consumed.apiSecretVersion
        ) {
          return res.status(409).json({
            error: 'LOCAL_WORKER_NOT_LIVE_DISARMED',
            evidence_status: 'UNVERIFIED',
          });
        }
        const preflight = await forwardWorkerRequest('/preflight/read-only', { method: 'POST' });
        const preflightData = releaseRequestObject(preflight.data) || {};
        const checks = Array.isArray(preflightData.checks)
          ? preflightData.checks.filter((check) => releaseRequestObject(check)?.status !== 'PASS')
          : [];
        const noOrders = Number(preflightData.orderSubmissionAttempts ?? preflightData.order_submission_attempts) === 0
          && Number(preflightData.orderEndpointAttempts ?? preflightData.order_endpoint_attempts) === 0;
        if (
          !preflight.response.ok
          || preflightData.preflightPassed !== true
          || preflightData.canArm !== false
          || !noOrders
          || checks.length > 0
        ) {
          return res.status(409).json({
            error: 'LOCAL_MAINNET_PREFLIGHT_FAILED',
            failedCheckIds: checks.map((check) => String(releaseRequestObject(check)?.id || 'UNKNOWN')),
            orderSubmissionAttempts: Number(preflightData.orderSubmissionAttempts ?? preflightData.order_submission_attempts ?? -1),
            orderEndpointAttempts: Number(preflightData.orderEndpointAttempts ?? preflightData.order_endpoint_attempts ?? -1),
            evidence_status: 'UNVERIFIED',
          });
        }
        requestedConfig.releaseApprovalId = consumed.approvalId;
        requestedConfig.instruments = requestedInstruments;
      } catch {
        return res.status(503).json({ error: 'LOCAL_RELEASE_VERIFICATION_FAILED', evidence_status: 'UNVERIFIED' });
      }
    } else if (requestedConfig.executionMode === 'LIVE') {
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
  } catch  {
    res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  }
});

// Continuation is a distinct trading_admin action. The browser supplies only
// opaque identifiers and strategy configuration; the Control Plane resolves
// the server-side approval and forwards a Google-authenticated request to the
// Worker. No endpoint here can set MAINNET_LIVE_APPROVED itself.
app.post('/api/system/continue', async (req: Request, res: Response) => {
  if (LOCAL_ONLY) {
    return res.status(409).json({
      error: 'LOCAL_PILOT_CAMPAIGN_REQUIRED',
      message: 'Legacy autonomous continuation is disabled for Local runtime.',
      evidence_status: 'NOT_RUN',
    });
  }
  const body = releaseRequestObject(req.body) || {};
  if (hasCredentialLikeKey(body)) {
    return res.status(400).json({ error: 'CONTINUATION_PAYLOAD_CONTAINS_CREDENTIALS' });
  }
  const continuationApprovalId = typeof body.continuationApprovalId === 'string'
    ? body.continuationApprovalId.trim()
    : '';
  const launchId = typeof body.launchId === 'string' ? body.launchId.trim() : '';
  const localContinuationId = /^local-continuation-[0-9a-f-]{36}$/i.test(continuationApprovalId);
  const cloudContinuationId = /^continuation-[0-9a-f-]{36}$/i.test(continuationApprovalId);
  if ((LOCAL_ONLY && !localContinuationId) || (!LOCAL_ONLY && !cloudContinuationId)) {
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
    if (LOCAL_ONLY) {
      if (!localWorkerSupervisor) {
        return res.status(404).json({ error: 'LOCAL_RUNTIME_NOT_AVAILABLE' });
      }
      const uid = res.locals.firebaseUid;
      if (typeof uid !== 'string' || !uid) {
        return res.status(401).json({ error: 'FIREBASE_IDENTITY_MISSING' });
      }
      const store = getServerLocalContinuationStore();
      const approval = await store.getApproval(continuationApprovalId);
      if (!approval) return res.status(404).json({ error: 'LOCAL_CONTINUATION_NOT_FOUND' });
      if (approval.launchId !== launchId || approval.tradingAdminUid !== uid) {
        return res.status(409).json({ error: 'LOCAL_CONTINUATION_SCOPE_OR_APPROVER_MISMATCH' });
      }
      const candidate = await getServerLocalReleaseStore().getCandidate(approval.candidateId);
      if (!candidate
        || candidate.status !== 'CONSUMED'
        || candidate.approvalId !== approval.initialApprovalId) {
        return res.status(409).json({ error: 'LOCAL_INITIAL_RELEASE_APPROVAL_INVALID' });
      }
      const fingerprint = currentLocalFingerprint(candidate.apiKeyVersion, candidate.apiSecretVersion);
      const promotion = localPromotionStatus(fingerprint);
      if (!promotion.passed) {
        return res.status(409).json({
          error: 'LOCAL_PROMOTION_GATE_NOT_PASSED',
          failures: promotion.failures,
          evidence_status: 'UNVERIFIED',
        });
      }
      const releaseBinding = localBindingFromFingerprint(fingerprint, promotion.bundleSha256 || '');
      assertExpectedLocalBinding(candidate, releaseBinding);
      const supervisor = localWorkerSupervisor.status();
      if (supervisor.mode !== 'LIVE'
        || !supervisor.workerRunning
        || supervisor.runId !== approval.runId
        || supervisor.approvalId !== approval.initialApprovalId
        || supervisor.sourceFingerprint !== approval.sourceFingerprint
        || supervisor.secretVersions?.apiKey !== candidate.apiKeyVersion
        || supervisor.secretVersions?.apiSecret !== candidate.apiSecretVersion
        || !(await localPersistenceIsDurable())) {
        return res.status(409).json({
          error: 'LOCAL_CONTINUATION_RUNTIME_BINDING_FAILED',
          evidence_status: 'UNVERIFIED',
          executionActivated: false,
        });
      }

      const activeState = await readLocalWorkerState();
      const alreadyActive = approval.status === 'CONSUMED'
        && activeState.execution_mode === 'LIVE'
        && activeState.engine_state === 'ARMED'
        && activeState.mainnet_live_approved === true
        && activeState.mainnet_launch_id === launchId
        && activeState.mainnet_launch_state === 'AUTONOMOUS_ACTIVE'
        && activeState.mainnet_continuation_approval_id === approval.continuationId;
      if (alreadyActive) {
        projectWorkerState(activeState);
        return res.json({
          ...activeState,
          runtimeTarget: 'LOCAL',
          continuationId: approval.continuationId,
          launchId,
          status: 'AUTONOMOUS_ACTIVE',
          idempotent: true,
          evidence_status: 'VERIFIED',
        });
      }
      if (approval.status === 'CONSUMED') {
        return res.status(409).json({
          error: 'LOCAL_CONTINUATION_CONSUMED_REQUIRES_RECONCILIATION',
          message: 'This continuation was consumed; reconcile Worker state before creating a new approval. The same approval will not be replayed.',
          executionActivated: 'UNKNOWN',
          evidence_status: 'UNVERIFIED',
        });
      }
      if (approval.status !== 'APPROVED' || !approval.approvalId) {
        return res.status(409).json({ error: 'LOCAL_CONTINUATION_NOT_APPROVED', status: approval.status });
      }

      const context = await inspectLocalContinuationContext(approval.candidateId, approval.launchId);
      if (context.failures.length || context.evidenceHash !== approval.firstOrderEvidenceHash) {
        return res.status(409).json({
          error: 'LOCAL_CONTINUATION_EVIDENCE_CHANGED',
          failures: context.failures.length ? context.failures : ['First-order evidence no longer matches the approved continuation'],
          evidence_status: 'UNVERIFIED',
          executionActivated: false,
        });
      }
      const currentBinding = {
        ...localContinuationBinding(approval),
        runtimeTarget: 'LOCAL' as const,
        candidateId: context.candidate.candidateId,
        initialApprovalId: context.consumedApprovalId,
        runId: context.binding.runId,
        launchId,
        sourceFingerprint: context.binding.sourceFingerprint,
        dependencyFingerprint: context.binding.dependencyFingerprint,
        migrationFingerprint: context.binding.migrationFingerprint,
        policyVersion: context.binding.policyVersion,
        policyHash: context.binding.policyHash,
        firstOrderEvidenceHash: context.evidenceHash,
        tradingAdminUid: uid,
      };
      const consumed = await store.consumeContinuation(approval.continuationId, currentBinding);
      let forwarded: { response: globalThis.Response; data: any } | null = null;
      try {
        forwarded = await forwardWorkerRequest('/continue', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            executionMode: 'LIVE',
            instruments: ['ETHUSDC'],
            strategies,
            riskProfile,
            enforcePreflight: true,
            continuationApprovalId: consumed.continuationId,
            launchId: consumed.launchId,
            initialApprovalId: consumed.initialApprovalId,
          }),
        });
      } catch {
        // A consumed approval is never replayed after an uncertain Worker call.
      }
      const readback: Record<string, unknown> = await readLocalWorkerState().catch(() => ({}));
      const readbackSupervisor = localWorkerSupervisor.status();
      const activated = readback.execution_mode === 'LIVE'
        && readback.engine_state === 'ARMED'
        && readback.mainnet_live_approved === true
        && readback.mainnet_launch_id === launchId
        && readback.mainnet_launch_policy === 'AUTONOMOUS_AFTER_REVIEW'
        && readback.mainnet_launch_state === 'AUTONOMOUS_ACTIVE'
        && readback.mainnet_continuation_approval_id === consumed.continuationId
        && readbackSupervisor.mode === 'LIVE'
        && readbackSupervisor.workerRunning
        && readbackSupervisor.approvalId === consumed.initialApprovalId
        && readbackSupervisor.sourceFingerprint === consumed.sourceFingerprint;
      if (!activated) {
        const rollback = await rollbackAutonomousContinuation();
        return res.status(409).json({
          error: forwarded && !forwarded.response.ok
            ? 'LOCAL_WORKER_REJECTED_CONTINUATION'
            : 'LOCAL_CONTINUATION_READBACK_FAILED',
          rollbackVerified: rollback.verified,
          workerDisarmed: rollback.verified,
          executionActivated: !rollback.verified,
          evidence_status: 'UNVERIFIED',
        });
      }
      projectWorkerState(readback);
      await auditRepository.logEvent({
        eventType: 'AUTONOMOUS_CONTINUATION_ACTIVATED',
        previousState: 'PAUSED_NEW_RISK',
        newState: 'ARMED',
        executionMode: 'LIVE',
        reason: 'Local first-order evidence and independent trading_admin continuation approval passed',
        metadata: {
          runtimeTarget: 'LOCAL',
          launchId,
          continuationId: consumed.continuationId,
          initialApprovalId: consumed.initialApprovalId,
          sourceFingerprint: consumed.sourceFingerprint,
        },
      });
      return res.json({
        ...readback,
        runtimeTarget: 'LOCAL',
        continuationId: consumed.continuationId,
        launchId,
        status: 'AUTONOMOUS_ACTIVE',
        evidence_status: 'VERIFIED',
      });
    }
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
    } catch  {
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
  } catch  {
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

    await auditRepository.logEvent({
      eventType: active ? 'PAUSE_NEW_RISK_ENGAGED' : 'PAUSE_NEW_RISK_RELEASED',
      newState: tradingSystemState.engineState,
      executionMode: tradingSystemState.executionMode,
      reason: active ? 'Pause new risk requested and worker accepted' : 'Resume new risk requested and worker accepted'
    });

    res.json(forwarded.data);
  } catch  {
    res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  }
});

app.post('/api/system/recovery-only', async (req, res) => {
  let reservedPilotTransition = false;
  try {
    let recoveryBody = req.body;
    const supervisor = localWorkerSupervisor?.status();
    if (LOCAL_ONLY && supervisor?.workerRunning && supervisor.mode === 'LIVE'
      && (supervisor.pilotCampaignId || typeof req.body?.campaignId === 'string')) {
      const campaignId = typeof req.body?.campaignId === 'string' ? req.body.campaignId.trim() : '';
      if (!campaignId || !supervisor.pilotCampaignId || campaignId !== supervisor.pilotCampaignId) {
        return res.status(409).json({ error: 'LOCAL_PILOT_WORKER_CAMPAIGN_MISMATCH' });
      }
      await confirmRunningLocalPilotCampaign(campaignId);
      if (req.body?.active !== true) {
        // Release re-opens new-risk authority: bound admin + ACTIVE unexpired campaign only.
        if (!reserveLocalPilotTransition()) {
          return res.status(409).json({ error: 'LOCAL_PILOT_TRANSITION_IN_PROGRESS' });
        }
        reservedPilotTransition = true;
        try {
          assertLocalLivePilotRecoveryReleaseAllowed(
            await getServerLocalLivePilotStore().get(campaignId),
            typeof res.locals.firebaseUid === 'string' ? res.locals.firebaseUid : '',
          );
        } catch (releaseError) {
          const code = releaseError instanceof Error ? releaseError.message : '';
          if (code === 'LOCAL_PILOT_RELEASE_UID_MISMATCH') return res.status(403).json({ error: code });
          if (code === 'LOCAL_PILOT_NOT_FOUND') return res.status(404).json({ error: code });
          return res.status(409).json({ error: 'LOCAL_PILOT_NOT_ACTIVE' });
        }
      }
      recoveryBody = { active: req.body?.active };
    }
    const forwarded = await forwardWorkerRequest('/recovery-only', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(recoveryBody)
    });
    if (!forwarded.response.ok) {
      return res.status(forwarded.response.status).json({ error: 'WORKER_REJECTED_RECOVERY', detail: forwarded.data });
    }
    const active = requireWorkerBoolean(forwarded.data, 'active');
    if (active === null) return res.status(502).json({ error: 'INVALID_WORKER_RESPONSE', detail: forwarded.data });
    tradingSystemState.recoveryOnly = active;
    tradingSystemState.engineState = active ? 'RECOVERY_ONLY' : (tradingSystemState.activeConfiguration ? 'ARMED' : 'DISARMED');

    await auditRepository.logEvent({
      eventType: active ? 'RECOVERY_ONLY_ENGAGED' : 'RECOVERY_ONLY_RELEASED',
      newState: tradingSystemState.engineState,
      executionMode: tradingSystemState.executionMode,
      reason: active ? 'Recovery-only mode requested and worker accepted' : 'Recovery-only mode released and worker accepted'
    });

    res.json(forwarded.data);
  } catch  {
    res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  } finally {
    if (reservedPilotTransition) {
      localPilotTransitionBusy = false;
    }
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
    const previousState = tradingSystemState.engineState;
    tradingSystemState.killSwitchActive = isActive;
    if (isActive) tradingSystemState.engineState = 'EMERGENCY';
    else if (forwarded.data.status === 'CONFIRMED') tradingSystemState.engineState = 'DISARMED';

    await auditRepository.logEvent({
      eventType: isActive ? 'KILL_SWITCH_ENGAGED' : 'KILL_SWITCH_RELEASED',
      previousState,
      newState: tradingSystemState.engineState,
      executionMode: tradingSystemState.executionMode,
      reason: isActive ? 'Kill switch engaged via /api/system/kill-switch' : 'Kill switch release verified by worker'
    });

    res.json(forwarded.data);
  } catch  {
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
  } catch  {
    res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  }
});

// ---------------------------------------------------------------------------
// Wealth Growth & 8D Learning Engine Endpoints
// ---------------------------------------------------------------------------
app.get('/api/wealth/metrics', async (req: Request, res: Response) => {
  try {
    const forwarded = await forwardWorkerRequest('/wealth/metrics');
    if (!forwarded.response.ok) {
      return res.status(503).json({ error: 'WEALTH_METRICS_UNAVAILABLE', evidence_status: 'UNAVAILABLE' });
    }
    return res.json(forwarded.data);
  } catch {
    return res.status(503).json({ error: 'WEALTH_METRICS_UNAVAILABLE', evidence_status: 'UNAVAILABLE' });
  }
});

app.get('/api/incidents/8d', async (req: Request, res: Response) => {
  try {
    const activeOnly = req.query.active_only === 'true' ? '?active_only=true' : '';
    const forwarded = await forwardWorkerRequest(`/incidents/8d${activeOnly}`);
    if (!forwarded.response.ok) {
      return res.status(503).json({ error: 'INCIDENTS_UNAVAILABLE', evidence_status: 'UNAVAILABLE' });
    }
    return res.json(forwarded.data);
  } catch {
    return res.status(503).json({ error: 'INCIDENTS_UNAVAILABLE', evidence_status: 'UNAVAILABLE' });
  }
});

app.post('/api/incidents/8d/:incidentId/close', async (req: Request, res: Response) => {
  const incidentId = req.params.incidentId;
  if (!isEightDIncidentId(incidentId)) {
    return res.status(400).json({ error: 'INVALID_INCIDENT_ID' });
  }
  const signoffUserId = res.locals.firebaseUid;
  if (typeof signoffUserId !== 'string' || !signoffUserId.trim()) {
    return res.status(401).json({ error: 'CONTROL_PLANE_AUTH_REQUIRED' });
  }
  const evidence = req.body as { verification?: unknown; prevention?: unknown; lessons?: unknown } | undefined;
  const fields = [evidence?.verification, evidence?.prevention, evidence?.lessons];
  if (fields.some((value) => typeof value !== 'string' || !value.trim() || value.length > 4000)) {
    return res.status(400).json({ error: 'CLOSURE_EVIDENCE_REQUIRED' });
  }
  try {
    const forwarded = await forwardWorkerRequest(`/incidents/8d/${encodeURIComponent(incidentId)}/close`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        verification: evidence?.verification,
        prevention: evidence?.prevention,
        lessons: evidence?.lessons,
        signoff_user_id: signoffUserId,
      }),
    });
    if (!forwarded.response.ok) {
      return res.status(forwarded.response.status).json(forwarded.data);
    }
    await auditRepository.logEvent({
      eventType: 'EIGHT_D_INCIDENT_CLOSED',
      previousState: tradingSystemState.engineState,
      newState: tradingSystemState.engineState,
      executionMode: tradingSystemState.executionMode,
      reason: `8D Incident ${req.params.incidentId} closed: ${req.body?.lessons || 'No notes'}`,
    });
    res.json(forwarded.data);
  } catch {
    res.status(503).json({ error: 'WORKER_UNREACHABLE' });
  }
});

app.get('/api/learning/lineages', async (req: Request, res: Response) => {
  try {
    const forwarded = await forwardWorkerRequest('/learning/lineages');
    if (!forwarded.response.ok) {
      return res.status(503).json({ error: 'LINEAGES_UNAVAILABLE', evidence_status: 'UNAVAILABLE' });
    }
    return res.json(forwarded.data);
  } catch {
    return res.status(503).json({ error: 'LINEAGES_UNAVAILABLE', evidence_status: 'UNAVAILABLE' });
  }
});

app.get('/api/learning/pdca', async (req: Request, res: Response) => {
  try {
    const forwarded = await forwardWorkerRequest('/learning/pdca');
    if (!forwarded.response.ok) {
      return res.status(503).json({ error: 'PDCA_UNAVAILABLE', evidence_status: 'UNAVAILABLE' });
    }
    return res.json(forwarded.data);
  } catch {
    return res.status(503).json({ error: 'PDCA_UNAVAILABLE', evidence_status: 'UNAVAILABLE' });
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
    const previousState = tradingSystemState.engineState;
    tradingSystemState.killSwitchActive = actualActive;
    if (actualActive) tradingSystemState.engineState = 'EMERGENCY';
    else if (forwarded.data.status === 'CONFIRMED') tradingSystemState.engineState = 'DISARMED';
    tradingSystemState.updatedAt = new Date().toISOString();
    quantEngineState.account.kill_switch_active = actualActive;
    quantEngineState.account.risk_state = actualActive ? 'EMERGENCY' : 'NORMAL';

    await auditRepository.logEvent({
      eventType: actualActive ? 'KILL_SWITCH_ENGAGED' : 'KILL_SWITCH_RELEASED',
      previousState,
      newState: tradingSystemState.engineState,
      executionMode: tradingSystemState.executionMode,
      reason: actualActive ? 'Kill switch engaged via /api/quant/risk/kill-switch' : 'Kill switch release verified by worker'
    });

    return res.json({ ...forwarded.data, risk_state: quantEngineState.account.risk_state });
  } catch  {
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
  } catch  {
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
    const prompt = `You are the Principal Quant Research Advisor and Algorithmic Trading Architect for Blessing AI v0.2.
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
app.all('/api/{*splat}', (req: Request, res: Response) => {
  res.status(404).json({
    error: `API endpoint not found: ${req.method} ${req.originalUrl}`,
    status: 404,
  });
});

// Vite middleware in dev or static files in production
async function startServer() {
  if (LOCAL_ONLY) {
    if (RUNTIME_TARGET !== 'LOCAL' || !LOCAL_RUN_ID || !LOCAL_WORKER_IDENTITY_TOKEN || !localWorkerSupervisor) {
      throw new Error('LOCAL_RUNTIME_SUPERVISOR_CONFIGURATION_INVALID');
    }
    // Every Control Plane start creates a fresh Paper-only Worker. Approval
    // records and environment flags are never replayed across process restarts.
    await localWorkerSupervisor.startPaper();
  }

  if (process.env.NODE_ENV !== 'production') {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: 'spa',
    });
    app.use(vite.middlewares);
  } else {
    const distPath = path.join(process.cwd(), 'dist');
    app.use(express.static(distPath));
    app.get('/{*splat}', (req: Request, res: Response) => {
      res.sendFile(path.join(distPath, 'index.html'));
    });
  }

  const httpServer = app.listen(PORT, BIND_HOST, () => {
    console.log('Blessing AI v0.2 Server listening on http://' + BIND_HOST + ':' + PORT);

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

  if (localWorkerSupervisor) {
    let closing = false;
    const shutdownLocalRuntime = () => {
      if (closing) return;
      closing = true;
      void localWorkerSupervisor.close().finally(() => {
        httpServer.close(() => process.exit(0));
      });
    };
    process.once('SIGINT', shutdownLocalRuntime);
    process.once('SIGTERM', shutdownLocalRuntime);
  }
}

startServer().catch((err) => {
  console.error('Failed to start server:', err);
});
