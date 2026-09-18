import { describe, expect, it } from 'vitest';
import {
  authorizeInternalServiceRequest,
  requiredControlPlaneRole,
} from '../src/backend/control-plane-auth.js';
import {
  hashEvidence,
  newReleaseCandidate,
  sanitizePreflightEvidence,
  validateApprovalPrerequisites,
  type ReleaseVerificationSnapshot,
} from '../src/backend/release.js';
import { InMemoryReleaseStore } from '../src/backend/release-store.js';

const IMAGE = 'asia-southeast1-docker.pkg.dev/gen-lang-client-0730128480/blessing-repo/trading-worker@sha256:'
  + 'a'.repeat(64);
const NOW = new Date();
const PREFLIGHT_AT = new Date(NOW.getTime() - 10_000);
const EXPIRY = new Date(NOW.getTime() + 60 * 60 * 1000);

function candidate() {
  return newReleaseCandidate({
    repoSha: 'fe21dba30f3379c59ff748f442d569e107fbb846',
    imageDigest: IMAGE,
    workerRevision: 'blessing-trading-worker-00003-abc',
    secretVersions: { sql: '1', apiKey: '1', apiSecret: '1' },
    preflightEvidenceHash: '1'.repeat(64),
    repoGateEvidenceHash: '2'.repeat(64),
    cloudGateEvidenceHash: '3'.repeat(64),
    expiresAt: EXPIRY.toISOString(),
    nonce: 'release-nonce-123456',
  }, NOW);
}

function passingSnapshot(): ReleaseVerificationSnapshot {
  return {
    currentImageDigest: IMAGE,
    currentWorkerRevision: 'blessing-trading-worker-00003-abc',
    currentExecutionMode: 'LIVE',
    currentMainnetLiveApproved: false,
    currentEngineState: 'DISARMED',
    currentOrderSubmissionAttempts: 0,
    currentSecretVersions: { sql: '1', apiKey: '1', apiSecret: '1' },
    preflightPassed: true,
    preflightObservedAt: PREFLIGHT_AT.toISOString(),
    reconciliationStatus: 'IN_SYNC',
    persistenceDurable: true,
    dataConnectCutover: false,
    killSwitchActive: false,
  };
}

