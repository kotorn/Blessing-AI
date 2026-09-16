import { applicationDefault, getApps, initializeApp } from 'firebase-admin/app';
import { getFirestore, type Firestore } from 'firebase-admin/firestore';
import crypto from 'node:crypto';
import {
  type ReleaseCandidate,
  type ReleaseVerificationSnapshot,
  sanitizePreflightEvidence,
  validateApprovalPrerequisites,
  validateReleaseCandidate,
  type SanitizedPreflightEvidence,
} from './release.js';

export interface ReleaseEvidenceRecord {
  candidateId: string;
  kind: 'PREFLIGHT' | 'VERIFY' | 'APPROVAL';
  generatedAt: string;
  evidenceHash: string;
  payload: Record<string, unknown>;
}

export interface ConsumedApproval {
  candidateId: string;
  approvalId: string;
  imageDigest: string;
  workerRevision: string;
  secretVersions: ReleaseCandidate['secretVersions'];
  executionMode: ReleaseCandidate['executionMode'];
  symbol: ReleaseCandidate['symbol'];
  launchPolicy: ReleaseCandidate['launchPolicy'];
  viteDataConnectCutover: false;
  orderSubmissionAttempts: 0;
  workerDisarmed: true;
  consumedAt: string;
}

export interface ReleaseStore {
  getCandidate(candidateId: string): Promise<ReleaseCandidate | null>;
  createCandidate(candidate: ReleaseCandidate): Promise<void>;
  putEvidence(record: ReleaseEvidenceRecord): Promise<void>;
  updatePreflight(candidateId: string, evidence: SanitizedPreflightEvidence, evidenceHash: string): Promise<ReleaseCandidate>;
  approveCandidate(
    candidateId: string,
    uid: string,
    snapshot: ReleaseVerificationSnapshot,
  ): Promise<ReleaseCandidate>;
  consumeApproval(candidateId: string): Promise<ConsumedApproval>;
  getConsumedApproval(approvalId: string): Promise<ConsumedApproval | null>;
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function candidateIdOrThrow(candidateId: string): string {
  if (!/^rc-[0-9a-f-]{36}$/i.test(candidateId)) throw new Error('Invalid release candidate id');
  return candidateId;
}

function approvalIdOrThrow(approvalId: unknown): string {
  const normalized = typeof approvalId === 'string' ? approvalId.trim() : '';
  if (!/^approval-[0-9a-f-]{36}$/i.test(normalized)) throw new Error('Invalid release approval id');
  return normalized;
}

function ensureCandidate(candidate: ReleaseCandidate): void {
  const failures = validateReleaseCandidate(candidate);
  if (failures.length) throw new Error(failures.join('; '));
}

function consumedApprovalFromCandidate(
  candidate: ReleaseCandidate,
  approvalId: string,
  now = new Date(),
): ConsumedApproval | null {
  if (
    candidate.status !== 'CONSUMED'
    || candidate.approvalId !== approvalId
    || !candidate.consumedAt
  ) return null;
  const consumedAt = Date.parse(candidate.consumedAt);
  if (!Number.isFinite(consumedAt) || consumedAt > now.getTime()) return null;
  const failures = validateReleaseCandidate(candidate, now);
  if (failures.length) return null;
  return {
    candidateId: candidate.candidateId,
    approvalId,
    imageDigest: candidate.imageDigest,
    workerRevision: candidate.workerRevision,
    secretVersions: candidate.secretVersions,
    executionMode: candidate.executionMode,
    symbol: candidate.symbol,
    launchPolicy: candidate.launchPolicy,
    viteDataConnectCutover: candidate.viteDataConnectCutover,
    orderSubmissionAttempts: candidate.orderSubmissionAttempts,
    workerDisarmed: candidate.workerDisarmed,
    consumedAt: candidate.consumedAt,
  };
}

/**
 * Firestore is used only by the server-side release controller. Browser rules
 * do not grant write access to these collections, and no credential/token is
 * accepted or persisted by this store.
 */
export class FirestoreReleaseStore implements ReleaseStore {
  private firestore: Firestore | null = null;

  private db(): Firestore {
    if (!this.firestore) {
      const projectId = (process.env.GCP_PROJECT_ID || 'gen-lang-client-0730128480').trim();
      const app = getApps()[0] || initializeApp({
        credential: applicationDefault(),
        projectId,
      });
      this.firestore = getFirestore(app);
    }
    return this.firestore;
  }

  async getCandidate(candidateId: string): Promise<ReleaseCandidate | null> {
    const snapshot = await this.db().collection('release_candidates').doc(candidateIdOrThrow(candidateId)).get();
    if (!snapshot.exists) return null;
    return snapshot.data() as ReleaseCandidate;
  }

  async createCandidate(candidate: ReleaseCandidate): Promise<void> {
    ensureCandidate(candidate);
    await this.db().collection('release_candidates').doc(candidateIdOrThrow(candidate.candidateId)).create(candidate);
  }

