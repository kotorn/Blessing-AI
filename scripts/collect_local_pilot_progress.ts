/**
 * Collect diagnostic Local Pilot checks without granting readiness.
 * This deliberately writes a separate, non-authoritative artifact.
 */
import { createHash } from 'node:crypto';
import { mkdirSync, renameSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';

import {
  buildLocalPilotCheckEnvironment,
} from '../src/backend/local-live-pilot-readiness.js';

const root = path.resolve(import.meta.dirname, '..');
const checkTimeoutMs = 60 * 60 * 1_000;
const sha256 = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');

export type ProgressStatus = 'PASS' | 'FAIL' | 'NOT_RUN';

export interface LocalPilotProgressCheck {
  id: string;
  status: ProgressStatus;
  startedAt: string | null;
  completedAt: string | null;
  durationMs: number | null;
  exitCode: number | null;
  outputSha256: string | null;
  reason?: string;
}

export function progressCheckFromResult(input: {
  id: string;
  startedAt: string;
  completedAt: string;
  durationMs: number;
  exitCode: number | null;
  output: string | Buffer;
  error?: string;
}): LocalPilotProgressCheck {
  const failed = input.exitCode !== 0 || Boolean(input.error);
  return {
    id: input.id,
    status: failed ? 'FAIL' : 'PASS',
    startedAt: input.startedAt,
    completedAt: input.completedAt,
    durationMs: input.durationMs,
    exitCode: input.exitCode,
    outputSha256: sha256(input.output),
    ...(input.error ? { reason: input.error } : {}),
  };
}

function notRun(id: string, reason: string): LocalPilotProgressCheck {
  return {
    id, status: 'NOT_RUN', startedAt: null, completedAt: null,
    durationMs: null, exitCode: null, outputSha256: null, reason,
  };
}

function execute(
  id: string,
  command: string,
  args: string[],
  env: NodeJS.ProcessEnv,
): LocalPilotProgressCheck {
  const started = Date.now();
  const startedAt = new Date(started).toISOString();
  const child = spawnSync(command, args, {
    cwd: root,
    env,
    encoding: 'buffer',
    maxBuffer: 32 * 1024 * 1024,
    timeout: checkTimeoutMs,
    windowsHide: true,
  });
  const completed = Date.now();
  const output = Buffer.concat([child.stdout || Buffer.alloc(0), child.stderr || Buffer.alloc(0)]);
  const processError = child.error as NodeJS.ErrnoException | undefined;
  const error = processError
    ? processError.code === 'ETIMEDOUT' ? 'CHECK_TIMEOUT' : `PROCESS_ERROR_${processError.code || 'UNKNOWN'}`
    : undefined;
  return progressCheckFromResult({
    id,
    startedAt,
    completedAt: new Date(completed).toISOString(),
    durationMs: completed - started,
    exitCode: child.status,
    output,
    error,
  });
}

function npmCommand(script: 'test' | 'lint' | 'build'): [string, string[]] {
  return process.platform === 'win32'
    ? ['cmd.exe', ['/d', '/s', '/c', 'npm', 'run', script]]
    : ['npm', ['run', script]];
}

function gitHead(): string | null {
  const result = spawnSync('git', ['rev-parse', '--verify', 'HEAD'], {
    cwd: root, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    windowsHide: true, timeout: 5_000,
  });
  const value = result.status === 0 ? result.stdout.trim() : '';
  return /^[a-f0-9]{40,64}$/.test(value) ? value : null;
}

function migrationDsnIsExplicitlyIsolated(value: string | undefined): boolean {
  if (!value) return false;
  try {
    const parsed = new URL(value);
    const database = decodeURIComponent(parsed.pathname.slice(1));
    return ['postgres:', 'postgresql:'].includes(parsed.protocol)
      && parsed.hostname === '127.0.0.1'
      && ['55433', '55434', '55435'].includes(parsed.port)
      && /^blessing_migration_test_[a-z0-9_]+$/.test(database)
      && !parsed.search && !parsed.hash;
  } catch {
    return false;
  }
}

export function collectLocalPilotProgress(): { path: string; checks: LocalPilotProgressCheck[] } {
  const isolatedHome = path.join(tmpdir(), `blessing-local-pilot-progress-${process.pid}`);
  mkdirSync(isolatedHome, { recursive: true });
  const baseEnv = buildLocalPilotCheckEnvironment(process.env, isolatedHome);
  // Diagnostic checks are not promotion evidence, so they do not require the
  // production collector's Authenticode/runtime trust gate. Their outputs are
  // explicitly non-authoritative and never feed readiness.
  const python = process.platform === 'win32' ? 'py' : 'python3.13';
  const pythonPrefix = process.platform === 'win32' ? ['-3.13'] : [];
  const checks: LocalPilotProgressCheck[] = [];

  for (const [id, script] of [
    ['TYPESCRIPT_TESTS', 'test'], ['LINT', 'lint'], ['BUILD', 'build'],
  ] as const) {
    const [command, args] = npmCommand(script);
    checks.push(execute(id, command, args, baseEnv));
  }

  checks.push(execute('PYTHON_TESTS', python, [...pythonPrefix,
    '-I', '-m', 'pytest', '-q', 'tests/python',
    '-m', 'not contract_readonly and not contract_mutating and not contract_soak',
  ], baseEnv));
  checks.push(execute('LEASE_FENCING', python, [...pythonPrefix,
    '-I', '-m', 'pytest', '-q',
    'tests/python/test_execution_lease.py', 'tests/python/test_local_postgres_identity.py',
  ], baseEnv));
  checks.push(execute('PROTECTION_CLOSE', python, [...pythonPrefix,
    '-I', '-m', 'pytest', '-q',
    'tests/python/test_binance_protection.py', 'tests/python/test_local_pilot_lifecycle_monitors.py',
  ], baseEnv));

  const dsn = process.env.BLESSING_MIGRATION_TEST_DSN;
  if (!migrationDsnIsExplicitlyIsolated(dsn)) {
    checks.push(notRun('POSTGRES_17_MIGRATIONS_RESTART', 'ISOLATED_LOOPBACK_POSTGRES_DSN_NOT_CONFIGURED'));
  } else {
    checks.push(execute('POSTGRES_17_MIGRATIONS_RESTART', python, [...pythonPrefix,
      '-I', '-m', 'pytest', '-q',
      'tests/python/test_local_postgres_migrations.py::test_populated_migration_upgrade_through_020_survives_reconnect',
    ], {
      ...baseEnv,
      BLESSING_MIGRATION_TEST_DSN: dsn,
      BLESSING_MIGRATION_TEST_PORT: new URL(dsn).port,
    }));
  }

  // These require separate, authenticated operations. This collector never
  // creates Testnet orders or accepts caller-authored review receipts.
  checks.push(notRun('TESTNET_E2E', 'SEPARATE_AUTHORIZED_TESTNET_TRIAL_REQUIRED'));
  checks.push(notRun('AUTH_RELEASE_REVIEW', 'AUTHENTICATED_INDEPENDENT_REVIEW_REQUIRED'));
  checks.push(notRun('ORDER_RISK_REVIEW', 'AUTHENTICATED_INDEPENDENT_REVIEW_REQUIRED'));
  checks.push(notRun('PERSISTENCE_REVIEW', 'AUTHENTICATED_INDEPENDENT_REVIEW_REQUIRED'));

  const observedAt = new Date().toISOString();
  const artifact = {
    schemaVersion: 1,
    authority: 'DIAGNOSTIC_ONLY_NOT_FOR_APPROVAL',
    gitSha: gitHead(),
    observedAt,
    status: checks.every((check) => check.status === 'PASS') ? 'ALL_CHECKS_PASS' : 'INCOMPLETE',
    checks,
  };
  const folder = path.join(root, 'artifacts');
  mkdirSync(folder, { recursive: true });
  const filename = `local-pilot-progress-${observedAt.replace(/[:.]/g, '-')}.json`;
  const destination = path.join(folder, filename);
  const temporary = `${destination}.${process.pid}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(artifact, null, 2)}\n`, { flag: 'wx' });
  renameSync(temporary, destination);
  return { path: destination, checks };
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(import.meta.filename)) {
  try {
    const result = collectLocalPilotProgress();
    const counts = Object.fromEntries(['PASS', 'FAIL', 'NOT_RUN'].map((status) => [
      status, result.checks.filter((check) => check.status === status).length,
    ]));
    process.stdout.write(`${JSON.stringify({ artifact: path.relative(root, result.path), counts })}\n`);
    if (result.checks.some((check) => check.status === 'FAIL')) process.exitCode = 1;
  } catch (error) {
    const reason = error instanceof Error ? error.name : 'UNKNOWN';
    process.stderr.write(`Local Pilot progress collection failed: ${reason}\n`);
    process.exitCode = 1;
  }
}
