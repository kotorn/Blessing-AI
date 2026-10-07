/**
 * Control-Plane to Worker signed pilot readiness verdict protocol (Decision D3).
 *
 * Produces deterministic, HMAC-SHA256 signed readiness verdicts verifying that
 * all 5 Track C classes are verified on the host on the exact frozen git SHA and binding.
 */
import { createHmac } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import type { LocalPilotReadiness } from './local-live-pilot-readiness.js';
import type { TrackCBinding } from './local-release-runtime.js';
import { TRACK_C_CLASSES } from './local-pilot-attestation.js';

export interface PilotReadinessVerdictPayload {
  verdict: 'READY';
  campaignId: string;
  gitSha: string;
  binding: {
    gitSha: string;
    sourceSha256: string;
    dependencySha256: string;
    migrationSha256: string;
    pilotPolicySha256: string;
  };
  verifiedClasses: readonly string[];
  issuedAt: string;
  expiresAt: string;
}

export interface PilotReadinessVerdict extends PilotReadinessVerdictPayload {
  signature: string;
}

/** Deterministic JSON serialization with sorted keys matching Python canonical_verdict_bytes. */
export function canonicalVerdictJson(payload: Record<string, unknown>): string {
  const clean: Record<string, unknown> = {};
  for (const key of Object.keys(payload).sort()) {
    if (key === 'signature') continue;
    const val = payload[key];
    if (val && typeof val === 'object' && !Array.isArray(val)) {
      clean[key] = JSON.parse(canonicalVerdictJson(val as Record<string, unknown>));
    } else {
      clean[key] = val;
    }
  }
  return JSON.stringify(clean);
}

export function signVerdict(payload: Record<string, unknown>, token: string): string {
  const canonical = canonicalVerdictJson(payload);
  return createHmac('sha256', token).update(canonical, 'utf8').digest('hex');
}

export function getOldestAttestationTime(root: string = process.cwd()): Date | null {
  const dir = path.resolve(root, 'artifacts/local-pilot-attestations');
  if (!existsSync(dir)) return null;
  let oldestMs = Infinity;
  for (const c of TRACK_C_CLASSES) {
    const file = path.join(dir, `${c}.json`);
    if (!existsSync(file)) continue;
    try {
      const raw = JSON.parse(readFileSync(file, 'utf8')) as Record<string, unknown>;
      if (typeof raw?.observedAt === 'string') {
        const ms = Date.parse(raw.observedAt);
        if (!Number.isNaN(ms) && ms < oldestMs) {
          oldestMs = ms;
        }
      }
    } catch {
      // ignore read/parse failure
    }
  }
  return oldestMs === Infinity ? null : new Date(oldestMs);
}

export interface CreatePilotVerdictParams {
  campaign: { campaignId: string; campaignExpiresAt?: string };
  binding: TrackCBinding;
  readiness: LocalPilotReadiness;
  workerIdentityToken: string;
  now?: Date;
  ttlSeconds?: number;
  oldestAttestationAt?: Date | string | null;
  root?: string;
}

export function createPilotReadinessVerdict(params: CreatePilotVerdictParams): PilotReadinessVerdict | null {
  const {
    campaign,
    binding,
    readiness,
    workerIdentityToken,
    now = new Date(),
    ttlSeconds = 300,
    oldestAttestationAt,
    root,
  } = params;
  if (readiness.status !== 'READY' || !readiness.canStart) {
    return null;
  }

  let oldestAttestationMs: number | null = null;
  if (oldestAttestationAt) {
    const parsed = typeof oldestAttestationAt === 'string' ? Date.parse(oldestAttestationAt) : oldestAttestationAt.getTime();
    if (!Number.isNaN(parsed)) oldestAttestationMs = parsed;
  }
  if (oldestAttestationMs === null) {
    const fromDisk = getOldestAttestationTime(root);
    if (fromDisk) oldestAttestationMs = fromDisk.getTime();
  }

  const effectiveTtlSeconds = Math.min(Math.max(1, ttlSeconds ?? 300), 3600);
  const ttlExpiryMs = now.getTime() + effectiveTtlSeconds * 1000;

  const candidates: number[] = [ttlExpiryMs];
  if (oldestAttestationMs !== null) {
    candidates.push(oldestAttestationMs + 24 * 60 * 60 * 1000);
  }
  if (campaign.campaignExpiresAt) {
    const campaignExpMs = Date.parse(campaign.campaignExpiresAt);
    if (!Number.isNaN(campaignExpMs)) {
      candidates.push(campaignExpMs);
    }
  }

  const finalExpiresMs = Math.min(...candidates);
  if (finalExpiresMs <= now.getTime()) {
    return null;
  }

  const issuedAt = now.toISOString();
  const expiresAt = new Date(finalExpiresMs).toISOString();
  const payload: PilotReadinessVerdictPayload = {
    verdict: 'READY',
    campaignId: campaign.campaignId,
    gitSha: binding.gitSha,
    binding: {
      gitSha: binding.gitSha,
      sourceSha256: binding.sourceSha256,
      dependencySha256: binding.dependencySha256,
      migrationSha256: binding.migrationSha256,
      pilotPolicySha256: binding.pilotPolicySha256,
    },
    verifiedClasses: [...TRACK_C_CLASSES],
    issuedAt,
    expiresAt,
  };
  const signature = signVerdict(payload as unknown as Record<string, unknown>, workerIdentityToken);
  return {
    ...payload,
    signature,
  };
}
