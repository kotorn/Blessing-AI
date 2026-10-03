import { describe, expect, it } from 'vitest';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { accessPinnedLocalMainnetSecrets, localSecretSourceIdentity, localSecretCrc32c, decodeVerifiedLocalSecretPayload } from '../src/backend/local-secret-manager.js';

it('verifies CRC32C with an independent standard check vector and fails closed on corrupted payloads', () => {
  const raw = Buffer.from('123456789');
  expect(localSecretCrc32c(raw)).toBe(0xe3069283);
  const data = raw.toString('base64');
  expect(decodeVerifiedLocalSecretPayload({ data, dataCrc32c: '3808858755' })).toBe('123456789');
  for (const payload of [
    { data }, { data, dataCrc32c: '1' }, { data, dataCrc32c: 3808858755 },
    { data: '!!!!', dataCrc32c: '0' },
  ]) {
    expect(() => decodeVerifiedLocalSecretPayload(payload)).toThrow('LOCAL_SECRET_MANAGER_RESPONSE_INVALID');
  }
  const bomCredential = Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from('fixture-key')]);
  expect(() => decodeVerifiedLocalSecretPayload({
    data: bomCredential.toString('base64'),
    dataCrc32c: String(localSecretCrc32c(bomCredential)),
  })).toThrow('LOCAL_SECRET_MANAGER_RESPONSE_INVALID');
});
import {
  LOCAL_RELEASE_EXPIRY_MS,
  LOCAL_RELEASE_POLICY_HASH,
  LOCAL_RELEASE_POLICY_VERSION,
  localReleaseBinding,
  newLocalReleaseCandidate,
  type LocalReleaseBinding,
} from '../src/backend/local-release.js';
import {
  InMemoryLocalReleaseStore,
  LOCAL_RELEASE_CANDIDATES_COLLECTION,
} from '../src/backend/local-release-store.js';

const NOW = new Date();

function input() {
  return {
    runId: 'run-local-20260924-001',
    sourceFingerprint: 'a'.repeat(64),
    dependencyFingerprint: 'b'.repeat(64),
    migrationFingerprint: 'c'.repeat(64),
    promotionEvidenceSha256: 'd'.repeat(64),
    apiKeyVersion: '12',
    apiSecretVersion: '34',
    ...localSecretSourceIdentity('test-local-project', '12', '34'),
    nonce: 'local-release-nonce-1234',
  };
}

function expectedBinding(candidate: ReturnType<typeof newLocalReleaseCandidate>): LocalReleaseBinding {
  return localReleaseBinding(candidate);
}

