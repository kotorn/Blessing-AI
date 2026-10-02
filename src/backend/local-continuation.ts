import crypto from 'node:crypto';
import {
  canonicalJson,
  LOCAL_RELEASE_EXPIRY_MS,
  LOCAL_RELEASE_POLICY_HASH,
  LOCAL_RELEASE_POLICY_VERSION,
  LOCAL_RELEASE_RUNTIME_TARGET,
  type LocalReleaseApprover,
  validateLocalApprover,
} from './local-release.js';

export const LOCAL_CONTINUATION_RUNTIME_TARGET = LOCAL_RELEASE_RUNTIME_TARGET;
export const LOCAL_CONTINUATION_POLICY_VERSION = LOCAL_RELEASE_POLICY_VERSION;
export const LOCAL_CONTINUATION_POLICY_HASH = LOCAL_RELEASE_POLICY_HASH;
export const LOCAL_CONTINUATION_EXPIRY_MS = LOCAL_RELEASE_EXPIRY_MS;
export const LOCAL_CONTINUATION_APPROVALS_COLLECTION = 'local_continuation_approvals' as const;

export type LocalContinuationStatus =
  | 'PENDING_APPROVAL'
  | 'APPROVED'
  | 'CONSUMED'
  | 'EXPIRED';

export interface LocalContinuationInput {
  continuationId?: string;
  candidateId: string;
  initialApprovalId: string;
  runId: string;
  launchId: string;
  sourceFingerprint: string;
  dependencyFingerprint: string;
  migrationFingerprint: string;
  firstOrderEvidenceHash: string;
  tradingAdminUid: string;
  nonce: string;
  policyVersion?: string;
  policyHash?: string;
}

export interface LocalContinuationBinding {
  runtimeTarget: typeof LOCAL_CONTINUATION_RUNTIME_TARGET;
  candidateId: string;
  initialApprovalId: string;
  runId: string;
  launchId: string;
  sourceFingerprint: string;
  dependencyFingerprint: string;
  migrationFingerprint: string;
  policyVersion: typeof LOCAL_CONTINUATION_POLICY_VERSION;
  policyHash: typeof LOCAL_CONTINUATION_POLICY_HASH;
  firstOrderEvidenceHash: string;
  tradingAdminUid: string;
  nonce: string;
}

export interface LocalContinuationApproval extends LocalContinuationBinding {
  continuationId: string;
  approvalId?: string;
  createdAt: string;
  expiresAt: string;
  status: LocalContinuationStatus;
  approvedByUid?: string;
  approvedAt?: string;
  consumedAt?: string;
}

export type LocalConsumedContinuationApproval = LocalContinuationApproval & {
  status: 'CONSUMED';
  approvalId: string;
  approvedByUid: string;
  approvedAt: string;
  consumedAt: string;
};

export type LocalContinuationApprover = LocalReleaseApprover;

const SHA256_RE = /^[0-9a-f]{64}$/i;
const RUN_ID_RE = /^run-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$/;
const LAUNCH_ID_RE = /^launch-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$/;
const NONCE_RE = /^[A-Za-z0-9_-]{16,128}$/;
const UID_RE = /^[A-Za-z0-9:_-]{1,256}$/;
const UUID_RE = '[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}';
const LOCAL_CANDIDATE_ID_RE = new RegExp(`^local-rc-${UUID_RE}$`, 'i');
const LOCAL_APPROVAL_ID_RE = new RegExp(`^local-approval-${UUID_RE}$`, 'i');
const CONTINUATION_ID_RE = new RegExp(`^local-continuation-${UUID_RE}$`, 'i');
const CONTINUATION_APPROVAL_ID_RE = new RegExp(`^local-continuation-approval-${UUID_RE}$`, 'i');

function trimmed(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function assertCredentialFreeInput(value: unknown): asserts value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Local continuation input must be an object');
  }
  const allowed = new Set([
    'candidateId',
    'continuationId',
    'initialApprovalId',
    'runId',
    'launchId',
    'sourceFingerprint',
    'dependencyFingerprint',
    'migrationFingerprint',
    'firstOrderEvidenceHash',
    'tradingAdminUid',
    'nonce',
    'policyVersion',
    'policyHash',
  ]);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw new Error('Local continuation input contains an unsupported or credential-like field');
    }
  }
}

