/**
 * Blessing AI v0.2 — Google Cloud & Google Products Automated Configuration
 * All Google products are pre-wired for Google Cloud Project: gen-lang-client-0730128480
 * Primary User: kotorn@gmail.com
 * Region: asia-southeast1 (Singapore / Southeast Asia)
 */

export interface GoogleProductConfig {
  id: string;
  name: string;
  category: 'DATABASE' | 'STORAGE' | 'ANALYTICS' | 'SECURITY' | 'COMPUTE' | 'AI' | 'WORKSPACE';
  status: 'CONNECTED' | 'READY' | 'ACTIVE' | 'SYNCED' | 'CONFIGURATION_DECLARED_NOT_VERIFIED';
  projectId: string;
  resourceIdentifier: string;
  region: string;
  description: string;
  consoleUrl: string;
  connectionParams: Record<string, string | number | boolean | string[]>;
  lastVerified: string | null;
}

export const GCP_PROJECT_ID = 'gen-lang-client-0730128480';
export const GCP_REGION = 'asia-southeast1';
export const USER_EMAIL = 'kotorn@gmail.com';

export const GOOGLE_PRODUCTS: GoogleProductConfig[] = [
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
      datasetsCount: 4,
      requirePartitionFilter: true,
      maxScanBytesLimitGb: 10.0,
      monthlyFreeTierGb: 1000,
    },
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
      minInstances: 1, // Zero cold start for trading loop
      maxInstances: 1,
      executionMode: 'PAPER_BY_DEFAULT',
      liveApproval: 'EXPLICIT_RELEASE_ONLY',
    },
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
    lastVerified: null,
  },
  {
    id: 'google_workspace',
    name: 'Google Workspace (Drive & Sheets)',
    category: 'WORKSPACE',
    status: 'CONFIGURATION_DECLARED_NOT_VERIFIED',
    projectId: GCP_PROJECT_ID,
    resourceIdentifier: `${USER_EMAIL} / Blessing AI v0.2 Quant Lakehouse`,
    region: 'global',
    description: 'Direct spreadsheet exporter for real-time risk summaries, basket tracking, and Google Drive audits.',
    consoleUrl: `https://drive.google.com`,
    connectionParams: {
      authorizedAccount: USER_EMAIL,
      driveFolder: 'Blessing AI v0.2 Quant Lakehouse',
      sheetsSpreadsheetName: 'Blessing AI v0.2 - Live Baskets & Risk Telemetry',
      exportFormat: 'Google Sheets (Native)',
    },
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
    lastVerified: null,
  },
];

/**
 * Fetch Google Cloud Products Status & Auto-Wired Parameters
 */
export async function getGoogleProductsConfig() {
  const res = await fetch('/api/google/products');
  if (!res.ok) {
    throw new Error('Failed to fetch Google Cloud products configuration');
  }
  return await res.json();
}

/**
 * Test connectivity to any Google Product
 */
export async function testGoogleProductConnection(productId: string) {
  const res = await fetch('/api/google/test-connection', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ productId }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || 'Connection test failed');
  }
  return await res.json();
}
