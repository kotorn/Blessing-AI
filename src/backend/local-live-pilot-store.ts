import crypto from 'node:crypto';
import {
  assertLocalLivePilotBinding,
  localLivePilotBinding,
  localLivePilotCanIncreaseRisk,
  LOCAL_LIVE_PILOT_DURATION_MS,
  newLocalLivePilotCampaign,
  validateLocalLivePilotActor,
  validateLocalLivePilotCampaign,
  type LocalLivePilotActor,
  type LocalLivePilotCampaign,
  type LocalLivePilotExpectedBinding,
  type LocalLivePilotInput,
} from './local-live-pilot.js';
import type { PreparedLocalPilot } from './local-pilot-preparation.js';

/**
 * Persistence boundary for the campaign domain. Production adapters must make
 * each transition compare-and-set on `version`; this module intentionally
 * provides no network or credential-bearing adapter.
 */
export interface LocalLivePilotStore {
  get(campaignId: string): Promise<LocalLivePilotCampaign | null>;
  create(input: LocalLivePilotInput, now?: Date): Promise<LocalLivePilotCampaign>;
  approve(campaignId: string, actor: LocalLivePilotActor, expected: LocalLivePilotExpectedBinding, now?: Date): Promise<LocalLivePilotCampaign>;
  recordPreparation(campaignId: string, expected: LocalLivePilotExpectedBinding, prepared: PreparedLocalPilot, now?: Date): Promise<LocalLivePilotCampaign>;
  activate(campaignId: string, expected: LocalLivePilotExpectedBinding, now?: Date): Promise<LocalLivePilotCampaign>;
  enterCloseOnly(campaignId: string, expected: LocalLivePilotExpectedBinding, now?: Date): Promise<LocalLivePilotCampaign>;
  expire(campaignId: string, now?: Date): Promise<LocalLivePilotCampaign>;
  complete(campaignId: string, expected: LocalLivePilotExpectedBinding, now?: Date): Promise<LocalLivePilotCampaign>;
  revoke(campaignId: string, actor: LocalLivePilotActor, expected: LocalLivePilotExpectedBinding, now?: Date): Promise<LocalLivePilotCampaign>;
  canIncreaseRisk(campaignId: string, now?: Date): Promise<boolean>;
}

function clone<T>(value: T): T {
  return structuredClone(value);
}

function validateCampaignId(campaignId: string): void {
  if (!/^pilot-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$/.test(campaignId)) {
    throw new Error('Invalid Local live pilot campaign id');
  }
}

function assertValid(campaign: LocalLivePilotCampaign, now: Date, permitElapsedPending = false): void {
  const validationTime = permitElapsedPending && campaign.status === 'PENDING_APPROVAL'
    ? new Date(Date.parse(campaign.requestedAt))
    : now;
  const errors = validateLocalLivePilotCampaign(campaign, validationTime);
  if (errors.length) throw new Error(errors.join('; '));
}

function assertCampaignBinding(
  campaign: LocalLivePilotCampaign,
  expected: LocalLivePilotExpectedBinding,
  now: Date,
): void {
  assertValid(campaign, now, true);
  assertLocalLivePilotBinding(campaign, expected);
}

function sameRequest(a: LocalLivePilotCampaign, b: LocalLivePilotCampaign): boolean {
  const left = localLivePilotBinding(a);
  const right = localLivePilotBinding(b);
  left.nonce = '';
  right.nonce = '';
  return JSON.stringify(left) === JSON.stringify(right)
    && a.status === 'PENDING_APPROVAL';
}

function expectedStatus(campaign: LocalLivePilotCampaign, status: LocalLivePilotCampaign['status']): void {
  if (campaign.status !== status) throw new Error(`Local live pilot must be ${status}, not ${campaign.status}`);
}

function nextVersion(campaign: LocalLivePilotCampaign, now: Date): Pick<LocalLivePilotCampaign, 'version' | 'updatedAt'> {
  return { version: campaign.version + 1, updatedAt: now.toISOString() };
}

/** In-memory CAS-style implementation for isolated tests and local composition. */
export class InMemoryLocalLivePilotStore implements LocalLivePilotStore {
  private readonly campaigns = new Map<string, LocalLivePilotCampaign>();
  private readonly locks = new Map<string, Promise<void>>();

  private async serialized<T>(campaignId: string, operation: () => T | Promise<T>): Promise<T> {
    const previous = this.locks.get(campaignId) || Promise.resolve();
    let release!: () => void;
    const current = new Promise<void>((resolve) => { release = resolve; });
    this.locks.set(campaignId, current);
    await previous;
    try {
      return await operation();
    } finally {
      release();
      if (this.locks.get(campaignId) === current) this.locks.delete(campaignId);
    }
  }

