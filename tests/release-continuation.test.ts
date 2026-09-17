import { describe, expect, it } from 'vitest';
import {
  AUTONOMOUS_CONTINUATION_POLICY,
  newContinuationApproval,
  resolveReconciliationStatus,
  validateContinuationPrerequisites,
  type ContinuationVerificationSnapshot,
} from '../src/backend/release.js';
import { InMemoryReleaseStore } from '../src/backend/release-store.js';

describe('resolveReconciliationStatus', () => {
  it('returns IN_SYNC when the dedicated check explicitly passes', () => {
    expect(
      resolveReconciliationStatus(
        [{ id: 'CHK-PREFLIGHT-RECONCILIATION', status: 'PASS' }],
        'IN_SYNC',
      ),
    ).toBe('IN_SYNC');
  });

  it('returns DRIFT_DETECTED when the dedicated check explicitly fails, even if the cached worker field still says IN_SYNC', () => {
    expect(
      resolveReconciliationStatus(
        [{ id: 'CHK-PREFLIGHT-RECONCILIATION', status: 'FAIL' }],
        'IN_SYNC',
      ),
    ).toBe('DRIFT_DETECTED');
  });

  it('falls back to the cached worker field only when the dedicated check is absent', () => {
    expect(resolveReconciliationStatus([], 'IN_SYNC')).toBe('IN_SYNC');
    expect(resolveReconciliationStatus([], 'DRIFT')).toBe('DRIFT');
    expect(resolveReconciliationStatus([], undefined)).toBe('');
  });

  it('ignores unrelated check ids and still falls back to the cache', () => {
    expect(
      resolveReconciliationStatus(
        [{ id: 'CHK-SOME-OTHER-CHECK', status: 'FAIL' }],
        'IN_SYNC',
      ),
    ).toBe('IN_SYNC');
  });
});

const NOW = new Date();
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

  it('releases a claimed continuation back to PENDING so a retry is not stranded in ACTIVATING', async () => {
    const store = new InMemoryReleaseStore();
    const record = approval();
    await store.createContinuationApproval(record);
    await store.claimContinuationApproval(record.continuationId);
    expect((await store.getContinuationApproval(record.continuationId))?.status).toBe('ACTIVATING');

    await store.releaseContinuationApproval(record.continuationId);
    expect((await store.getContinuationApproval(record.continuationId))?.status).toBe('PENDING');

    // The same approval can now be claimed again instead of requiring a
    // brand new one.
    const reclaimed = await store.claimContinuationApproval(record.continuationId);
    expect(reclaimed.status).toBe('ACTIVATING');
  });

  it('leaves a CONSUMED continuation alone when release is called after a concurrent consume', async () => {
    const store = new InMemoryReleaseStore();
    const record = approval();
    await store.createContinuationApproval(record);
    await store.claimContinuationApproval(record.continuationId);
    await store.consumeContinuationApproval(record.continuationId);

    // Simulates the losing side of a race: its own consume attempt failed,
    // but a concurrent request already legitimately consumed it -- release
    // must be a no-op here, not revert a real success back to PENDING.
    await store.releaseContinuationApproval(record.continuationId);
    expect((await store.getContinuationApproval(record.continuationId))?.status).toBe('CONSUMED');
  });

  it('releasing an unclaimed (PENDING) or unknown continuation is a safe no-op', async () => {
    const store = new InMemoryReleaseStore();
    const record = approval();
    await store.createContinuationApproval(record);

    await store.releaseContinuationApproval(record.continuationId);
    expect((await store.getContinuationApproval(record.continuationId))?.status).toBe('PENDING');

    await expect(store.releaseContinuationApproval('continuation-00000000-0000-0000-0000-000000000000')).resolves.toBeUndefined();
  });

  it('keeps the policy distinct from the initial staged release policy', () => {
    expect(AUTONOMOUS_CONTINUATION_POLICY).toBe('AUTONOMOUS_AFTER_REVIEW');
    expect(approval().firstOrderEvidenceHash).toMatch(/^[0-9a-f]{64}$/);
  });
});