function validateBinding(binding: Partial<LocalContinuationBinding> | undefined): string[] {
  if (!binding) return ['Local continuation binding is missing'];
  const failures: string[] = [];
  if (binding.runtimeTarget !== LOCAL_CONTINUATION_RUNTIME_TARGET) failures.push('runtimeTarget must be LOCAL');
  if (!LOCAL_CANDIDATE_ID_RE.test(trimmed(binding.candidateId))) failures.push('candidateId must use local-rc-UUID');
  if (!LOCAL_APPROVAL_ID_RE.test(trimmed(binding.initialApprovalId))) failures.push('initialApprovalId must use local-approval-UUID');
  if (!RUN_ID_RE.test(trimmed(binding.runId))) failures.push('runId is invalid');
  if (!LAUNCH_ID_RE.test(trimmed(binding.launchId))) failures.push('launchId is invalid');
  for (const [name, value] of [
    ['sourceFingerprint', binding.sourceFingerprint],
    ['dependencyFingerprint', binding.dependencyFingerprint],
    ['migrationFingerprint', binding.migrationFingerprint],
    ['firstOrderEvidenceHash', binding.firstOrderEvidenceHash],
  ] as const) {
    if (!SHA256_RE.test(trimmed(value))) failures.push(`${name} must be a SHA-256 hash`);
  }
  if (binding.policyVersion !== LOCAL_CONTINUATION_POLICY_VERSION) {
    failures.push('policyVersion does not match the canonical Local risk policy');
  }
  if (trimmed(binding.policyHash) !== LOCAL_CONTINUATION_POLICY_HASH) {
    failures.push('policyHash does not match the canonical Local risk policy');
  }
  if (!UID_RE.test(trimmed(binding.tradingAdminUid))) failures.push('tradingAdminUid is invalid');
  if (!NONCE_RE.test(trimmed(binding.nonce))) failures.push('nonce is invalid');
  return failures;
}

function validateApprovalMetadata(approval: Partial<LocalContinuationApproval>): string[] {
  const failures: string[] = [];
  if (!CONTINUATION_ID_RE.test(trimmed(approval.continuationId))) failures.push('continuationId is invalid');
  if (approval.approvalId && !CONTINUATION_APPROVAL_ID_RE.test(trimmed(approval.approvalId))) {
    failures.push('approvalId is invalid');
  }
  for (const [name, value] of [
    ['createdAt', approval.createdAt],
    ['expiresAt', approval.expiresAt],
    ['approvedAt', approval.approvedAt],
    ['consumedAt', approval.consumedAt],
  ] as const) {
    if (value !== undefined && !Number.isFinite(Date.parse(trimmed(value)))) failures.push(`${name} is invalid`);
  }
  if (approval.approvedByUid !== undefined && !UID_RE.test(trimmed(approval.approvedByUid))) {
    failures.push('approvedByUid is invalid');
  }
  return failures;
}

export function localContinuationBinding(
  approval: LocalContinuationApproval,
): LocalContinuationBinding {
  return {
    runtimeTarget: approval.runtimeTarget,
    candidateId: approval.candidateId,
    initialApprovalId: approval.initialApprovalId,
    runId: approval.runId,
    launchId: approval.launchId,
    sourceFingerprint: approval.sourceFingerprint,
    dependencyFingerprint: approval.dependencyFingerprint,
    migrationFingerprint: approval.migrationFingerprint,
    policyVersion: approval.policyVersion,
    policyHash: approval.policyHash,
    firstOrderEvidenceHash: approval.firstOrderEvidenceHash,
    tradingAdminUid: approval.tradingAdminUid,
    nonce: approval.nonce,
  };
}

export function validateLocalContinuation(
  approval: Partial<LocalContinuationApproval> | undefined,
  now = new Date(),
): string[] {
  if (!approval) return ['Local continuation approval is missing'];
  const failures = [...validateBinding(approval), ...validateApprovalMetadata(approval)];
  const createdAt = Date.parse(trimmed(approval.createdAt));
  const expiresAt = Date.parse(trimmed(approval.expiresAt));
  if (!Number.isFinite(createdAt)) failures.push('createdAt is invalid');
  if (!Number.isFinite(expiresAt)) failures.push('expiresAt is invalid');
  if (Number.isFinite(createdAt) && Number.isFinite(expiresAt)) {
    if (expiresAt - createdAt !== LOCAL_CONTINUATION_EXPIRY_MS) {
      failures.push('expiresAt must be exactly one hour after createdAt');
    }
    if (approval.status !== 'EXPIRED' && expiresAt <= now.getTime()) failures.push('Local continuation approval is expired');
  }
  if (!['PENDING_APPROVAL', 'APPROVED', 'CONSUMED', 'EXPIRED'].includes(String(approval.status))) {
    failures.push('continuation status is invalid');
  }
  if (approval.status === 'PENDING_APPROVAL' && (approval.approvalId || approval.approvedByUid || approval.approvedAt || approval.consumedAt)) {
    failures.push('pending continuation must not contain approval or consumption metadata');
  }
  if (approval.status === 'APPROVED' && (!approval.approvalId || !approval.approvedByUid || !approval.approvedAt || approval.consumedAt)) {
    failures.push('approved continuation must contain approval metadata only');
  }
  if (approval.status === 'CONSUMED' && (!approval.approvalId || !approval.approvedByUid || !approval.approvedAt || !approval.consumedAt)) {
    failures.push('consumed continuation must contain complete approval metadata');
  }
  return failures;
}

