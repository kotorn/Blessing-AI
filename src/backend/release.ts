import crypto from 'node:crypto';

export const RELEASE_POLICY = 'STAGED_FIRST_ORDER' as const;
export const RELEASE_SYMBOL = 'ETHUSDC' as const;
export const RELEASE_EXECUTION_MODE = 'LIVE' as const;

export interface SecretVersionSet {
  sql: string;
  apiKey: string;
  apiSecret: string;
}

export interface ReleaseCandidate {
  candidateId: string;
  repoSha: string;
  imageDigest: string;
  workerRevision: string;
  secretVersions: SecretVersionSet;
  executionMode: typeof RELEASE_EXECUTION_MODE;
  symbol: typeof RELEASE_SYMBOL;
  preflightEvidenceHash: string;
  repoGateEvidenceHash: string;
  cloudGateEvidenceHash: string;
  launchPolicy: typeof RELEASE_POLICY;
  expiresAt: string;
  nonce: string;
  viteDataConnectCutover: false;
  orderSubmissionAttempts: 0;
  workerDisarmed: true;
  createdAt: string;
  status: 'PENDING_APPROVAL' | 'APPROVED' | 'CONSUMED' | 'EXPIRED';
  approvalId?: string;
  approvedByUid?: string;
  approvedAt?: string;
  consumedAt?: string;
}

export interface ReleaseCandidateInput {
  repoSha: string;
  imageDigest: string;
  workerRevision: string;
  secretVersions: SecretVersionSet;
  preflightEvidenceHash: string;
  repoGateEvidenceHash: string;
  cloudGateEvidenceHash: string;
  expiresAt: string;
  nonce: string;
}

export interface ReleaseVerificationSnapshot {
  currentImageDigest: string;
  currentWorkerRevision: string;
  currentExecutionMode: string;
  currentMainnetLiveApproved: boolean;
  currentEngineState: string;
  currentOrderSubmissionAttempts: number;
  preflightPassed: boolean;
  preflightObservedAt: string;
  reconciliationStatus: string;
  persistenceDurable: boolean;
  dataConnectCutover: boolean;
  killSwitchActive: boolean;
}

const SHA256_RE = /^[0-9a-f]{64}$/i;
const IMAGE_DIGEST_RE = /^.+@sha256:[0-9a-f]{64}$/i;
const REVISION_RE = /^[a-z0-9][a-z0-9-]{0,62}$/i;
const NONCE_RE = /^[A-Za-z0-9_-]{16,128}$/;

function asString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function isNumericSecretVersion(value: unknown): value is string {
  return typeof value === 'string' && /^[1-9][0-9]*$/.test(value);
}

export function validateSecretVersions(value: unknown): string[] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return ['secretVersions must be an object'];
  }
  const candidate = value as Record<string, unknown>;
  const expected = ['sql', 'apiKey', 'apiSecret'];
  const keys = Object.keys(candidate).sort();
  if (keys.join(',') !== expected.slice().sort().join(',')) {
    return ['secretVersions must contain only sql, apiKey, and apiSecret'];
  }
  return expected.flatMap((key) =>
    isNumericSecretVersion(candidate[key]) ? [] : [`secretVersions.${key} must be numeric`],
  );
}

/** Return deterministic JSON suitable for hashing evidence, never credentials. */
export function stableJson(value: unknown): string {
  if (value === undefined) return 'null';
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`)
    .join(',')}}`;
}

export function hashEvidence(value: unknown): string {
  return crypto.createHash('sha256').update(stableJson(value), 'utf8').digest('hex');
}