describe('mainnet release control boundary', () => {
  it('accepts only a fixed, verified Google service identity', async () => {
    const request = (authorization: string) => ({
      header: () => authorization,
    });
    const verify = async () => ({
      iss: 'https://accounts.google.com',
      aud: 'https://blessing-control-plane.example.run.app',
      email: 'blessing-release-controller@gen-lang-client-0730128480.iam.gserviceaccount.com',
      email_verified: true,
      sub: 'controller-sub',
    });
    const allowed = await authorizeInternalServiceRequest(
      request('Bearer google-oidc-token'),
      {
        audience: 'https://blessing-control-plane.example.run.app',
        allowedServiceAccounts: ['blessing-release-controller@gen-lang-client-0730128480.iam.gserviceaccount.com'],
        verifyIdToken: verify,
      },
    );
    expect(allowed.ok).toBe(true);
    expect(allowed.serviceAccount).toContain('blessing-release-controller@');

    const wrongAudience = await authorizeInternalServiceRequest(
      request('Bearer google-oidc-token'),
      {
        audience: 'https://other.example.run.app',
        allowedServiceAccounts: ['blessing-release-controller@gen-lang-client-0730128480.iam.gserviceaccount.com'],
        verifyIdToken: verify,
      },
    );
    expect(wrongAudience.ok).toBe(false);

    const wrongIssuer = await authorizeInternalServiceRequest(
      request('Bearer google-oidc-token'),
      {
        audience: 'https://blessing-control-plane.example.run.app',
        allowedServiceAccounts: ['blessing-release-controller@gen-lang-client-0730128480.iam.gserviceaccount.com'],
        verifyIdToken: async () => ({
          ...await verify(),
          iss: 'https://evil.example.invalid',
        }),
      },
    );
    expect(wrongIssuer.ok).toBe(false);

    const anonymous = await authorizeInternalServiceRequest(
      request(''),
      { audience: 'https://blessing-control-plane.example.run.app', allowedServiceAccounts: ['controller@example.com'], verifyIdToken: verify },
    );
    expect(anonymous.ok).toBe(false);
  });

  it('keeps release approval at trading_admin and never changes approval itself', () => {
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/release/mainnet/approve' })).toBe('trading_admin');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/release/mainnet/continuation/approve' })).toBe('trading_admin');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/system/continue' })).toBe('trading_admin');
    expect(requiredControlPlaneRole({ method: 'GET', path: '/api/release/mainnet/rc-id' })).toBe('viewer');
  });

  it('rejects stale, mismatched, or already-approved release evidence', () => {
    const release = candidate();
    expect(validateApprovalPrerequisites(release, passingSnapshot(), NOW)).toEqual([]);
    expect(validateApprovalPrerequisites(release, { ...passingSnapshot(), currentImageDigest: IMAGE.replace(/a/g, 'b') }, NOW)).toContain(
      'worker image digest does not match candidate',
    );
    expect(validateApprovalPrerequisites(release, { ...passingSnapshot(), preflightObservedAt: '2026-09-15T23:00:00.000Z' }, NOW)).toContain(
      'Mainnet preflight evidence is missing, failed, or stale',
    );
    expect(validateApprovalPrerequisites(release, { ...passingSnapshot(), currentEngineState: 'ARMED' }, NOW)).toContain(
      'worker must remain DISARMED before approval',
    );
    expect(validateApprovalPrerequisites(
      release,
      { ...passingSnapshot(), currentSecretVersions: { sql: '2', apiKey: '1', apiSecret: '1' } },
      NOW,
    )).toContain('Worker Secret Manager versions do not match the approved release');
  });

  it('provides one-time approval consumption with no execution activation', async () => {
    const store = new InMemoryReleaseStore();
    const release = candidate();
    await store.createCandidate(release);
    const approved = await store.approveCandidate(release.candidateId, 'firebase-admin-uid', passingSnapshot());
    expect(approved.status).toBe('APPROVED');
    const approvalId = approved.approvalId as string;
    expect(await store.getConsumedApproval(approvalId)).toBeNull();
    const consumed = await store.consumeApproval(release.candidateId);
    expect(consumed.approvalId).toBe(approvalId);
    expect(await store.getConsumedApproval(approvalId)).toMatchObject({
      candidateId: release.candidateId,
      approvalId,
      executionMode: 'LIVE',
      symbol: 'ETHUSDC',
      launchPolicy: 'STAGED_FIRST_ORDER',
      orderSubmissionAttempts: 0,
      workerDisarmed: true,
    });
    await expect(store.consumeApproval(release.candidateId)).rejects.toThrow('no consumable approval');
    await expect(store.getConsumedApproval('approval-not-a-uuid')).rejects.toThrow('Invalid release approval id');
  });

  it('allows approved candidate to update preflight and verify before consumption', async () => {
    const store = new InMemoryReleaseStore();
    const release = candidate();
    await store.createCandidate(release);
    const approved = await store.approveCandidate(release.candidateId, 'firebase-admin-uid', passingSnapshot());
    expect(approved.status).toBe('APPROVED');

    // Verification prerequisites pass for APPROVED candidate
    expect(validateApprovalPrerequisites(approved, passingSnapshot(), NOW)).toEqual([]);

    // Preflight can be refreshed while candidate is APPROVED
    const updated = await store.updatePreflight(
      release.candidateId,
      sanitizePreflightEvidence({ preflightPassed: true, checks: [] }),
      'f'.repeat(64),
    );
    expect(updated.status).toBe('APPROVED');
    expect(updated.preflightEvidenceHash).toBe('f'.repeat(64));

    // Re-approval of an already APPROVED candidate is rejected
    await expect(store.approveCandidate(release.candidateId, 'uid', passingSnapshot())).rejects.toThrow(
      'Release candidate is not pending approval',
    );

    // Consumed candidate can also pass verification before promotion
    const consumed = { ...approved, status: 'CONSUMED' as const };
    expect(validateApprovalPrerequisites(consumed, passingSnapshot(), NOW)).toEqual([]);

    // But once worker is already Mainnet approved, replay is blocked
    expect(validateApprovalPrerequisites(consumed, { ...passingSnapshot(), currentMainnetLiveApproved: true }, NOW)).toContain(
      'worker is already Mainnet-approved; approval cannot be replayed',
    );

    // Expired candidate rejects prerequisites
    const expired = { ...approved, status: 'EXPIRED' as const };
    expect(validateApprovalPrerequisites(expired, passingSnapshot(), NOW)).toContain(
      'candidate status is EXPIRED',
    );
  });

  it('redacts and hashes only sanitized preflight evidence', () => {
    const evidence = sanitizePreflightEvidence({
      preflightPassed: true,
      orderSubmissionAttempts: 0,
      orderEndpointAttempts: 0,
      observedAt: PREFLIGHT_AT.toISOString(),
      checks: [{ id: 'AUTH', name: 'Account', required: true, status: 'PASS', message: 'token=secret-value' }],
      apiSecret: 'must-not-be-stored',
    });
    expect(evidence.checks[0].message).toContain('token=<redacted>');
    expect(JSON.stringify(evidence)).not.toContain('secret-value');
    expect(hashEvidence(evidence)).toMatch(/^[0-9a-f]{64}$/);
  });
});
