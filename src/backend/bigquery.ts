import { BigQuery } from '@google-cloud/bigquery';
import { applicationDefault, getApps, initializeApp } from 'firebase-admin/app';
import { getAuth } from 'firebase-admin/auth';
import type { Request } from 'express';
import {
  controlPlaneRoles,
  hasControlPlaneRole,
  highestControlPlaneRole,
  type ControlPlaneRole,
} from './control-plane-auth.js';

export const BIGQUERY_PROJECT_ID = process.env.BIGQUERY_PROJECT_ID?.trim() || 'gen-lang-client-0730128480';
export const BIGQUERY_LOCATION = process.env.BIGQUERY_LOCATION?.trim() || 'US';
export const BIGQUERY_CONSOLE_URL = `https://console.cloud.google.com/bigquery?project=${encodeURIComponent(BIGQUERY_PROJECT_ID)}&ws=!1m0`;
export const MAX_SCAN_BYTES = 10 * 1024 * 1024 * 1024;
export const MAX_RESULT_ROWS = 500;

export const BIGQUERY_TABLES = {
  market_data: {
    description: 'Normalized historical and live market telemetry',
    tables: {
      ohlcv_bars: {
        description: 'Partitioned OHLCV and derived market metrics',
        partitionField: 'DATE(timestamp)',
        clusterFields: ['symbol', 'resolution'],
        require_partition_filter: true,
      },
    },
  },
  signals: {
    description: 'Strategy intents and decision telemetry',
    tables: {
      strategy_decisions: {
        description: 'Partitioned strategy decision and allocation telemetry',
        partitionField: 'DATE(timestamp)',
        clusterFields: ['strategy_id', 'symbol', 'regime'],
        require_partition_filter: true,
      },
    },
  },
  risk: {
    description: 'Risk governor and exposure telemetry',
    tables: {
      portfolio_snapshots: {
        description: 'Partitioned portfolio risk snapshots',
        partitionField: 'DATE(timestamp)',
        clusterFields: ['risk_state'],
        require_partition_filter: true,
      },
    },
  },
  backtests: {
    description: 'Backtest experiment evidence',
    tables: {
      experiment_runs: {
        description: 'Partitioned backtest experiment results',
        partitionField: 'DATE(created_at)',
        clusterFields: ['strategy_id', 'model_version'],
        require_partition_filter: true,
      },
    },
  },
} as const;

type DatasetId = keyof typeof BIGQUERY_TABLES;
type TableId = (typeof BIGQUERY_TABLES)[DatasetId]['tables'] extends infer T
  ? keyof T
  : never;

export class BigQueryServiceError extends Error {
  readonly code: string;
  readonly httpStatus: number;

  constructor(code: string, message: string, httpStatus = 503) {
    super(message);
    this.name = 'BigQueryServiceError';
    this.code = code;
    this.httpStatus = httpStatus;
  }
}

let client: BigQuery | null = null;

