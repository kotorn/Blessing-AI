import crypto from 'node:crypto';

export const RELEASE_POLICY = 'STAGED_FIRST_ORDER' as const;
export const AUTONOMOUS_CONTINUATION_POLICY = 'AUTONOMOUS_AFTER_REVIEW' as const;
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
  currentSecretVersions: SecretVersionSet;
  preflightPassed: boolean;
  preflightObservedAt: string;
  reconciliationStatus: string;
  persistenceDurable: boolean;
  dataConnectCutover: boolean;
  killSwitchActive: boolean;
}

export type ContinuationApprovalStatus = 'PENDING' | 'ACTIVATING' | 'CONSUMED' | 'EXPIRED';

export interface ContinuationApproval {
  continuationId: string;
  candidateId: string;
  launchId: string;
  initialApprovalId: string;
  imageDigest: string;
  workerRevision: string;
  secretVersions: SecretVersionSet;
  firstOrderEvidenceHash: string;
  reconciliationStatus: 'IN_SYNC';
  preflightObservedAt: string;
  nonce: string;
  requesterUid: string;
  createdAt: string;
  expiresAt: string;
  status: ContinuationApprovalStatus;
  consumedAt?: string;
}

export interface ContinuationApprovalInput {
  candidateId: string;
  launchId: string;
  initialApprovalId: string;
  imageDigest: string;
  workerRevision: string;
  secretVersions: SecretVersionSet;
  firstOrderEvidenceHash: string;
  preflightObservedAt: string;
  nonce: string;
  requesterUid: string;
  expiresAt: string;
}

export interface ContinuationVerificationSnapshot {
  currentImageDigest: string;
  currentWorkerRevision: string;
  currentExecutionMode: string;
  currentMainnetLiveApproved: boolean;
  currentEngineState: string;
  currentLaunchId: string;
  currentLaunchPolicy: string;
  currentLaunchState: string;
  currentContinuationApprovalId?: string;
  currentSubmittedOrders: number;
  currentSecretVersions: SecretVersionSet;
  preflightPassed: boolean;
  preflightObservedAt: string;
  preflightOrderEndpointAttempts: number;
  preflightOrderSubmissionAttempts: number;
  reconciliationStatus: string;
  persistenceDurable: boolean;
  dataConnectCutover: boolean;
  killSwitchActive: boolean;
}

export interface PreflightCheckLike {
  id: string;
  status: 'PASS' | 'FAIL';
}

/**
 * Resolve the reconciliation status for a continuation snapshot from a
 * fresh preflight's dedicated check plus a fallback cached worker field.
 *
 * A fresh, explicit FAIL must never be masked by falling back to the
 * separately-cached (and possibly stale) worker field -- only the absence
 * of the dedicated check should defer to that cache.
 */
export function resolveReconciliationStatus(
  checks: readonly PreflightCheckLike[],
  cachedReconciliationStatus: unknown,
): string {
  const check = checks.find((item) => item.id === 'CHK-PREFLIGHT-RECONCILIATION');
  if (check?.status === 'PASS') return 'IN_SYNC';
  if (check?.status === 'FAIL') return 'DRIFT_DETECTED';
  return String(cachedReconciliationStatus || '');
}

