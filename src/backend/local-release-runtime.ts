import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, lstatSync, readFileSync, readdirSync, realpathSync, statSync } from 'node:fs';
import path from 'node:path';
import { localSecretSourceIdentity, type LocalSecretSourceIdentity } from './local-secret-manager.js';

export const LOCAL_RUNTIME_TARGET = 'LOCAL' as const;
export const LOCAL_RISK_POLICY_PATH = 'config/risk/mainnet_local_policy.json';

// These lists MUST equal SOURCE_PATHS / DEPENDENCY_PATHS in scripts/local_pilot_track_c_source.py:
// the Python verifier rejects any --binding that differs from its own recomputation.
// tests/local-pilot-binding-parity.test.ts compares both lists and the resulting hashes.
export const TRACK_C_SOURCE_PATHS = [
  'server.ts',
  'Dockerfile.worker',
  'src/backend',
  'apps/trading_worker',
  'domain',
  'scripts/start-local.ps1',
  'scripts/apply_local_postgres_migrations.py',
  LOCAL_RISK_POLICY_PATH,
  'config/risk/live_research_pilot.json',
  'scripts/local_pilot_track_c.py', 'scripts/local_pilot_track_c_source.py',
  'scripts/verify_local_pilot_track_c.py', 'scripts/produce_local_pilot_track_c.py',
  'scripts/run_local_pilot_ci_regressions.py',
  '.github/workflows/ci.yml', '.github/workflows/local-pilot-track-c.yml',
] as const;
export const TRACK_C_DEPENDENCY_PATHS = [
  'package.json',
  'package-lock.json',
  'pyproject.toml',
  'requirements-worker.txt',
  'requirements-worker.lock',
] as const;
export const TRACK_C_MIGRATION_PATHS = ['infra/postgres/migrations'] as const;
export const TRACK_C_POLICY_PATH = 'config/risk/live_research_pilot.json';
// Strategy hashes are persisted with campaigns and predate Track C; they keep their original
// four-file dependency list so unrelated binding changes cannot invalidate them.
const STRATEGY_DEPENDENCY_PATHS = ['package.json', 'package-lock.json', 'pyproject.toml', 'requirements-worker.txt'];
const EXPECTED_POLICY = {
  version: 'local-mainnet-risk-v1',
  target: 'LOCAL',
  symbol: 'ETHUSDC',
  basket_budget_usdc: '250',
  basket_drawdown_usdc: '125',
  daily_loss_usdc: '5',
  collateral_usdc: '250',
  gross_exposure_usdc: '1000',
  first_order_notional_usdc: '50',
  active_exposure_chains: 1,
  max_leverage: '10',
  risk_reward: {
    risk: '1',
    reward: '2',
    net_of_costs: true,
  },
} as const;

export const LOCAL_LIVE_PILOT_POLICY_PATH = 'config/risk/live_research_pilot.json';
const EXPECTED_LOCAL_LIVE_PILOT_POLICY = {
  version: 'local-live-research-pilot-v1',
  target: 'LOCAL',
  symbol: 'ETHUSDC',
  market: 'USD_M_FUTURES',
  position_notional_usdc: '50',
  order_notional_usdc: '50',
  total_exposure_usdc: '50',
  planned_stop_risk_usdc: '2',
  campaign_drawdown_usdc: '5',
  max_leverage: '10',
  quick_target_net_usdc: '0.25',
  quick_max_hold_seconds: 86400,
  min_net_reward_usdc: '0.25',
  min_reward_to_risk: '0.125',
  management_mode: 'QUICK',
} as const;