describe('Local release domain', () => {
  it('creates a credential-free LOCAL candidate with fixed policy and one-hour expiry', () => {
    const candidate = newLocalReleaseCandidate(input(), NOW);

    expect(candidate.candidateId).toMatch(/^local-rc-[0-9a-f-]{36}$/i);
    expect(candidate.runtimeTarget).toBe('LOCAL');
    expect(candidate.status).toBe('PENDING_APPROVAL');
    expect(Date.parse(candidate.expiresAt) - Date.parse(candidate.createdAt)).toBe(LOCAL_RELEASE_EXPIRY_MS);
    expect(candidate.policyVersion).toBe(LOCAL_RELEASE_POLICY_VERSION);
    expect(candidate.policyHash).toBe(LOCAL_RELEASE_POLICY_HASH);
    expect(candidate.apiKeyVersion).toBe('12');
    expect(candidate.apiSecretVersion).toBe('34');
    expect(candidate.secretManagerProjectId).toBe('test-local-project');
    expect(candidate.apiKeySecretVersionResource).toBe('projects/test-local-project/secrets/blessing-binance-mainnet-api-key/versions/12');
    expect(candidate.apiSecretSecretVersionResource).toBe('projects/test-local-project/secrets/blessing-binance-mainnet-api-secret/versions/34');
    expect(candidate).not.toHaveProperty('apiSecret');
    expect(candidate).not.toHaveProperty('apiKey');
    expect(candidate).not.toHaveProperty('password');
    expect(candidate).not.toHaveProperty('token');
  });

  it('uses a canonical policy hash and keeps Cloud collection identity separate', () => {
    const expectedHash = createHash('sha256')
      .update(readFileSync(resolve(process.cwd(), 'config/risk/mainnet_local_policy.json')))
      .digest('hex');
    expect(LOCAL_RELEASE_POLICY_HASH).toBe(expectedHash);
    expect(LOCAL_RELEASE_CANDIDATES_COLLECTION).toBe('local_release_candidates');
    expect(LOCAL_RELEASE_CANDIDATES_COLLECTION).not.toBe('release_candidates');
  });

  it('rejects credential-like fields and non-numeric pinned versions', () => {
    expect(() => newLocalReleaseCandidate({
      ...input(),
      apiSecret: 'raw-secret',
    } as never, NOW)).toThrow(/credential-like|unsupported/i);

    expect(() => newLocalReleaseCandidate({
      ...input(),
      apiKeyVersion: 'latest',
    }, NOW)).toThrow(/apiKeyVersion must be numeric/);
    expect(() => newLocalReleaseCandidate({
      ...input(),
      apiKeySecretVersionResource: 'projects/other-project/secrets/blessing-binance-mainnet-api-key/versions/12',
    }, NOW)).toThrow(/secret source resource binding/);
  });

  it('rejects project and full secret resource changes during approval and consumption', async () => {
    const store = new InMemoryLocalReleaseStore();
    const candidate = newLocalReleaseCandidate(input(), NOW);
    await store.createCandidate(candidate);
    const changedProject = localSecretSourceIdentity('other-project', '12', '34');
    await expect(store.approveCandidate(candidate.candidateId, { uid: 'admin-1', role: 'trading_admin' }, {
      ...expectedBinding(candidate), ...changedProject,
    }, NOW)).rejects.toThrow(/binding does not match/);
    await expect(store.approveCandidate(candidate.candidateId, { uid: 'admin-1', role: 'trading_admin' }, {
      ...expectedBinding(candidate), apiSecretSecretVersionResource: 'projects/test-local-project/secrets/other-secret/versions/34',
    }, NOW)).rejects.toThrow(/secret source resource binding/);
    await store.approveCandidate(candidate.candidateId, { uid: 'admin-1', role: 'trading_admin' }, expectedBinding(candidate), NOW);
    await expect(store.consumeApproval(candidate.candidateId, {
      ...expectedBinding(candidate), ...changedProject,
    }, NOW)).rejects.toThrow(/binding does not match/);
  });

  it('rejects changed identity before authenticating to Secret Manager', async () => {
    await expect(accessPinnedLocalMainnetSecrets({
      projectId: 'other-project',
      apiKeyVersion: '12',
      apiSecretVersion: '34',
      identity: localSecretSourceIdentity('test-local-project', '12', '34'),
      campaign: {
        campaignId: 'pilot-2026-test-0001',
        status: 'APPROVED',
        adminUid: 'admin-1',
        approvedByUid: 'admin-1',
        campaignExpiresAt: new Date(Date.now() + 60_000).toISOString(),
        secretManagerProjectId: 'other-project',
        apiKeyVersion: '12',
        apiSecretVersion: '34',
      },
    })).rejects.toThrow('LOCAL_SECRET_SOURCE_BINDING_MISMATCH');
  });

  it('requires a current approved campaign binding before Secret Manager authentication', async () => {
    const identity = localSecretSourceIdentity('test-local-project', '12', '34');
    const auth = { getAccessToken: async () => { throw new Error('must not authenticate'); } };
    await expect(accessPinnedLocalMainnetSecrets({
      projectId: 'test-local-project',
      apiKeyVersion: '12',
      apiSecretVersion: '34',
      identity,
      campaign: {
        campaignId: 'pilot-2026-test-0001',
        status: 'APPROVED',
        adminUid: 'admin-1',
        approvedByUid: 'admin-1',
        campaignExpiresAt: new Date(Date.now() - 1_000).toISOString(),
        secretManagerProjectId: 'test-local-project',
        apiKeyVersion: '12',
        apiSecretVersion: '34',
      },
      auth: auth as never,
    })).rejects.toThrow('LOCAL_PILOT_SECRET_AUTHORIZATION_INVALID');
  });

  it('reads only the bound full resource names with a mocked Secret Manager', async () => {
    const identity = localSecretSourceIdentity('test-local-project', '12', '34');
    const urls: string[] = [];
    const result = await accessPinnedLocalMainnetSecrets({
      projectId: identity.secretManagerProjectId,
      apiKeyVersion: '12',
      apiSecretVersion: '34',
      identity,
      campaign: {
        campaignId: 'pilot-2026-test-0001',
        status: 'APPROVED',
        adminUid: 'admin-1',
        approvedByUid: 'admin-1',
        campaignExpiresAt: new Date(Date.now() + 60_000).toISOString(),
        secretManagerProjectId: 'test-local-project',
        apiKeyVersion: '12',
        apiSecretVersion: '34',
      },
      auth: { getAccessToken: async () => 'fake-test-token' } as never,
      fetcher: (async (url: string | URL | Request) => {
        urls.push(String(url));
        return new Response(JSON.stringify({ payload: { data: Buffer.from('test-value').toString('base64'), dataCrc32c: String(localSecretCrc32c(Buffer.from('test-value'))) } }), {
          status: 200,
        });
      }) as typeof fetch,
    });
    expect(urls).toEqual([
      `https://secretmanager.googleapis.com/v1/${identity.apiKeySecretVersionResource}:access`,
      `https://secretmanager.googleapis.com/v1/${identity.apiSecretSecretVersionResource}:access`,
    ]);
    expect(result.apiKeyVersion).toBe('12');
    expect(result.apiSecretVersion).toBe('34');
  });

  it('reuses the same pinned secrets for an unexpired ACTIVE campaign recovery', async () => {
    const identity = localSecretSourceIdentity('test-local-project', '12', '34');
    const urls: string[] = [];
    await accessPinnedLocalMainnetSecrets({
      projectId: identity.secretManagerProjectId,
      apiKeyVersion: '12',
      apiSecretVersion: '34',
      identity,
      campaign: {
        campaignId: 'pilot-2026-test-0001',
        status: 'ACTIVE',
        adminUid: 'admin-1',
        approvedByUid: 'admin-1',
        campaignExpiresAt: new Date(Date.now() + 60_000).toISOString(),
        secretManagerProjectId: 'test-local-project',
        apiKeyVersion: '12',
        apiSecretVersion: '34',
      },
      auth: { getAccessToken: async () => 'fake-test-token' } as never,
      fetcher: (async (url: string | URL | Request) => {
        urls.push(String(url));
        return new Response(JSON.stringify({ payload: { data: Buffer.from('test-value').toString('base64'), dataCrc32c: String(localSecretCrc32c(Buffer.from('test-value'))) } }), {
          status: 200,
        });
      }) as typeof fetch,
    });
    expect(urls).toEqual([
      `https://secretmanager.googleapis.com/v1/${identity.apiKeySecretVersionResource}:access`,
      `https://secretmanager.googleapis.com/v1/${identity.apiSecretSecretVersionResource}:access`,
    ]);
  });

  it('requires trading_admin and rechecks every expected binding during approval', async () => {
    const store = new InMemoryLocalReleaseStore();
    const candidate = newLocalReleaseCandidate(input(), NOW);
    await store.createCandidate(candidate);

    await expect(store.approveCandidate(
      candidate.candidateId,
      { uid: 'operator-1', role: 'operator' } as never,
      expectedBinding(candidate),
      NOW,
    )).rejects.toThrow(/trading_admin/);

    await expect(store.approveCandidate(
      candidate.candidateId,
      { uid: 'operator-1', role: 'trading_admin' },
      { ...expectedBinding(candidate), runId: 'run-local-20260924-002' },
      NOW,
    )).rejects.toThrow(/binding does not match/);

    await expect(store.approveCandidate(
      candidate.candidateId,
      { uid: 'operator-1', role: 'trading_admin' },
      expectedBinding(candidate),
      NOW,
    )).resolves.toMatchObject({ status: 'APPROVED', runtimeTarget: 'LOCAL' });
  });

  it('performs one-time approval and consumption with replay rejection', async () => {
    const store = new InMemoryLocalReleaseStore();
    const candidate = newLocalReleaseCandidate(input(), NOW);
    await store.createCandidate(candidate);
    const binding = expectedBinding(candidate);

    const approved = await store.approveCandidate(
      candidate.candidateId,
      { uid: 'admin-1', role: 'trading_admin' },
      binding,
      NOW,
    );
    expect(approved.approvalId).toMatch(/^local-approval-[0-9a-f-]{36}$/i);

    const consumed = await store.consumeApproval(candidate.candidateId, binding, NOW);
    expect(consumed).toMatchObject({
      candidateId: candidate.candidateId,
      runtimeTarget: 'LOCAL',
      runId: candidate.runId,
      sourceFingerprint: candidate.sourceFingerprint,
      dependencyFingerprint: candidate.dependencyFingerprint,
      migrationFingerprint: candidate.migrationFingerprint,
      policyVersion: candidate.policyVersion,
      policyHash: candidate.policyHash,
      approvedByUid: 'admin-1',
    });
    expect(JSON.stringify(consumed)).not.toMatch(/raw-secret|password|token/i);
    expect(await store.getConsumedApproval(consumed.approvalId, NOW)).toEqual(consumed);

    await expect(store.consumeApproval(candidate.candidateId, binding, NOW)).rejects.toThrow(/no consumable approval/);
  });

  it('does not approve an expired candidate', async () => {
    const store = new InMemoryLocalReleaseStore();
    const candidate = newLocalReleaseCandidate(input(), NOW);
    await store.createCandidate(candidate);
    await expect(store.approveCandidate(
      candidate.candidateId,
      { uid: 'admin-1', role: 'trading_admin' },
      expectedBinding(candidate),
      new Date(NOW.getTime() + LOCAL_RELEASE_EXPIRY_MS),
    )).rejects.toThrow(/expired/);
  });
});
