import crypto from 'node:crypto';
import {
  localRiskPolicySha256,
  readLocalRiskPolicy,
} from './local-release-runtime.js';
import { localSecretSourceIdentity, type LocalSecretSourceIdentity } from './local-secret-manager.js';

export const LOCAL_RELEASE_RUNTIME_TARGET = 'LOCAL' as const;
export const LOCAL_RELEASE_POLICY_VERSION = 'local-mainnet-risk-v1' as const;
export const LOCAL_RELEASE_EXPIRY_MS = 60 * 60 * 1000;

/**
 * This policy is deliberately owned by the Local release domain. It is not
 * imported from the Cloud Run release domain, whose collection and consumer
 * have a separate lifecycle.
 */
const canonicalRiskPolicy = readLocalRiskPolicy();
export const LOCAL_RELEASE_POLICY = Object.freeze({
  policyVersion: canonicalRiskPolicy.version,
  symbol: canonicalRiskPolicy.symbol,
  basketBudgetUsdc: canonicalRiskPolicy.basket_budget_usdc,
  basketDrawdownUsdc: canonicalRiskPolicy.basket_drawdown_usdc,
  dailyLossUsdc: canonicalRiskPolicy.daily_loss_usdc,
  collateralUsdc: canonicalRiskPolicy.collateral_usdc,
  grossExposureUsdc: canonicalRiskPolicy.gross_exposure_usdc,
  firstOrderNotionalUsdc: canonicalRiskPolicy.first_order_notional_usdc,
  maxActiveExposureChains: canonicalRiskPolicy.active_exposure_chains,
  maxLeverage: canonicalRiskPolicy.max_leverage,
  riskRewardRatio: `${canonicalRiskPolicy.risk_reward.risk}:${canonicalRiskPolicy.risk_reward.reward}`,
  netOfCosts: canonicalRiskPolicy.risk_reward.net_of_costs,
  launchPolicy: 'STAGED_FIRST_ORDER',
} as const);

const SHA256_RE = /^[0-9a-f]{64}$/i;
const VERSION_RE = /^[1-9][0-9]*$/;
const RUN_ID_RE = /^run-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$/;
const NONCE_RE = /^[A-Za-z0-9_-]{16,128}$/;
const UID_RE = /^[A-Za-z0-9:_-]{1,256}$/;
const LOCAL_CANDIDATE_ID_RE = /^local-rc-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const LOCAL_APPROVAL_ID_RE = /^local-approval-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export interface LocalReleaseCandidateInput extends LocalSecretSourceIdentity {
  runId: string;
  sourceFingerprint: string;
  dependencyFingerprint: string;
  migrationFingerprint: string;
  promotionEvidenceSha256: string;
  apiKeyVersion: string;
  apiSecretVersion: string;
  nonce: string;
  policyVersion?: string;
  policyHash?: string;
}

export interface LocalReleaseBinding extends LocalSecretSourceIdentity {
  runtimeTarget: typeof LOCAL_RELEASE_RUNTIME_TARGET;
  runId: string;
  sourceFingerprint: string;
  dependencyFingerprint: string;
  migrationFingerprint: string;
  promotionEvidenceSha256: string;
  apiKeyVersion: string;
  apiSecretVersion: string;
  policyVersion: typeof LOCAL_RELEASE_POLICY_VERSION;
  policyHash: string;
}

export type LocalReleaseCandidateStatus =
  | 'PENDING_APPROVAL'
  | 'APPROVED'
  | 'CONSUMED'
  | 'EXPIRED';

export interface LocalReleaseCandidate extends LocalReleaseBinding {
  candidateId: string;
  createdAt: string;
  expiresAt: string;
  nonce: string;
  status: LocalReleaseCandidateStatus;
  approvalId?: string;
  approvedByUid?: string;
  approvedAt?: string;
  consumedAt?: string;
}

export interface LocalReleaseApprover {
  uid: string;
  role: 'trading_admin';
}

export interface LocalConsumedApproval extends LocalReleaseBinding {
  candidateId: string;
  approvalId: string;
  nonce: string;
  approvedByUid: string;
  approvedAt: string;
  consumedAt: string;
}

/** Deterministic JSON used for the release policy hash and evidence binding. */
export function canonicalJson(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('Canonical JSON cannot contain a non-finite number');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`)
      .join(',')}}`;
  }
  throw new Error('Canonical JSON contains an unsupported value');
}

export function sha256Canonical(value: unknown): string {
  return crypto.createHash('sha256').update(canonicalJson(value), 'utf8').digest('hex');
}

export const LOCAL_RELEASE_POLICY_HASH = localRiskPolicySha256();

