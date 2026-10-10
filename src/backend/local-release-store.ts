import { applicationDefault, getApps, initializeApp } from 'firebase-admin/app';
import { getFirestore, type Firestore } from 'firebase-admin/firestore';
import crypto from 'node:crypto';
import {
  assertExpectedLocalBinding,
  consumedApprovalFromCandidate,
  type LocalConsumedApproval,
  type LocalReleaseApprover,
  type LocalReleaseCandidate,
  type LocalReleaseBinding,
  validateLocalApprover,
  validateLocalReleaseCandidate,
} from './local-release.js';

/** Deliberately separate from Cloud ReleaseStore's release_candidates collection. */
export const LOCAL_RELEASE_CANDIDATES_COLLECTION = 'local_release_candidates' as const;

export interface LocalReleaseStore {
  getCandidate(candidateId: string): Promise<LocalReleaseCandidate | null>;
  createCandidate(candidate: LocalReleaseCandidate): Promise<void>;
  approveCandidate(
    candidateId: string,
    approver: LocalReleaseApprover,
    expected: LocalReleaseBinding,
    now?: Date,
  ): Promise<LocalReleaseCandidate>;
  consumeApproval(
    candidateId: string,
    expected: LocalReleaseBinding,
    now?: Date,
  ): Promise<LocalConsumedApproval>;
  getConsumedApproval(approvalId: string, now?: Date): Promise<LocalConsumedApproval | null>;
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function candidateIdOrThrow(candidateId: string): string {
  if (!/^local-rc-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(candidateId)) {
    throw new Error('Invalid Local release candidate id');
  }
  return candidateId;
}

function approvalIdOrThrow(approvalId: string): string {
  if (!/^local-approval-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(approvalId)) {
    throw new Error('Invalid Local release approval id');
  }
  return approvalId;
}

function ensureCandidate(candidate: LocalReleaseCandidate, now = new Date()): void {
  const failures = validateLocalReleaseCandidate(candidate, now);
  if (failures.length) throw new Error(failures.join('; '));
}

function ensurePendingCandidate(candidate: LocalReleaseCandidate, now: Date): void {
  ensureCandidate(candidate, now);
  if (candidate.status !== 'PENDING_APPROVAL') {
    throw new Error('Local release candidate is no longer pending approval');
  }
}

function ensureApprovedCandidate(candidate: LocalReleaseCandidate, now: Date): void {
  ensureCandidate(candidate, now);
  if (candidate.status !== 'APPROVED' || !candidate.approvalId) {
    throw new Error('Local release candidate has no consumable approval');
  }
}

function consumedOrNull(candidate: LocalReleaseCandidate, approvalId: string, now: Date): LocalConsumedApproval | null {
  if (candidate.status !== 'CONSUMED' || candidate.approvalId !== approvalId) return null;
  const failures = validateLocalReleaseCandidate(candidate, now);
  if (failures.length) return null;
  return consumedApprovalFromCandidate(candidate);
}

function approvalUpdate(candidate: LocalReleaseCandidate, approver: LocalReleaseApprover, now: Date): LocalReleaseCandidate {
  const approvalId = `local-approval-${crypto.randomUUID()}`;
  return {
    ...candidate,
    status: 'APPROVED',
    approvalId,
    approvedByUid: approver.uid.trim(),
    approvedAt: now.toISOString(),
  };
}

function consumedUpdate(candidate: LocalReleaseCandidate, now: Date): LocalReleaseCandidate {
  return {
    ...candidate,
    status: 'CONSUMED',
    consumedAt: now.toISOString(),
  };
}

/**
 * Server-side Firestore Admin SDK store for Local approvals. It never reads
 * or writes Cloud ReleaseStore documents and persists only non-secret version
 * metadata plus immutable release bindings.
 */
export class FirestoreLocalReleaseStore implements LocalReleaseStore {
  private firestore: Firestore | null;

  constructor(firestore?: Firestore) {
    this.firestore = firestore || null;
  }

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

  private candidateRef(candidateId: string) {
    return this.db()
      .collection(LOCAL_RELEASE_CANDIDATES_COLLECTION)
      .doc(candidateIdOrThrow(candidateId));
  }

  async getCandidate(candidateId: string): Promise<LocalReleaseCandidate | null> {
    const snapshot = await this.candidateRef(candidateId).get();
    if (!snapshot.exists) return null;
    const candidate = snapshot.data() as LocalReleaseCandidate;
    ensureCandidate(candidate);
    return candidate;
  }

  async createCandidate(candidate: LocalReleaseCandidate): Promise<void> {
    ensureCandidate(candidate);
    if (candidate.status !== 'PENDING_APPROVAL') {
      throw new Error('Local release candidate must start as PENDING_APPROVAL');
    }
    await this.candidateRef(candidate.candidateId).create(candidate);
  }

