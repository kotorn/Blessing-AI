import crypto from 'node:crypto';
import type { Firestore } from 'firebase-admin/firestore';
import {
  assertExpectedLocalContinuationBinding,
  consumedLocalContinuation,
  localContinuationBinding,
  type LocalConsumedContinuationApproval,
  type LocalContinuationApprover,
  type LocalContinuationApproval,
  type LocalContinuationBinding,
  validateLocalContinuation,
  validateLocalContinuationApprover,
} from './local-continuation.js';

/**
 * This is deliberately separate from Cloud `release_continuations` and
 * `release_candidates`; it never reuses the Cloud ReleaseStore collections.
 */
export const LOCAL_CONTINUATION_APPROVALS_COLLECTION = 'local_continuation_approvals' as const;

export interface LocalContinuationStore {
  getApproval(continuationId: string): Promise<LocalContinuationApproval | null>;
  createApproval(approval: LocalContinuationApproval, now?: Date): Promise<LocalContinuationApproval>;
  approveContinuation(
    continuationId: string,
    approver: LocalContinuationApprover,
    expected: LocalContinuationBinding,
    now?: Date,
  ): Promise<LocalContinuationApproval>;
  consumeContinuation(
    continuationId: string,
    expected: LocalContinuationBinding,
    now?: Date,
  ): Promise<LocalConsumedContinuationApproval>;
  getConsumedApproval(
    approvalId: string,
    now?: Date,
  ): Promise<LocalConsumedContinuationApproval | null>;
}

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function continuationIdOrThrow(value: string): string {
  if (!/^local-continuation-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
    throw new Error('Invalid Local continuation id');
  }
  return value;
}

function approvalIdOrThrow(value: string): string {
  if (!/^local-continuation-approval-[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
    throw new Error('Invalid Local continuation approval id');
  }
  return value;
}

function ensureApproval(approval: LocalContinuationApproval, now = new Date()): void {
  const failures = validateLocalContinuation(approval, now);
  if (failures.length) throw new Error(failures.join('; '));
}

function ensurePending(approval: LocalContinuationApproval, now: Date): void {
  ensureApproval(approval, now);
  if (approval.status !== 'PENDING_APPROVAL') throw new Error('Local continuation is no longer pending approval');
}

function ensureApproved(approval: LocalContinuationApproval, now: Date): void {
  ensureApproval(approval, now);
  if (approval.status !== 'APPROVED' || !approval.approvalId) {
    throw new Error('Local continuation has no consumable approval');
  }
}

function samePendingRequest(
  existing: LocalContinuationApproval,
  requested: LocalContinuationApproval,
): boolean {
  if (existing.status !== 'PENDING_APPROVAL') return false;
  const existingBinding = localContinuationBinding(existing);
  const requestedBinding = localContinuationBinding(requested);
  const { nonce: _existingNonce, ...existingIdentity } = existingBinding;
  const { nonce: _requestedNonce, ...requestedIdentity } = requestedBinding;
  return JSON.stringify(existingIdentity) === JSON.stringify(requestedIdentity);
}

function approveUpdate(
  approval: LocalContinuationApproval,
  approver: LocalContinuationApprover,
  now: Date,
): LocalContinuationApproval {
  return {
    ...approval,
    status: 'APPROVED',
    approvalId: `local-continuation-approval-${crypto.randomUUID()}`,
    approvedByUid: approver.uid.trim(),
    approvedAt: now.toISOString(),
  };
}

function consumeUpdate(approval: LocalContinuationApproval, now: Date): LocalContinuationApproval {
  return {
    ...approval,
    status: 'CONSUMED',
    consumedAt: now.toISOString(),
  };
}

function consumedOrNull(
  approval: LocalContinuationApproval,
  approvalId: string,
  now: Date,
): LocalConsumedContinuationApproval | null {
  if (approval.status !== 'CONSUMED' || approval.approvalId !== approvalId) return null;
  const failures = validateLocalContinuation(approval, now);
  if (failures.length) return null;
  return consumedLocalContinuation(approval);
}

/** Firestore Admin SDK implementation; all writes use the Local-only collection. */
export class FirestoreLocalContinuationStore implements LocalContinuationStore {
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
      const app = getApps()[0] || initializeApp({
        credential: applicationDefault(),
        projectId,
      });
      this.firestore = getFirestore(app);
    }
    return this.firestore;
  }

  private async approvalRef(continuationId: string) {
    const db = await this.db();
    return db
      .collection(LOCAL_CONTINUATION_APPROVALS_COLLECTION)
      .doc(continuationIdOrThrow(continuationId));
  }

  async getApproval(continuationId: string): Promise<LocalContinuationApproval | null> {
    const ref = await this.approvalRef(continuationId);
    const snapshot = await ref.get();
    if (!snapshot.exists) return null;
    const approval = snapshot.data() as LocalContinuationApproval;
    ensureApproval(approval);
    return approval;
  }

  async createApproval(approval: LocalContinuationApproval, now = new Date()): Promise<LocalContinuationApproval> {
    ensureApproval(approval, now);
    if (approval.status !== 'PENDING_APPROVAL') throw new Error('Local continuation must start as PENDING_APPROVAL');
    const ref = await this.approvalRef(approval.continuationId);
    try {
      await ref.create(approval);
      return approval;
    } catch (error) {
      const snapshot = await ref.get().catch(() => null);
      if (!snapshot?.exists) throw error;
      const existing = snapshot.data() as LocalContinuationApproval;
      ensureApproval(existing, now);
      if (samePendingRequest(existing, approval)) return existing;
      throw new Error('Local continuation already exists with a different or consumed binding');
    }
  }

  async approveContinuation(
    continuationId: string,
    approver: LocalContinuationApprover,
    expected: LocalContinuationBinding,
    now = new Date(),
  ): Promise<LocalContinuationApproval> {
    validateLocalContinuationApprover(approver);
    if (approver.uid.trim() !== expected.tradingAdminUid) throw new Error('Approver UID does not match Local trading_admin binding');
    const ref = await this.approvalRef(continuationId);
    const db = await this.db();
    return db.runTransaction(async (transaction) => {
      const snapshot = await transaction.get(ref);
      if (!snapshot.exists) throw new Error('Local continuation approval not found');
      const approval = snapshot.data() as LocalContinuationApproval;
      ensurePending(approval, now);
      assertExpectedLocalContinuationBinding(approval, expected);
      const next = approveUpdate(approval, approver, now);
      ensureApproval(next, now);
      transaction.update(ref, {
        status: next.status,
        approvalId: next.approvalId,
        approvedByUid: next.approvedByUid,
        approvedAt: next.approvedAt,
      });
      return next;
    });
  }

  async consumeContinuation(
    continuationId: string,
    expected: LocalContinuationBinding,
    now = new Date(),
  ): Promise<LocalConsumedContinuationApproval> {
    const ref = await this.approvalRef(continuationId);
    const db = await this.db();
    return db.runTransaction(async (transaction) => {
      const snapshot = await transaction.get(ref);
      if (!snapshot.exists) throw new Error('Local continuation approval not found');
      const approval = snapshot.data() as LocalContinuationApproval;
      ensureApproved(approval, now);
      assertExpectedLocalContinuationBinding(approval, expected);
      const next = consumeUpdate(approval, now);
      ensureApproval(next, now);
      transaction.update(ref, {
        status: next.status,
        consumedAt: next.consumedAt,
      });
      return consumedLocalContinuation(next);
    });
  }

  async getConsumedApproval(
    approvalId: string,
    now = new Date(),
  ): Promise<LocalConsumedContinuationApproval | null> {
    const normalized = approvalIdOrThrow(approvalId);
    const db = await this.db();
    const snapshot = await db
      .collection(LOCAL_CONTINUATION_APPROVALS_COLLECTION)
      .where('approvalId', '==', normalized)
      .limit(2)
      .get();
    if (snapshot.empty) return null;
    if (snapshot.size !== 1) throw new Error('Local continuation approval id is not unique');
    return consumedOrNull(snapshot.docs[0].data() as LocalContinuationApproval, normalized, now);
  }
}

