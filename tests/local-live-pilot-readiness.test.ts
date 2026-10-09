import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, utimesSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  LOCAL_PILOT_CAPABILITY_EVIDENCE_PATH,
  buildLocalPilotCheckEnvironment,
  buildLocalPilotGitEnvironment,
  localLivePilotReadiness,
  localPilotCiAttestation,
  localPilotPostgresAcceptancePassed,
  protectedEthTestnetTrialPassed,
  type LocalPilotCapabilityEvidence,
} from '../src/backend/local-live-pilot-readiness.js';
import type { LocalReleaseFingerprint } from '../src/backend/local-release-runtime.js';

const fixtures: string[] = [];

it('requires all five PostgreSQL migration, crash, session and fill acceptance tests with no skipped cases', () => {
  expect(localPilotPostgresAcceptancePassed('..... [100%]\n5 passed in 12.34s\n')).toBe(true);
  expect(localPilotPostgresAcceptancePassed('5 passed, 1 warning in 12.34s')).toBe(true);
  expect(localPilotPostgresAcceptancePassed('3 passed in 12.34s')).toBe(false);
  expect(localPilotPostgresAcceptancePassed('3 passed, 2 skipped in 12.34s')).toBe(false);
  expect(localPilotPostgresAcceptancePassed('5 passed, 1 deselected in 12.34s')).toBe(false);
  expect(localPilotPostgresAcceptancePassed('1 passed, 2 skipped in 1.00s')).toBe(false);
  expect(localPilotPostgresAcceptancePassed('3 skipped in 0.01s')).toBe(false);
  expect(localPilotPostgresAcceptancePassed('2 passed in 1.00s')).toBe(false);
});

it('does not invoke a verifier for missing CI artifacts or a dirty checkout', () => {
  const { root, evidence } = fixture();
  expect(localPilotCiAttestation(root, evidence.gitSha)).toEqual({
    status: 'NOT_RUN', reason: 'CI_ATTESTATION_MISSING',
  });
  writeFileSync(path.join(root, 'unreviewed.txt'), 'not committed');
  expect(localPilotCiAttestation(root, evidence.gitSha)).toEqual({
    status: 'NOT_RUN', reason: 'CI_ATTESTATION_SOURCE_NOT_VERIFIED',
  });
});
afterEach(() => {
  for (const root of fixtures.splice(0)) {
    if (path.basename(root).startsWith('blessing-pilot-readiness-')
      && path.dirname(root) === tmpdir()) rmSync(root, { recursive: true, force: true });
  }
});