  async putEvidence(record: ReleaseEvidenceRecord): Promise<void> {
    candidateIdOrThrow(record.candidateId);
    await this.db()
      .collection('release_evidence')
      .doc(`${record.candidateId}-${record.kind.toLowerCase()}`)
      .set(record, { merge: true });
  }

  async updatePreflight(
    candidateId: string,
    evidence: SanitizedPreflightEvidence,
    evidenceHash: string,
  ): Promise<ReleaseCandidate> {
    const ref = this.db().collection('release_candidates').doc(candidateIdOrThrow(candidateId));
    const evidenceRef = this.db()
      .collection('release_evidence')
      .doc(`${candidateId}-${'preflight'}`);
    const evidenceRecord: ReleaseEvidenceRecord = {
      candidateId,
      kind: 'PREFLIGHT',
      generatedAt: new Date().toISOString(),
      evidenceHash,
      payload: clone(evidence) as unknown as Record<string, unknown>,
    };
    const updated = await this.db().runTransaction(async (transaction) => {
      const snapshot = await transaction.get(ref);
      if (!snapshot.exists) throw new Error('Release candidate not found');
      const candidate = snapshot.data() as ReleaseCandidate;
      if (candidate.status !== 'PENDING_APPROVAL') throw new Error('Release candidate is no longer pending approval');
      const next = { ...candidate, preflightEvidenceHash: evidenceHash };
      ensureCandidate(next);
      transaction.update(ref, { preflightEvidenceHash: evidenceHash });
      transaction.set(evidenceRef, evidenceRecord, { merge: true });
      return next;
    });
    return updated;
  }

  async approveCandidate(
    candidateId: string,
    uid: string,
    snapshot: ReleaseVerificationSnapshot,
  ): Promise<ReleaseCandidate> {
    const ref = this.db().collection('release_candidates').doc(candidateIdOrThrow(candidateId));
    const evidenceRef = this.db()
      .collection('release_evidence')
      .doc(`${candidateId}-${'approval'}`);
    const now = new Date();
    const approved = await this.db().runTransaction(async (transaction) => {
      const document = await transaction.get(ref);
      if (!document.exists) throw new Error('Release candidate not found');
      const candidate = document.data() as ReleaseCandidate;
      const failures = validateApprovalPrerequisites(candidate, snapshot, now);
      if (failures.length) throw new Error(failures.join('; '));
      const approvalId = `approval-${crypto.randomUUID()}`;
      const next: ReleaseCandidate = {
        ...candidate,
        status: 'APPROVED',
        approvalId,
        approvedByUid: uid,
        approvedAt: now.toISOString(),
      };
      transaction.update(ref, {
        status: next.status,
        approvalId,
        approvedByUid: uid,
        approvedAt: next.approvedAt,
      });
      transaction.set(
        evidenceRef,
        {
          candidateId,
          kind: 'APPROVAL',
          generatedAt: now.toISOString(),
          evidenceHash: crypto
            .createHash('sha256')
            .update(`${next.candidateId}:${approvalId}`)
            .digest('hex'),
          payload: {
            status: 'APPROVED',
            approvedByUid: uid,
            approvedAt: next.approvedAt || now.toISOString(),
          },
        } satisfies ReleaseEvidenceRecord,
        { merge: true },
      );
      return next;
    });
    return approved;
  }

  async consumeApproval(candidateId: string): Promise<ConsumedApproval> {
    const ref = this.db().collection('release_candidates').doc(candidateIdOrThrow(candidateId));
    const now = new Date();
    return this.db().runTransaction(async (transaction) => {
      const document = await transaction.get(ref);
      if (!document.exists) throw new Error('Release candidate not found');
      const candidate = document.data() as ReleaseCandidate;
      if (candidate.status !== 'APPROVED' || !candidate.approvalId) {
        throw new Error('Release candidate has no consumable approval');
      }
      ensureCandidate(candidate);
      if (Date.parse(candidate.expiresAt) <= now.getTime()) {
        transaction.update(ref, { status: 'EXPIRED' });
        throw new Error('Release candidate approval has expired');
      }
      const consumed: ConsumedApproval = {
        candidateId: candidate.candidateId,
        approvalId: candidate.approvalId,
        imageDigest: candidate.imageDigest,
        workerRevision: candidate.workerRevision,
        secretVersions: candidate.secretVersions,
        executionMode: candidate.executionMode,
        symbol: candidate.symbol,
        launchPolicy: candidate.launchPolicy,
        viteDataConnectCutover: candidate.viteDataConnectCutover,
        orderSubmissionAttempts: candidate.orderSubmissionAttempts,
        workerDisarmed: candidate.workerDisarmed,
        consumedAt: now.toISOString(),
      };
      transaction.update(ref, { status: 'CONSUMED', consumedAt: consumed.consumedAt });
      return consumed;
    });
  }