function getClient(): BigQuery {
  if (!client) {
    // The Google client uses Application Default Credentials. No user OAuth
    // token or service-account JSON is ever accepted by this module.
    client = new BigQuery({ projectId: BIGQUERY_PROJECT_ID });
  }
  return client;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(2)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(2)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function isTruthy(value: string | undefined): boolean {
  return ['1', 'true', 'yes', 'on'].includes((value || '').trim().toLowerCase());
}

export interface BigQueryAuthResult {
  ok: boolean;
  mode: 'FIREBASE_ID_TOKEN' | 'LOCAL_DEV_ONLY';
  uid?: string;
  claims?: Record<string, unknown>;
  roles?: ControlPlaneRole[];
  role?: ControlPlaneRole;
  forbidden?: boolean;
  error?: string;
}

/**
 * Verify a Firebase ID token. Google OAuth access tokens are deliberately not
 * accepted here: the BigQuery client uses server-side Application Default
 * Credentials, while the browser proves user identity with Firebase Auth.
 */
export async function authorizeFirebaseRequest(
  req: Request,
  options: {
    localBypassEnv?: string;
    requireOperator?: boolean;
    requiredRole?: ControlPlaneRole;
    requireTelemetryProducer?: boolean;
  } = {},
): Promise<BigQueryAuthResult> {
  const localBypassEnv = options.localBypassEnv || 'BIGQUERY_ALLOW_LOCAL_UNAUTHENTICATED';
  const runningOnCloudRun = Boolean(process.env.K_SERVICE);
  if (!runningOnCloudRun && process.env.NODE_ENV !== 'production' && isTruthy(process.env[localBypassEnv])) {
    return { ok: true, mode: 'LOCAL_DEV_ONLY' };
  }

  const authorization = req.header('authorization') || '';
  const match = /^Bearer\s+([^\s]+)$/i.exec(authorization);
  if (!match) {
    return { ok: false, mode: 'FIREBASE_ID_TOKEN', error: 'Firebase ID token is required' };
  }

  try {
    const app = getApps()[0] || initializeApp({
      credential: applicationDefault(),
      projectId: BIGQUERY_PROJECT_ID,
    });
    const decoded = await getAuth(app).verifyIdToken(match[1]);
    const claims: Record<string, unknown> = { ...decoded };
    const roles = controlPlaneRoles(claims);
    const requiredRole = options.requiredRole || (options.requireOperator ? 'operator' : 'viewer');
    if (!hasControlPlaneRole(roles, requiredRole)) {
      if (requiredRole) {
        return {
          ok: false,
          mode: 'FIREBASE_ID_TOKEN',
          uid: decoded.uid,
          claims,
          roles,
          role: highestControlPlaneRole(roles),
          forbidden: true,
          error: `${requiredRole} authorization is required`,
        };
      }
    }
    if (options.requireTelemetryProducer) {
      const isTelemetryProducer = claims.telemetryProducer === true || claims.bigqueryTelemetryProducer === true;
      if (!isTelemetryProducer) {
        return {
          ok: false,
          mode: 'FIREBASE_ID_TOKEN',
          uid: decoded.uid,
          claims,
          roles,
          role: highestControlPlaneRole(roles),
          forbidden: true,
          error: 'Telemetry producer authorization is required',
        };
      }
    }
    return {
      ok: true,
      mode: 'FIREBASE_ID_TOKEN',
      uid: decoded.uid,
      claims,
      roles,
      role: highestControlPlaneRole(roles),
    };
  } catch {
    // Never echo token or provider internals into an HTTP response.
    return { ok: false, mode: 'FIREBASE_ID_TOKEN', error: 'Firebase ID token could not be verified' };
  }
}

export async function authorizeBigQueryRequest(
  req: Request,
  options: { requireTelemetryProducer?: boolean } = {},
): Promise<BigQueryAuthResult> {
  return authorizeFirebaseRequest(req, {
    localBypassEnv: 'BIGQUERY_ALLOW_LOCAL_UNAUTHENTICATED',
    requiredRole: 'viewer',
    requireTelemetryProducer: options.requireTelemetryProducer,
  });
}

export async function authorizeOperatorRequest(
  req: Request,
  options: { requiredRole?: ControlPlaneRole } = {},
): Promise<BigQueryAuthResult> {
  return authorizeFirebaseRequest(req, {
    localBypassEnv: 'CONTROL_PLANE_ALLOW_UNAUTHENTICATED_LOCAL',
    requiredRole: options.requiredRole || 'operator',
  });
}

function queryText(query: unknown): string {
  if (typeof query !== 'string' || !query.trim()) {
    throw new BigQueryServiceError('INVALID_QUERY', 'SQL query string is required', 400);
  }
  const value = query.trim();
  if (value.length > 100_000) {
    throw new BigQueryServiceError('INVALID_QUERY', 'SQL query exceeds the 100 KB request limit', 400);
  }
  return value;
}

const PARTITION_FILTER_PATTERN = /\b(?:timestamp|created_at)\b\s*(?:>=|>|=|BETWEEN)\s*/i;

const TABLE_REFERENCE_PATTERN = /\b(?:FROM|JOIN)\s+`?([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){1,2})`?/gi;

function validateAllowlistedTableReferences(query: string): void {
  const references = [...query.matchAll(TABLE_REFERENCE_PATTERN)].map((match) => match[1]);
  for (const reference of references) {
    const parts = reference.split('.');
    const projectId = parts.length === 3 ? parts[0] : BIGQUERY_PROJECT_ID;
    const datasetId = parts.length === 3 ? parts[1] : parts[0];
    const tableId = parts.length === 3 ? parts[2] : parts[1];
    const allowlisted = Boolean(
      projectId === BIGQUERY_PROJECT_ID
      && datasetId in BIGQUERY_TABLES
      && tableId
      && Object.prototype.hasOwnProperty.call(
        BIGQUERY_TABLES[datasetId as DatasetId].tables,
        tableId,
      ),
    );
    if (!allowlisted) {
      throw new BigQueryServiceError(
        'TABLE_NOT_ALLOWLISTED',
        'The query references a table outside the configured BigQuery datasets',
        400,
      );
    }
  }
}

function validateReadOnlyQuery(query: string): void {
  // Do not allow scripting or a hidden second statement to bypass the
  // mutation keyword and table allowlists. The UI/backend boundary accepts
  // one explicit read statement only.
  if (query.includes(';') || !/^\s*(SELECT|WITH)\b/i.test(query)) {
    throw new BigQueryServiceError(
      'READ_ONLY_REQUIRED',
      'Only a single read-only SELECT or WITH query is allowed',
      400,
    );
  }
  if (/\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|EXPORT|LOAD|CALL|EXECUTE|DECLARE|SET)\b/i.test(query)) {
    throw new BigQueryServiceError('READ_ONLY_REQUIRED', 'Only read-only BigQuery queries are allowed', 400);
  }

  const partitionedReference = /(?:market_data\.ohlcv_bars|signals\.strategy_decisions|risk\.portfolio_snapshots|backtests\.experiment_runs)/i.test(query);
  if (partitionedReference && !PARTITION_FILTER_PATTERN.test(query)) {
    throw new BigQueryServiceError(
      'PARTITION_FILTER_REQUIRED',
      'A partition filter on timestamp or created_at is required for this dataset',
      400,
    );
  }
  validateAllowlistedTableReferences(query);
}

