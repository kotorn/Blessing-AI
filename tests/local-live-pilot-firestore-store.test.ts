import { describe, expect, it } from 'vitest';
import type { Firestore } from 'firebase-admin/firestore';
import {
  localLivePilotBinding,
  type LocalLivePilotCampaign,
  type LocalLivePilotInput,
} from '../src/backend/local-live-pilot.js';
import { FirestoreLocalLivePilotStore } from '../src/backend/local-live-pilot-firestore-store.js';
import { preparedLocalPilotEvidenceHash } from '../src/backend/local-pilot-preparation.js';

const NOW = new Date('2026-09-26T03:00:00.000Z');
const ADMIN = { uid: 'pilot-admin-1', role: 'trading_admin' as const };
const HASHES = {
  sourceHash: 'a'.repeat(64),
  dependencyHash: 'b'.repeat(64),
  migrationHash: 'c'.repeat(64),
  strategyHash: 'd'.repeat(64),
  riskPolicyHash: 'e'.repeat(64),
};

function input(overrides: Partial<LocalLivePilotInput> = {}): LocalLivePilotInput {
  return {
    campaignId: 'pilot-2026-firestore-0001',
    runId: 'run-2026-firestore-0001',
    adminUid: ADMIN.uid,
    role: ADMIN.role,
    ...HASHES,
    gitSha: 'f'.repeat(40),
    strategyId: 'trend',
    secretManagerProjectId: 'blessing-project-123',
    apiKeyVersion: '1',
    apiSecretVersion: '1',
    managementMode: 'QUICK',
    ...overrides,
  };
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

/** Minimal transaction fake: serialized commits model Firestore's transaction conflict retry. */
function fakeFirestore() {
  const documents = new Map<string, Record<string, unknown>>();
  let tail: Promise<void> = Promise.resolve();
  const db = {
    collection(collectionName: string) {
      return {
        doc(id: string) {
          const key = `${collectionName}/${id}`;
          return {
            async get() {
              const data = documents.get(key);
              return { exists: Boolean(data), data: () => data ? clone(data) : undefined };
            },
          };
        },
      };
    },
    async runTransaction<T>(operation: (transaction: {
      get: (ref: { key?: string }) => Promise<{ exists: boolean; data: () => Record<string, unknown> | undefined }>;
      create: (ref: { key?: string }, value: unknown) => void;
      update: (ref: { key?: string }, value: unknown) => void;
    }) => Promise<T>): Promise<T> {
      let release!: () => void;
      const turn = new Promise<void>((resolve) => { release = resolve; });
      const previous = tail;
      tail = turn;
      await previous;
      const writes: Array<{ key: string; value: Record<string, unknown>; createOnly: boolean }> = [];
      const transaction = {
        async get(ref: { key?: string }) {
          const key = ref.key || '';
          const data = documents.get(key);
          return { exists: Boolean(data), data: () => data ? clone(data) : undefined };
        },
        create(ref: { key?: string }, value: unknown) {
          writes.push({ key: ref.key || '', value: clone(value as Record<string, unknown>), createOnly: true });
        },
        update(ref: { key?: string }, value: unknown) {
          writes.push({ key: ref.key || '', value: clone(value as Record<string, unknown>), createOnly: false });
        },
      };
      try {
        const result = await operation(transaction);
        for (const write of writes) {
          if (write.createOnly && documents.has(write.key)) throw new Error('already exists');
          if (!write.createOnly && !documents.has(write.key)) throw new Error('missing document');
          documents.set(write.key, clone(write.value));
        }
        return result;
      } finally {
        release();
      }
    },
  };

  // Firestore DocumentReference includes its path; attach the path expected by the fake transaction.
  const originalCollection = db.collection.bind(db);
  db.collection = ((collectionName: string) => {
    const collection = originalCollection(collectionName);
    const originalDoc = collection.doc.bind(collection);
    collection.doc = ((id: string) => Object.assign(originalDoc(id), { key: `${collectionName}/${id}` })) as typeof collection.doc;
    return collection;
  }) as typeof db.collection;

  return { db: db as unknown as Firestore, documents };
}

describe('FirestoreLocalLivePilotStore', () => {
  it('persists only the strict campaign schema and recovers approval across store instances', async () => {
    const { db, documents } = fakeFirestore();
    const first = new FirestoreLocalLivePilotStore(db);
    const pending = await first.create(input(), NOW);
    const stored = [...documents.values()][0];
    expect(stored).toEqual(pending);
    expect(JSON.stringify(stored)).not.toMatch(/"apiKey"\s*:|"apiSecret"\s*:|password|credential|token/i);
    await expect(first.create(input(), new Date(NOW.getTime() + 1))).resolves.toEqual(pending);

    const expected = localLivePilotBinding(pending);
    const recoveredStore = new FirestoreLocalLivePilotStore(db);
    await expect(recoveredStore.get(pending.campaignId)).resolves.toEqual(pending);
    const approved = await recoveredStore.approve(pending.campaignId, ADMIN, expected, NOW);
    const recoveredAgain = new FirestoreLocalLivePilotStore(db);
    await expect(recoveredAgain.approve(pending.campaignId, ADMIN, expected, new Date(NOW.getTime() + 1000)))
      .resolves.toEqual(approved);
    expect(approved.version).toBe(2);
  });

  it('activates exactly once and restart recovery preserves active risk eligibility', async () => {
    const { db } = fakeFirestore();
    const initial = new FirestoreLocalLivePilotStore(db);
    const pending = await initial.create(input(), NOW);
    const expected = localLivePilotBinding(pending);
    await initial.approve(pending.campaignId, ADMIN, expected, NOW);

    const resumed = new FirestoreLocalLivePilotStore(db);
    const active = await resumed.activate(pending.campaignId, expected, new Date(NOW.getTime() + 100));
    const afterRestart = new FirestoreLocalLivePilotStore(db);
    await expect(afterRestart.get(pending.campaignId)).resolves.toEqual(active);
    await expect(afterRestart.activate(pending.campaignId, expected, new Date(NOW.getTime() + 200)))
      .resolves.toEqual(active);
    await expect(afterRestart.canIncreaseRisk(pending.campaignId, new Date(NOW.getTime() + 200))).resolves.toBe(true);
  });

  it('persists preparation receipts and permits safe DISARMED retries after the durable ACTIVE-before-ARM transition', async () => {
    const { db } = fakeFirestore();
    const store = new FirestoreLocalLivePilotStore(db);
    const pending = await store.create(input(), NOW);
    const binding = localLivePilotBinding(pending);
    await store.approve(pending.campaignId, ADMIN, binding, NOW);
    const preflightEvidence = {
      observedAt: NOW.toISOString(), executionMode: 'LIVE', engineState: 'DISARMED',
      mainnetLiveApproved: true, preflightOnly: true, preflightPassed: true,
      canArm: false, orderSubmissionAttempts: 0, orderEndpointAttempts: 0, checks: [],
    };
    const receipt = {
      campaignId: pending.campaignId,
      runId: pending.runId,
      sourceFingerprint: pending.sourceHash,
      approvalId: `local-approval-${pending.campaignId.slice('pilot-'.length)}`,
      workerGeneration: 1,
      supervisorInstanceId: '12345678-1234-4234-8234-123456789abc',
      preflightEvidence,
      preflightSha256: preparedLocalPilotEvidenceHash(preflightEvidence),
      preflightObservedAt: NOW.toISOString(),
      preparedAt: NOW.toISOString(),
    };
    const prepared = await store.recordPreparation(pending.campaignId, binding, receipt, NOW);
    expect(prepared.preparation).toEqual(receipt);
    await expect(new FirestoreLocalLivePilotStore(db).get(pending.campaignId)).resolves.toEqual(prepared);
    const active = await store.activate(pending.campaignId, binding, NOW);
    const retryReceipt = { ...receipt, workerGeneration: 2 };
    const retried = await store.recordPreparation(pending.campaignId, binding, retryReceipt, NOW);
    expect(active.preparation).toEqual(receipt);
    expect(retried.preparation).toEqual(retryReceipt);
  });

  it('uses transactional compare-and-set for concurrent approval and activation retries', async () => {
    const { db } = fakeFirestore();
    const store = new FirestoreLocalLivePilotStore(db);
    const pending = await store.create(input(), NOW);
    const expected = localLivePilotBinding(pending);
    const approved = await Promise.all([
      store.approve(pending.campaignId, ADMIN, expected, NOW),
      store.approve(pending.campaignId, ADMIN, expected, NOW),
    ]);
    expect(approved[0]).toEqual(approved[1]);
    expect(approved[0].version).toBe(2);
    const active = await Promise.all([
      store.activate(pending.campaignId, expected, NOW),
      store.activate(pending.campaignId, expected, NOW),
    ]);
    expect(active[0]).toEqual(active[1]);
    expect(active[0].version).toBe(3);
  });

  it('rejects malformed ids, cross-campaign bindings, and extra binding fields', async () => {
    const { db } = fakeFirestore();
    const store = new FirestoreLocalLivePilotStore(db);
    await expect(store.get('../pilot')).rejects.toThrow(/campaign id/i);
    const campaign = await store.create(input(), NOW);
    const expected = localLivePilotBinding(campaign);
    await expect(store.approve(campaign.campaignId, ADMIN, { ...expected, strategyHash: HASHES.sourceHash }, NOW))
      .rejects.toThrow(/binding mismatch/i);
    await expect(store.activate(campaign.campaignId, { ...expected, credential: 'must-not-persist' } as never, NOW))
      .rejects.toThrow(/unsupported fields/i);
  });

  it('rejects persisted document id, status, risk binding, or secret-field tampering and fails risk checks closed', async () => {
    const { db, documents } = fakeFirestore();
    const store = new FirestoreLocalLivePilotStore(db);
    const campaign = await store.create(input(), NOW);
    const [key, original] = [...documents.entries()][0];

    documents.set(key, { ...original, campaignId: 'pilot-2026-firestore-0002' });
    await expect(store.get(campaign.campaignId)).rejects.toThrow(/document id/i);
    await expect(store.canIncreaseRisk(campaign.campaignId, NOW)).resolves.toBe(false);

    documents.set(key, { ...original, status: 'ARMED' });
    await expect(store.get(campaign.campaignId)).rejects.toThrow(/invalid/i);

    documents.set(key, { ...original, limits: { ...(original.limits as object), maxLeverage: 100 } });
    await expect(store.get(campaign.campaignId)).rejects.toThrow(/policy|limits/i);

    documents.set(key, { ...original, apiSecret: 'must-never-be-stored' });
    await expect(store.get(campaign.campaignId)).rejects.toThrow(/unsupported fields/i);
  });

  it('persists close-only/completion transitions idempotently without restoring risk access', async () => {
    const { db } = fakeFirestore();
    const store = new FirestoreLocalLivePilotStore(db);
    const pending = await store.create(input(), NOW);
    const expected = localLivePilotBinding(pending);
    await store.approve(pending.campaignId, ADMIN, expected, NOW);
    await store.activate(pending.campaignId, expected, NOW);
    const closeOnly = await store.enterCloseOnly(pending.campaignId, expected, NOW);
    await expect(store.enterCloseOnly(pending.campaignId, expected, NOW)).resolves.toEqual(closeOnly);
    await expect(store.canIncreaseRisk(pending.campaignId, NOW)).resolves.toBe(false);
    const completed = await store.complete(pending.campaignId, expected, NOW);
    await expect(store.complete(pending.campaignId, expected, NOW)).resolves.toEqual(completed);
    expect(completed.status).toBe('COMPLETED');
  });

  it('supports and recovers revocation before activation without inventing activation metadata', async () => {
    const { db } = fakeFirestore();
    const store = new FirestoreLocalLivePilotStore(db);
    const pending = await store.create(input(), NOW);
    const expected = localLivePilotBinding(pending);
    await store.approve(pending.campaignId, ADMIN, expected, NOW);
    const revoked = await store.revoke(pending.campaignId, ADMIN, expected, NOW);
    expect(revoked.status).toBe('REVOKED');
    expect(revoked.activatedAt).toBeUndefined();
    await expect(new FirestoreLocalLivePilotStore(db).get(pending.campaignId)).resolves.toEqual(revoked);
    await expect(store.canIncreaseRisk(pending.campaignId, NOW)).resolves.toBe(false);
  });

  it('rejects unauthorized revocation without changing the persisted campaign', async () => {
    const { db, documents } = fakeFirestore();
    const store = new FirestoreLocalLivePilotStore(db);
    const pending = await store.create(input(), NOW);
    const expected = localLivePilotBinding(pending);
    const approved = await store.approve(pending.campaignId, ADMIN, expected, NOW);
    const before = [...documents.values()][0];

    await expect(store.revoke(pending.campaignId, { uid: 'other-admin', role: 'trading_admin' }, expected, NOW))
      .rejects.toThrow(/Revoker UID/i);

    await expect(store.get(pending.campaignId)).resolves.toEqual(approved);
    expect([...documents.values()][0]).toEqual(before);
  });

  it('rejects corrupt lifecycle documents instead of treating them as recoverable', async () => {
    const { db, documents } = fakeFirestore();
    const store = new FirestoreLocalLivePilotStore(db);
    const campaign = await store.create(input(), NOW);
    const key = [...documents.keys()][0];
    const corrupted = { ...[...documents.values()][0], status: 'ACTIVE' } as unknown as LocalLivePilotCampaign;
    documents.set(key, corrupted as unknown as Record<string, unknown>);
    await expect(store.get(campaign.campaignId)).rejects.toThrow(/approved campaign metadata|activation metadata/i);
    await expect(store.canIncreaseRisk(campaign.campaignId, NOW)).resolves.toBe(false);
  });
});