export function newReleaseCandidate(input: ReleaseCandidateInput, now = new Date()): ReleaseCandidate {
  const candidate: ReleaseCandidate = {
    candidateId: `rc-${crypto.randomUUID()}`,
    repoSha: asString(input.repoSha),
    imageDigest: asString(input.imageDigest),
    workerRevision: asString(input.workerRevision),
    secretVersions: {
      sql: asString(input.secretVersions?.sql),
      apiKey: asString(input.secretVersions?.apiKey),
      apiSecret: asString(input.secretVersions?.apiSecret),
    },
    executionMode: RELEASE_EXECUTION_MODE,
    symbol: RELEASE_SYMBOL,
    preflightEvidenceHash: asString(input.preflightEvidenceHash),
    repoGateEvidenceHash: asString(input.repoGateEvidenceHash),
    cloudGateEvidenceHash: asString(input.cloudGateEvidenceHash),
    launchPolicy: RELEASE_POLICY,
    expiresAt: asString(input.expiresAt),
    nonce: asString(input.nonce),
    viteDataConnectCutover: false,
    orderSubmissionAttempts: 0,
    workerDisarmed: true,
    createdAt: now.toISOString(),
    status: 'PENDING_APPROVAL',
  };
  const failures = validateReleaseCandidate(candidate, now);
  if (failures.length) throw new Error(failures.join('; '));
  return candidate;
}

export function validateReleaseCandidate(
  candidate: Partial<ReleaseCandidate> | undefined,
  now = new Date(),
): string[] {
  if (!candidate) return ['release candidate is missing'];
  const failures: string[] = [];
  if (!/^rc-[0-9a-f-]{36}$/i.test(asString(candidate.candidateId))) {
    failures.push('candidateId is invalid');
  }
  if (!/^[0-9a-f]{40}$/i.test(asString(candidate.repoSha)) && !SHA256_RE.test(asString(candidate.repoSha))) {
    failures.push('repoSha must be a commit or SHA-256 hex');
  }
  if (!IMAGE_DIGEST_RE.test(asString(candidate.imageDigest))) failures.push('imageDigest must be immutable');
  if (!REVISION_RE.test(asString(candidate.workerRevision))) failures.push('workerRevision is invalid');
  failures.push(...validateSecretVersions(candidate.secretVersions));
  if (candidate.executionMode !== RELEASE_EXECUTION_MODE) failures.push('executionMode must be LIVE');
  if (candidate.symbol !== RELEASE_SYMBOL) failures.push('symbol must be ETHUSDC');
  for (const [name, value] of [
    ['preflightEvidenceHash', candidate.preflightEvidenceHash],
    ['repoGateEvidenceHash', candidate.repoGateEvidenceHash],
    ['cloudGateEvidenceHash', candidate.cloudGateEvidenceHash],
  ] as const) {
    if (!SHA256_RE.test(asString(value))) failures.push(`${name} must be a SHA-256 hash`);
  }
  if (candidate.launchPolicy !== RELEASE_POLICY) failures.push('launchPolicy must be STAGED_FIRST_ORDER');
  const expiresAt = Date.parse(asString(candidate.expiresAt));
  const nowMs = now.getTime();
  if (!Number.isFinite(expiresAt) || expiresAt <= nowMs) failures.push('release candidate is expired or has an invalid expiry');
  if (Number.isFinite(expiresAt) && expiresAt > nowMs + 24 * 60 * 60 * 1000) {
    failures.push('release candidate expiry cannot exceed 24 hours');
  }
  if (!NONCE_RE.test(asString(candidate.nonce))) failures.push('nonce is invalid');
  if (candidate.viteDataConnectCutover !== false) failures.push('VITE_DATA_CONNECT_CUTOVER must remain false');
  if (candidate.orderSubmissionAttempts !== 0) failures.push('candidate already has order submissions');
  if (candidate.workerDisarmed !== true) failures.push('worker must be disarmed');
  if (!asString(candidate.createdAt) || !Number.isFinite(Date.parse(asString(candidate.createdAt)))) {
    failures.push('createdAt is invalid');
  }
  return failures;
}

function recentEnough(timestamp: string, now: Date, maxAgeSeconds: number): boolean {
  const time = Date.parse(timestamp);
  if (!Number.isFinite(time)) return false;
  const age = (now.getTime() - time) / 1000;
  return age >= 0 && age <= maxAgeSeconds;
}