const SHA256_RE = /^[0-9a-f]{64}$/i;
const IMAGE_DIGEST_RE = /^.+@sha256:[0-9a-f]{64}$/i;
const REVISION_RE = /^[a-z0-9][a-z0-9-]{0,62}$/i;
const NONCE_RE = /^[A-Za-z0-9_-]{16,128}$/;
const CONTINUATION_ID_RE = /^continuation-[0-9a-f-]{36}$/i;
const LAUNCH_ID_RE = /^launch-[A-Za-z0-9-]{8,127}$/;
const UID_RE = /^[A-Za-z0-9:_-]{1,256}$/;

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
  if (candidate.status !== 'PENDING_APPROVAL' && candidate.status !== 'APPROVED' && candidate.status !== 'CONSUMED') {
    failures.push(`candidate status is ${candidate.status}`);
  }
  if (snapshot.currentImageDigest !== candidate.imageDigest) failures.push('worker image digest does not match candidate');
  if (snapshot.currentWorkerRevision !== candidate.workerRevision) failures.push('worker revision does not match candidate');
  if (snapshot.currentExecutionMode !== RELEASE_EXECUTION_MODE) failures.push('worker execution mode is not LIVE');
  if (snapshot.currentMainnetLiveApproved) failures.push('worker is already Mainnet-approved; approval cannot be replayed');
  if (snapshot.currentEngineState !== 'DISARMED') failures.push('worker must remain DISARMED before approval');
  if (snapshot.currentOrderSubmissionAttempts !== 0) failures.push('worker has already attempted an order');
  failures.push(...validateSecretVersionMatch(candidate.secretVersions, snapshot.currentSecretVersions));
  if (!snapshot.preflightPassed || !recentEnough(snapshot.preflightObservedAt, now, maxPreflightAgeSeconds)) {
    failures.push('Mainnet preflight evidence is missing, failed, or stale');
  }
  if (snapshot.reconciliationStatus !== 'IN_SYNC') failures.push('reconciliation is not IN_SYNC');
  if (!snapshot.persistenceDurable) failures.push('required persistence is not durable');
  if (snapshot.dataConnectCutover) failures.push('Data Connect cutover must remain disabled');
  if (snapshot.killSwitchActive) failures.push('kill switch state requires explicit explanation');
  return failures;
}

function validateSecretVersionMatch(
  expected: SecretVersionSet,
  actual: SecretVersionSet,
): string[] {
  const failures = [
    ...validateSecretVersions(actual),
  ];
  if (stableJson(expected) !== stableJson(actual)) {
    failures.push('Worker Secret Manager versions do not match the approved release');
  }
  return failures;
}

export function validateContinuationApproval(
  approval: Partial<ContinuationApproval> | undefined,
  now = new Date(),
): string[] {
  if (!approval) return ['continuation approval is missing'];
  const failures: string[] = [];
  if (!CONTINUATION_ID_RE.test(asString(approval.continuationId))) failures.push('continuationId is invalid');
  if (!/^rc-[0-9a-f-]{36}$/i.test(asString(approval.candidateId))) failures.push('candidateId is invalid');
  if (!LAUNCH_ID_RE.test(asString(approval.launchId))) failures.push('launchId is invalid');
  if (!/^approval-[0-9a-f-]{36}$/i.test(asString(approval.initialApprovalId))) failures.push('initialApprovalId is invalid');
  if (!IMAGE_DIGEST_RE.test(asString(approval.imageDigest))) failures.push('imageDigest must be immutable');
  if (!REVISION_RE.test(asString(approval.workerRevision))) failures.push('workerRevision is invalid');
  failures.push(...validateSecretVersions(approval.secretVersions));
  if (!SHA256_RE.test(asString(approval.firstOrderEvidenceHash))) failures.push('firstOrderEvidenceHash must be a SHA-256 hash');
  if (approval.reconciliationStatus !== 'IN_SYNC') failures.push('reconciliationStatus must be IN_SYNC');
  const preflightObservedAt = Date.parse(asString(approval.preflightObservedAt));
  if (!Number.isFinite(preflightObservedAt) || preflightObservedAt > now.getTime()) failures.push('preflight evidence is missing or from the future');
  if (!NONCE_RE.test(asString(approval.nonce))) failures.push('nonce is invalid');
  if (!UID_RE.test(asString(approval.requesterUid))) failures.push('requesterUid is invalid');
  const createdAt = Date.parse(asString(approval.createdAt));
  const expiresAt = Date.parse(asString(approval.expiresAt));
  if (!Number.isFinite(createdAt)) failures.push('createdAt is invalid');
  if (!Number.isFinite(expiresAt) || expiresAt <= now.getTime()) failures.push('continuation approval is expired or has an invalid expiry');
  if (Number.isFinite(expiresAt) && expiresAt > now.getTime() + 24 * 60 * 60 * 1000) failures.push('continuation approval expiry cannot exceed 24 hours');
  if (!['PENDING', 'ACTIVATING', 'CONSUMED', 'EXPIRED'].includes(String(approval.status))) failures.push('continuation approval status is invalid');
  return failures;
}

