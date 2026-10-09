import type { Firestore } from 'firebase-admin/firestore';
import {
  assertLocalLivePilotBinding,
  LOCAL_LIVE_PILOT_COLLECTION,
  LOCAL_LIVE_PILOT_DURATION_MS,
  localLivePilotBinding,
  localLivePilotCanIncreaseRisk,
  newLocalLivePilotCampaign,
  validateLocalLivePilotActor,
  validateLocalLivePilotCampaign,
  type LocalLivePilotActor,
  type LocalLivePilotCampaign,
  type LocalLivePilotExpectedBinding,
  type LocalLivePilotInput,
} from './local-live-pilot.js';
import type { LocalLivePilotStore } from './local-live-pilot-store.js';
import { preparedLocalPilotEvidenceHash, type PreparedLocalPilot } from './local-pilot-preparation.js';

const CAMPAIGN_ID_RE = /^pilot-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$/;
const HASH_RE = /^[a-f0-9]{64}$/i;
const UID_RE = /^[A-Za-z0-9:_-]{1,256}$/;
const NONCE_RE = /^[a-f0-9]{48}$/;

const DOCUMENT_FIELDS = [
  'runtimeTarget', 'campaignId', 'runId', 'adminUid', 'sourceHash', 'gitSha', 'dependencyHash', 'migrationHash',
  'strategyHash', 'riskPolicyHash', 'strategyId', 'secretManagerProjectId', 'apiKeyVersion',
  'apiSecretVersion', 'nonce', 'symbol', 'market', 'managementMode', 'limits',
  'status', 'version', 'requestedAt', 'pendingExpiresAt', 'approvedByUid', 'approvedAt',
  'campaignExpiresAt', 'activatedAt', 'closeOnlyAt', 'completedAt', 'revokedAt', 'preparation', 'updatedAt',
] as const;

const BINDING_FIELDS = [
  'runtimeTarget', 'campaignId', 'runId', 'adminUid', 'sourceHash', 'gitSha', 'dependencyHash', 'migrationHash',
  'strategyHash', 'riskPolicyHash', 'strategyId', 'secretManagerProjectId', 'apiKeyVersion',
  'apiSecretVersion', 'nonce', 'symbol', 'market', 'managementMode', 'limits',
] as const;

function validateCampaignId(campaignId: string): string {
  if (typeof campaignId !== 'string' || !CAMPAIGN_ID_RE.test(campaignId)) {
    throw new Error('Invalid Local live pilot campaign id');
  }
  return campaignId;
}