  async get(campaignId: string): Promise<LocalLivePilotCampaign | null> {
    validateCampaignId(campaignId);
    const campaign = this.campaigns.get(campaignId);
    return campaign ? clone(campaign) : null;
  }

  async create(input: LocalLivePilotInput, now = new Date()): Promise<LocalLivePilotCampaign> {
    const requested = newLocalLivePilotCampaign(input, now);
    return this.serialized(requested.campaignId, () => {
      const existing = this.campaigns.get(requested.campaignId);
      if (existing) {
        assertValid(existing, now, true);
        if (sameRequest(existing, requested)) return clone(existing);
        throw new Error('Local live pilot campaign id already has a different or non-pending binding');
      }
      this.campaigns.set(requested.campaignId, clone(requested));
      return clone(requested);
    });
  }

  async approve(
    campaignId: string,
    actor: LocalLivePilotActor,
    expected: LocalLivePilotExpectedBinding,
    now = new Date(),
  ): Promise<LocalLivePilotCampaign> {
    validateLocalLivePilotActor(actor);
    validateCampaignId(campaignId);
    return this.serialized(campaignId, () => {
      const current = this.campaigns.get(campaignId);
      if (!current) throw new Error('Local live pilot campaign not found');
      assertCampaignBinding(current, expected, now);
      if (current.adminUid !== actor.uid.trim()) throw new Error('Approver UID does not match bound trading_admin');
      if (current.status === 'APPROVED' || current.status === 'ACTIVE' || current.status === 'CLOSE_ONLY' || current.status === 'EXPIRED') {
        if (current.approvedByUid === actor.uid.trim()) return clone(current);
      }
      expectedStatus(current, 'PENDING_APPROVAL');
      if (Date.parse(current.pendingExpiresAt) <= now.getTime()) throw new Error('Local live pilot approval window has expired');
      const approvedAt = now.toISOString();
      const next: LocalLivePilotCampaign = {
        ...current,
        status: 'APPROVED',
        approvedByUid: actor.uid.trim(),
        approvedAt,
        campaignExpiresAt: new Date(now.getTime() + LOCAL_LIVE_PILOT_DURATION_MS).toISOString(),
        ...nextVersion(current, now),
      };
      assertValid(next, now);
      this.campaigns.set(campaignId, clone(next));
      return clone(next);
    });
  }

  async activate(campaignId: string, expected: LocalLivePilotExpectedBinding, now = new Date()): Promise<LocalLivePilotCampaign> {
    validateCampaignId(campaignId);
    return this.serialized(campaignId, () => {
      const current = this.campaigns.get(campaignId);
      if (!current) throw new Error('Local live pilot campaign not found');
      assertCampaignBinding(current, expected, now);
      if (current.status === 'ACTIVE') return clone(current);
      if (!['APPROVED', 'ACTIVE'].includes(current.status)) {
        throw new Error(`Local live pilot must be APPROVED or ACTIVE, not ${current.status}`);
      }
      if (Date.parse(current.campaignExpiresAt || '') <= now.getTime()) throw new Error('Local live pilot campaign has expired');
      const next = { ...current, status: 'ACTIVE' as const, activatedAt: now.toISOString(), ...nextVersion(current, now) };
      assertValid(next, now);
      this.campaigns.set(campaignId, clone(next));
      return clone(next);
    });
  }

  async recordPreparation(campaignId: string, expected: LocalLivePilotExpectedBinding, prepared: PreparedLocalPilot, now = new Date()): Promise<LocalLivePilotCampaign> {
    validateCampaignId(campaignId);
    return this.serialized(campaignId, () => {
      const current = this.campaigns.get(campaignId);
      if (!current) throw new Error('Local live pilot campaign not found');
      assertCampaignBinding(current, expected, now);
      if (!['APPROVED', 'ACTIVE'].includes(current.status)) {
        throw new Error(`Local live pilot must be APPROVED or ACTIVE, not ${current.status}`);
      }
      if (prepared.campaignId !== current.campaignId || prepared.runId !== current.runId
        || prepared.sourceFingerprint !== current.sourceHash
        || prepared.approvalId !== `local-approval-${current.campaignId.slice('pilot-'.length)}`) {
        throw new Error('Local live pilot preparation binding mismatch');
      }
      const next = { ...current, preparation: clone(prepared), ...nextVersion(current, now) };
      assertValid(next, now);
      this.campaigns.set(campaignId, clone(next));
      return clone(next);
    });
  }

