/** Collect evidence for one clean, reviewed Local Pilot commit. No exchange calls. */
import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

import {
  buildLocalPilotCheckEnvironment,
  buildLocalPilotGitEnvironment,
  LOCAL_PILOT_CAPABILITY_EVIDENCE_PATH,
  localLivePilotReadiness,
  localPilotPostgresAcceptancePassed,
  protectedEthTestnetTrialPassed,
  type LocalPilotCapabilityEvidence,
} from '../src/backend/local-live-pilot-readiness.js';
import {
  computeLocalReleaseFingerprint,
  localLivePilotPolicySha256,
} from '../src/backend/local-release-runtime.js';
import { resolveTrustedLocalPythonRuntime } from '../src/backend/local-python-runtime.js';
import { localPilotNpmCommand } from '../src/backend/local-pilot-check-command.js';

const root = path.resolve(import.meta.dirname, '..');
let isolatedCheckHome: string | null = null;
const sha256 = (data: string | Buffer) => createHash('sha256').update(data).digest('hex');
const git = (args: string[]) => execFileSync('git', args, {
  cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
  env: buildLocalPilotGitEnvironment(process.env),
}).trim();

function assertCleanCommit(): string {
  const sha = git(['rev-parse', '--verify', 'HEAD']);
  if (!/^[a-f0-9]{40,64}$/.test(sha) || git(['status', '--porcelain', '--untracked-files=all'])) {
    throw new Error('LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN');
  }
  return sha;
}

function runCheck(
  id: string,
  command: string,
  args: string[],
  environment: NodeJS.ProcessEnv,
  gitSha: string,
  beforeRun?: () => void,
) {
  beforeRun?.();
  const child = spawnSync(command, args, {
    cwd: root, env: environment, encoding: 'buffer', maxBuffer: 16 * 1024 * 1024,
    timeout: 15 * 60 * 1_000, windowsHide: true, shell: false,
  });
  if (child.error || child.status !== 0 || child.signal) {
    throw new Error(`${id}_FAILED`);
  }
  if (id === 'POSTGRES_17_MIGRATIONS_RESTART'
    && !localPilotPostgresAcceptancePassed((child.stdout || Buffer.alloc(0)).toString('utf8'))) {
    throw new Error('POSTGRES_17_ACCEPTANCE_INCOMPLETE_OR_SKIPPED');
  }
  const observedAt = new Date().toISOString();
  const outputSha256 = sha256(Buffer.concat([child.stdout || Buffer.alloc(0), child.stderr || Buffer.alloc(0)]));
  const record = { id, gitSha, status: 'PASS', exitCode: 0, observedAt, outputSha256 };
  const raw = JSON.stringify(record);
  const file = path.resolve(root, 'artifacts', 'local-pilot-checks', `${id}.json`);
  writeFileSync(file, raw, { flag: 'w' });
  return { id, status: 'PASS' as const, observedAt, resultSha256: sha256(raw) };
}

function commandForNpm(script: 'test' | 'lint' | 'build'): [string, string[]] {
  return localPilotNpmCommand(script);
}

function readCurrentEthTestnetTrial(gitSha: string): { observedAt: string; trialSha256: string } {
  const folder = path.resolve(root, 'artifacts');
  const trials = readdirSync(folder)
    .filter((name) => /^testnet-trial-[A-Za-z0-9_-]+\.json$/.test(name))
    .map((name) => ({ name, modifiedAt: statSync(path.resolve(folder, name)).mtimeMs }))
    .sort((a, b) => b.modifiedAt - a.modifiedAt);
  for (const trial of trials) {
    const file = path.resolve(folder, trial.name);
    const raw = readFileSync(file);
    if (raw.length > 65_536) continue;
    let value: Record<string, unknown>;
    try { value = JSON.parse(raw.toString('utf8')) as Record<string, unknown>; }
    catch { continue; }
    const ageMs = Date.now() - trial.modifiedAt;
    if (ageMs < 0 || ageMs > 24 * 60 * 60 * 1_000
      || !protectedEthTestnetTrialPassed(value, gitSha)) continue;
    return { observedAt: new Date(trial.modifiedAt).toISOString(), trialSha256: sha256(raw) };
  }
  throw new Error('LOCAL_PILOT_ETHUSDC_TESTNET_E2E_NOT_VERIFIED');
}

