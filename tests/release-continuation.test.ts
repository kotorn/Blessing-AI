import { describe, expect, it } from 'vitest';
import {
  AUTONOMOUS_CONTINUATION_POLICY,
  newContinuationApproval,
  validateContinuationPrerequisites,
  type ContinuationVerificationSnapshot,
} from '../src/backend/release.js';
import { InMemoryReleaseStore } from '../src/backend/release-store.js';

const NOW = new Date('2026-09-16T12:00:00.000Z');
const IMAGE = 'asia-southeast1-docker.pkg.dev/gen-lang-client-0730128480/blessing-repo/trading-worker@sha256:'
  + 'b'.repeat(64);

function approval() {
  return newContinuationApproval({
    candidateId: 'rc-00000000-0000-0000-0000-000000000000',
    launchId: 'launch-approval-00000000',
    initialApprovalId: 'approval-00000000-0000-0000-0000-000000000000',
    imageDigest: IMAGE,
    workerRevision: 'blessing-trading-worker-00004-abc',
    secretVersions: { sql: '1', apiKey: '1', apiSecret: '1' },
    firstOrderEvidenceHash: 'c'.repeat(64),
    preflightObservedAt: NOW.toISOString(),
    nonce: 'continuation-nonce-1234',
    requesterUid: 'firebase-admin-uid',
    expiresAt: new Date(NOW.getTime() + 12 * 60 * 60 * 1000).toISOString(),
  }, NOW);
}

function snapshot(): ContinuationVerificationSnapshot {
  return {
    currentImageDigest: IMAGE,
    currentWorkerRevision: 'blessing-trading-worker-00004-abc',
    currentExecutionMode: 'LIVE',
    currentMainnetLiveApproved: true,
    currentEngineState: 'PAUSED_NEW_RISK',
    currentLaunchId: 'launch-approval-00000000',
    currentLaunchPolicy: 'STAGED_FIRST_ORDER',
    currentLaunchState: 'PAUSED_NEW_RISK',
    currentSubmittedOrders: 1,
    currentSecretVersions: { sql: '1', apiKey: '1', apiSecret: '1' },
    preflightPassed: true,
    preflightObservedAt: NOW.toISOString(),
    preflightOrderEndpointAttempts: 0,
    preflightOrderSubmissionAttempts: 0,
    reconciliationStatus: 'IN_SYNC',
    persistenceDurable: true,
    dataConnectCutover: false,
    killSwitchActive: false,
  };
}

describe('autonomous continuation release boundary', () => {
  it('requires fresh scope/evidence and rejects mismatches', () => {
    const record = approval();
    expect(validateContinuationPrerequisites(record, snapshot(), NOW)).toEqual([]);
    expect(validateContinuationPrerequisites(
      record,
      { ...snapshot(), currentImageDigest: IMAGE.replace(/b/g, 'a') },
      NOW,
    )).toContain('worker image digest does not match continuation approval');
    expect(validateContinuationPrerequisites(
      record,
      { ...snapshot(), preflightOrderEndpointAttempts: 1 },
      NOW,
    )).toContain('continuation preflight called an order endpoint');
    expect(validateContinuationPrerequisites(
      record,
      { ...snapshot(), currentEngineState: 'ARMED' },
      NOW,
    )).toContain('worker must be paused or disarmed before continuation');
    expect(validateContinuationPrerequisites(
      record,
      { ...snapshot(), currentSecretVersions: { sql: '2', apiKey: '1', apiSecret: '1' } },
      NOW,
    )).toContain('Worker Secret Manager versions do not match the approved release');
  });

  it('claims and consumes a continuation exactly once', async () => {
    const store = new InMemoryReleaseStore();
    const record = approval();
    await store.createContinuationApproval(record);
    expect(await store.getContinuationApproval(record.continuationId)).toMatchObject({
      continuationId: record.continuationId,
      status: 'PENDING',
    });
    const claimed = await store.claimContinuationApproval(record.continuationId);
    expect(claimed.status).toBe('ACTIVATING');
    await expect(store.claimContinuationApproval(record.continuationId)).rejects.toThrow('already being activated');
    const consumed = await store.consumeContinuationApproval(record.continuationId);
    expect(consumed.status).toBe('CONSUMED');
    await expect(store.claimContinuationApproval(record.continuationId)).rejects.toThrow('already been consumed');
    await expect(store.consumeContinuationApproval(record.continuationId)).rejects.toThrow('already been consumed');
  });

  it('keeps the policy distinct from the initial staged release policy', () => {
    expect(AUTONOMOUS_CONTINUATION_POLICY).toBe('AUTONOMOUS_AFTER_REVIEW');
    expect(approval().firstOrderEvidenceHash).toMatch(/^[0-9a-f]{64}$/);
  });
});