function asTrimmedString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function assertCredentialFreeInput(value: unknown): asserts value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Local release candidate input must be an object');
  }

  const allowed = new Set([
    'runId',
    'sourceFingerprint',
    'dependencyFingerprint',
    'migrationFingerprint',
    'promotionEvidenceSha256',
    'apiKeyVersion',
    'apiSecretVersion',
    'secretManagerProjectId',
    'apiKeySecretVersionResource',
    'apiSecretSecretVersionResource',
    'nonce',
    'policyVersion',
    'policyHash',
  ]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new Error('Local release candidate input contains an unsupported or credential-like field');
    }
  }
}

function validateBinding(binding: Partial<LocalReleaseBinding> | undefined): string[] {
  if (!binding) return ['Local release binding is missing'];
  const failures: string[] = [];
  if (binding.runtimeTarget !== LOCAL_RELEASE_RUNTIME_TARGET) failures.push('runtimeTarget must be LOCAL');
  if (!RUN_ID_RE.test(asTrimmedString(binding.runId))) failures.push('runId is invalid');
  for (const [name, value] of [
    ['sourceFingerprint', binding.sourceFingerprint],
    ['dependencyFingerprint', binding.dependencyFingerprint],
    ['migrationFingerprint', binding.migrationFingerprint],
    ['promotionEvidenceSha256', binding.promotionEvidenceSha256],
    ['policyHash', binding.policyHash],
  ] as const) {
    if (!SHA256_RE.test(asTrimmedString(value))) failures.push(`${name} must be a SHA-256 hash`);
  }
  if (!VERSION_RE.test(asTrimmedString(binding.apiKeyVersion))) failures.push('apiKeyVersion must be numeric');
  if (!VERSION_RE.test(asTrimmedString(binding.apiSecretVersion))) failures.push('apiSecretVersion must be numeric');
  try {
    const identity = localSecretSourceIdentity(
      asTrimmedString(binding.secretManagerProjectId),
      asTrimmedString(binding.apiKeyVersion),
      asTrimmedString(binding.apiSecretVersion),
    );
    if (binding.apiKeySecretVersionResource !== identity.apiKeySecretVersionResource
      || binding.apiSecretSecretVersionResource !== identity.apiSecretSecretVersionResource) {
      failures.push('secret source resource binding is invalid');
    }
  } catch {
    failures.push('secret source identity is invalid');
  }
  if (binding.policyVersion !== LOCAL_RELEASE_POLICY_VERSION) {
    failures.push('policyVersion does not match the fixed Local policy');
  }
  if (asTrimmedString(binding.policyHash) !== LOCAL_RELEASE_POLICY_HASH) {
    failures.push('policyHash does not match the canonical fixed Local policy');
  }
  return failures;
}

export function localReleaseBinding(candidate: LocalReleaseCandidate): LocalReleaseBinding {
  return {
    runtimeTarget: candidate.runtimeTarget,
    runId: candidate.runId,
    sourceFingerprint: candidate.sourceFingerprint,
    dependencyFingerprint: candidate.dependencyFingerprint,
    migrationFingerprint: candidate.migrationFingerprint,
    promotionEvidenceSha256: candidate.promotionEvidenceSha256,
    apiKeyVersion: candidate.apiKeyVersion,
    apiSecretVersion: candidate.apiSecretVersion,
    secretManagerProjectId: candidate.secretManagerProjectId,
    apiKeySecretVersionResource: candidate.apiKeySecretVersionResource,
    apiSecretSecretVersionResource: candidate.apiSecretSecretVersionResource,
    policyVersion: candidate.policyVersion,
    policyHash: candidate.policyHash,
  };
}

export function validateLocalReleaseCandidate(
  candidate: Partial<LocalReleaseCandidate> | undefined,
  now = new Date(),
): string[] {
  if (!candidate) return ['Local release candidate is missing'];
  const failures = [
    ...validateBinding(candidate),
  ];
  if (!LOCAL_CANDIDATE_ID_RE.test(asTrimmedString(candidate.candidateId))) {
    failures.push('candidateId must use the local-rc-UUID format');
  }
  if (!NONCE_RE.test(asTrimmedString(candidate.nonce))) failures.push('nonce is invalid');

  const createdAt = Date.parse(asTrimmedString(candidate.createdAt));
  const expiresAt = Date.parse(asTrimmedString(candidate.expiresAt));
  if (!Number.isFinite(createdAt)) failures.push('createdAt is invalid');
  if (!Number.isFinite(expiresAt)) failures.push('expiresAt is invalid');
  if (Number.isFinite(createdAt) && Number.isFinite(expiresAt)) {
    if (expiresAt - createdAt !== LOCAL_RELEASE_EXPIRY_MS) {
      failures.push('expiresAt must be exactly one hour after createdAt');
    }
    if (candidate.status !== 'EXPIRED' && expiresAt <= now.getTime()) {
      failures.push('Local release candidate is expired');
    }
  }

  if (!['PENDING_APPROVAL', 'APPROVED', 'CONSUMED', 'EXPIRED'].includes(String(candidate.status))) {
    failures.push('candidate status is invalid');
  }
  if (candidate.status === 'PENDING_APPROVAL' && (candidate.approvalId || candidate.approvedByUid || candidate.approvedAt || candidate.consumedAt)) {
    failures.push('pending candidate must not contain approval or consumption metadata');
  }
  if (candidate.approvalId && !LOCAL_APPROVAL_ID_RE.test(asTrimmedString(candidate.approvalId))) {
    failures.push('approvalId is invalid');
  }
  if (candidate.approvedByUid && !UID_RE.test(asTrimmedString(candidate.approvedByUid))) {
    failures.push('approvedByUid is invalid');
  }
  if (candidate.approvedAt && !Number.isFinite(Date.parse(asTrimmedString(candidate.approvedAt)))) {
    failures.push('approvedAt is invalid');
  }
  if (candidate.consumedAt && !Number.isFinite(Date.parse(asTrimmedString(candidate.consumedAt)))) {
    failures.push('consumedAt is invalid');
  }
  return failures;
}