export function validateApprovalPrerequisites(
  candidate: ReleaseCandidate,
  snapshot: ReleaseVerificationSnapshot,
  now = new Date(),
  maxPreflightAgeSeconds = 60,
): string[] {
  const failures = validateReleaseCandidate(candidate, now);
  if (candidate.status !== 'PENDING_APPROVAL') failures.push(`candidate status is ${candidate.status}`);
  if (snapshot.currentImageDigest !== candidate.imageDigest) failures.push('worker image digest does not match candidate');
  if (snapshot.currentWorkerRevision !== candidate.workerRevision) failures.push('worker revision does not match candidate');
  if (snapshot.currentExecutionMode !== RELEASE_EXECUTION_MODE) failures.push('worker execution mode is not LIVE');
  if (snapshot.currentMainnetLiveApproved) failures.push('worker is already Mainnet-approved; approval cannot be replayed');
  if (snapshot.currentEngineState !== 'DISARMED') failures.push('worker must remain DISARMED before approval');
  if (snapshot.currentOrderSubmissionAttempts !== 0) failures.push('worker has already attempted an order');
  if (!snapshot.preflightPassed || !recentEnough(snapshot.preflightObservedAt, now, maxPreflightAgeSeconds)) {
    failures.push('Mainnet preflight evidence is missing, failed, or stale');
  }
  if (snapshot.reconciliationStatus !== 'IN_SYNC') failures.push('reconciliation is not IN_SYNC');
  if (!snapshot.persistenceDurable) failures.push('required persistence is not durable');
  if (snapshot.dataConnectCutover) failures.push('Data Connect cutover must remain disabled');
  if (snapshot.killSwitchActive) failures.push('kill switch state requires explicit explanation');
  return failures;
}

function redactEvidenceText(value: unknown): string {
  const text = asString(value);
  if (!text) return '';
  return text
    .replace(/(authorization|api[-_ ]?key|api[-_ ]?secret|password|token|dsn)\s*[:=]\s*[^,;\s]+/gi, '$1=<redacted>')
    .slice(0, 500);
}

export interface SanitizedPreflightEvidence {
  executionMode: 'LIVE';
  preflightOnly: true;
  preflightPassed: boolean;
  orderSubmissionAttempts: number;
  orderEndpointAttempts: number;
  observedAt: string;
  checks: Array<{ id: string; name: string; required: boolean; status: 'PASS' | 'FAIL'; message: string }>;
}

/** Keep only the non-sensitive evidence needed by a release gate. */
export function sanitizePreflightEvidence(value: unknown): SanitizedPreflightEvidence {
  const raw = value && typeof value === 'object' ? value as Record<string, unknown> : {};
  const checks = Array.isArray(raw.checks)
    ? raw.checks.flatMap((item) => {
        if (!item || typeof item !== 'object') return [];
        const check = item as Record<string, unknown>;
        const status: 'PASS' | 'FAIL' | null = check.status === 'PASS'
          ? 'PASS'
          : check.status === 'FAIL'
            ? 'FAIL'
            : null;
        if (!status || !asString(check.id) || !asString(check.name)) return [];
        return [{
          id: asString(check.id).slice(0, 80),
          name: redactEvidenceText(check.name),
          required: check.required !== false,
          status,
          message: redactEvidenceText(check.message),
        }];
      })
    : [];
  const attempts = Number(raw.orderSubmissionAttempts ?? raw.order_submission_attempts);
  const endpointAttempts = Number(raw.orderEndpointAttempts ?? raw.order_endpoint_attempts);
  return {
    executionMode: 'LIVE',
    preflightOnly: true,
    preflightPassed: raw.preflightPassed === true || raw.preflight_passed === true,
    orderSubmissionAttempts: Number.isInteger(attempts) && attempts >= 0 ? attempts : -1,
    orderEndpointAttempts: Number.isInteger(endpointAttempts) && endpointAttempts >= 0 ? endpointAttempts : -1,
    observedAt: asString(raw.observedAt || raw.observed_at),
    checks,
  };
}
