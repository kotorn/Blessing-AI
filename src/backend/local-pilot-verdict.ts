/**
 * Control-Plane to Worker signed pilot readiness verdict protocol (Decision D3).
 *
 * Produces deterministic, HMAC-SHA256 signed readiness verdicts verifying that
 * all 5 Track C classes are verified on the host on the exact frozen git SHA and binding.
 */
import { createHmac } from 'node:crypto';
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

export interface CreatePilotVerdictParams {
  campaign: { campaignId: string };
  binding: TrackCBinding;
  readiness: LocalPilotReadiness;
  workerIdentityToken: string;
  now?: Date;
  ttlSeconds?: number;
}

export function createPilotReadinessVerdict(params: CreatePilotVerdictParams): PilotReadinessVerdict | null {
  const { campaign, binding, readiness, workerIdentityToken, now = new Date(), ttlSeconds = 3600 } = params;
  if (readiness.status !== 'READY' || !readiness.canStart) {
    return null;
  }
  const issuedAt = now.toISOString();
  const expiresAt = new Date(now.getTime() + ttlSeconds * 1000).toISOString();
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
