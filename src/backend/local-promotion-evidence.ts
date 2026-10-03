import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync, realpathSync, statSync } from 'node:fs';
import path from 'node:path';

export const LOCAL_PROMOTION_EVIDENCE_PATH = 'evidence/local-mainnet-promotion.json';
export const LOCAL_PROMOTION_THRESHOLDS = Object.freeze({
  totalClosedBaskets: 50,
  maxDrawdownPct: 3.5,
  minSharpe: 1,
  minWinRatePct: 50,
  maxRule0Violations: 0,
});

const VERIFIER_MODULE = 'apps.trading_worker.backtest.local_promotion_verifier';
const VERIFIER_TIMEOUT_MS = 120_000;
const MAX_OUTPUT_BYTES = 2 * 1024 * 1024;
const MAX_BUNDLE_BYTES = 2 * 1024 * 1024;

type CohortName = 'OOS' | 'SHADOW';
export type PromotionEvidenceStatus = 'PASS' | 'FAIL' | 'NOT_RUN';

export interface LocalCohortMetrics {
  status: PromotionEvidenceStatus;
  closedBaskets?: number;
  maxDrawdownPct?: number;
  eventMaxDrawdownPct?: number;
  sharpe?: number | null;
  winRatePct?: number;
  rule0Violations?: number;
  failures?: string[];
}

export interface LocalPromotionVerification {
  status: PromotionEvidenceStatus;
  passed: boolean;
  failures: string[];
  bundleSha256?: string;
  totalClosedBaskets?: number;
  cohorts?: Partial<Record<CohortName, LocalCohortMetrics>>;
}

