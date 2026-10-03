import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

import type { LocalReleaseFingerprint } from './local-release-runtime.js';
import { buildTrustedPythonVerificationEnvironment, resolveTrustedLocalPythonRuntime } from './local-python-runtime.js';
import { trackCPhases, verifiedTrackCClasses } from './local-pilot-attestation.js';

export const LOCAL_PILOT_CAPABILITY_EVIDENCE_PATH = 'artifacts/local-pilot-capability.json';
export const LOCAL_PILOT_CAPABILITY_MAX_AGE_MS = 24 * 60 * 60 * 1_000;

/** Exact opt-in PostgreSQL acceptance suite must run all three tests, not skip. */
export function localPilotPostgresAcceptancePassed(output: string): boolean {
  const uncolored = output.split(String.fromCharCode(27))
    .map((part) => part.replace(/^\[[0-9;]*m/, '')).join('');
  const lines = uncolored.trim().split(/\r?\n/);
  return /^3 passed(?:, \d+ warnings?)? in \d+(?:\.\d+)?s(?: \([^\r\n]*\))?$/.test(lines.at(-1) || '');
}

const REQUIRED_CHECKS = [
  'TYPESCRIPT_TESTS', 'PYTHON_TESTS', 'LINT', 'BUILD',
  'POSTGRES_17_MIGRATIONS_RESTART', 'LEASE_FENCING', 'PROTECTION_CLOSE', 'TESTNET_E2E',
] as const;
const REQUIRED_REVIEW_DOMAINS = ['AUTH_RELEASE', 'ORDER_RISK', 'PERSISTENCE'] as const;
const LOCAL_PILOT_CHECK_ENV_KEYS = [
  'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'SYSTEMDRIVE',
  'PROGRAMFILES', 'PROGRAMFILES(X86)', 'NUMBER_OF_PROCESSORS',
  'PROCESSOR_ARCHITECTURE', 'OS', 'TEMP', 'TMP', 'CI', 'TZ', 'NODE_ENV',
] as const;

export function buildLocalPilotGitEnvironment(source: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const allowed = new Set(['PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'TEMP', 'TMP']);
  const result: NodeJS.ProcessEnv = {};
  for (const [name, value] of Object.entries(source)) {
    const key = name.toUpperCase();
    if (allowed.has(key) && typeof value === 'string') result[key] = value;
  }
  result.GIT_CONFIG_NOSYSTEM = '1';
  result.GIT_NO_REPLACE_OBJECTS = '1';
  result.GIT_CONFIG_GLOBAL = process.platform === 'win32' ? 'NUL' : '/dev/null';
  return result;
}

export interface LocalPilotCapabilityEvidence {
  schemaVersion: 1;
  gitSha: string;
  sourceSha256: string;
  dependencySha256: string;
  migrationSha256: string;
  pilotPolicySha256: string;
  observedAt: string;
  checks: Array<{ id: string; status: 'PASS'; observedAt: string; resultSha256: string }>;
  reviews: Array<{ domain: string; reviewerId: string; status: 'PASS'; observedAt: string; reportSha256: string }>;
}

export type LocalPilotPhaseStatus = 'PASS' | 'FAIL' | 'NOT_RUN';

export interface LocalPilotReadinessCheck {
  id: string;
  status: LocalPilotPhaseStatus;
  reason: string;
}

/** Diagnostic phases only: a PASS check does not confer approval or start authority. */
export interface LocalPilotReadinessPhase {
  status: LocalPilotPhaseStatus;
  checks: LocalPilotReadinessCheck[];
}

function readinessPhase(checks: LocalPilotReadinessCheck[]): LocalPilotReadinessPhase {
  return {
    status: checks.some((check) => check.status === 'FAIL') ? 'FAIL'
      : checks.length === 0 || checks.some((check) => check.status === 'NOT_RUN') ? 'NOT_RUN' : 'PASS',
    checks,
  };
}

export interface LocalPilotReadiness {
  status: 'READY' | 'BLOCKED';
  canApprove: boolean;
  canStart: boolean;
  implementationReady: LocalPilotReadinessPhase;
  approvalReady: LocalPilotReadinessPhase;
  prepared: LocalPilotReadinessPhase;
  provenance: {
    localChecks: 'VERIFIED' | 'UNVERIFIED';
    reviews: 'VERIFIED' | 'UNVERIFIED';
    testnet: 'VERIFIED' | 'UNVERIFIED';
  };
  blockers: string[];
  ciAttestation?: { status: 'PASS' | 'FAIL' | 'NOT_RUN'; reason: string; scope?: 'CI_ONLY'; gitSha?: string };
}

/** CI cannot authorize local DB receipts, independent reviews, or exchange trials. */
export function localPilotCiAttestation(root: string, gitSha: string): NonNullable<LocalPilotReadiness['ciAttestation']> {
  if (!/^[a-f0-9]{40}$/.test(gitSha) || !committedClean(root, gitSha)) {
    return { status: 'NOT_RUN', reason: 'CI_ATTESTATION_SOURCE_NOT_VERIFIED' };
  }
  if (!existsSync(path.resolve(root, 'artifacts/local-pilot-ci-evidence.json'))
    || !existsSync(path.resolve(root, 'artifacts/local-pilot-ci-attestation.bundle.json'))) {
    return { status: 'NOT_RUN', reason: 'CI_ATTESTATION_MISSING' };
  }
  try {
    const runtime = resolveTrustedLocalPythonRuntime(process.env, root);
    runtime.assertUnchanged();
    const raw = execFileSync(runtime.executable, [
      '-I', path.resolve(root, 'scripts/verify_local_pilot_ci_attestation.py'), '--root', root, '--sha', gitSha,
    ], {
      cwd: root, env: buildTrustedPythonVerificationEnvironment(process.env, runtime.executable),
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], shell: false,
      timeout: 40_000, maxBuffer: 65_536, windowsHide: true,
    });
    runtime.assertUnchanged();
    const result = JSON.parse(raw) as Record<string, unknown>;
    if (!committedClean(root, gitSha)) {
      return { status: 'FAIL', reason: 'CI_ATTESTATION_SOURCE_CHANGED' };
    }
    if (result.status !== 'PASS' || result.reason !== 'CI_ATTESTATION_VERIFIED'
      || result.scope !== 'CI_ONLY' || result.gitSha !== gitSha
      || !sha256(result.subjectSha256) || !sha256(result.bundleSha256)) {
      throw new Error('invalid attestation result');
    }
    return { status: 'PASS', reason: 'CI_ATTESTATION_VERIFIED', scope: 'CI_ONLY', gitSha };
  } catch {
    return { status: 'FAIL', reason: 'CI_ATTESTATION_INVALID' };
  }
}

/** Keep operator credentials and cloud ADC out of test/build child processes. */
export function buildLocalPilotCheckEnvironment(
  source: NodeJS.ProcessEnv,
  isolatedHome: string,
): NodeJS.ProcessEnv {
  const result: NodeJS.ProcessEnv = {};
  const allowed = new Set(LOCAL_PILOT_CHECK_ENV_KEYS.map((key) => key.toUpperCase()));
  for (const [name, value] of Object.entries(source)) {
    if (value !== undefined && allowed.has(name.toUpperCase())) result[name] = value;
  }
  result.HOME = isolatedHome;
  result.USERPROFILE = isolatedHome;
  result.APPDATA = path.join(isolatedHome, 'AppData', 'Roaming');
  result.LOCALAPPDATA = path.join(isolatedHome, 'AppData', 'Local');
  // Contract fixtures must not reload credentials from the repository .env.
  result.BLESSING_DISABLE_TEST_DOTENV = '1';
  return result;
}

function freshTimestamp(value: unknown, now: Date): boolean {
  if (typeof value !== 'string') return false;
  const at = Date.parse(value);
  return Number.isFinite(at) && at <= now.getTime() + 2_000
    && now.getTime() - at <= LOCAL_PILOT_CAPABILITY_MAX_AGE_MS;
}

function sha256(value: unknown): boolean {
  return typeof value === 'string' && /^[a-f0-9]{64}$/.test(value);
}

function readMatchingArtifact(root: string, relativePath: string, digest: unknown): Record<string, unknown> | null {
  if (!sha256(digest)) return null;
  try {
    const raw = readFileSync(path.resolve(root, relativePath));
    if (raw.length > 65_536 || createHash('sha256').update(raw).digest('hex') !== digest) return null;
    const parsed = JSON.parse(raw.toString('utf8')) as unknown;
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

export function protectedEthTestnetTrialPassed(trial: Record<string, unknown>, gitSha: string): boolean {
  const entryId = trial.entry_client_order_id;
  const closeId = trial.close_client_order_id;
  const stopId = trial.stop_client_algo_id;
  const targetId = trial.target_client_algo_id;
  const closeProtection = trial.protection_at_close;
  const validCloseProtection = (() => {
    if (!closeProtection || typeof closeProtection !== 'object' || Array.isArray(closeProtection)) return false;
    const proof = closeProtection as Record<string, unknown>;
    const exactKeys = (value: Record<string, unknown>, keys: string[]) =>
      Object.keys(value).sort().join('|') === [...keys].sort().join('|');
    if (!exactKeys(proof, ['status', 'observed_at', 'close_submission_at', 'stop', 'target'])
      || proof.status !== 'PROTECTED' || typeof proof.observed_at !== 'string'
      || typeof proof.close_submission_at !== 'string'
      || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(proof.observed_at)
      || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/.test(proof.close_submission_at)
      || !Number.isFinite(Date.parse(proof.observed_at))
      || !Number.isFinite(Date.parse(proof.close_submission_at))
      || new Date(proof.observed_at).toISOString() !== proof.observed_at
      || new Date(proof.close_submission_at).toISOString() !== proof.close_submission_at
      || Date.parse(proof.close_submission_at) < Date.parse(proof.observed_at)
      || Date.parse(proof.close_submission_at) - Date.parse(proof.observed_at) > 5000) return false;
    const validAlgo = (value: unknown, expectedClientId: unknown, orderType: string): boolean => {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
      const algo = value as Record<string, unknown>;
      return exactKeys(algo, ['algo_id', 'client_algo_id', 'order_type', 'status', 'close_position', 'reduce_only'])
        && typeof algo.algo_id === 'string' && /^[1-9][0-9]*$/.test(algo.algo_id)
        && algo.client_algo_id === expectedClientId && algo.order_type === orderType
        && algo.status === 'NEW' && algo.close_position === true && algo.reduce_only === false;
    };
    return validAlgo(proof.stop, stopId, 'STOP_MARKET')
      && validAlgo(proof.target, targetId, 'TAKE_PROFIT_MARKET');
  })();
  return trial.trial_type === 'PROTECTED_ETHUSDC_V1'
    && trial.build_sha === gitSha && trial.environment === 'BINANCE_TESTNET'
    && trial.symbol === 'ETHUSDC' && trial.status === 'PASS'
    && trial.protection_status === 'PROTECTED_VERIFIED'
    && trial.close_status === 'VERIFIED'
    && trial.close_order_type === 'MARKET'
    && trial.close_order_reduce_only === true
    && trial.reconciliation_status === 'IN_SYNC' && trial.diff_count === 0
    && typeof trial.entry_fill_count === 'number' && Number.isSafeInteger(trial.entry_fill_count)
    && trial.entry_fill_count >= 1
    && Array.isArray(trial.position_after) && trial.position_after.length === 0
    && Array.isArray(trial.open_orders_after) && trial.open_orders_after.length === 0
    && Array.isArray(trial.open_algo_after) && trial.open_algo_after.length === 0
    && validCloseProtection
    && [entryId, closeId, stopId, targetId].every((id) => typeof id === 'string' && id.length > 0)
    && new Set([entryId, closeId, stopId, targetId]).size === 4;
}

function verifiedEthTestnetTrial(root: string, gitSha: string, digest: unknown, now: Date): boolean {
  if (!sha256(digest)) return false;
  try {
    const folder = path.resolve(root, 'artifacts');
    for (const name of readdirSync(folder)) {
      if (!/^testnet-trial-[A-Za-z0-9_-]+\.json$/.test(name)) continue;
      const file = path.resolve(folder, name);
      const modifiedAt = statSync(file).mtimeMs;
      if (modifiedAt > now.getTime() + 2_000
        || now.getTime() - modifiedAt > LOCAL_PILOT_CAPABILITY_MAX_AGE_MS) continue;
      const raw = readFileSync(file);
      if (raw.length > 65_536 || createHash('sha256').update(raw).digest('hex') !== digest) continue;
      const trial = JSON.parse(raw.toString('utf8')) as Record<string, unknown>;
      if (protectedEthTestnetTrialPassed(trial, gitSha)) return true;
    }
  } catch { /* Missing or malformed Testnet evidence blocks readiness. */ }
  return false;
}

function committedClean(root: string, expectedSha: string): boolean {
  try {
    const head = execFileSync('git', ['rev-parse', '--verify', 'HEAD'], {
      cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
      env: buildLocalPilotGitEnvironment(process.env),
    }).trim();
    const status = execFileSync('git', ['status', '--porcelain', '--untracked-files=all'], {
      cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
      env: buildLocalPilotGitEnvironment(process.env),
    });
    return head === expectedSha && status.trim() === '';
  } catch {
    return false;
  }
}

export interface LocalPilotReadinessOptions {
  root: string;
  fingerprint: LocalReleaseFingerprint;
  pilotPolicySha256: string;
  now?: Date;
  authenticatedServerAuthority?: {
    adminUid: string;
    verifiedAt: string;
  };
}

export function localLivePilotReadiness(options?: LocalPilotReadinessOptions): LocalPilotReadiness {
  // Hashes detect accidental mutation; they do not authenticate who ran a
  // check, authored a review, or observed an exchange lifecycle. Disk JSON is
  // only an audit export and never authorizes a campaign by itself. In
  // particular, a logged-in admin identity is not provenance evidence.
  let provenanceBlockers: string[] = [
    'LOCAL_PILOT_CHECK_PROVENANCE_UNVERIFIED',
    'LOCAL_PILOT_REVIEW_PROVENANCE_UNVERIFIED',
    'LOCAL_PILOT_TESTNET_PROVENANCE_UNVERIFIED',
  ];
  const capabilityBlockers: string[] = [];
  const implementationChecks: LocalPilotReadinessCheck[] = [{
    id: 'SOURCE_COMMIT', status: 'NOT_RUN', reason: 'LOCAL_PILOT_RUNTIME_EVIDENCE_NOT_VERIFIED',
  }];
  let attestedPhases: ReturnType<typeof trackCPhases> | undefined;
  // There is no trusted implementation runner yet. Receipt validation below
  // checks export integrity only; even a complete PASS export cannot fill this gap.
  implementationChecks.push(...REQUIRED_CHECKS.map((id): LocalPilotReadinessCheck => ({
    id, status: 'NOT_RUN', reason: 'LOCAL_PILOT_TRUSTED_CHECK_RUNNER_NOT_AVAILABLE',
  })));

  if (!options) {
    capabilityBlockers.push('LOCAL_PILOT_RUNTIME_EVIDENCE_NOT_VERIFIED');
  } else {
    const root = path.resolve(options.root);
    const now = options.now || new Date();
    const expected = options.fingerprint;
    const sourceVerified = /^[a-f0-9]{40}$/.test(expected.gitSha) && committedClean(root, expected.gitSha);
    implementationChecks[0] = {
      id: 'SOURCE_COMMIT', status: sourceVerified ? 'PASS' : 'FAIL',
      reason: sourceVerified ? 'LOCAL_PILOT_SOURCE_COMMIT_CLEAN' : 'LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN',
    };
    if (!sourceVerified) capabilityBlockers.push('LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN');
    const verifiedClasses = sourceVerified ? verifiedTrackCClasses(root, {
      gitSha: expected.gitSha, sourceSha256: expected.sourceSha256,
      dependencySha256: expected.dependencySha256, migrationSha256: expected.migrationSha256,
      pilotPolicySha256: options.pilotPolicySha256,
    }, now) : [];
    // Source must still match after the external signature verifier returns.
    attestedPhases = trackCPhases(verifiedClasses, sourceVerified && committedClean(root, expected.gitSha));
    provenanceBlockers = attestedPhases.blockers.filter((b) => b.includes('PROVENANCE'));
    let evidence: LocalPilotCapabilityEvidence | null = null;
    try {
      const raw = readFileSync(path.resolve(root, LOCAL_PILOT_CAPABILITY_EVIDENCE_PATH), 'utf8');
      if (raw.length <= 65_536) evidence = JSON.parse(raw) as LocalPilotCapabilityEvidence;
    } catch { /* Missing or malformed evidence blocks approval. */ }
    if (!evidence || evidence.schemaVersion !== 1
      || evidence.gitSha !== expected.gitSha
      || evidence.sourceSha256 !== expected.sourceSha256
      || evidence.dependencySha256 !== expected.dependencySha256
      || evidence.migrationSha256 !== expected.migrationSha256
      || evidence.pilotPolicySha256 !== options.pilotPolicySha256
      || !freshTimestamp(evidence.observedAt, now)) {
      capabilityBlockers.push('LOCAL_PILOT_CAPABILITY_EVIDENCE_MISSING_OR_STALE');
    } else {
      const checks = Array.isArray(evidence.checks) ? evidence.checks : [];
      const checkIds = checks.map((check) => check?.id);
      let checksPassed = true;
      if (checks.length !== REQUIRED_CHECKS.length
        || new Set(checkIds).size !== checks.length) {
        checksPassed = false;
      } else {
        for (const id of REQUIRED_CHECKS) {
          const check = checks.find((c) => c?.id === id);
          if (!check || check.status !== 'PASS' || !freshTimestamp(check.observedAt, now)) {
            checksPassed = false;
            break;
          }
          const result = readMatchingArtifact(root, `artifacts/local-pilot-checks/${id}.json`, check.resultSha256);
          if (!result || result.id !== id || result.gitSha !== expected.gitSha
            || result.status !== 'PASS' || result.exitCode !== 0
            || result.observedAt !== check.observedAt || !sha256(result.outputSha256)
            || (id === 'TESTNET_E2E' && !verifiedEthTestnetTrial(root, expected.gitSha, result.outputSha256, now))) {
            checksPassed = false;
            break;
          }
        }
      }
      if (!checksPassed) {
        capabilityBlockers.push('LOCAL_PILOT_CAPABILITY_TESTS_NOT_VERIFIED');
      }

      const reviews = Array.isArray(evidence.reviews) ? evidence.reviews : [];
      const reviewerIds = reviews.map((review) => review?.reviewerId);
      let reviewsPassed = true;
      if (reviews.length !== REQUIRED_REVIEW_DOMAINS.length
        || new Set(reviewerIds).size !== reviews.length) {
        reviewsPassed = false;
      } else {
        for (const domain of REQUIRED_REVIEW_DOMAINS) {
          const review = reviews.find((r) => r?.domain === domain);
          if (!review || review.status !== 'PASS'
            || typeof review.reviewerId !== 'string' || !review.reviewerId.trim()
            || !freshTimestamp(review.observedAt, now)) {
            reviewsPassed = false;
            break;
          }
          const report = readMatchingArtifact(root, `artifacts/local-pilot-reviews/${domain}.json`, review.reportSha256);
          if (!report || report.domain !== domain || report.gitSha !== expected.gitSha
            || report.reviewerId !== review.reviewerId || report.status !== 'PASS'
            || report.observedAt !== review.observedAt) {
            reviewsPassed = false;
            break;
          }
        }
      }
      if (!reviewsPassed) {
        capabilityBlockers.push('LOCAL_PILOT_INDEPENDENT_REVIEWS_NOT_VERIFIED');
      }
    }
  }

  const blockers = [...new Set([...provenanceBlockers, ...capabilityBlockers])];
  if (attestedPhases) {
    // Signed, validated payloads replace unsigned exports as capability authority.
    // Partial/missing attestations keep the legacy export diagnostics visible.
    if (attestedPhases.status === 'READY') return { ...attestedPhases,
      ciAttestation: options ? localPilotCiAttestation(path.resolve(options.root), options.fingerprint.gitSha) : undefined };
    implementationChecks.splice(0, implementationChecks.length, ...attestedPhases.implementationReady.checks);
    for (const blocker of attestedPhases.blockers) if (!blockers.includes(blocker)) blockers.push(blocker);
  }
  return {
    status: 'BLOCKED' as const,
    canApprove: false,
    canStart: false,
    implementationReady: readinessPhase(implementationChecks),
    approvalReady: readinessPhase(provenanceBlockers.map((reason) => ({
      id: reason, status: 'FAIL', reason,
    }))),
    prepared: readinessPhase([{
      id: 'SERVER_OWNED_PREPARATION', status: 'NOT_RUN',
      reason: 'LOCAL_PILOT_AUTHENTICATED_PREPARATION_EVIDENCE_NOT_AVAILABLE',
    }]),
    ciAttestation: options
      ? localPilotCiAttestation(path.resolve(options.root), options.fingerprint.gitSha)
      : { status: 'NOT_RUN', reason: 'CI_ATTESTATION_SOURCE_NOT_VERIFIED' },
    provenance: attestedPhases?.provenance || { localChecks: 'UNVERIFIED', reviews: 'UNVERIFIED', testnet: 'UNVERIFIED' } as const,
    blockers,
  };
}
