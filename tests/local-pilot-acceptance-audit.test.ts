import type { Firestore } from 'firebase-admin/firestore';
import { expect, it } from 'vitest';
import { FirestorePilotAcceptanceAudit } from '../src/backend/local-pilot-acceptance-audit.js';

const instance = '11111111-1111-4111-8111-111111111111';
const run = '22222222-2222-4222-8222-222222222222';
function fixture() {
  let row: Record<string, unknown> | undefined;
  const transaction = {
    get: async () => ({ exists: Boolean(row), data: () => row }),
    create: (_ref: unknown, data: Record<string, unknown>) => { row = structuredClone(data); },
    update: (_ref: unknown, data: Record<string, unknown>) => { row = { ...row, ...data }; },
  };
  const query = {
    where: (_field: string, _operator: string, _value: string) => query,
    limit: (_maximum: number) => query,
    get: async () => ({ docs: row ? [{ id: run }] : [] }),
  };
  const database = {
    collection: () => ({ ...query, doc: () => ({ get: transaction.get }) }),
    runTransaction: async (action: (tx: typeof transaction) => Promise<void>) => action(transaction),
  } as unknown as Firestore;
  return { store: new FirestorePilotAcceptanceAudit(database, instance, 'verified-admin'), database, read: () => row };
}
const binding = { gitSha: 'a'.repeat(40), sourceSha256: 'b'.repeat(64), dependencySha256: 'c'.repeat(64),
  migrationSha256: 'd'.repeat(64), policySha256: 'e'.repeat(64) };
const result = () => ({ status: 'PASS' as const, outputSha256: 'f'.repeat(64),
  reason: 'LOCAL_PILOT_CHECK_COMPLETED', observedAt: new Date().toISOString() });

it('creates immutable binding and finishes only once', async () => {
  const f = fixture(); await f.store.begin(run, 'LINT', binding);
  expect(f.read()?.status).toBe('RUNNING');
  await f.store.finish(run, result());
  expect(f.read()?.version).toBe(2);
  expect(f.read()?.binding).toEqual(binding);
  expect(f.read()?.requestedByUid).toBe('verified-admin');
  await expect(f.store.finish(run, result())).rejects.toThrow('CAS_FAILED');
  await expect(f.store.begin(run, 'LINT', binding)).rejects.toThrow('RUN_EXISTS');
});

it('recovers run identifiers only for the exact actor and fingerprint', async () => {
  const f = fixture();
  await f.store.begin(run, 'LINT', binding);
  await f.store.finish(run, result());
  expect(await f.store.findRuns(binding)).toMatchObject({
    bounded: true, exhaustive: false, runs: [{ runId: run, status: 'PASS', check: 'LINT' }],
  });
  expect((await new FirestorePilotAcceptanceAudit(f.database, instance, 'another-admin').findRuns(binding)).runs).toEqual([]);
  expect((await f.store.findRuns({ ...binding, policySha256: 'f'.repeat(64) })).runs).toEqual([]);
});

it('cannot begin without verified actor or finish under a different actor', async () => {
  const f = fixture();
  await expect(new FirestorePilotAcceptanceAudit(f.database, instance).begin(run, 'LINT', binding))
    .rejects.toThrow('ACTOR_INVALID');
  expect(f.read()).toBeUndefined();
  await f.store.begin(run, 'LINT', binding);
  await expect(new FirestorePilotAcceptanceAudit(f.database, instance, 'another-admin').finish(run, result()))
    .rejects.toThrow('CAS_FAILED');
  expect(f.read()?.status).toBe('RUNNING');
  expect((await new FirestorePilotAcceptanceAudit(f.database, instance, 'another-admin')
    .read(run, binding)).status).toBe('UNKNOWN');
});
it('rejects results without a reservation or from another server instance', async () => {
  const f = fixture(); await expect(f.store.finish(run, result())).rejects.toThrow('CAS_FAILED');
  await f.store.begin(run, 'BUILD', binding);
  const other = new FirestorePilotAcceptanceAudit(f.database, '33333333-3333-4333-8333-333333333333');
  await expect(other.finish(run, result())).rejects.toThrow('CAS_FAILED');
  expect(f.read()?.status).toBe('RUNNING');
});
it('rejects injected checks and incomplete binding', async () => {
  const f = fixture();
  await expect(f.store.begin(run, 'ORDER' as 'BUILD', binding)).rejects.toThrow('BINDING_INVALID');
  await expect(f.store.begin(run, 'BUILD', { ...binding, policySha256: '' })).rejects.toThrow('BINDING_INVALID');
  expect(f.read()).toBeUndefined();
});

it('reads only fresh results for the exact fingerprint', async () => {
  const f = fixture();
  expect((await f.store.read(run, binding)).status).toBe('UNKNOWN');
  await f.store.begin(run, 'LINT', binding);
  expect((await f.store.read(run, binding)).status).toBe('RUNNING');
  await f.store.finish(run, result());
  expect((await f.store.read(run, binding)).status).toBe('PASS');
  expect((await f.store.read(run, { ...binding, gitSha: 'f'.repeat(40) })).status).toBe('UNKNOWN');
  expect((await f.store.read(run, binding, new Date(Date.now() + 25 * 60 * 60 * 1000))).status).toBe('UNKNOWN');
});

it('rejects partial or extended expected bindings instead of accepting a subset match', async () => {
  const f = fixture();
  await f.store.begin(run, 'LINT', binding);
  await f.store.finish(run, result());
  await expect(f.store.read(run, {} as typeof binding)).rejects.toThrow('BINDING_INVALID');
  await expect(f.store.read(run, { ...binding, extra: 'unbound' } as typeof binding)).rejects.toThrow('BINDING_INVALID');
  await expect(f.store.begin('44444444-4444-4444-8444-444444444444', 'BUILD',
    { ...binding, extra: 'unbound' } as typeof binding)).rejects.toThrow('BINDING_INVALID');
  expect((await f.store.read(run, binding)).status).toBe('PASS');
});