  async enterCloseOnly(campaignId: string, expected: LocalLivePilotExpectedBinding, now = new Date()): Promise<LocalLivePilotCampaign> {
    validateCampaignId(campaignId);
    return this.serialized(campaignId, () => {
      const current = this.campaigns.get(campaignId);
      if (!current) throw new Error('Local live pilot campaign not found');
      assertCampaignBinding(current, expected, now);
      if (current.status === 'CLOSE_ONLY') return clone(current);
      if (!['APPROVED', 'ACTIVE', 'EXPIRED'].includes(current.status)) {
        throw new Error(`Cannot enter close-only from ${current.status}`);
      }
      const next = { ...current, status: 'CLOSE_ONLY' as const, closeOnlyAt: now.toISOString(), ...nextVersion(current, now) };
      assertValid(next, now);
      this.campaigns.set(campaignId, clone(next));
      return clone(next);
    });
  }

  async expire(campaignId: string, now = new Date()): Promise<LocalLivePilotCampaign> {
    validateCampaignId(campaignId);
    return this.serialized(campaignId, () => {
      const current = this.campaigns.get(campaignId);
      if (!current) throw new Error('Local live pilot campaign not found');
      assertValid(current, now, true);
      if (current.status === 'EXPIRED') return clone(current);
      if (current.status === 'COMPLETED' || current.status === 'REVOKED' || current.status === 'CLOSE_ONLY') {
        throw new Error(`Cannot expire a ${current.status} campaign`);
      }
      const deadline = current.status === 'PENDING_APPROVAL'
        ? Date.parse(current.pendingExpiresAt)
        : Date.parse(current.campaignExpiresAt || '');
      if (now.getTime() < deadline) throw new Error('Local live pilot campaign has not expired');
      const next = { ...current, status: 'EXPIRED' as const, ...nextVersion(current, now) };
      assertValid(next, now);
      this.campaigns.set(campaignId, clone(next));
      return clone(next);
    });
  }

  async complete(campaignId: string, expected: LocalLivePilotExpectedBinding, now = new Date()): Promise<LocalLivePilotCampaign> {
    validateCampaignId(campaignId);
    return this.serialized(campaignId, () => {
      const current = this.campaigns.get(campaignId);
      if (!current) throw new Error('Local live pilot campaign not found');
      assertCampaignBinding(current, expected, now);
      if (current.status === 'COMPLETED') return clone(current);
      if (!['APPROVED', 'ACTIVE', 'CLOSE_ONLY', 'EXPIRED'].includes(current.status)) {
        throw new Error(`Cannot complete a ${current.status} campaign`);
      }
      const next = { ...current, status: 'COMPLETED' as const, completedAt: now.toISOString(), ...nextVersion(current, now) };
      assertValid(next, now);
      this.campaigns.set(campaignId, clone(next));
      return clone(next);
    });
  }

  async revoke(
    campaignId: string,
    actor: LocalLivePilotActor,
    expected: LocalLivePilotExpectedBinding,
    now = new Date(),
  ): Promise<LocalLivePilotCampaign> {
    validateLocalLivePilotActor(actor);
    validateCampaignId(campaignId);
    return this.serialized(campaignId, () => {
      const current = this.campaigns.get(campaignId);
      if (!current) throw new Error('Local live pilot campaign not found');
      assertCampaignBinding(current, expected, now);
      if (actor.uid.trim() !== current.adminUid) throw new Error('Revoker UID does not match bound trading_admin');
      if (current.status === 'REVOKED') return clone(current);
      if (current.status === 'COMPLETED') throw new Error('Completed campaign cannot be revoked');
      const next = { ...current, status: 'REVOKED' as const, revokedAt: now.toISOString(), ...nextVersion(current, now) };
      assertValid(next, now);
      this.campaigns.set(campaignId, clone(next));
      return clone(next);
    });
  }

  async canIncreaseRisk(campaignId: string, now = new Date()): Promise<boolean> {
    const campaign = await this.get(campaignId);
    if (!campaign) return false;
    return localLivePilotCanIncreaseRisk(campaign, now);
  }
}

export function localLivePilotExpectedBinding(campaign: LocalLivePilotCampaign): LocalLivePilotExpectedBinding {
  return localLivePilotBinding(campaign);
}

export function localLivePilotStableApprovalKey(campaignId: string): string {
  validateCampaignId(campaignId);
  return crypto.createHash('sha256').update(`LOCAL_LIVE_PILOT\u0000${campaignId}`).digest('hex');
}