  async getConsumedApproval(approvalId: string): Promise<ConsumedApproval | null> {
    const normalized = approvalIdOrThrow(approvalId);
    const snapshot = await this.db()
      .collection('release_candidates')
      .where('approvalId', '==', normalized)
      .limit(2)
      .get();
    if (snapshot.empty) return null;
    if (snapshot.size !== 1) throw new Error('Release approval id is not unique');
    const candidate = snapshot.docs[0].data() as ReleaseCandidate;
    return consumedApprovalFromCandidate(candidate, normalized);
  }
}

/** Deterministic store used by unit tests; it has the same one-time semantics. */
export class InMemoryReleaseStore implements ReleaseStore {
  private candidates = new Map<string, ReleaseCandidate>();
  private evidence = new Map<string, ReleaseEvidenceRecord>();

  async getCandidate(candidateId: string): Promise<ReleaseCandidate | null> {
    return this.candidates.has(candidateId) ? clone(this.candidates.get(candidateId) as ReleaseCandidate) : null;
  }

  async createCandidate(candidate: ReleaseCandidate): Promise<void> {
    ensureCandidate(candidate);
    if (this.candidates.has(candidate.candidateId)) throw new Error('Release candidate already exists');
    this.candidates.set(candidate.candidateId, clone(candidate));
  }

  async putEvidence(record: ReleaseEvidenceRecord): Promise<void> {
    this.evidence.set(`${record.candidateId}:${record.kind}`, clone(record));
  }

  async updatePreflight(candidateId: string, evidence: SanitizedPreflightEvidence, evidenceHash: string): Promise<ReleaseCandidate> {
    const candidate = await this.getCandidate(candidateId);
    if (!candidate || candidate.status !== 'PENDING_APPROVAL') throw new Error('Release candidate is not pending approval');
    const next = { ...candidate, preflightEvidenceHash: evidenceHash };
    ensureCandidate(next);
    this.candidates.set(candidateId, clone(next));
    await this.putEvidence({ candidateId, kind: 'PREFLIGHT', generatedAt: new Date().toISOString(), evidenceHash, payload: clone(evidence) as unknown as Record<string, unknown> });
    return clone(next);
  }

  async approveCandidate(candidateId: string, uid: string, snapshot: ReleaseVerificationSnapshot): Promise<ReleaseCandidate> {
    const candidate = await this.getCandidate(candidateId);
    if (!candidate) throw new Error('Release candidate not found');
    const failures = validateApprovalPrerequisites(candidate, snapshot);
    if (failures.length) throw new Error(failures.join('; '));
    const next = { ...candidate, status: 'APPROVED' as const, approvalId: `approval-${crypto.randomUUID()}`, approvedByUid: uid, approvedAt: new Date().toISOString() };
    this.candidates.set(candidateId, clone(next));
    await this.putEvidence({
      candidateId,
      kind: 'APPROVAL',
      generatedAt: next.approvedAt as string,
      evidenceHash: crypto.createHash('sha256').update(`${next.candidateId}:${next.approvalId}`).digest('hex'),
      payload: {
        status: 'APPROVED',
        approvedByUid: uid,
        approvedAt: next.approvedAt,
      },
    });
    return clone(next);
  }

  async consumeApproval(candidateId: string): Promise<ConsumedApproval> {
    const candidate = await this.getCandidate(candidateId);
    if (!candidate || candidate.status !== 'APPROVED' || !candidate.approvalId) throw new Error('Release candidate has no consumable approval');
    ensureCandidate(candidate);
    if (Date.parse(candidate.expiresAt) <= Date.now()) throw new Error('Release candidate approval has expired');
    const consumed: ConsumedApproval = {
      candidateId: candidate.candidateId,
      approvalId: candidate.approvalId,
      imageDigest: candidate.imageDigest,
      workerRevision: candidate.workerRevision,
      secretVersions: candidate.secretVersions,
      executionMode: candidate.executionMode,
      symbol: candidate.symbol,
      launchPolicy: candidate.launchPolicy,
      viteDataConnectCutover: candidate.viteDataConnectCutover,
      orderSubmissionAttempts: candidate.orderSubmissionAttempts,
      workerDisarmed: candidate.workerDisarmed,
      consumedAt: new Date().toISOString(),
    };
    this.candidates.set(candidateId, clone({ ...candidate, status: 'CONSUMED' as const, consumedAt: consumed.consumedAt }));
    return consumed;
  }

  async getConsumedApproval(approvalId: string): Promise<ConsumedApproval | null> {
    const normalized = approvalIdOrThrow(approvalId);
    const matches = [...this.candidates.values()].filter(
      (candidate) => candidate.approvalId === normalized,
    );
    if (matches.length > 1) throw new Error('Release approval id is not unique');
    if (!matches.length) return null;
    return consumedApprovalFromCandidate(matches[0], normalized);
  }
}

export function getReleaseStore(): ReleaseStore {
  return new FirestoreReleaseStore();
}

export { sanitizePreflightEvidence };