function compareFingerprintPaths(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

export interface LocalReleaseFingerprint {
  runtimeTarget: typeof LOCAL_RUNTIME_TARGET;
  runId: string;
  gitSha: string;
  sourceSha256: string;
  dependencySha256: string;
  migrationSha256: string;
  riskPolicyVersion: string;
  riskPolicySha256: string;
  secretVersions: { apiKey: string; apiSecret: string };
  secretSource: LocalSecretSourceIdentity;
}

function sha256(data: string | Buffer): string {
  return createHash('sha256').update(data).digest('hex');
}

function collectFiles(root: string, relativePath: string): string[] {
  const absolute = path.resolve(root, relativePath);
  if (!existsSync(absolute)) return [];
  const stat = statSync(absolute);
  if (stat.isFile()) return [relativePath.replaceAll('\\', '/')];
  if (!stat.isDirectory()) return [];
  const files: string[] = [];
  for (const entry of readdirSync(absolute, { withFileTypes: true }).sort((a, b) =>
    compareFingerprintPaths(a.name, b.name))) {
    if (entry.name === '__pycache__' || entry.name === '.pytest_cache') continue;
    const child = path.posix.join(relativePath.replaceAll('\\', '/'), entry.name);
    if (entry.isDirectory()) files.push(...collectFiles(root, child));
    else if (entry.isFile()) files.push(child);
  }
  return files;
}

function hashFiles(root: string, paths: readonly string[], required: boolean): string {
  const files = [...new Set(paths.flatMap((item) => collectFiles(root, item)))].sort(compareFingerprintPaths);
  if (required && files.length === 0) throw new Error('LOCAL_RELEASE_FINGERPRINT_INPUT_MISSING');
  const digest = createHash('sha256');
  for (const file of files) {
    digest.update(file, 'utf8');
    digest.update('\0');
    digest.update(readFileSync(path.resolve(root, file)));
    digest.update('\0');
  }
  return digest.digest('hex');
}

function readGitSha(root: string): string {
  try {
    const sha = execFileSync('git', ['rev-parse', '--verify', 'HEAD'], {
      cwd: root,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
    if (!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i.test(sha)) throw new Error('LOCAL_RELEASE_GIT_IDENTITY_UNAVAILABLE');
    return sha;
  } catch {
    throw new Error('LOCAL_RELEASE_GIT_IDENTITY_UNAVAILABLE');
  }
}

const TRACK_C_GIT_ENVIRONMENT_KEYS = new Set(['PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'TEMP', 'TMP']);

/** Mirrors git() in scripts/local_pilot_track_c_source.py: scrubbed environment, no user/system config. */
function trackCGit(root: string, args: string[]): string {
  const environment: NodeJS.ProcessEnv = {};
  for (const [name, value] of Object.entries(process.env)) {
    if (TRACK_C_GIT_ENVIRONMENT_KEYS.has(name.toUpperCase()) && typeof value === 'string') environment[name] = value;
  }
  environment.GIT_CONFIG_NOSYSTEM = '1';
  environment.GIT_NO_REPLACE_OBJECTS = '1';
  environment.GIT_CONFIG_GLOBAL = process.platform === 'win32' ? 'NUL' : '/dev/null';
  return execFileSync('git', args, {
    cwd: root, env: environment, encoding: 'utf8', shell: false, timeout: 10_000,
    stdio: ['ignore', 'pipe', 'ignore'], maxBuffer: 64 * 1024 * 1024, windowsHide: true,
  }).trim();
}

/**
 * Same algorithm as hash_files() in scripts/local_pilot_track_c_source.py: tracked files only
 * (git ls-files), code-point ordered, symlinks and escaping paths rejected, always non-empty.
 */
function hashTrackedFiles(root: string, paths: readonly string[]): string {
  const files = [...new Set(trackCGit(root, ['ls-files', '-z', '--', ...paths]).split('\0'))]
    .filter((name) => name !== '')
    .sort((left, right) => Buffer.compare(Buffer.from(left, 'utf8'), Buffer.from(right, 'utf8')));
  if (files.length === 0) throw new Error('LOCAL_RELEASE_FINGERPRINT_INPUT_MISSING');
  const realRoot = realpathSync(root);
  const digest = createHash('sha256');
  for (const name of files) {
    const file = path.resolve(root, name);
    const relative = path.relative(realRoot, realpathSync(file));
    if (lstatSync(file).isSymbolicLink() || relative.startsWith('..') || path.isAbsolute(relative)) {
      throw new Error('LOCAL_RELEASE_FINGERPRINT_PATH_INVALID');
    }
    digest.update(name, 'utf8');
    digest.update('\0');
    digest.update(readFileSync(file));
    digest.update('\0');
  }
  return digest.digest('hex');
}

export interface TrackCBinding {
  gitSha: string; sourceSha256: string; dependencySha256: string; migrationSha256: string; pilotPolicySha256: string;
}

/**
 * Binding the Python Track C verifier recomputes (source_binding). Unlike Python this does not
 * refuse a dirty tree; callers keep their own clean-commit gates, and the verifier still refuses.
 */
export function computeTrackCBinding(root = process.cwd()): TrackCBinding {
  const resolved = path.resolve(root);
  return {
    gitSha: readGitSha(resolved),
    sourceSha256: hashTrackedFiles(resolved, TRACK_C_SOURCE_PATHS),
    dependencySha256: hashTrackedFiles(resolved, TRACK_C_DEPENDENCY_PATHS),
    migrationSha256: hashTrackedFiles(resolved, TRACK_C_MIGRATION_PATHS),
    pilotPolicySha256: sha256(readFileSync(path.resolve(resolved, TRACK_C_POLICY_PATH))),
  };
}

export function readLocalRiskPolicy(root = process.cwd()): typeof EXPECTED_POLICY {
  let value: unknown;
  try {
    value = JSON.parse(readFileSync(path.resolve(root, LOCAL_RISK_POLICY_PATH), 'utf8'));
  } catch {
    throw new Error('LOCAL_RELEASE_RISK_POLICY_UNAVAILABLE');
  }
  if (stableLocalJson(value) !== stableLocalJson(EXPECTED_POLICY)) {
    throw new Error('LOCAL_RELEASE_RISK_POLICY_NOT_APPROVED');
  }
  return EXPECTED_POLICY;
}

export function localRiskPolicySha256(root = process.cwd()): string {
  try {
    return sha256(readFileSync(path.resolve(root, LOCAL_RISK_POLICY_PATH)));
  } catch {
    throw new Error('LOCAL_RELEASE_RISK_POLICY_UNAVAILABLE');
  }
}

export function localLivePilotPolicy(root = process.cwd()): typeof EXPECTED_LOCAL_LIVE_PILOT_POLICY {
  let value: unknown;
  try {
    value = JSON.parse(readFileSync(path.resolve(root, LOCAL_LIVE_PILOT_POLICY_PATH), 'utf8'));
  } catch {
    throw new Error('LOCAL_LIVE_PILOT_POLICY_UNAVAILABLE');
  }
  if (stableLocalJson(value) !== stableLocalJson(EXPECTED_LOCAL_LIVE_PILOT_POLICY)) {
    throw new Error('LOCAL_LIVE_PILOT_POLICY_NOT_APPROVED');
  }
  return EXPECTED_LOCAL_LIVE_PILOT_POLICY;
}

export function localLivePilotPolicySha256(root = process.cwd()): string {
  localLivePilotPolicy(root);
  try {
    return sha256(readFileSync(path.resolve(root, LOCAL_LIVE_PILOT_POLICY_PATH)));
  } catch {
    throw new Error('LOCAL_LIVE_PILOT_POLICY_UNAVAILABLE');
  }
}

export function localLivePilotStrategySha256(strategyId: 'grid' | 'trend' | 'shock' | 'carry', root = process.cwd()): string {
  const policy = localLivePilotPolicy(root);
  return sha256(stableLocalJson({
    strategyId,
    mode: policy.management_mode,
    sourceSha256: hashFiles(path.resolve(root), ['apps/trading_worker/strategies', 'apps/trading_worker/engine'], false),
    dependencySha256: hashFiles(path.resolve(root), STRATEGY_DEPENDENCY_PATHS, true),
  }));
}

export function stableLocalJson(value: unknown): string {
  if (value === undefined) return 'null';
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stableLocalJson).join(',')}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${stableLocalJson(record[key])}`).join(',')}}`;
}

export function computeLocalReleaseFingerprint(
  options: {
    root?: string;
    runId: string;
    apiKeyVersion: string;
    apiSecretVersion: string;
    secretManagerProjectId: string;
  },
): LocalReleaseFingerprint {
  const root = path.resolve(options.root || process.cwd());
  const runId = options.runId.trim();
  if (!/^run-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$/.test(runId)) {
    throw new Error('LOCAL_RELEASE_RUN_ID_INVALID');
  }
  const secretSource = localSecretSourceIdentity(
    options.secretManagerProjectId, options.apiKeyVersion, options.apiSecretVersion,
  );
  // One source of truth: the same function the parity tests compare against the Python verifier.
  const trackC = computeTrackCBinding(root);
  const policy = readLocalRiskPolicy(root);
  return {
    runtimeTarget: LOCAL_RUNTIME_TARGET,
    runId,
    gitSha: trackC.gitSha,
    sourceSha256: trackC.sourceSha256,
    dependencySha256: trackC.dependencySha256,
    migrationSha256: trackC.migrationSha256,
    riskPolicyVersion: policy.version,
    riskPolicySha256: localRiskPolicySha256(root),
    secretVersions: {
      apiKey: options.apiKeyVersion,
      apiSecret: options.apiSecretVersion,
    },
    secretSource,
  };
}

export function localFingerprintsMatch(
  approved: LocalReleaseFingerprint,
  current: LocalReleaseFingerprint,
): boolean {
  return stableLocalJson(approved) === stableLocalJson(current);
}