export function assertExpectedLocalContinuationBinding(
  approval: LocalContinuationApproval,
  expected: LocalContinuationBinding,
): void {
  const failures = validateBinding(expected);
  if (failures.length) throw new Error(failures.join('; '));
  if (canonicalJson(localContinuationBinding(approval)) !== canonicalJson(expected)) {
    throw new Error('Local continuation candidate, launch, fingerprint, evidence, UID, nonce, or policy binding does not match');
  }
}

export function newLocalContinuationApproval(
  input: LocalContinuationInput,
  now = new Date(),
): LocalContinuationApproval {
  assertCredentialFreeInput(input);
  if (input.policyVersion !== undefined && input.policyVersion !== LOCAL_CONTINUATION_POLICY_VERSION) {
    throw new Error('policyVersion does not match the canonical Local risk policy');
  }
  if (input.policyHash !== undefined && input.policyHash !== LOCAL_CONTINUATION_POLICY_HASH) {
    throw new Error('policyHash does not match the canonical Local risk policy');
  }
  const approval: LocalContinuationApproval = {
    continuationId: input.continuationId?.trim() || `local-continuation-${crypto.randomUUID()}`,
    runtimeTarget: LOCAL_CONTINUATION_RUNTIME_TARGET,
    candidateId: trimmed(input.candidateId),
    initialApprovalId: trimmed(input.initialApprovalId),
    runId: trimmed(input.runId),
    launchId: trimmed(input.launchId),
    sourceFingerprint: trimmed(input.sourceFingerprint),
    dependencyFingerprint: trimmed(input.dependencyFingerprint),
    migrationFingerprint: trimmed(input.migrationFingerprint),
    policyVersion: LOCAL_CONTINUATION_POLICY_VERSION,
    policyHash: LOCAL_CONTINUATION_POLICY_HASH,
    firstOrderEvidenceHash: trimmed(input.firstOrderEvidenceHash),
    tradingAdminUid: trimmed(input.tradingAdminUid),
    nonce: trimmed(input.nonce),
    createdAt: now.toISOString(),
    expiresAt: new Date(now.getTime() + LOCAL_CONTINUATION_EXPIRY_MS).toISOString(),
    status: 'PENDING_APPROVAL',
  };
  const failures = validateLocalContinuation(approval, now);
  if (failures.length) throw new Error(failures.join('; '));
  return approval;
}

/** Stable document identity prevents duplicate pending requests for one launch. */
export function localContinuationIdFor(candidateId: string, launchId: string): string {
  if (!LOCAL_CANDIDATE_ID_RE.test(trimmed(candidateId)) || !LAUNCH_ID_RE.test(trimmed(launchId))) {
    throw new Error('Local continuation identity requires a valid candidate and launch');
  }
  const hex = crypto
    .createHash('sha256')
    .update(`LOCAL\u0000${candidateId.trim()}\u0000${launchId.trim()}`)
    .digest('hex')
    .slice(0, 32);
  const variant = ((Number.parseInt(hex[16], 16) & 0x3) | 0x8).toString(16);
  const uuid = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-5${hex.slice(13, 16)}-${variant}${hex.slice(17, 20)}-${hex.slice(20, 32)}`;
  return `local-continuation-${uuid}`;
}

export function validateLocalContinuationApprover(approver: LocalContinuationApprover): void {
  validateLocalApprover(approver);
}

export function consumedLocalContinuation(
  approval: LocalContinuationApproval,
): LocalConsumedContinuationApproval {
  if (
    approval.status !== 'CONSUMED'
    || !approval.approvalId
    || !approval.approvedByUid
    || !approval.approvedAt
    || !approval.consumedAt
  ) {
    throw new Error('Local continuation approval has no complete consumed approval');
  }
  return approval as LocalConsumedContinuationApproval;
}