function sha256(bytes: Buffer): string {
  return createHash('sha256').update(bytes).digest('hex');
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function safeRelativePath(relative: string): boolean {
  if (!relative || relative.includes('\0') || path.isAbsolute(relative)) return false;
  const normalized = relative.replace(/\\/g, '/');
  if (/^[a-zA-Z]:/.test(normalized)) return false;
  return !normalized.split('/').some((part) => part === '..' || part === '.' || part === '');
}

function safeEvidenceFile(root: string, relative: string, maxBytes: number): Buffer | null {
  if (!safeRelativePath(relative)) return null;
  try {
    const resolvedRoot = realpathSync(root);
    const absolute = path.resolve(resolvedRoot, relative);
    const resolvedFile = realpathSync(absolute);
    const prefix = resolvedRoot.endsWith(path.sep) ? resolvedRoot : `${resolvedRoot}${path.sep}`;
    const containsFile = process.platform === 'win32'
      ? resolvedFile.toLowerCase().startsWith(prefix.toLowerCase())
      : resolvedFile.startsWith(prefix);
    if (!containsFile || !statSync(resolvedFile).isFile()) return null;
    const stat = statSync(resolvedFile);
    if (stat.size <= 0 || stat.size > maxBytes) return null;
    return readFileSync(resolvedFile);
  } catch {
    return null;
  }
}

function notRun(failures: string[], bundleSha256?: string): LocalPromotionVerification {
  return { status: 'NOT_RUN', passed: false, failures, ...(bundleSha256 ? { bundleSha256 } : {}) };
}

function resolveRuntimeProjectRoot(): string | null {
  try {
    const configuredRoot = process.env.LOCAL_PROJECT_ROOT;
    let candidate: string;
    if (configuredRoot !== undefined) {
      if (!path.isAbsolute(configuredRoot)) return null;
      candidate = configuredRoot;
    } else {
      const entry = process.argv[1];
      if (!entry || !path.isAbsolute(entry)) return null;
      const resolvedEntry = realpathSync(entry);
      const entryName = path.basename(resolvedEntry).toLowerCase();
      const entryDirectory = path.dirname(resolvedEntry);
      if (entryName === 'server.ts' || entryName === 'server.js') {
        candidate = entryDirectory;
      } else if (entryName === 'server.cjs' && path.basename(entryDirectory).toLowerCase() === 'dist') {
        candidate = path.dirname(entryDirectory);
      } else {
        return null;
      }
    }

    const resolvedRoot = realpathSync(candidate);
    if (!statSync(resolvedRoot).isDirectory()) return null;
    const verifier = realpathSync(path.resolve(resolvedRoot, 'apps/trading_worker/backtest/local_promotion_verifier.py'));
    const prefix = resolvedRoot.endsWith(path.sep) ? resolvedRoot : `${resolvedRoot}${path.sep}`;
    const contained = process.platform === 'win32'
      ? verifier.toLowerCase().startsWith(prefix.toLowerCase())
      : verifier.startsWith(prefix);
    return contained && statSync(verifier).isFile() ? resolvedRoot : null;
  } catch {
    return null;
  }
}

function resolvePythonExecutable(): string | null {
  try {
    const configured = process.env.LOCAL_PYTHON_EXECUTABLE;
    if (!configured || !path.isAbsolute(configured)) return null;
    const executable = realpathSync(configured);
    const expectedNames = process.platform === 'win32' ? ['python.exe'] : ['python', 'python3'];
    if (!expectedNames.includes(path.basename(executable).toLowerCase())) return null;
    return statSync(executable).isFile() ? executable : null;
  } catch {
    return null;
  }
}

function failed(
  failures: string[],
  bundleSha256?: string,
  totalClosedBaskets?: number,
  cohorts?: Partial<Record<CohortName, LocalCohortMetrics>>,
): LocalPromotionVerification {
  return {
    status: 'FAIL',
    passed: false,
    failures,
    ...(bundleSha256 ? { bundleSha256 } : {}),
    ...(totalClosedBaskets === undefined ? {} : { totalClosedBaskets }),
    ...(cohorts ? { cohorts } : {}),
  };
}

function parseVerifierOutput(
  stdout: string,
  bundleSha256: string,
  expectedGitSha: string,
  expectedSourceFingerprint: string,
): LocalPromotionVerification {
  let payload: unknown;
  try {
    payload = JSON.parse(stdout.trim());
  } catch {
    return notRun(['Independent OOS/Shadow verifier returned malformed JSON'], bundleSha256);
  }
  if (
    !isRecord(payload)
    || payload.schemaVersion !== 1
    || !['PASS', 'FAIL', 'NOT_RUN'].includes(String(payload.status))
    || typeof payload.passed !== 'boolean'
    || payload.passed !== (payload.status === 'PASS')
    || typeof payload.bundleSha256 !== 'string'
    || !/^[0-9a-f]{64}$/.test(payload.bundleSha256)
    || payload.bundleSha256 !== bundleSha256
    || !Array.isArray(payload.failures)
    || payload.failures.some((failure) => typeof failure !== 'string')
  ) return notRun(['Independent OOS/Shadow verifier returned an invalid result schema'], bundleSha256);

  const status = payload.status as PromotionEvidenceStatus;
  const failures = payload.failures as string[];
  const total = typeof payload.totalClosedBaskets === 'number' && Number.isInteger(payload.totalClosedBaskets)
    ? payload.totalClosedBaskets
    : undefined;
  const rawCohorts = isRecord(payload.cohorts) ? payload.cohorts : {};
  const cohorts: Partial<Record<CohortName, LocalCohortMetrics>> = {};
  for (const name of ['OOS', 'SHADOW'] as const) {
    const value = rawCohorts[name];
    if (!isRecord(value) || !['PASS', 'FAIL', 'NOT_RUN'].includes(String(value.status))) continue;
    const sharpe = value.sharpe;
    cohorts[name] = {
      status: value.status as PromotionEvidenceStatus,
      ...(typeof value.closedBaskets === 'number' ? { closedBaskets: value.closedBaskets } : {}),
      ...(typeof value.maxDrawdownPct === 'number' ? { maxDrawdownPct: value.maxDrawdownPct } : {}),
      ...(typeof value.eventMaxDrawdownPct === 'number' ? { eventMaxDrawdownPct: value.eventMaxDrawdownPct } : {}),
      ...(typeof sharpe === 'number' ? { sharpe } : sharpe === null ? { sharpe: null } : {}),
      ...(typeof value.winRatePct === 'number' ? { winRatePct: value.winRatePct } : {}),
      ...(typeof value.rule0Violations === 'number' ? { rule0Violations: value.rule0Violations } : {}),
      ...(Array.isArray(value.failures) && value.failures.every((item) => typeof item === 'string')
        ? { failures: value.failures as string[] }
        : {}),
    };
  }

  if (status === 'PASS') {
    if (failures.length !== 0) {
      return failed(
        ['Independent verifier returned PASS together with failure details'],
        bundleSha256,
        total,
        cohorts,
      );
    }
    const identityMatches = payload.sourceGitSha === expectedGitSha
      && payload.sourceFingerprint === expectedSourceFingerprint;
    const metricsPass = (name: CohortName) => {
      const cohort = cohorts[name];
      return cohort?.status === 'PASS'
        && Number.isInteger(cohort.closedBaskets)
        && (cohort.closedBaskets as number) > 0
        && Number.isFinite(cohort.maxDrawdownPct)
        && (cohort.maxDrawdownPct as number) >= 0
        && (cohort.maxDrawdownPct as number) <= LOCAL_PROMOTION_THRESHOLDS.maxDrawdownPct
        && Number.isFinite(cohort.eventMaxDrawdownPct)
        && (cohort.eventMaxDrawdownPct as number) >= 0
        && (cohort.eventMaxDrawdownPct as number) <= LOCAL_PROMOTION_THRESHOLDS.maxDrawdownPct
        && Number.isFinite(cohort.sharpe)
        && (cohort.sharpe as number) >= LOCAL_PROMOTION_THRESHOLDS.minSharpe
        && Number.isFinite(cohort.winRatePct)
        && (cohort.winRatePct as number) >= LOCAL_PROMOTION_THRESHOLDS.minWinRatePct
        && cohort.rule0Violations === LOCAL_PROMOTION_THRESHOLDS.maxRule0Violations;
    };
    if (
      !identityMatches
      || total === undefined
      || total < LOCAL_PROMOTION_THRESHOLDS.totalClosedBaskets
      || !metricsPass('OOS')
      || !metricsPass('SHADOW')
    ) {
      return failed(
        [...failures, 'Verifier PASS did not include matching source identity and independently passing cohort metrics'],
        bundleSha256,
        total,
        cohorts,
      );
    }
  }
  return {
    status,
    passed: status === 'PASS',
    failures,
    bundleSha256,
    ...(total === undefined ? {} : { totalClosedBaskets: total }),
    ...(Object.keys(cohorts).length ? { cohorts } : {}),
  };
}

export function verifyLocalPromotionBundle(
  root: string,
  expectedGitSha: string,
  expectedSourceFingerprint: string,
  bundlePath = LOCAL_PROMOTION_EVIDENCE_PATH,
): LocalPromotionVerification {
  if (!/^[0-9a-f]{40,64}$/i.test(expectedGitSha)) {
    return notRun(['Reviewed runtime Git SHA is missing or malformed']);
  }
  if (!/^[0-9a-f]{64}$/i.test(expectedSourceFingerprint)) {
    return notRun(['Reviewed runtime source fingerprint is missing or malformed']);
  }
  if (!safeRelativePath(bundlePath)) return notRun(['Promotion evidence path is not a safe relative path']);

  let resolvedRoot: string;
  try {
    if (!path.isAbsolute(root)) return notRun(['Promotion evidence root must be an absolute path']);
    resolvedRoot = realpathSync(root);
    if (!statSync(resolvedRoot).isDirectory()) return notRun(['Promotion evidence root is not a directory']);
  } catch {
    return notRun(['Promotion evidence root is unavailable']);
  }
  const workingDirectory = resolveRuntimeProjectRoot();
  if (!workingDirectory) {
    return notRun(['Validated absolute Local project root or recognized server entry point is unavailable']);
  }
  const rootsMatch = process.platform === 'win32'
    ? resolvedRoot.toLowerCase() === workingDirectory.toLowerCase()
    : resolvedRoot === workingDirectory;
  if (!rootsMatch) return notRun(['Promotion evidence root does not match the validated Local project root']);
  const pythonExecutable = resolvePythonExecutable();
  if (!pythonExecutable) {
    return notRun(['Validated absolute LOCAL_PYTHON_EXECUTABLE is unavailable']);
  }
  const bundleBytes = safeEvidenceFile(resolvedRoot, bundlePath, MAX_BUNDLE_BYTES);
  if (!bundleBytes) return notRun([`Promotion evidence missing or unsafe: ${bundlePath}`]);
  const bundleSha256 = sha256(bundleBytes);

  const minimalEnv: NodeJS.ProcessEnv = {
    PATH: path.dirname(pythonExecutable),
    PYTHONUTF8: '1',
    PYTHONDONTWRITEBYTECODE: '1',
    OMP_NUM_THREADS: '1',
    MKL_NUM_THREADS: '1',
  };
  for (const key of [
    'SystemRoot', 'WINDIR', 'APPDATA', 'LOCALAPPDATA', 'USERPROFILE',
    'HOMEDRIVE', 'HOMEPATH', 'TEMP', 'TMP', 'HOME',
  ]) {
    const value = process.env[key];
    if (value) minimalEnv[key] = value;
  }

  const invocation = spawnSync(
    pythonExecutable,
    [
      '-m',
      VERIFIER_MODULE,
      '--evidence-root',
      resolvedRoot,
      '--bundle-path',
      bundlePath,
      '--expected-git-sha',
      expectedGitSha.toLowerCase(),
      '--expected-source-fingerprint',
      expectedSourceFingerprint.toLowerCase(),
    ],
    {
      cwd: workingDirectory,
      env: minimalEnv,
      encoding: 'utf8',
      maxBuffer: MAX_OUTPUT_BYTES,
      shell: false,
      timeout: VERIFIER_TIMEOUT_MS,
      windowsHide: true,
    },
  );
  if (invocation.error || invocation.status !== 0 || typeof invocation.stdout !== 'string') {
    return notRun(['Independent OOS/Shadow replay verifier was unavailable or exited unsuccessfully'], bundleSha256);
  }
  const bundleAfterVerification = safeEvidenceFile(resolvedRoot, bundlePath, MAX_BUNDLE_BYTES);
  if (!bundleAfterVerification || sha256(bundleAfterVerification) !== bundleSha256) {
    return notRun(['Promotion evidence changed while the independent verifier was running'], bundleSha256);
  }
  const result = parseVerifierOutput(
    invocation.stdout,
    bundleSha256,
    expectedGitSha.toLowerCase(),
    expectedSourceFingerprint.toLowerCase(),
  );
  return result;
}