export function validateContinuationPrerequisites(
  approval: ContinuationApproval,
  snapshot: ContinuationVerificationSnapshot,
  now = new Date(),
  maxPreflightAgeSeconds = 60,
): string[] {
  const failures = validateContinuationApproval(approval, now);
  if (approval.status !== 'PENDING' && approval.status !== 'ACTIVATING') failures.push(`continuation approval status is ${approval.status}`);
  if (snapshot.currentImageDigest !== approval.imageDigest) failures.push('worker image digest does not match continuation approval');
  if (snapshot.currentWorkerRevision !== approval.workerRevision) failures.push('worker revision does not match continuation approval');
  if (snapshot.currentExecutionMode !== RELEASE_EXECUTION_MODE) failures.push('worker execution mode is not LIVE');
  if (!snapshot.currentMainnetLiveApproved) failures.push('worker is not Mainnet-approved');
  if (!['PAUSED_NEW_RISK', 'DISARMED'].includes(snapshot.currentEngineState)) failures.push('worker must be paused or disarmed before continuation');
  if (snapshot.currentLaunchId !== approval.launchId) failures.push('launch id does not match continuation approval');
  if (![RELEASE_POLICY, AUTONOMOUS_CONTINUATION_POLICY].includes(snapshot.currentLaunchPolicy as typeof RELEASE_POLICY | typeof AUTONOMOUS_CONTINUATION_POLICY)) failures.push('launch policy is not a supported continuation policy');
  if (!['PAUSED_NEW_RISK', 'REAUTH_REQUIRED'].includes(snapshot.currentLaunchState)) failures.push('durable launch session is not awaiting continuation');
  if (snapshot.currentSubmittedOrders < 1) failures.push('durable first-order evidence is missing');
  failures.push(...validateSecretVersionMatch(approval.secretVersions, snapshot.currentSecretVersions));
  if (!snapshot.preflightPassed || !recentEnough(snapshot.preflightObservedAt, now, maxPreflightAgeSeconds)) failures.push('continuation preflight evidence is missing, failed, or stale');
  if (snapshot.preflightOrderEndpointAttempts !== 0) failures.push('continuation preflight called an order endpoint');
  if (snapshot.preflightOrderSubmissionAttempts !== 0) failures.push('continuation preflight attempted an order');
  if (snapshot.reconciliationStatus !== 'IN_SYNC') failures.push('reconciliation is not IN_SYNC');
  if (!snapshot.persistenceDurable) failures.push('required persistence is not durable');
  if (snapshot.dataConnectCutover) failures.push('Data Connect cutover must remain disabled');
  if (snapshot.killSwitchActive) failures.push('kill switch is active');
  return failures;
}

export function newContinuationApproval(
  input: ContinuationApprovalInput,
  now = new Date(),
): ContinuationApproval {
  const approval: ContinuationApproval = {
    continuationId: `continuation-${crypto.randomUUID()}`,
    candidateId: asString(input.candidateId),
    launchId: asString(input.launchId),
    initialApprovalId: asString(input.initialApprovalId),
    imageDigest: asString(input.imageDigest),
    workerRevision: asString(input.workerRevision),
    secretVersions: {
      sql: asString(input.secretVersions?.sql),
      apiKey: asString(input.secretVersions?.apiKey),
      apiSecret: asString(input.secretVersions?.apiSecret),
    },
    firstOrderEvidenceHash: asString(input.firstOrderEvidenceHash),
    reconciliationStatus: 'IN_SYNC',
    preflightObservedAt: asString(input.preflightObservedAt),
    nonce: asString(input.nonce),
    requesterUid: asString(input.requesterUid),
    createdAt: now.toISOString(),
    expiresAt: asString(input.expiresAt),
    status: 'PENDING',
  };
  const failures = validateContinuationApproval(approval, now);
  if (failures.length) throw new Error(failures.join('; '));
  return approval;
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