  async approveCandidate(
    candidateId: string,
    approver: LocalReleaseApprover,
    expected: LocalReleaseBinding,
    now = new Date(),
  ): Promise<LocalReleaseCandidate> {
    validateLocalApprover(approver);
    const ref = this.candidateRef(candidateId);
    return this.db().runTransaction(async (transaction) => {
      const snapshot = await transaction.get(ref);
      if (!snapshot.exists) throw new Error('Local release candidate not found');
      const candidate = snapshot.data() as LocalReleaseCandidate;
      ensurePendingCandidate(candidate, now);
      assertExpectedLocalBinding(candidate, expected);
      const next = approvalUpdate(candidate, approver, now);
      ensureCandidate(next, now);
      transaction.update(ref, {
        status: next.status,
        approvalId: next.approvalId,
        approvedByUid: next.approvedByUid,
        approvedAt: next.approvedAt,
      });
      return next;
    });
  }

  async consumeApproval(
    candidateId: string,
    expected: LocalReleaseBinding,
    now = new Date(),
  ): Promise<LocalConsumedApproval> {
    const ref = this.candidateRef(candidateId);
    return this.db().runTransaction(async (transaction) => {
      const snapshot = await transaction.get(ref);
      if (!snapshot.exists) throw new Error('Local release candidate not found');
      const candidate = snapshot.data() as LocalReleaseCandidate;
      ensureApprovedCandidate(candidate, now);
      assertExpectedLocalBinding(candidate, expected);
      const next = consumedUpdate(candidate, now);
      ensureCandidate(next, now);
      transaction.update(ref, {
        status: next.status,
        consumedAt: next.consumedAt,
      });
      return consumedApprovalFromCandidate(next);
    });
  }

  async getConsumedApproval(approvalId: string, now = new Date()): Promise<LocalConsumedApproval | null> {
    const normalized = approvalIdOrThrow(approvalId);
    const snapshot = await this.db()
      .collection(LOCAL_RELEASE_CANDIDATES_COLLECTION)
      .where('approvalId', '==', normalized)
      .limit(2)
      .get();
    if (snapshot.empty) return null;
    if (snapshot.size !== 1) throw new Error('Local release approval id is not unique');
    return consumedOrNull(snapshot.docs[0].data() as LocalReleaseCandidate, normalized, now);
  }
}

/** Deterministic store used by focused tests; it has the same one-time semantics. */
export class InMemoryLocalReleaseStore implements LocalReleaseStore {
  private readonly candidates = new Map<string, LocalReleaseCandidate>();

  async getCandidate(candidateId: string): Promise<LocalReleaseCandidate | null> {
    candidateIdOrThrow(candidateId);
    const candidate = this.candidates.get(candidateId);
    return candidate ? clone(candidate) : null;
  }

  async createCandidate(candidate: LocalReleaseCandidate): Promise<void> {
    ensureCandidate(candidate);
    if (candidate.status !== 'PENDING_APPROVAL') {
      throw new Error('Local release candidate must start as PENDING_APPROVAL');
    }
    candidateIdOrThrow(candidate.candidateId);
    if (this.candidates.has(candidate.candidateId)) {
      throw new Error('Local release candidate already exists');
    }
    this.candidates.set(candidate.candidateId, clone(candidate));
  }

  async approveCandidate(
    candidateId: string,
    approver: LocalReleaseApprover,
    expected: LocalReleaseBinding,
    now = new Date(),
  ): Promise<LocalReleaseCandidate> {
    validateLocalApprover(approver);
    candidateIdOrThrow(candidateId);
    const candidate = this.candidates.get(candidateId);
    if (!candidate) throw new Error('Local release candidate not found');
    ensurePendingCandidate(candidate, now);
    assertExpectedLocalBinding(candidate, expected);
    const next = approvalUpdate(candidate, approver, now);
    ensureCandidate(next, now);
    this.candidates.set(candidateId, clone(next));
    return clone(next);
  }

  async consumeApproval(
    candidateId: string,
    expected: LocalReleaseBinding,
    now = new Date(),
  ): Promise<LocalConsumedApproval> {
    candidateIdOrThrow(candidateId);
    const candidate = this.candidates.get(candidateId);
    if (!candidate) throw new Error('Local release candidate not found');
    ensureApprovedCandidate(candidate, now);
    assertExpectedLocalBinding(candidate, expected);
    const next = consumedUpdate(candidate, now);
    ensureCandidate(next, now);
    this.candidates.set(candidateId, clone(next));
    return consumedApprovalFromCandidate(next);
  }

  async getConsumedApproval(approvalId: string, now = new Date()): Promise<LocalConsumedApproval | null> {
    const normalized = approvalIdOrThrow(approvalId);
    const matches = [...this.candidates.values()].filter((candidate) => candidate.approvalId === normalized);
    if (matches.length > 1) throw new Error('Local release approval id is not unique');
    if (!matches.length) return null;
    return consumedOrNull(matches[0], normalized, now);
  }
}

export function getLocalReleaseStore(): LocalReleaseStore {
  return new FirestoreLocalReleaseStore();
}