function fixture() {
  const root = mkdtempSync(path.join(tmpdir(), 'blessing-pilot-readiness-'));
  fixtures.push(root);
  writeFileSync(path.join(root, '.gitignore'), 'artifacts/\n');
  execFileSync('git', ['init', '-q'], { cwd: root });
  execFileSync('git', ['add', '.gitignore'], { cwd: root });
  execFileSync('git', ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
    'commit', '-qm', 'readiness fixture'], { cwd: root });
  const gitSha = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim();
  const digest = (value: string) => value.repeat(64);
  const fingerprint = {
    gitSha,
    sourceSha256: digest('a'),
    dependencySha256: digest('b'),
    migrationSha256: digest('c'),
  } as LocalReleaseFingerprint;
  const now = new Date('2026-09-28T00:00:00.000Z');
  const evidence: LocalPilotCapabilityEvidence = {
    schemaVersion: 1,
    gitSha,
    sourceSha256: fingerprint.sourceSha256,
    dependencySha256: fingerprint.dependencySha256,
    migrationSha256: fingerprint.migrationSha256,
    pilotPolicySha256: digest('d'),
    observedAt: now.toISOString(),
    checks: [
      'TYPESCRIPT_TESTS', 'PYTHON_TESTS', 'LINT', 'BUILD',
      'POSTGRES_17_MIGRATIONS_RESTART', 'LEASE_FENCING', 'PROTECTION_CLOSE', 'TESTNET_E2E',
    ].map((id) => ({ id, status: 'PASS', observedAt: now.toISOString(), resultSha256: '' })),
    reviews: ['AUTH_RELEASE', 'ORDER_RISK', 'PERSISTENCE'].map((domain, index) => ({
      domain, reviewerId: `reviewer-${index}`, status: 'PASS', observedAt: now.toISOString(),
      reportSha256: '',
    })),
  };
  mkdirSync(path.join(root, 'artifacts'));
  mkdirSync(path.join(root, 'artifacts', 'local-pilot-checks'));
  mkdirSync(path.join(root, 'artifacts', 'local-pilot-reviews'));
  const hash = (raw: string) => createHash('sha256').update(raw).digest('hex');
  const trialRaw = JSON.stringify({ trial_type: 'PROTECTED_ETHUSDC_V1', build_sha: gitSha,
    environment: 'BINANCE_TESTNET', symbol: 'ETHUSDC', status: 'PASS',
    protection_status: 'PROTECTED_VERIFIED', close_status: 'VERIFIED',
    reconciliation_status: 'IN_SYNC', diff_count: 0, entry_fill_count: 1,
    position_after: [], open_orders_after: [], open_algo_after: [],
    entry_client_order_id: 'entry-1', close_client_order_id: 'close-1',
    stop_client_algo_id: 'stop-1', target_client_algo_id: 'target-1' });
  const trialPath = path.join(root, 'artifacts', 'testnet-trial-fixture.json');
  writeFileSync(trialPath, trialRaw);
  utimesSync(trialPath, now, now);
  for (const check of evidence.checks) {
    const raw = JSON.stringify({ id: check.id, gitSha, status: 'PASS', exitCode: 0,
      observedAt: check.observedAt, outputSha256: check.id === 'TESTNET_E2E' ? hash(trialRaw) : digest('e') });
    writeFileSync(path.join(root, 'artifacts', 'local-pilot-checks', `${check.id}.json`), raw);
    check.resultSha256 = hash(raw);
  }
  for (const review of evidence.reviews) {
    const raw = JSON.stringify({ domain: review.domain, gitSha, reviewerId: review.reviewerId,
      status: 'PASS', observedAt: review.observedAt });
    writeFileSync(path.join(root, 'artifacts', 'local-pilot-reviews', `${review.domain}.json`), raw);
    review.reportSha256 = hash(raw);
  }
  const save = () => writeFileSync(path.join(root, LOCAL_PILOT_CAPABILITY_EVIDENCE_PATH), JSON.stringify(evidence));
  save();
  const pilotPolicySha256 = digest('d');
  const evaluate = () => localLivePilotReadiness({ root, fingerprint, pilotPolicySha256, now });
  return { root, evidence, save, evaluate, fingerprint, now, pilotPolicySha256 };
}