function asNumber(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

async function dryRunJob(query: string): Promise<{ bytes: number; statementType?: string }> {
  try {
    const [job] = await getClient().createQueryJob({
      query,
      location: BIGQUERY_LOCATION,
      dryRun: true,
      useLegacySql: false,
      maximumBytesBilled: String(MAX_SCAN_BYTES),
    });
    const [metadata] = await job.getMetadata();
    const statistics = (metadata as { statistics?: { totalBytesProcessed?: string; query?: { statementType?: string } } }).statistics;
    const bytes = asNumber(statistics?.totalBytesProcessed);
    if (bytes > MAX_SCAN_BYTES) {
      throw new BigQueryServiceError(
        'SCAN_CAP_EXCEEDED',
        `Query exceeds the 10 GB scan safety limit (${formatBytes(bytes)})`,
        400,
      );
    }
    return { bytes, statementType: statistics?.query?.statementType };
  } catch (error) {
    if (error instanceof BigQueryServiceError) throw error;
    throw new BigQueryServiceError('BIGQUERY_UNAVAILABLE', 'BigQuery dry run is unavailable');
  }
}

export async function dryRunQuery(rawQuery: unknown) {
  const query = queryText(rawQuery);
  validateReadOnlyQuery(query);
  const result = await dryRunJob(query);
  const estimatedCostUsd = Number(((result.bytes / (1024 ** 4)) * 6.25).toFixed(6));
  return {
    valid: true,
    totalBytesProcessed: result.bytes,
    totalBytesProcessedFormatted: formatBytes(result.bytes),
    estimatedCostUsd,
    withinFreeTier: true,
    exceedsSafetyCap: false,
    statementType: result.statementType,
    hasPartitionFilter: PARTITION_FILTER_PATTERN.test(query),
    data_source: 'BIGQUERY' as const,
    // A dry run validates syntax and estimated bytes only. It has not read
    // application data, so it must never be presented as live evidence.
    evidence_status: 'UNVERIFIED' as const,
    verified: false,
    verification_kind: 'DRY_RUN' as const,
    message: `BigQuery dry run passed. Estimated scan: ${formatBytes(result.bytes)}.`,
  };
}

export async function executeQuery(rawQuery: unknown) {
  const query = queryText(rawQuery);
  validateReadOnlyQuery(query);
  const dryRun = await dryRunJob(query);
  const startedAt = Date.now();
  try {
    const [job] = await getClient().createQueryJob({
      query,
      location: BIGQUERY_LOCATION,
      useLegacySql: false,
      maximumBytesBilled: String(MAX_SCAN_BYTES),
    });
    const [rows, nextQuery, apiResponse] = await job.getQueryResults({
      autoPaginate: false,
      maxResults: MAX_RESULT_ROWS,
    });
    const metadata = (apiResponse || {}) as {
      schema?: { fields?: Array<{ name?: string }> };
      statistics?: { totalBytesProcessed?: string; query?: { cacheHit?: boolean } };
      totalRows?: string | number;
      nextPageToken?: string;
    };
    const schemaFields = (metadata.schema?.fields || [])
      .map((field) => field.name)
      .filter((name): name is string => Boolean(name));
    const columns = schemaFields.length > 0 ? schemaFields : Object.keys(rows[0] || {});
    const statistics = metadata.statistics;
    const bytes = Math.max(dryRun.bytes, asNumber(statistics?.totalBytesProcessed));
    const nextPageToken = metadata.nextPageToken || (nextQuery as { pageToken?: string } | null)?.pageToken;
    const totalRows = Math.max(rows.length, asNumber(metadata.totalRows));
    return {
      columns,
      rows,
      totalRows,
      returnedRows: rows.length,
      hasMore: Boolean(nextPageToken) || totalRows > rows.length,
      nextPageToken,
      maxResultRows: MAX_RESULT_ROWS,
      bytesProcessed: bytes,
      bytesProcessedFormatted: formatBytes(bytes),
      executionTimeMs: Date.now() - startedAt,
      cacheHit: statistics?.query?.cacheHit === true,
      projectId: BIGQUERY_PROJECT_ID,
      data_source: 'BIGQUERY' as const,
      evidence_status: 'VERIFIED' as const,
      verified: true,
    };
  } catch (error) {
    if (error instanceof BigQueryServiceError) throw error;
    throw new BigQueryServiceError('BIGQUERY_UNAVAILABLE', 'BigQuery query execution is unavailable');
  }
}

export async function readBigQueryConfig() {
  const datasets = [];
  try {
    for (const [datasetId, definition] of Object.entries(BIGQUERY_TABLES) as Array<[DatasetId, typeof BIGQUERY_TABLES[DatasetId]]>) {
      const dataset = getClient().dataset(datasetId);
      const [metadata] = await dataset.getMetadata();
      if (metadata.location && metadata.location !== BIGQUERY_LOCATION) {
        throw new BigQueryServiceError('LOCATION_MISMATCH', `${datasetId} is not in ${BIGQUERY_LOCATION}`);
      }
      const tables = [];
      for (const [tableId, tableDefinition] of Object.entries(definition.tables) as Array<[string, { description: string; partitionField: string; clusterFields: string[]; require_partition_filter: boolean }]>) {
        const [tableMetadata] = await dataset.table(tableId).getMetadata();
        tables.push({
          tableId,
          description: tableDefinition.description,
          partitionField: tableDefinition.partitionField,
          clusterFields: tableDefinition.clusterFields,
          requirePartitionFilter: tableDefinition.require_partition_filter,
          rowCountEstimate: asNumber(tableMetadata.numRows),
          sizeMbEstimate: Number((asNumber(tableMetadata.numBytes) / (1024 ** 2)).toFixed(2)),
        });
      }
      datasets.push({ datasetId, description: definition.description, location: metadata.location || BIGQUERY_LOCATION, tables });
    }
  } catch (error) {
    if (error instanceof BigQueryServiceError) throw error;
    throw new BigQueryServiceError('BIGQUERY_UNAVAILABLE', 'BigQuery dataset or table read-back is unavailable');
  }

  return {
    projectId: BIGQUERY_PROJECT_ID,
    consoleUrl: BIGQUERY_CONSOLE_URL,
    location: BIGQUERY_LOCATION,
    datasets,
    verified: true,
    data_source: 'BIGQUERY' as const,
    evidence_status: 'VERIFIED' as const,
    status: 'VERIFIED' as const,
    costControls: {
      maxScanBytes: MAX_SCAN_BYTES,
      maxScanBytesFormatted: formatBytes(MAX_SCAN_BYTES),
      dryRunMandatory: true,
      freeTierMonthlyAllowanceGb: 1000,
    },
  };
}

const TELEMETRY_ID_FIELDS: Record<string, string> = {
  'market_data.ohlcv_bars': 'bar_id',
  'signals.strategy_decisions': 'decision_id',
  'risk.portfolio_snapshots': 'snapshot_id',
  'backtests.experiment_runs': 'experiment_id',
};

type TelemetryRow = Record<string, unknown>;

type TelemetryReadbackSpec = {
  identityField: string;
  partitionField: 'timestamp' | 'created_at';
  ids: string[];
  partitionStart: string;
  partitionEnd: string;
};

function isTelemetryRow(value: unknown): value is TelemetryRow {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function timestampMilliseconds(value: unknown): number | null {
  if (value instanceof Date) {
    const milliseconds = value.getTime();
    return Number.isFinite(milliseconds) ? milliseconds : null;
  }
  if (typeof value === 'number' && Number.isFinite(value)) {
    const milliseconds = Math.abs(value) < 1_000_000_000_000 ? value * 1000 : value;
    return Number.isFinite(milliseconds) ? milliseconds : null;
  }
  if (typeof value === 'string' && value.trim()) {
    const milliseconds = Date.parse(value);
    return Number.isFinite(milliseconds) ? milliseconds : null;
  }
  return null;
}

function telemetryReadbackSpec(
  rows: TelemetryRow[],
  datasetId: string,
  tableId: string,
): TelemetryReadbackSpec {
  const identityField = TELEMETRY_ID_FIELDS[`${datasetId}.${tableId}`];
  if (!identityField) {
    throw new BigQueryServiceError(
      'TELEMETRY_ID_REQUIRED',
      'This telemetry table has no allowlisted stable identity for read-back verification',
      400,
    );
  }
  const partitionField: 'timestamp' | 'created_at' = datasetId === 'backtests' ? 'created_at' : 'timestamp';
  const ids = [...new Set(rows.map((row) => String(row[identityField] ?? '').trim()).filter(Boolean))];
  if (ids.length !== rows.length) {
    throw new BigQueryServiceError(
      'TELEMETRY_ID_REQUIRED',
      `Every telemetry row must include a unique ${identityField} for read-back verification`,
      400,
    );
  }
  const partitionTimes = rows.map((row) => timestampMilliseconds(row[partitionField]));
  if (partitionTimes.some((value) => value === null)) {
    throw new BigQueryServiceError(
      'TELEMETRY_TIMESTAMP_REQUIRED',
      `Every telemetry row must include a valid ${partitionField} for partitioned read-back verification`,
      400,
    );
  }
  const startMilliseconds = Math.min(...(partitionTimes as number[])) - 60_000;
  const endMilliseconds = Math.max(...(partitionTimes as number[])) + 60_000;
  return {
    identityField,
    partitionField,
    ids,
    partitionStart: new Date(startMilliseconds).toISOString(),
    partitionEnd: new Date(endMilliseconds).toISOString(),
  };
}

async function verifyTelemetryReadback(
  spec: TelemetryReadbackSpec,
  datasetId: string,
  tableId: string,
): Promise<void> {
  try {
    const [rows] = await getClient().query({
      query: `SELECT COUNT(DISTINCT ${spec.identityField}) AS matched_rows
              FROM \`${BIGQUERY_PROJECT_ID}.${datasetId}.${tableId}\`
              WHERE ${spec.identityField} IN UNNEST(@ids)
                AND ${spec.partitionField} >= TIMESTAMP(@partitionStart)
                AND ${spec.partitionField} < TIMESTAMP(@partitionEnd)`,
      params: {
        ids: spec.ids,
        partitionStart: spec.partitionStart,
        partitionEnd: spec.partitionEnd,
      },
      location: BIGQUERY_LOCATION,
      useLegacySql: false,
      maximumBytesBilled: String(MAX_SCAN_BYTES),
    });
    const matched = asNumber(rows?.[0]?.matched_rows);
    if (matched < spec.ids.length) {
      throw new BigQueryServiceError(
        'READBACK_UNVERIFIED',
        'Telemetry insert completed without a matching BigQuery read-back',
        503,
      );
    }
  } catch (error) {
    if (error instanceof BigQueryServiceError) throw error;
    throw new BigQueryServiceError('READBACK_UNVERIFIED', 'BigQuery telemetry read-back is unavailable', 503);
  }
}

export async function syncTelemetry(rawRows: unknown, datasetId: string = 'signals', tableId: string = 'strategy_decisions') {
  if (!Array.isArray(rawRows)) {
    throw new BigQueryServiceError('INVALID_TELEMETRY', 'Telemetry rows must be an array', 400);
  }
  if (!(datasetId in BIGQUERY_TABLES) || !Object.prototype.hasOwnProperty.call(BIGQUERY_TABLES[datasetId as DatasetId].tables, tableId)) {
    throw new BigQueryServiceError('INVALID_TELEMETRY_TARGET', 'Telemetry target is not allowlisted', 400);
  }
  if (rawRows.length === 0) {
    return {
      success: true,
      status: 'NOOP',
      flushedRows: 0,
      targetLakehouse: `${BIGQUERY_PROJECT_ID}.${datasetId}.${tableId}`,
      data_source: 'BIGQUERY' as const,
      verified: false,
      evidence_status: 'UNVERIFIED' as const,
    };
  }
  if (rawRows.length > 2_000 || rawRows.some((row) => !isTelemetryRow(row))) {
    throw new BigQueryServiceError('INVALID_TELEMETRY', 'Telemetry batch is invalid or exceeds 2,000 rows', 400);
  }

  try {
    const rows = rawRows.filter(isTelemetryRow);
    const readbackSpec = telemetryReadbackSpec(rows, datasetId, tableId);
    // Use the stable producer identity as BigQuery's streaming insert id. A
    // retry after a successful insert/read-back timeout must not intentionally
    // create another copy of the same telemetry row.
    const insertRows = rows.map((row) => ({
      insertId: String(row[readbackSpec.identityField]).trim(),
      json: row,
    }));
    await getClient().dataset(datasetId).table(tableId).insert(insertRows, { raw: true });
    await verifyTelemetryReadback(readbackSpec, datasetId, tableId);
    return {
      success: true,
      status: 'VERIFIED',
      flushedRows: rawRows.length,
      targetLakehouse: `${BIGQUERY_PROJECT_ID}.${datasetId}.${tableId}`,
      data_source: 'BIGQUERY' as const,
      verified: true,
      evidence_status: 'VERIFIED' as const,
      lastFlushTime: new Date().toISOString(),
    };
  } catch (error) {
    if (error instanceof BigQueryServiceError) throw error;
    throw new BigQueryServiceError('BIGQUERY_UNAVAILABLE', 'BigQuery telemetry flush is unavailable');
  }
}

export function bigQueryErrorResponse(error: unknown): { status: number; body: Record<string, unknown> } {
  if (error instanceof BigQueryServiceError) {
    return {
      status: error.httpStatus,
      body: {
        error: error.code,
        message: error.message,
        status: 'DEGRADED',
        data_source: 'BIGQUERY',
        verified: false,
        evidence_status: 'UNVERIFIED',
      },
    };
  }
  return {
    status: 503,
    body: {
      error: 'BIGQUERY_UNAVAILABLE',
      message: 'BigQuery is unavailable',
      status: 'DEGRADED',
      data_source: 'BIGQUERY',
      verified: false,
      evidence_status: 'UNVERIFIED',
    },
  };
}

export function tableIdIsAllowlisted(value: string): value is TableId {
  return Object.values(BIGQUERY_TABLES).some((definition) => Object.prototype.hasOwnProperty.call(definition.tables, value));
}