function assertDate(now: Date): void {
  if (!(now instanceof Date) || !Number.isFinite(now.getTime())) throw new Error('Local live pilot operation time is invalid');
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function assertExactKeys(value: unknown, allowed: readonly string[], label: string): asserts value is Record<string, unknown> {
  if (!isRecord(value)) throw new Error(`${label} must be an object`);
  const allowedSet = new Set(allowed);
  if (Object.keys(value).some((key) => !allowedSet.has(key))) throw new Error(`${label} contains unsupported fields`);
}

function sameKeys(actual: readonly string[], expected: readonly string[]): boolean {
  return actual.length === expected.length && expected.every((key) => actual.includes(key));
}

function validateBindingShape(expected: LocalLivePilotExpectedBinding, campaignId: string): void {
  assertExactKeys(expected, BINDING_FIELDS, 'Local live pilot binding');
  if (!sameKeys(Object.keys(expected), BINDING_FIELDS)) throw new Error('Local live pilot binding is incomplete');
  if (expected.runtimeTarget !== 'LOCAL' || expected.campaignId !== campaignId) throw new Error('Local live pilot binding mismatch');
  if (!UID_RE.test(expected.adminUid) || !NONCE_RE.test(expected.nonce)) throw new Error('Local live pilot binding identity is invalid');
  for (const key of ['sourceHash', 'dependencyHash', 'migrationHash', 'strategyHash', 'riskPolicyHash'] as const) {
    if (!HASH_RE.test(expected[key])) throw new Error(`Local live pilot ${key} binding is invalid`);
  }
  if (!/^[a-f0-9]{40,64}$/i.test(expected.gitSha)) throw new Error('Local live pilot commit binding is invalid');
  if (expected.symbol !== 'ETHUSDC' || expected.market !== 'USD_M_FUTURES') throw new Error('Local live pilot market binding is invalid');
  if (expected.managementMode !== 'QUICK') throw new Error('first Local live pilot management binding must be QUICK');
  if (!/^run-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$/.test(expected.runId)) throw new Error('Local live pilot run binding is invalid');
  if (expected.strategyId !== 'grid') throw new Error('Local live pilot strategy binding is invalid');
  if (!/^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(expected.secretManagerProjectId)
    || !/^[1-9][0-9]*$/.test(expected.apiKeyVersion)
    || !/^[1-9][0-9]*$/.test(expected.apiSecretVersion)) {
    throw new Error('Local live pilot Secret Manager binding is invalid');
  }
  assertExactKeys(expected.limits, [
    'positionNotionalUsdc', 'orderNotionalUsdc', 'entryTargetNotionalUsdc',
    'executionRiskBufferUsdc', 'totalExposureUsdc', 'plannedRiskUsdc',
    'campaignDrawdownUsdc', 'maxLeverage', 'quickTargetNetUsdc', 'quickMaxHoldMs',
    'sessionEntryCutoffSeconds', 'sessionCloseAfterSeconds', 'sessionEndSeconds',
  ], 'Local live pilot limits binding');
  if (!sameKeys(Object.keys(expected.limits), [
    'positionNotionalUsdc', 'orderNotionalUsdc', 'entryTargetNotionalUsdc',
    'executionRiskBufferUsdc', 'totalExposureUsdc', 'plannedRiskUsdc',
    'campaignDrawdownUsdc', 'maxLeverage', 'quickTargetNetUsdc', 'quickMaxHoldMs',
    'sessionEntryCutoffSeconds', 'sessionCloseAfterSeconds', 'sessionEndSeconds',
  ])) throw new Error('Local live pilot limits binding is incomplete');
}

function validateStoredCampaign(value: unknown, documentId: string, now: Date): LocalLivePilotCampaign {
  assertExactKeys(value, DOCUMENT_FIELDS, 'Stored Local live pilot campaign');
  const data = value as unknown as LocalLivePilotCampaign;
  if (!sameKeys(Object.keys(data).filter((key) => data[key as keyof LocalLivePilotCampaign] !== undefined), [
    ...BINDING_FIELDS,
    'status', 'version', 'requestedAt', 'pendingExpiresAt', 'updatedAt',
    ...(data.approvedByUid === undefined ? [] : ['approvedByUid']),
    ...(data.approvedAt === undefined ? [] : ['approvedAt']),
    ...(data.campaignExpiresAt === undefined ? [] : ['campaignExpiresAt']),
    ...(data.activatedAt === undefined ? [] : ['activatedAt']),
    ...(data.closeOnlyAt === undefined ? [] : ['closeOnlyAt']),
    ...(data.completedAt === undefined ? [] : ['completedAt']),
    ...(data.revokedAt === undefined ? [] : ['revokedAt']),
    ...(data.preparation === undefined ? [] : ['preparation']),
  ])) throw new Error('Stored Local live pilot campaign fields are incomplete or invalid');
  if (documentId !== data.campaignId) throw new Error('Stored Local live pilot document id does not match campaign id');

  // Validate pending requests at their creation instant so an expired request can
  // still be read and transitioned to EXPIRED without weakening its schema checks.
  const validationTime = data.status === 'PENDING_APPROVAL'
    ? new Date(Date.parse(data.requestedAt))
    : now;
  const errors = validateLocalLivePilotCampaign(data, validationTime);
  if (errors.length) throw new Error(`Stored Local live pilot campaign is invalid: ${errors.join('; ')}`);

  const timestamps: Array<keyof LocalLivePilotCampaign> = [
    'requestedAt', 'pendingExpiresAt', 'updatedAt', 'approvedAt', 'campaignExpiresAt',
    'activatedAt', 'closeOnlyAt', 'completedAt', 'revokedAt',
  ];
  for (const field of timestamps) {
    const timestamp = data[field];
    if (timestamp !== undefined && (typeof timestamp !== 'string' || !Number.isFinite(Date.parse(timestamp)))) {
      throw new Error(`Stored Local live pilot ${field} is invalid`);
    }
  }
  if (data.status === 'ACTIVE' && !data.activatedAt) throw new Error('Stored Local live pilot activation metadata is incomplete');
  if (data.status === 'CLOSE_ONLY' && !data.closeOnlyAt) throw new Error('Stored Local live pilot close-only metadata is incomplete');
  if (data.status === 'COMPLETED' && !data.completedAt) throw new Error('Stored Local live pilot completion metadata is incomplete');
  if (data.status === 'REVOKED' && !data.revokedAt) throw new Error('Stored Local live pilot revocation metadata is incomplete');
  if (data.preparation !== undefined) validatePreparation(data.preparation, data);

  const has = (field: keyof LocalLivePilotCampaign) => data[field] !== undefined;
  if (data.status === 'PENDING_APPROVAL'
    && ['approvedAt', 'campaignExpiresAt', 'activatedAt', 'closeOnlyAt', 'completedAt', 'revokedAt'].some((field) => has(field as keyof LocalLivePilotCampaign))) {
    throw new Error('Stored pending Local live pilot has lifecycle metadata');
  }
  if (data.status === 'APPROVED' && ['activatedAt', 'closeOnlyAt', 'completedAt', 'revokedAt'].some((field) => has(field as keyof LocalLivePilotCampaign))) {
    throw new Error('Stored approved Local live pilot has conflicting lifecycle metadata');
  }
  if (data.status === 'ACTIVE' && ['closeOnlyAt', 'completedAt', 'revokedAt'].some((field) => has(field as keyof LocalLivePilotCampaign))) {
    throw new Error('Stored active Local live pilot has conflicting lifecycle metadata');
  }
  if (data.status === 'CLOSE_ONLY' && ['completedAt', 'revokedAt'].some((field) => has(field as keyof LocalLivePilotCampaign))) {
    throw new Error('Stored close-only Local live pilot has conflicting lifecycle metadata');
  }
  if (data.status === 'COMPLETED' && has('revokedAt')) throw new Error('Stored completed Local live pilot has conflicting lifecycle metadata');
  if (data.status === 'REVOKED' && has('completedAt')) throw new Error('Stored revoked Local live pilot has conflicting lifecycle metadata');
  return structuredClone(data);
}

function validatePreparation(value: unknown, campaign: LocalLivePilotCampaign): asserts value is PreparedLocalPilot {
  assertExactKeys(value, ['campaignId', 'runId', 'sourceFingerprint', 'approvalId', 'workerGeneration', 'supervisorInstanceId', 'preflightEvidence', 'preflightSha256', 'preflightObservedAt', 'preparedAt'], 'Stored Local live pilot preparation');
  const receipt = value as unknown as PreparedLocalPilot;
  if (receipt.campaignId !== campaign.campaignId || receipt.runId !== campaign.runId
    || receipt.sourceFingerprint !== campaign.sourceHash
    || receipt.approvalId !== `local-approval-${campaign.campaignId.slice('pilot-'.length)}`
    || !Number.isSafeInteger(receipt.workerGeneration) || receipt.workerGeneration < 1
    || !/^[A-Za-z0-9-]{36}$/.test(receipt.supervisorInstanceId)
    || !HASH_RE.test(receipt.preflightSha256)
    || !isRecord(receipt.preflightEvidence)
    || receipt.preflightSha256 !== preparedLocalPilotEvidenceHash(receipt.preflightEvidence as PreparedLocalPilot['preflightEvidence'])
    || !Number.isFinite(Date.parse(receipt.preflightObservedAt))
    || !Number.isFinite(Date.parse(receipt.preparedAt))) {
    throw new Error('Stored Local live pilot preparation is invalid');
  }
}

function samePendingRequest(existing: LocalLivePilotCampaign, requested: LocalLivePilotCampaign): boolean {
  if (existing.status !== 'PENDING_APPROVAL') return false;
  const current = localLivePilotBinding(existing);
  const proposed = localLivePilotBinding(requested);
  current.nonce = '';
  proposed.nonce = '';
  return JSON.stringify(current) === JSON.stringify(proposed);
}

function nextVersion(campaign: LocalLivePilotCampaign, now: Date): Pick<LocalLivePilotCampaign, 'version' | 'updatedAt'> {
  return { version: campaign.version + 1, updatedAt: now.toISOString() };
}

function assertVersionTransition(current: LocalLivePilotCampaign, next: LocalLivePilotCampaign): void {
  if (!Number.isInteger(current.version) || next.version !== current.version + 1) {
    throw new Error('Local live pilot compare-and-set version conflict');
  }
}

/** Durable Local pilot state. Every mutation reads and validates the current version inside a Firestore transaction. */
export class FirestoreLocalLivePilotStore implements LocalLivePilotStore {
  private firestore: Firestore | null;

  constructor(firestore?: Firestore) {
    this.firestore = firestore || null;
  }

  private async db(): Promise<Firestore> {
    if (!this.firestore) {
      const [{ applicationDefault, getApps, initializeApp }, { getFirestore }] = await Promise.all([
        import('firebase-admin/app'),
        import('firebase-admin/firestore'),
      ]);
      const projectId = (process.env.GCP_PROJECT_ID || 'gen-lang-client-0730128480').trim();
      const app = getApps()[0] || initializeApp({ credential: applicationDefault(), projectId });
      this.firestore = getFirestore(app);
    }
    return this.firestore;
  }

  async get(campaignId: string): Promise<LocalLivePilotCampaign | null> {
    const id = validateCampaignId(campaignId);
    const db = await this.db();
    const snapshot = await db.collection(LOCAL_LIVE_PILOT_COLLECTION).doc(id).get();
    if (!snapshot.exists) return null;
    return validateStoredCampaign(snapshot.data(), id, new Date());
  }

  async create(input: LocalLivePilotInput, now = new Date()): Promise<LocalLivePilotCampaign> {
    assertDate(now);
    const requested = newLocalLivePilotCampaign(input, now);
    const db = await this.db();
    const ref = db.collection(LOCAL_LIVE_PILOT_COLLECTION).doc(requested.campaignId);
    return db.runTransaction(async (transaction) => {
      const snapshot = await transaction.get(ref);
      if (snapshot.exists) {
        const existing = validateStoredCampaign(snapshot.data(), requested.campaignId, now);
        if (samePendingRequest(existing, requested)) return existing;
        throw new Error('Local live pilot campaign id already has a different or non-pending binding');
      }
      transaction.create(ref, requested);
      return structuredClone(requested);
    });
  }

  async approve(
    campaignId: string,
    actor: LocalLivePilotActor,
    expected: LocalLivePilotExpectedBinding,
    now = new Date(),
  ): Promise<LocalLivePilotCampaign> {
    assertDate(now);
    validateLocalLivePilotActor(actor);
    const id = validateCampaignId(campaignId);
    validateBindingShape(expected, id);
    const db = await this.db();
    const ref = db.collection(LOCAL_LIVE_PILOT_COLLECTION).doc(id);
    return db.runTransaction(async (transaction) => {
      const snapshot = await transaction.get(ref);
      if (!snapshot.exists) throw new Error('Local live pilot campaign not found');
      const current = validateStoredCampaign(snapshot.data(), id, now);
      assertLocalLivePilotBinding(current, expected);
      if (current.adminUid !== actor.uid.trim()) throw new Error('Approver UID does not match bound trading_admin');
      if (['APPROVED', 'ACTIVE', 'CLOSE_ONLY', 'EXPIRED'].includes(current.status)
        && current.approvedByUid === actor.uid.trim()) return current;
      if (current.status !== 'PENDING_APPROVAL') throw new Error(`Local live pilot must be PENDING_APPROVAL, not ${current.status}`);
      if (Date.parse(current.pendingExpiresAt) <= now.getTime()) throw new Error('Local live pilot approval window has expired');
      const next: LocalLivePilotCampaign = {
        ...current,
        status: 'APPROVED',
        approvedByUid: actor.uid.trim(),
        approvedAt: now.toISOString(),
        campaignExpiresAt: new Date(now.getTime() + LOCAL_LIVE_PILOT_DURATION_MS).toISOString(),
        ...nextVersion(current, now),
      };
      assertVersionTransition(current, next);
      validateStoredCampaign(next, id, now);
      transaction.update(ref, next);
      return structuredClone(next);
    });
  }

  async recordPreparation(campaignId: string, expected: LocalLivePilotExpectedBinding, prepared: PreparedLocalPilot, now = new Date()): Promise<LocalLivePilotCampaign> {
    assertDate(now);
    const id = validateCampaignId(campaignId);
    validateBindingShape(expected, id);
    const db = await this.db();
    const ref = db.collection(LOCAL_LIVE_PILOT_COLLECTION).doc(id);
    return db.runTransaction(async (transaction) => {
      const snapshot = await transaction.get(ref);
      if (!snapshot.exists) throw new Error('Local live pilot campaign not found');
      const current = validateStoredCampaign(snapshot.data(), id, now);
      assertLocalLivePilotBinding(current, expected);
      if (!['APPROVED', 'ACTIVE'].includes(current.status)) {
        throw new Error(`Local live pilot must be APPROVED or ACTIVE, not ${current.status}`);
      }
      if (prepared.campaignId !== current.campaignId || prepared.runId !== current.runId
        || prepared.sourceFingerprint !== current.sourceHash
        || prepared.approvalId !== `local-approval-${current.campaignId.slice('pilot-'.length)}`) {
        throw new Error('Local live pilot preparation binding mismatch');
      }
      const next: LocalLivePilotCampaign = { ...current, preparation: structuredClone(prepared), ...nextVersion(current, now) };
      assertVersionTransition(current, next);
      validateStoredCampaign(next, id, now);
      transaction.update(ref, next);
      return structuredClone(next);
    });
  }

  async activate(campaignId: string, expected: LocalLivePilotExpectedBinding, now = new Date()): Promise<LocalLivePilotCampaign> {
    assertDate(now);
    const id = validateCampaignId(campaignId);
    validateBindingShape(expected, id);
    const db = await this.db();
    const ref = db.collection(LOCAL_LIVE_PILOT_COLLECTION).doc(id);
    return db.runTransaction(async (transaction) => {
      const snapshot = await transaction.get(ref);
      if (!snapshot.exists) throw new Error('Local live pilot campaign not found');
      const current = validateStoredCampaign(snapshot.data(), id, now);
      assertLocalLivePilotBinding(current, expected);
      if (current.status === 'ACTIVE') return current;
      if (current.status !== 'APPROVED') throw new Error(`Local live pilot must be APPROVED, not ${current.status}`);
      if (Date.parse(current.campaignExpiresAt || '') <= now.getTime()) throw new Error('Local live pilot campaign has expired');
      const next: LocalLivePilotCampaign = {
        ...current,
        status: 'ACTIVE',
        activatedAt: now.toISOString(),
        ...nextVersion(current, now),
      };
      assertVersionTransition(current, next);
      validateStoredCampaign(next, id, now);
      transaction.update(ref, next);
      return structuredClone(next);
    });
  }

  async enterCloseOnly(campaignId: string, expected: LocalLivePilotExpectedBinding, now = new Date()): Promise<LocalLivePilotCampaign> {
    return this.transition(campaignId, expected, now, (current) => {
      if (current.status === 'CLOSE_ONLY') return current;
      if (!['APPROVED', 'ACTIVE', 'EXPIRED'].includes(current.status)) throw new Error(`Cannot enter close-only from ${current.status}`);
      return { ...current, status: 'CLOSE_ONLY', closeOnlyAt: now.toISOString(), ...nextVersion(current, now) };
    });
  }

  async expire(campaignId: string, now = new Date()): Promise<LocalLivePilotCampaign> {
    assertDate(now);
    const id = validateCampaignId(campaignId);
    const db = await this.db();
    const ref = db.collection(LOCAL_LIVE_PILOT_COLLECTION).doc(id);
    return db.runTransaction(async (transaction) => {
      const snapshot = await transaction.get(ref);
      if (!snapshot.exists) throw new Error('Local live pilot campaign not found');
      const current = validateStoredCampaign(snapshot.data(), id, now);
      if (current.status === 'EXPIRED') return current;
      if (current.status === 'COMPLETED' || current.status === 'REVOKED' || current.status === 'CLOSE_ONLY') {
        throw new Error(`Cannot expire a ${current.status} campaign`);
      }
      const deadline = current.status === 'PENDING_APPROVAL'
        ? Date.parse(current.pendingExpiresAt)
        : Date.parse(current.campaignExpiresAt || '');
      if (now.getTime() < deadline) throw new Error('Local live pilot campaign has not expired');
      const next: LocalLivePilotCampaign = { ...current, status: 'EXPIRED', ...nextVersion(current, now) };
      assertVersionTransition(current, next);
      validateStoredCampaign(next, id, now);
      transaction.update(ref, next);
      return structuredClone(next);
    });
  }

  async complete(campaignId: string, expected: LocalLivePilotExpectedBinding, now = new Date()): Promise<LocalLivePilotCampaign> {
    return this.transition(campaignId, expected, now, (current) => {
      if (current.status === 'COMPLETED') return current;
      if (!['APPROVED', 'ACTIVE', 'CLOSE_ONLY', 'EXPIRED'].includes(current.status)) throw new Error(`Cannot complete a ${current.status} campaign`);
      return { ...current, status: 'COMPLETED', completedAt: now.toISOString(), ...nextVersion(current, now) };
    });
  }

  async revoke(
    campaignId: string,
    actor: LocalLivePilotActor,
    expected: LocalLivePilotExpectedBinding,
    now = new Date(),
  ): Promise<LocalLivePilotCampaign> {
    validateLocalLivePilotActor(actor);
    return this.transition(campaignId, expected, now, (current) => {
      if (actor.uid.trim() !== current.adminUid) throw new Error('Revoker UID does not match bound trading_admin');
      if (current.status === 'REVOKED') return current;
      if (current.status === 'COMPLETED') throw new Error('Completed campaign cannot be revoked');
      return { ...current, status: 'REVOKED', revokedAt: now.toISOString(), ...nextVersion(current, now) };
    });
  }

  async canIncreaseRisk(campaignId: string, now = new Date()): Promise<boolean> {
    try {
      const campaign = await this.get(campaignId);
      return campaign ? localLivePilotCanIncreaseRisk(campaign, now) : false;
    } catch {
      return false;
    }
  }

  private async transition(
    campaignId: string,
    expected: LocalLivePilotExpectedBinding,
    now: Date,
    update: (current: LocalLivePilotCampaign) => LocalLivePilotCampaign,
  ): Promise<LocalLivePilotCampaign> {
    assertDate(now);
    const id = validateCampaignId(campaignId);
    validateBindingShape(expected, id);
    const db = await this.db();
    const ref = db.collection(LOCAL_LIVE_PILOT_COLLECTION).doc(id);
    return db.runTransaction(async (transaction) => {
      const snapshot = await transaction.get(ref);
      if (!snapshot.exists) throw new Error('Local live pilot campaign not found');
      const current = validateStoredCampaign(snapshot.data(), id, now);
      assertLocalLivePilotBinding(current, expected);
      const next = update(current);
      if (next === current) return current;
      assertVersionTransition(current, next);
      validateStoredCampaign(next, id, now);
      transaction.update(ref, next);
      return structuredClone(next);
    });
  }
}

export function getLocalLivePilotStore(): LocalLivePilotStore {
  return new FirestoreLocalLivePilotStore();
}