function main(): void {
  const gitSha = assertCleanCommit();
  const apiKeyVersion = (process.env.LOCAL_MAINNET_API_KEY_VERSION || '').trim();
  const apiSecretVersion = (process.env.LOCAL_MAINNET_API_SECRET_VERSION || '').trim();
  const projectId = (process.env.LOCAL_SECRET_MANAGER_PROJECT_ID || '').trim();
  if (!apiKeyVersion || !apiSecretVersion || !projectId) throw new Error('LOCAL_PILOT_PINNED_SECRET_IDENTITY_MISSING');
  if (!process.env.BLESSING_MIGRATION_TEST_DSN) throw new Error('LOCAL_PILOT_ISOLATED_POSTGRES_DSN_MISSING');
  const fingerprint = computeLocalReleaseFingerprint({
    root, runId: 'run-capability-attestation', apiKeyVersion, apiSecretVersion,
    secretManagerProjectId: projectId,
  });
  if (fingerprint.gitSha !== gitSha) throw new Error('LOCAL_PILOT_FINGERPRINT_CHANGED');
  const policySha256 = localLivePilotPolicySha256(root);
  const checkDir = path.resolve(root, 'artifacts', 'local-pilot-checks');
  mkdirSync(checkDir, { recursive: true });
  // An interrupted or failed recheck must not leave a previous PASS usable.
  writeFileSync(path.resolve(root, LOCAL_PILOT_CAPABILITY_EVIDENCE_PATH),
    JSON.stringify({ schemaVersion: 1, status: 'IN_PROGRESS', gitSha }));
  isolatedCheckHome = mkdtempSync(path.join(tmpdir(), 'blessing-local-pilot-check-'));
  const environment = buildLocalPilotCheckEnvironment(process.env, isolatedCheckHome);
  // Resolve the PATH-selected interpreter once and require the official
  // Python Software Foundation Authenticode signer before any child sees DSN.
  const pythonRuntime = resolveTrustedLocalPythonRuntime(environment, root);
  const python = pythonRuntime.executable;
  const verifyPythonBeforeUse = () => pythonRuntime.assertUnchanged();
  const checks: LocalPilotCapabilityEvidence['checks'] = [];
  for (const [id, script] of [
    ['TYPESCRIPT_TESTS', 'test'], ['LINT', 'lint'], ['BUILD', 'build'],
  ] as const) {
    const [command, args] = commandForNpm(script);
    checks.push(runCheck(id, command, args, environment, gitSha));
  }
  checks.push(runCheck('PYTHON_TESTS', python, ['-I', '-m', 'pytest', '-q', 'tests/python'], environment, gitSha, verifyPythonBeforeUse));
  const postgresTestEnvironment = {
    ...environment,
    BLESSING_MIGRATION_TEST_DSN: process.env.BLESSING_MIGRATION_TEST_DSN,
    BLESSING_MIGRATION_TEST_PORT: process.env.BLESSING_MIGRATION_TEST_PORT || '55433',
  };
  checks.push(runCheck(
    'POSTGRES_17_MIGRATIONS_RESTART', python, ['-I', '-m', 'pytest', '-q',
      'tests/python/test_local_postgres_migrations.py::test_populated_migration_upgrade_through_020_survives_reconnect',
      'tests/python/test_local_pilot_process_kill.py'],
    postgresTestEnvironment, gitSha, verifyPythonBeforeUse,
  ));
  checks.push(runCheck('LEASE_FENCING', python, ['-I', '-m', 'pytest', '-q',
    'tests/python/test_execution_lease.py', 'tests/python/test_local_postgres_identity.py'], environment, gitSha, verifyPythonBeforeUse));
  checks.push(runCheck('PROTECTION_CLOSE', python, ['-I', '-m', 'pytest', '-q',
    'tests/python/test_binance_protection.py', 'tests/python/test_local_pilot_lifecycle_monitors.py'], environment, gitSha, verifyPythonBeforeUse));
  const trial = readCurrentEthTestnetTrial(gitSha);
  const trialRecord = { id: 'TESTNET_E2E', gitSha, status: 'PASS', exitCode: 0,
    observedAt: trial.observedAt, outputSha256: trial.trialSha256 };
  const trialRecordRaw = JSON.stringify(trialRecord);
  writeFileSync(path.resolve(checkDir, 'TESTNET_E2E.json'), trialRecordRaw);
  checks.push({ id: 'TESTNET_E2E', status: 'PASS', observedAt: trial.observedAt,
    resultSha256: sha256(trialRecordRaw) });
  const reviews: LocalPilotCapabilityEvidence['reviews'] = [];
  for (const domain of ['AUTH_RELEASE', 'ORDER_RISK', 'PERSISTENCE']) {
    const raw = readFileSync(path.resolve(root, 'artifacts', 'local-pilot-reviews', `${domain}.json`));
    if (raw.length > 65_536) throw new Error('LOCAL_PILOT_REVIEW_INVALID');
    const review = JSON.parse(raw.toString('utf8')) as Record<string, unknown>;
    if (review.domain !== domain || review.gitSha !== gitSha || review.status !== 'PASS'
      || typeof review.reviewerId !== 'string' || !review.reviewerId.trim()
      || typeof review.observedAt !== 'string') throw new Error('LOCAL_PILOT_REVIEW_INVALID');
    reviews.push({ domain, reviewerId: review.reviewerId, status: 'PASS',
      observedAt: review.observedAt, reportSha256: sha256(raw) });
  }
  if (new Set(reviews.map((review) => review.reviewerId)).size !== reviews.length) {
    throw new Error('LOCAL_PILOT_REVIEWERS_NOT_INDEPENDENT');
  }
  if (assertCleanCommit() !== gitSha) throw new Error('LOCAL_PILOT_COMMIT_CHANGED_DURING_CHECKS');
  const refreshed = computeLocalReleaseFingerprint({
    root, runId: 'run-capability-attestation', apiKeyVersion, apiSecretVersion,
    secretManagerProjectId: projectId,
  });
  if (JSON.stringify(refreshed) !== JSON.stringify(fingerprint)
    || localLivePilotPolicySha256(root) !== policySha256) {
    throw new Error('LOCAL_PILOT_SOURCE_CHANGED_DURING_CHECKS');
  }
  const evidence: LocalPilotCapabilityEvidence = {
    schemaVersion: 1, gitSha, sourceSha256: fingerprint.sourceSha256,
    dependencySha256: fingerprint.dependencySha256,
    migrationSha256: fingerprint.migrationSha256,
    pilotPolicySha256: policySha256, observedAt: new Date().toISOString(),
    checks, reviews,
  };
  writeFileSync(path.resolve(root, LOCAL_PILOT_CAPABILITY_EVIDENCE_PATH), JSON.stringify(evidence));
  const readiness = localLivePilotReadiness({ root, fingerprint, pilotPolicySha256: policySha256 });
  if (!readiness.canApprove) throw new Error(`LOCAL_PILOT_CAPABILITY_NOT_READY:${readiness.blockers.join(',')}`);
  process.stdout.write(`Local Pilot capability evidence PASS for commit ${gitSha}.\n`);
}

try { main(); } catch (error) {
  const reason = error instanceof Error ? error.message : 'UNKNOWN';
  process.stderr.write(`Local Pilot capability evidence FAILED: ${reason}\n`);
  process.exitCode = 1;
} finally {
  if (isolatedCheckHome) rmSync(isolatedCheckHome, { recursive: true, force: true });
}