describe('Local live pilot capability gate', () => {
  it('normalizes allowed Git environment keys and excludes operator credentials', () => {
    const environment = buildLocalPilotGitEnvironment({
      Path: 'C:\\Windows\\System32', SystemRoot: 'C:\\Windows',
      BINANCE_MAINNET_API_SECRET: 'must-not-forward',
    });

    expect(environment).toMatchObject({ PATH: 'C:\\Windows\\System32', SYSTEMROOT: 'C:\\Windows' });
    expect(environment.BINANCE_MAINNET_API_SECRET).toBeUndefined();
    expect(environment.GIT_NO_REPLACE_OBJECTS).toBe('1');
  });

  it('does not forward cloud or exchange credentials into evidence test processes', () => {
    const child = buildLocalPilotCheckEnvironment({
      PATH: 'C:\\tools',
      BINANCE_API_KEY: 'never-forward',
      BINANCE_API_SECRET: 'never-forward',
      GOOGLE_APPLICATION_CREDENTIALS: 'never-forward',
      FIREBASE_PRIVATE_KEY: 'never-forward',
      BLESSING_MIGRATION_TEST_DSN: 'never-forward',
      LOCAL_PYTHON_EXECUTABLE: 'untrusted-wrapper.exe',
      HOME: 'operator-home',
    }, 'C:\\isolated-check-home');

    expect(child.PATH).toBe('C:\\tools');
    expect(child.HOME).toBe('C:\\isolated-check-home');
    expect(child.USERPROFILE).toBe('C:\\isolated-check-home');
    expect(child.BLESSING_DISABLE_TEST_DOTENV).toBe('1');
    expect(child.BINANCE_API_KEY).toBeUndefined();
    expect(child.BINANCE_API_SECRET).toBeUndefined();
    expect(child.GOOGLE_APPLICATION_CREDENTIALS).toBeUndefined();
    expect(child.FIREBASE_PRIVATE_KEY).toBeUndefined();
    expect(child.BLESSING_MIGRATION_TEST_DSN).toBeUndefined();
    expect(child.LOCAL_PYTHON_EXECUTABLE).toBeUndefined();
  });

  it('does not accept the legacy BTCUSDT amend/cancel trial as protected ETHUSDC evidence', () => {
    const trial = {
      trial_type: 'PROTECTED_ETHUSDC_V1', build_sha: 'a'.repeat(40),
      environment: 'BINANCE_TESTNET', symbol: 'ETHUSDC', status: 'PASS',
      protection_status: 'PROTECTED_VERIFIED', close_status: 'VERIFIED',
      close_order_type: 'MARKET', close_order_reduce_only: true,
      reconciliation_status: 'IN_SYNC', diff_count: 0, entry_fill_count: 1,
      filled_quantity: '0.01',
      position_after: [], open_orders_after: [], open_algo_after: [],
      entry_client_order_id: 'entry', close_client_order_id: 'close',
      stop_client_algo_id: 'stop', target_client_algo_id: 'target',
      protection_at_close: {
        status: 'PROTECTED', observed_at: '2026-10-03T00:00:00.000Z',
        close_submission_at: '2026-10-03T00:00:00.000Z',
        stop: { algo_id: '101', client_algo_id: 'stop', order_type: 'STOP_MARKET',
          status: 'NEW', close_position: false, reduce_only: true, quantity: '0.01' },
        target: { algo_id: '102', client_algo_id: 'target', order_type: 'TAKE_PROFIT_MARKET',
          status: 'NEW', close_position: false, reduce_only: true, quantity: '0.01' },
      },
    };
    expect(protectedEthTestnetTrialPassed(trial, 'a'.repeat(40))).toBe(true);
    expect(protectedEthTestnetTrialPassed({ ...trial, protection_at_close: undefined }, 'a'.repeat(40))).toBe(false);
    expect(protectedEthTestnetTrialPassed({ ...trial, protection_at_close: {
      ...trial.protection_at_close, observed_at: '2026-02-30T00:00:00.000Z',
    } }, 'a'.repeat(40))).toBe(false);
    expect(protectedEthTestnetTrialPassed({ ...trial, protection_at_close: {
      ...trial.protection_at_close, close_submission_at: '2026-10-03T00:00:05.001Z',
    } }, 'a'.repeat(40))).toBe(false);
    expect(protectedEthTestnetTrialPassed({ ...trial, close_order_reduce_only: false }, 'a'.repeat(40))).toBe(false);
    expect(protectedEthTestnetTrialPassed({ ...trial, protection_at_close: {
      ...trial.protection_at_close, stop: { ...trial.protection_at_close.stop, status: 'CANCELED' },
    } }, 'a'.repeat(40))).toBe(false);
    expect(protectedEthTestnetTrialPassed({ ...trial, protection_at_close: {
      ...trial.protection_at_close, target: { ...trial.protection_at_close.target, client_algo_id: 'other' },
    } }, 'a'.repeat(40))).toBe(false);
    expect(protectedEthTestnetTrialPassed({ ...trial, protection_at_close: {
      ...trial.protection_at_close, stop: { ...trial.protection_at_close.stop, close_position: true, reduce_only: false },
    } }, 'a'.repeat(40))).toBe(false);
    expect(protectedEthTestnetTrialPassed({ ...trial, protection_at_close: {
      ...trial.protection_at_close, target: { ...trial.protection_at_close.target, quantity: '0.02' },
    } }, 'a'.repeat(40))).toBe(false);
    expect(protectedEthTestnetTrialPassed({ ...trial, filled_quantity: '0.05' }, 'a'.repeat(40))).toBe(false);
    expect(protectedEthTestnetTrialPassed({ ...trial, symbol: 'BTCUSDT' }, 'a'.repeat(40))).toBe(false);
    expect(protectedEthTestnetTrialPassed({ ...trial, stop_client_algo_id: '' }, 'a'.repeat(40))).toBe(false);
    expect(protectedEthTestnetTrialPassed({ ...trial, open_algo_after: [{}] }, 'a'.repeat(40))).toBe(false);
  });
  it('stays blocked until runtime readiness is verified instead of trusting an empty static list', () => {
    const readiness = localLivePilotReadiness();

    expect(readiness.status).toBe('BLOCKED');
    expect(readiness.canApprove).toBe(false);
    expect(readiness.canStart).toBe(false);
    expect(readiness.blockers).toContain('LOCAL_PILOT_RUNTIME_EVIDENCE_NOT_VERIFIED');
    expect(readiness.implementationReady.status).toBe('NOT_RUN');
    expect(readiness.implementationReady.checks.every((check) => check.status === 'NOT_RUN')).toBe(true);
    expect(readiness.approvalReady.status).toBe('FAIL');
    expect(readiness.prepared.status).toBe('NOT_RUN');
  });

  it('reports a verified source check but no implementation test execution from complete receipts', () => {
    const { evaluate } = fixture();
    const readiness = evaluate();

    expect(readiness.implementationReady.status).toBe('NOT_RUN');
    expect(readiness.implementationReady.checks[0]).toEqual({
      id: 'SOURCE_COMMIT', status: 'PASS', reason: 'LOCAL_PILOT_SOURCE_COMMIT_CLEAN',
    });
    expect(readiness.implementationReady.checks.slice(1)).toEqual([
      'TYPESCRIPT_TESTS', 'PYTHON_TESTS', 'LINT', 'BUILD',
      'POSTGRES_17_MIGRATIONS_RESTART', 'LEASE_FENCING', 'PROTECTION_CLOSE', 'TESTNET_E2E',
    ].map((id) => ({ id, status: 'NOT_RUN', reason: 'LOCAL_PILOT_TRUSTED_CHECK_RUNNER_NOT_AVAILABLE' })));
    expect(readiness.approvalReady.checks.map((check) => check.reason)).toEqual([
      'LOCAL_PILOT_CHECK_PROVENANCE_UNVERIFIED',
      'LOCAL_PILOT_REVIEW_PROVENANCE_UNVERIFIED',
      'LOCAL_PILOT_TESTNET_PROVENANCE_UNVERIFIED',
    ]);
    expect(readiness.approvalReady.checks.every((check) => check.status === 'FAIL')).toBe(true);
    expect(readiness.prepared).toEqual({ status: 'NOT_RUN', checks: [{
      id: 'SERVER_OWNED_PREPARATION', status: 'NOT_RUN',
      reason: 'LOCAL_PILOT_AUTHENTICATED_PREPARATION_EVIDENCE_NOT_AVAILABLE',
    }] });
    expect(readiness).toMatchObject({ status: 'BLOCKED', canApprove: false, canStart: false });
  });

  it('keeps absent and malformed receipts NOT_RUN rather than claiming executed test failures', () => {
    const { root, evaluate } = fixture();
    const evidencePath = path.join(root, LOCAL_PILOT_CAPABILITY_EVIDENCE_PATH);
    for (const raw of [null, '{malformed', JSON.stringify({ schemaVersion: 1 })]) {
      if (raw === null) rmSync(evidencePath);
      else writeFileSync(evidencePath, raw);
      const readiness = evaluate();
      expect(readiness.implementationReady.status).toBe('NOT_RUN');
      expect(readiness.implementationReady.checks.slice(1).every((check) => check.status === 'NOT_RUN')).toBe(true);
      expect(readiness.blockers).toContain('LOCAL_PILOT_CAPABILITY_EVIDENCE_MISSING_OR_STALE');
      expect(readiness).toMatchObject({ canApprove: false, canStart: false });
    }
  });

  it('reports source failure without promoting unexecuted tests or preparation', () => {
    const { root, evaluate } = fixture();
    writeFileSync(path.join(root, 'unreviewed.ts'), 'export const unreviewed = true;\n');
    const readiness = evaluate();
    expect(readiness.implementationReady.status).toBe('FAIL');
    expect(readiness.implementationReady.checks[0].status).toBe('FAIL');
    expect(readiness.implementationReady.checks.slice(1).every((check) => check.status === 'NOT_RUN')).toBe(true);
    expect(readiness.approvalReady.status).toBe('FAIL');
    expect(readiness.prepared.status).toBe('NOT_RUN');
    expect(readiness.blockers).toContain('LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN');
    expect(readiness).toMatchObject({ canApprove: false, canStart: false });
  });

  it('ignores self-authored phase and runner claims in capability JSON', () => {
    const { root, evidence, evaluate } = fixture();
    writeFileSync(path.join(root, LOCAL_PILOT_CAPABILITY_EVIDENCE_PATH), JSON.stringify({
      ...evidence, implementationReady: { status: 'PASS' }, approvalReady: { status: 'PASS' },
      prepared: { status: 'PASS' }, trustedRunner: true, canApprove: true, canStart: true,
    }));
    expect(evaluate()).toMatchObject({
      status: 'BLOCKED', canApprove: false, canStart: false,
      implementationReady: { status: 'NOT_RUN' }, approvalReady: { status: 'FAIL' },
      prepared: { status: 'NOT_RUN' },
    });
  });

  it('never promotes self-authored check, review, or Testnet JSON into trusted readiness', () => {
    const { root, evidence, save, evaluate } = fixture();
    const initial = evaluate();
    expect(initial.status).toBe('BLOCKED');
    expect(initial.canApprove).toBe(false);
    expect(initial.canStart).toBe(false);
    expect(initial.provenance).toEqual({ localChecks: 'UNVERIFIED', reviews: 'UNVERIFIED', testnet: 'UNVERIFIED' });
    expect(initial.blockers).toContain('LOCAL_PILOT_REVIEW_PROVENANCE_UNVERIFIED');
    expect(initial.blockers).toContain('LOCAL_PILOT_TESTNET_PROVENANCE_UNVERIFIED');
    const trialPath = path.join(root, 'artifacts', 'testnet-trial-fixture.json');
    const trial = readFileSync(trialPath);
    rmSync(trialPath);
    expect(evaluate().blockers).toContain('LOCAL_PILOT_CAPABILITY_TESTS_NOT_VERIFIED');
    writeFileSync(trialPath, trial);
    utimesSync(trialPath, new Date(evidence.observedAt), new Date(evidence.observedAt));
    evidence.checks.pop();
    save();
    expect(evaluate().blockers).toContain('LOCAL_PILOT_CAPABILITY_TESTS_NOT_VERIFIED');
    const protection = JSON.parse(readFileSync(path.join(root, 'artifacts', 'local-pilot-checks', 'PROTECTION_CLOSE.json'), 'utf8')) as object;
    evidence.checks.push({
      id: 'PROTECTION_CLOSE', status: 'PASS', observedAt: evidence.observedAt,
      resultSha256: createHash('sha256').update(JSON.stringify(protection)).digest('hex'),
    });
    evidence.reviews[1].reviewerId = evidence.reviews[0].reviewerId;
    save();
    expect(evaluate().blockers).toContain('LOCAL_PILOT_INDEPENDENT_REVIEWS_NOT_VERIFIED');
    evidence.reviews[1].reviewerId = 'reviewer-1';
    save();
    writeFileSync(path.join(root, 'unreviewed.ts'), 'export const unreviewed = true;\n');
    expect(evaluate().blockers).toContain('LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN');
  });

  it('never treats authenticated admin identity as a substitute for provenance attestations', () => {
    const shared = JSON.parse(readFileSync(
      path.resolve(process.cwd(), 'tests/fixtures/local-pilot-admin-readiness.json'), 'utf8',
    )) as {
      identityCases: Array<{ id: string; adminUid: string }>;
      expected: {
        status: 'BLOCKED'; canApprove: false; canStart: false;
        provenance: { localChecks: 'UNVERIFIED'; reviews: 'UNVERIFIED'; testnet: 'UNVERIFIED' };
        requiredBlockers: string[];
      };
    };

    for (const identityCase of shared.identityCases) {
      const { root, fingerprint, save, evidence, now, pilotPolicySha256 } = fixture();
      evidence.reviews[1].reviewerId = 'reviewer-1';
      save();
      const readiness = localLivePilotReadiness({
        root,
        fingerprint,
        pilotPolicySha256,
        now,
        authenticatedServerAuthority: {
          adminUid: identityCase.adminUid,
          verifiedAt: now.toISOString(),
        },
      });

      expect({
        id: identityCase.id,
        status: readiness.status,
        canApprove: readiness.canApprove,
        canStart: readiness.canStart,
        provenance: readiness.provenance,
      }).toEqual({
        id: identityCase.id,
        status: shared.expected.status,
        canApprove: shared.expected.canApprove,
        canStart: shared.expected.canStart,
        provenance: shared.expected.provenance,
      });
      for (const blocker of shared.expected.requiredBlockers) {
        expect(readiness.blockers).toContain(blocker);
      }
    }
  }, 20_000);
});
