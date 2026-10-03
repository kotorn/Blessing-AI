import type { Firestore } from 'firebase-admin/firestore';
import type { PilotAcceptanceAudit, PilotAcceptanceBinding, PilotOfflineCheck } from './local-pilot-acceptance-runner.js';

const COLLECTION = 'local_pilot_acceptance_runs';
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;
const HASH = /^[a-f0-9]{64}$/;
const CHECKS = new Set(['TYPESCRIPT_TESTS', 'LINT', 'BUILD']);
const BINDING_KEYS = ['gitSha', 'sourceSha256', 'dependencySha256', 'migrationSha256', 'policySha256'] as const;
const validActor = (value: unknown): value is string => typeof value === 'string'
  && value.length > 0 && value.length <= 128
  && [...value].every((character) => character.charCodeAt(0) >= 32 && character.charCodeAt(0) !== 127);

function validBinding(value: unknown): value is PilotAcceptanceBinding {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const fields = value as Record<string, unknown>;
  return Object.keys(fields).length === BINDING_KEYS.length && BINDING_KEYS.every((key) =>
    typeof fields[key] === 'string' && (key === 'gitSha' ? /^[a-f0-9]{40}$/ : HASH).test(fields[key]));
}

/** Admin-SDK only: browser-produced receipt JSON must never populate this store. */
export class FirestorePilotAcceptanceAudit implements PilotAcceptanceAudit {
  constructor(private readonly firestore: Firestore, private readonly serverInstanceId: string,
    private readonly requestedByUid?: string) {
    if (!UUID.test(serverInstanceId)) throw new Error('LOCAL_PILOT_AUDIT_INSTANCE_INVALID');
  }

  async begin(id: string, check: PilotOfflineCheck, binding: PilotAcceptanceBinding): Promise<void> {
    if (!validActor(this.requestedByUid)) throw new Error('LOCAL_PILOT_AUDIT_ACTOR_INVALID');
    if (!UUID.test(id) || !CHECKS.has(check) || !validBinding(binding)) {
      throw new Error('LOCAL_PILOT_AUDIT_BINDING_INVALID');
    }
    const reference = this.firestore.collection(COLLECTION).doc(id);
    const startedAt = new Date().toISOString();
    await this.firestore.runTransaction(async (transaction) => {
      if ((await transaction.get(reference)).exists) throw new Error('LOCAL_PILOT_AUDIT_RUN_EXISTS');
      transaction.create(reference, {
        schemaVersion: 1, runtimeTarget: 'LOCAL', runId: id, serverInstanceId: this.serverInstanceId,
        requestedByUid: this.requestedByUid,
        check, binding: structuredClone(binding), status: 'RUNNING', version: 1, startedAt,
      });
    });
  }

  async read(id: string, expected: PilotAcceptanceBinding, now = new Date()): Promise<{
    runId: string; status: 'PASS' | 'FAIL' | 'RUNNING' | 'UNKNOWN'; check?: PilotOfflineCheck;
  }> {
    if (!UUID.test(id)) throw new Error('LOCAL_PILOT_AUDIT_RUN_INVALID');
    if (!validActor(this.requestedByUid)) throw new Error('LOCAL_PILOT_AUDIT_ACTOR_INVALID');
    if (!validBinding(expected)) throw new Error('LOCAL_PILOT_AUDIT_BINDING_INVALID');
    const snapshot = await this.firestore.collection(COLLECTION).doc(id).get();
    const value = snapshot.data();
    const unknown = { runId: id, status: 'UNKNOWN' as const };
    if (!snapshot.exists || !value || value.schemaVersion !== 1 || value.runtimeTarget !== 'LOCAL'
      || value.runId !== id || !UUID.test(value.serverInstanceId || '') || !CHECKS.has(value.check)
      || !validActor(value.requestedByUid)
      || value.requestedByUid !== this.requestedByUid
      || !validBinding(value.binding)
      || Object.entries(expected).some(([key, data]) => value.binding[key] !== data)) return unknown;
    const began = typeof value.startedAt === 'string' ? Date.parse(value.startedAt) : NaN;
    if (!Number.isFinite(now.getTime()) || !Number.isFinite(began) || began > now.getTime() + 2000) return unknown;
    if (value.status === 'RUNNING' && value.version === 1) {
      return now.getTime() - began <= 15 * 60 * 1000
        ? { runId: id, status: 'RUNNING', check: value.check } : unknown;
    }
    const observed = typeof value.observedAt === 'string' ? Date.parse(value.observedAt) : NaN;
    if (!['PASS', 'FAIL'].includes(value.status) || value.version !== 2 || !HASH.test(value.outputSha256 || '')
      || !Number.isFinite(observed) || observed < began || observed > now.getTime() + 2000
      || now.getTime() - observed > 24 * 60 * 60 * 1000
      || typeof value.reason !== 'string' || !/^LOCAL_PILOT_[A-Z_]{1,96}$/.test(value.reason)
      || (value.status === 'PASS' && value.reason !== 'LOCAL_PILOT_CHECK_COMPLETED')) return unknown;
    return { runId: id, status: value.status, check: value.check };
  }

  /** Bounded recovery lookup after an HTTP response is lost. Never returns raw receipts. */
  async findRuns(expected: PilotAcceptanceBinding, now = new Date()) {
    if (!validActor(this.requestedByUid)) throw new Error('LOCAL_PILOT_AUDIT_ACTOR_INVALID');
    if (!validBinding(expected)) throw new Error('LOCAL_PILOT_AUDIT_BINDING_INVALID');
    const snapshot = await this.firestore.collection(COLLECTION)
      .where('requestedByUid', '==', this.requestedByUid)
      .where('binding.gitSha', '==', expected.gitSha).limit(20).get();
    const runs = [];
    for (const document of snapshot.docs) {
      if (!UUID.test(document.id)) continue;
      const result = await this.read(document.id, expected, now);
      if (result.status !== 'UNKNOWN') runs.push(result);
    }
    return { runs, bounded: true as const, exhaustive: false as const };
  }

  async finish(id: string, result: Parameters<PilotAcceptanceAudit['finish']>[1]): Promise<void> {
    const observed = Date.parse(result.observedAt);
    if (!UUID.test(id) || !['PASS', 'FAIL'].includes(result.status) || !HASH.test(result.outputSha256)
      || !Number.isFinite(observed) || observed > Date.now() + 2000
      || !/^LOCAL_PILOT_[A-Z_]{1,96}$/.test(result.reason)) {
      throw new Error('LOCAL_PILOT_AUDIT_RESULT_INVALID');
    }
    const reference = this.firestore.collection(COLLECTION).doc(id);
    await this.firestore.runTransaction(async (transaction) => {
      const snapshot = await transaction.get(reference);
      const current = snapshot.data();
      if (!snapshot.exists || !current || current.serverInstanceId !== this.serverInstanceId
        || current.schemaVersion !== 1 || !CHECKS.has(current.check) || !validBinding(current.binding)
        || !validActor(this.requestedByUid) || current.requestedByUid !== this.requestedByUid
        || current.runtimeTarget !== 'LOCAL' || current.runId !== id
        || current.status !== 'RUNNING' || current.version !== 1
        || observed < Date.parse(current.startedAt)
        || !Number.isFinite(Date.parse(current.startedAt))) {
        throw new Error('LOCAL_PILOT_AUDIT_CAS_FAILED');
      }
      transaction.update(reference, { ...result, version: 2 });
    });
  }
}
