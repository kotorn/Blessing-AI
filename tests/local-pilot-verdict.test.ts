import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  canonicalVerdictJson,
  createPilotReadinessVerdict,
  signVerdict,
  type PilotReadinessVerdict,
} from '../src/backend/local-pilot-verdict.js';
import type { LocalPilotReadiness } from '../src/backend/local-live-pilot-readiness.js';
import type { TrackCBinding } from '../src/backend/local-release-runtime.js';

const repo = path.resolve(import.meta.dirname, '..');
const environment = Object.fromEntries(Object.entries(process.env).filter(([k]) =>
  ['PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'].includes(k.toUpperCase())));

function pythonVerify(verdict: PilotReadinessVerdict, token: string): { ok: boolean; reason: string; details: any } {
  const py = `
import json, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1])))
from apps.trading_worker.venues.binance.local_pilot_verdict import verify_pilot_readiness_verdict
import os
os.environ["LOCAL_LIVE_PILOT_CAMPAIGN_ID"] = sys.argv[2]
os.environ["LOCAL_LIVE_PILOT_GIT_SHA"] = sys.argv[3]
os.environ["LOCAL_LIVE_PILOT_SOURCE_HASH"] = sys.argv[4]
os.environ["LOCAL_LIVE_PILOT_DEPENDENCY_HASH"] = sys.argv[5]
os.environ["LOCAL_LIVE_PILOT_MIGRATION_HASH"] = sys.argv[6]
os.environ["LOCAL_LIVE_PILOT_RISK_POLICY_HASH"] = sys.argv[7]
verdict = json.loads(sys.argv[8])
token = sys.argv[9]
ok, reason, details = verify_pilot_readiness_verdict(verdict, token)
print(json.dumps({"ok": ok, "reason": reason, "details": details}))
`;
  const out = execFileSync('python', ['-I', '-c', py,
    repo,
    verdict.campaignId,
    verdict.gitSha,
    verdict.binding.sourceSha256,
    verdict.binding.dependencySha256,
    verdict.binding.migrationSha256,
    verdict.binding.pilotPolicySha256,
    JSON.stringify(verdict),
    token,
  ], { cwd: repo, env: environment, encoding: 'utf8' });
  return JSON.parse(out);
}

describe('Pilot Readiness Verdict Interoperability', () => {
  const token = 'test-worker-token-xyz-12345';
  const campaign = {
    campaignId: 'pilot-test-campaign-12345678',
  };
  const binding: TrackCBinding = {
    gitSha: 'a'.repeat(40),
    sourceSha256: 'b'.repeat(64),
    dependencySha256: 'c'.repeat(64),
    migrationSha256: 'd'.repeat(64),
    pilotPolicySha256: 'f'.repeat(64),
  };
  const readyReadiness: LocalPilotReadiness = {
    status: 'READY',
    canApprove: true,
    canStart: true,
    implementationReady: { status: 'PASS', checks: [] },
    approvalReady: { status: 'PASS', checks: [] },
    prepared: { status: 'NOT_RUN', checks: [] },
    provenance: { localChecks: 'VERIFIED', reviews: 'VERIFIED', testnet: 'VERIFIED' },
    blockers: [],
  };

  it('generates a verdict that is accepted by Python verifier', () => {
    const verdict = createPilotReadinessVerdict({
      campaign,
      binding,
      readiness: readyReadiness,
      workerIdentityToken: token,
      now: new Date(),
      ttlSeconds: 3600,
    });
    expect(verdict).not.toBeNull();
    const result = pythonVerify(verdict!, token);
    expect(result.ok).toBe(true);
    expect(result.reason).toBe('TRACK_C_VERDICT_VERIFIED');
  });

  it('returns null if readiness is not READY or cannot start', () => {
    const blockedReadiness: LocalPilotReadiness = {
      ...readyReadiness,
      status: 'BLOCKED',
      canStart: false,
    };
    const verdict = createPilotReadinessVerdict({
      campaign,
      binding,
      readiness: blockedReadiness,
      workerIdentityToken: token,
    });
    expect(verdict).toBeNull();
  });

  it('computes deterministic canonical json and signature matching golden fixture', () => {
    const goldenRaw = readFileSync(path.resolve(repo, 'tests/fixtures/pilot_verdict_golden.json'), 'utf8');
    const golden = JSON.parse(goldenRaw);
    const canonical = canonicalVerdictJson(golden.rawPayload);
    expect(canonical).toBe(golden.canonicalJson);
    const signature = signVerdict(golden.rawPayload, golden.token);
    expect(signature).toBe(golden.expectedSignature);
  });

  it('defaults TTL to 300s and clamps TTL to max 3600s', () => {
    const t0 = new Date('2026-10-07T12:00:00.000Z');
    const defaultVerdict = createPilotReadinessVerdict({
      campaign,
      binding,
      readiness: readyReadiness,
      workerIdentityToken: token,
      now: t0,
    });
    expect(defaultVerdict).not.toBeNull();
    expect(defaultVerdict?.expiresAt).toBe('2026-10-07T12:05:00.000Z');

    const maxVerdict = createPilotReadinessVerdict({
      campaign,
      binding,
      readiness: readyReadiness,
      workerIdentityToken: token,
      now: t0,
      ttlSeconds: 7200,
    });
    expect(maxVerdict).not.toBeNull();
    // Clamped to 3600s = 1 hour
    expect(maxVerdict?.expiresAt).toBe('2026-10-07T13:00:00.000Z');
  });

  it('bounds expiresAt by oldestAttestationAt + 24h and campaignExpiresAt', () => {
    const t0 = new Date('2026-10-07T12:00:00.000Z');
    // Oldest attestation was 23 hours and 58 minutes ago -> expires in 2 minutes
    const oldAttestation = new Date(t0.getTime() - (23 * 3600 + 58 * 60) * 1000);
    const verdict = createPilotReadinessVerdict({
      campaign,
      binding,
      readiness: readyReadiness,
      workerIdentityToken: token,
      now: t0,
      ttlSeconds: 300,
      oldestAttestationAt: oldAttestation,
    });
    expect(verdict).not.toBeNull();
    expect(verdict?.expiresAt).toBe(new Date(oldAttestation.getTime() + 24 * 3600 * 1000).toISOString());

    // Campaign expires before TTL
    const campaignExp = '2026-10-07T12:02:00.000Z';
    const campaignWithExpiry = { ...campaign, campaignExpiresAt: campaignExp };
    const campVerdict = createPilotReadinessVerdict({
      campaign: campaignWithExpiry,
      binding,
      readiness: readyReadiness,
      workerIdentityToken: token,
      now: t0,
      ttlSeconds: 300,
    });
    expect(campVerdict).not.toBeNull();
    expect(campVerdict?.expiresAt).toBe(campaignExp);
  });

  it('returns null if computed expiresAt is not greater than issuedAt', () => {
    const t0 = new Date('2026-10-07T12:00:00.000Z');
    // Campaign already expired
    const expiredCampaign = { ...campaign, campaignExpiresAt: '2026-10-07T11:59:00.000Z' };
    const verdict = createPilotReadinessVerdict({
      campaign: expiredCampaign,
      binding,
      readiness: readyReadiness,
      workerIdentityToken: token,
      now: t0,
    });
    expect(verdict).toBeNull();
  });
});
