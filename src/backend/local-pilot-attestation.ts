import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import type { TrackCBinding } from './local-release-runtime.js';
import {
  TRUSTED_PYTHON_RESOLVER_CODES, buildTrustedPythonVerificationEnvironment, resolveTrustedLocalPythonRuntime,
} from './local-python-runtime.js';

export const LOCAL_PILOT_VERIFIER_RUNTIME_UNAVAILABLE = 'LOCAL_PILOT_VERIFIER_RUNTIME_UNAVAILABLE';

export const TRACK_C_CLASSES = ['CHECKS', 'REVIEW_AUTH_RELEASE', 'REVIEW_ORDER_RISK', 'REVIEW_PERSISTENCE', 'TESTNET_ETHUSDC'] as const;
export type { TrackCBinding };

/** Projects authenticated verifier output. Never call this with a browser/disk receipt. */
export function trackCPhases(passClasses: string[], sourceClean: boolean) {
  const classes = new Set(sourceClean ? passClasses.filter((c) => (TRACK_C_CLASSES as readonly string[]).includes(c)) : []);
  const groups = [
    ['localChecks', ['CHECKS'], 'LOCAL_PILOT_CHECK_PROVENANCE_UNVERIFIED'],
    ['reviews', TRACK_C_CLASSES.slice(1, 4), 'LOCAL_PILOT_REVIEW_PROVENANCE_UNVERIFIED'],
    ['testnet', ['TESTNET_ETHUSDC'], 'LOCAL_PILOT_TESTNET_PROVENANCE_UNVERIFIED'],
  ] as const;
  const blockers: string[] = groups.filter(([, needed]) => !needed.every((c) => classes.has(c))).map(([, , b]) => b);
  if (!sourceClean) blockers.push('LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN');
  const checks: Array<{ id: string; status: 'PASS' | 'FAIL' | 'NOT_RUN'; reason: string }> = [{
    id: 'SOURCE_COMMIT', status: sourceClean ? 'PASS' : 'FAIL',
    reason: sourceClean ? 'LOCAL_PILOT_SOURCE_COMMIT_CLEAN' : 'LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN',
  }];
  for (const id of ['TYPESCRIPT_TESTS', 'PYTHON_TESTS', 'LINT', 'BUILD', 'POSTGRES_17_MIGRATIONS_RESTART', 'LEASE_FENCING', 'PROTECTION_CLOSE', 'TESTNET_E2E']) {
    const passed = classes.has(id === 'TESTNET_E2E' ? 'TESTNET_ETHUSDC' : 'CHECKS');
    checks.push({ id, status: passed ? 'PASS' : 'NOT_RUN', reason: passed ? 'TRACK_C_ATTESTATION_VERIFIED' : 'LOCAL_PILOT_TRUSTED_CHECK_RUNNER_NOT_AVAILABLE' });
  }
  return {
    status: blockers.length ? 'BLOCKED' as const : 'READY' as const,
    canApprove: blockers.length === 0, canStart: blockers.length === 0, blockers,
    provenance: Object.fromEntries(groups.map(([name, needed]) => [name, needed.every((c) => classes.has(c)) ? 'VERIFIED' : 'UNVERIFIED'])) as {
      localChecks: 'VERIFIED' | 'UNVERIFIED'; reviews: 'VERIFIED' | 'UNVERIFIED'; testnet: 'VERIFIED' | 'UNVERIFIED';
    },
    implementationReady: { status: !sourceClean ? 'FAIL' as const : checks.every((c) => c.status === 'PASS') ? 'PASS' as const : 'NOT_RUN' as const, checks },
    approvalReady: { status: blockers.length ? 'FAIL' as const : 'PASS' as const,
      checks: blockers.map((b) => ({ id: b, status: 'FAIL' as const, reason: b })) },
    prepared: { status: 'NOT_RUN' as const, checks: [{ id: 'SERVER_OWNED_PREPARATION', status: 'NOT_RUN' as const,
      reason: 'LOCAL_PILOT_AUTHENTICATED_PREPARATION_EVIDENCE_NOT_AVAILABLE' }] },
  };
}

/**
 * Maps a thrown error to a fixed code. Only exact resolver codes pass through; raw stderr,
 * paths, tokens and any other message collapse to the generic runtime code.
 */
export function verifierDiagnosticCode(error: unknown): string {
  if (error instanceof Error && (TRUSTED_PYTHON_RESOLVER_CODES as readonly string[]).includes(error.message)) {
    return error.message;
  }
  return LOCAL_PILOT_VERIFIER_RUNTIME_UNAVAILABLE;
}

/** Production has no injectable verifier and never consumes a stored PASS result. */
export function verifyTrackCAttestations(
  root: string, binding: TrackCBinding, now: Date,
): { classes: string[]; diagnostic?: string } {
  if (!TRACK_C_CLASSES.some((c) => existsSync(path.join(root, 'artifacts/local-pilot-attestations', `${c}.json`)))) return { classes: [] };
  try {
    const runtime = resolveTrustedLocalPythonRuntime(process.env, root);
    runtime.assertUnchanged();
    const result = JSON.parse(execFileSync(runtime.executable, [
      '-I', path.join(root, 'scripts/verify_local_pilot_track_c.py'), '--root', root,
      '--binding', JSON.stringify(binding), '--now', now.toISOString(),
    ], { cwd: root, env: buildTrustedPythonVerificationEnvironment(process.env, runtime.executable),
      shell: false, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 240_000, maxBuffer: 65536, windowsHide: true }));
    runtime.assertUnchanged();
    if (result.sourceClean !== true || Object.keys(binding).some((k) => result.binding?.[k] !== binding[k as keyof TrackCBinding])
      || !Array.isArray(result.classes) || result.classes.length !== TRACK_C_CLASSES.length
      || TRACK_C_CLASSES.some((c) => result.classes.filter((r: any) => r?.evidenceClass === c).length !== 1)
      || result.classes.some((r: any) => !['PASS', 'FAIL', 'NOT_RUN'].includes(r.status))) return { classes: [] };
    return { classes: result.classes.filter((r: any) => r.status === 'PASS' && r.reason === 'TRACK_C_ATTESTATION_VERIFIED').map((r: any) => r.evidenceClass) };
  } catch (error) {
    return { classes: [], diagnostic: verifierDiagnosticCode(error) };
  }
}

export function verifiedTrackCClasses(root: string, binding: TrackCBinding, now: Date): string[] {
  return verifyTrackCAttestations(root, binding, now).classes;
}
