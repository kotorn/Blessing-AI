import { describe, expect, it } from 'vitest';
import {
  LOCAL_CONTINUATION_APPROVALS_COLLECTION,
  LOCAL_CONTINUATION_EXPIRY_MS,
  LOCAL_CONTINUATION_POLICY_HASH,
  LOCAL_CONTINUATION_POLICY_VERSION,
  localContinuationBinding,
  localContinuationIdFor,
  newLocalContinuationApproval,
  type LocalContinuationInput,
} from '../src/backend/local-continuation.js';
import { InMemoryLocalContinuationStore } from '../src/backend/local-continuation-store.js';

const NOW = new Date('2026-09-24T00:00:00.000Z');
const HASHES = {
  source: 'a'.repeat(64),
  dependency: 'b'.repeat(64),
  migration: 'c'.repeat(64),
  evidence: 'd'.repeat(64),
};

function input(overrides: Partial<LocalContinuationInput> = {}): LocalContinuationInput {
  return {
    candidateId: 'local-rc-00000000-0000-4000-8000-000000000001',
    initialApprovalId: 'local-approval-00000000-0000-4000-8000-000000000002',
    runId: 'run-local-continuation-001',
    launchId: 'launch-local-001',
    sourceFingerprint: HASHES.source,
    dependencyFingerprint: HASHES.dependency,
    migrationFingerprint: HASHES.migration,
    firstOrderEvidenceHash: HASHES.evidence,
    tradingAdminUid: 'admin-1',
    nonce: 'nonce-local-continuation-001',
    ...overrides,
  };
}

async function pendingStore(overrides: Partial<LocalContinuationInput> = {}) {
  const record = newLocalContinuationApproval(input(overrides), NOW);
  const store = new InMemoryLocalContinuationStore();
  await store.createApproval(record, NOW);
  return { record, store, expected: localContinuationBinding(record) };
}

