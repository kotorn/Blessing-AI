import { execFileSync } from 'node:child_process';
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

  it('computes deterministic canonical json and signature', () => {
    const payload = {
      b: 2,
      a: 1,
      nested: { z: 9, y: 8 },
      signature: 'ignored',
    };
    expect(canonicalVerdictJson(payload)).toBe('{"a":1,"b":2,"nested":{"y":8,"z":9}}');
    const sig1 = signVerdict(payload, token);
    const sig2 = signVerdict({ nested: { y: 8, z: 9 }, a: 1, b: 2 }, token);
    expect(sig1).toBe(sig2);
  });
});