/** In-memory implementation with the same one-time and mismatch semantics. */
export class InMemoryLocalContinuationStore implements LocalContinuationStore {
  private readonly approvals = new Map<string, LocalContinuationApproval>();

  async getApproval(continuationId: string): Promise<LocalContinuationApproval | null> {
    continuationIdOrThrow(continuationId);
    const approval = this.approvals.get(continuationId);
    return approval ? clone(approval) : null;
  }

  async createApproval(approval: LocalContinuationApproval, now = new Date()): Promise<LocalContinuationApproval> {
    ensureApproval(approval, now);
    if (approval.status !== 'PENDING_APPROVAL') throw new Error('Local continuation must start as PENDING_APPROVAL');
    continuationIdOrThrow(approval.continuationId);
    const existing = this.approvals.get(approval.continuationId);
    if (existing) {
      ensureApproval(existing, now);
      if (samePendingRequest(existing, approval)) return clone(existing);
      throw new Error('Local continuation already exists with a different or consumed binding');
    }
    this.approvals.set(approval.continuationId, clone(approval));
    return clone(approval);
  }

  async approveContinuation(
    continuationId: string,
    approver: LocalContinuationApprover,
    expected: LocalContinuationBinding,
    now = new Date(),
  ): Promise<LocalContinuationApproval> {
    validateLocalContinuationApprover(approver);
    if (approver.uid.trim() !== expected.tradingAdminUid) throw new Error('Approver UID does not match Local trading_admin binding');
    continuationIdOrThrow(continuationId);
    const approval = this.approvals.get(continuationId);
    if (!approval) throw new Error('Local continuation approval not found');
    ensurePending(approval, now);
    assertExpectedLocalContinuationBinding(approval, expected);
    const next = approveUpdate(approval, approver, now);
    ensureApproval(next, now);
    this.approvals.set(continuationId, clone(next));
    return clone(next);
  }

  async consumeContinuation(
    continuationId: string,
    expected: LocalContinuationBinding,
    now = new Date(),
  ): Promise<LocalConsumedContinuationApproval> {
    continuationIdOrThrow(continuationId);
    const approval = this.approvals.get(continuationId);
    if (!approval) throw new Error('Local continuation approval not found');
    ensureApproved(approval, now);
    assertExpectedLocalContinuationBinding(approval, expected);
    const next = consumeUpdate(approval, now);
    ensureApproval(next, now);
    this.approvals.set(continuationId, clone(next));
    return clone(consumedLocalContinuation(next));
  }

  async getConsumedApproval(
    approvalId: string,
    now = new Date(),
  ): Promise<LocalConsumedContinuationApproval | null> {
    const normalized = approvalIdOrThrow(approvalId);
    const matches = [...this.approvals.values()].filter((approval) => approval.approvalId === normalized);
    if (matches.length > 1) throw new Error('Local continuation approval id is not unique');
    if (!matches.length) return null;
    return consumedOrNull(matches[0], normalized, now);
  }
}

export function getLocalContinuationStore(): LocalContinuationStore {
  return new FirestoreLocalContinuationStore();
}