describe('Local continuation approval domain', () => {
  it('creates a credential-free LOCAL record bound to the initial release and first-order evidence', () => {
    const record = newLocalContinuationApproval(input(), NOW);

    expect(record.runtimeTarget).toBe('LOCAL');
    expect(record.continuationId).toMatch(/^local-continuation-/);
    expect(record.candidateId).toMatch(/^local-rc-/);
    expect(record.initialApprovalId).toMatch(/^local-approval-/);
    expect(record.policyVersion).toBe(LOCAL_CONTINUATION_POLICY_VERSION);
    expect(record.policyHash).toBe(LOCAL_CONTINUATION_POLICY_HASH);
    expect(record.firstOrderEvidenceHash).toBe(HASHES.evidence);
    expect(record.tradingAdminUid).toBe('admin-1');
    expect(Date.parse(record.expiresAt) - Date.parse(record.createdAt)).toBe(LOCAL_CONTINUATION_EXPIRY_MS);
    expect(record.status).toBe('PENDING_APPROVAL');
    expect(JSON.stringify(record)).not.toMatch(/apiKey|apiSecret|password|token|credential/i);
  });

  it('uses a collection distinct from Cloud continuation and candidate collections', () => {
    expect(LOCAL_CONTINUATION_APPROVALS_COLLECTION).toBe('local_continuation_approvals');
    expect(LOCAL_CONTINUATION_APPROVALS_COLLECTION).not.toBe('release_continuations');
    expect(LOCAL_CONTINUATION_APPROVALS_COLLECTION).not.toBe('release_candidates');
  });

  it('uses a stable request identity and returns the existing pending approval on duplicate requests', async () => {
    const continuationId = localContinuationIdFor(input().candidateId, input().launchId);
    expect(continuationId).toBe(localContinuationIdFor(input().candidateId, input().launchId));
    const first = newLocalContinuationApproval(input({ continuationId }), NOW);
    const retry = newLocalContinuationApproval(input({ continuationId, nonce: 'another-nonce-for-retry' }), new Date(NOW.getTime() + 5_000));
    const store = new InMemoryLocalContinuationStore();
    await store.createApproval(first, NOW);
    await expect(store.createApproval(retry, new Date(NOW.getTime() + 5_000))).resolves.toMatchObject({
      continuationId,
      createdAt: first.createdAt,
      nonce: first.nonce,
      status: 'PENDING_APPROVAL',
    });
  });

  it('rejects credential-like payload fields before storage', () => {
    expect(() => newLocalContinuationApproval({
      ...input(),
      apiSecret: 'must-not-be-accepted',
    } as LocalContinuationInput & { apiSecret: string }, NOW)).toThrow(/credential-like|unsupported/i);
  });

  it('requires the bound trading_admin role and UID', async () => {
    const { record, store, expected } = await pendingStore();
    await expect(store.approveContinuation(record.continuationId, { uid: 'admin-1', role: 'operator' } as never, expected, NOW)).rejects.toThrow(/trading_admin/i);
    await expect(store.approveContinuation(record.continuationId, { uid: 'other-admin', role: 'trading_admin' }, expected, NOW)).rejects.toThrow(/UID/i);
  });

  it('rejects a binding mismatch at approval time', async () => {
    const { record, store, expected } = await pendingStore();
    await expect(store.approveContinuation(record.continuationId, { uid: 'admin-1', role: 'trading_admin' }, {
      ...expected,
      launchId: 'launch-different',
    }, NOW)).rejects.toThrow(/binding|launch|match/i);
  });

  it('approves and consumes exactly once, with replay rejected', async () => {
    const { record, store, expected } = await pendingStore();
    const approved = await store.approveContinuation(record.continuationId, { uid: 'admin-1', role: 'trading_admin' }, expected, NOW);
    expect(approved.status).toBe('APPROVED');
    expect(approved.approvalId).toMatch(/^local-continuation-approval-/);

    const consumed = await store.consumeContinuation(record.continuationId, expected, new Date(NOW.getTime() + 1_000));
    expect(consumed.status).toBe('CONSUMED');
    expect(consumed.approvalId).toBe(approved.approvalId);
    expect(consumed.approvedByUid).toBe('admin-1');
    await expect(store.consumeContinuation(record.continuationId, expected, new Date(NOW.getTime() + 2_000))).rejects.toThrow(/consumable|APPROVED|no longer/i);
    await expect(store.getConsumedApproval(consumed.approvalId, new Date(NOW.getTime() + 2_000))).resolves.toMatchObject({
      continuationId: record.continuationId,
      status: 'CONSUMED',
    });
  });

  it('rejects expiry before approval and does not consume an expired approval', async () => {
    const { record, store, expected } = await pendingStore();
    const expired = new Date(NOW.getTime() + LOCAL_CONTINUATION_EXPIRY_MS);
    await expect(store.approveContinuation(record.continuationId, { uid: 'admin-1', role: 'trading_admin' }, expected, expired)).rejects.toThrow(/expired/i);
  });

  it('rechecks every expected immutable binding before approval', async () => {
    const { record, store, expected } = await pendingStore();
    for (const field of ['candidateId', 'initialApprovalId', 'runId', 'sourceFingerprint', 'dependencyFingerprint', 'migrationFingerprint', 'policyVersion', 'policyHash', 'firstOrderEvidenceHash', 'tradingAdminUid', 'nonce'] as const) {
      const mismatched = { ...expected, [field]: `${expected[field]}-mismatch` };
      await expect(store.approveContinuation(record.continuationId, { uid: 'admin-1', role: 'trading_admin' }, mismatched, NOW)).rejects.toThrow(/binding|match|policy|fingerprint|hash|candidate|approval|nonce/i);
    }
  });

  it('also rechecks the binding at consumption time', async () => {
    const { record, store, expected } = await pendingStore();
    await store.approveContinuation(record.continuationId, { uid: 'admin-1', role: 'trading_admin' }, expected, NOW);
    await expect(store.consumeContinuation(record.continuationId, { ...expected, firstOrderEvidenceHash: HASHES.source }, NOW)).rejects.toThrow(/binding|evidence|match/i);
  });
});