export function assertExpectedLocalBinding(
  candidate: LocalReleaseCandidate,
  expected: LocalReleaseBinding,
): void {
  const failures = [
    ...validateBinding(expected),
  ];
  if (failures.length) throw new Error(failures.join('; '));
  if (canonicalJson(localReleaseBinding(candidate)) !== canonicalJson(expected)) {
    throw new Error('Local release fingerprint, run, secret source, or policy binding does not match');
  }
}

export function newLocalReleaseCandidate(
  input: LocalReleaseCandidateInput,
  now = new Date(),
): LocalReleaseCandidate {
  assertCredentialFreeInput(input);
  if (input.policyVersion !== undefined && input.policyVersion !== LOCAL_RELEASE_POLICY_VERSION) {
    throw new Error('policyVersion does not match the fixed Local policy');
  }
  if (input.policyHash !== undefined && input.policyHash !== LOCAL_RELEASE_POLICY_HASH) {
    throw new Error('policyHash does not match the canonical fixed Local policy');
  }

  const candidate: LocalReleaseCandidate = {
    candidateId: `local-rc-${crypto.randomUUID()}`,
    runtimeTarget: LOCAL_RELEASE_RUNTIME_TARGET,
    runId: asTrimmedString(input.runId),
    sourceFingerprint: asTrimmedString(input.sourceFingerprint),
    dependencyFingerprint: asTrimmedString(input.dependencyFingerprint),
    migrationFingerprint: asTrimmedString(input.migrationFingerprint),
    promotionEvidenceSha256: asTrimmedString(input.promotionEvidenceSha256),
    apiKeyVersion: asTrimmedString(input.apiKeyVersion),
    apiSecretVersion: asTrimmedString(input.apiSecretVersion),
    secretManagerProjectId: asTrimmedString(input.secretManagerProjectId),
    apiKeySecretVersionResource: asTrimmedString(input.apiKeySecretVersionResource),
    apiSecretSecretVersionResource: asTrimmedString(input.apiSecretSecretVersionResource),
    policyVersion: LOCAL_RELEASE_POLICY_VERSION,
    policyHash: LOCAL_RELEASE_POLICY_HASH,
    createdAt: now.toISOString(),
    expiresAt: new Date(now.getTime() + LOCAL_RELEASE_EXPIRY_MS).toISOString(),
    nonce: asTrimmedString(input.nonce),
    status: 'PENDING_APPROVAL',
  };
  const failures = validateLocalReleaseCandidate(candidate, now);
  if (failures.length) throw new Error(failures.join('; '));
  return candidate;
}

export function validateLocalApprover(approver: LocalReleaseApprover): void {
  if (approver.role !== 'trading_admin') throw new Error('Local release approval requires trading_admin');
  if (!UID_RE.test(asTrimmedString(approver.uid))) throw new Error('Local release approver uid is invalid');
}

export function consumedApprovalFromCandidate(
  candidate: LocalReleaseCandidate,
): LocalConsumedApproval {
  if (
    candidate.status !== 'CONSUMED'
    || !candidate.approvalId
    || !candidate.approvedByUid
    || !candidate.approvedAt
    || !candidate.consumedAt
  ) {
    throw new Error('Local release candidate has no complete consumed approval');
  }
  return {
    ...localReleaseBinding(candidate),
    candidateId: candidate.candidateId,
    approvalId: candidate.approvalId,
    nonce: candidate.nonce,
    approvedByUid: candidate.approvedByUid,
    approvedAt: candidate.approvedAt,
    consumedAt: candidate.consumedAt,
  };
}
