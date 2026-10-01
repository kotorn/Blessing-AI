import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('node:child_process', () => ({ spawnSync: vi.fn() }));

import { spawnSync } from 'node:child_process';
import { verifyLocalPromotionBundle } from '../src/backend/local-promotion-evidence.js';

const sourceGitSha = 'a'.repeat(40);
const sourceFingerprint = 'b'.repeat(64);

function spawnResult(stdout: string, status: number | null = 0) {
  return {
    pid: 123,
    output: [null, stdout, ''],
    stdout,
    stderr: '',
    status,
    signal: null,
  } as unknown as ReturnType<typeof spawnSync>;
}

function verifierOutput(root: string, payload: Record<string, unknown>): string {
  const bundlePath = path.join(root, 'evidence', 'local-mainnet-promotion.json');
  const bundleSha256 = createHash('sha256').update(readFileSync(bundlePath)).digest('hex');
  return JSON.stringify({ schemaVersion: 1, bundleSha256, ...payload });
}

function setupRuntimeRoot(includeBundle = true) {
  const root = mkdtempSync(path.join(os.tmpdir(), 'blessing-promotion-gate-'));
  const verifier = path.join(root, 'apps', 'trading_worker', 'backtest', 'local_promotion_verifier.py');
  mkdirSync(path.dirname(verifier), { recursive: true });
  writeFileSync(verifier, '# test runtime path\n');
  writeFileSync(path.join(root, 'server.ts'), '// test entry point\n');
  const pythonExecutable = path.join(root, process.platform === 'win32' ? 'python.exe' : 'python3');
  writeFileSync(pythonExecutable, 'test executable placeholder');
  if (includeBundle) {
    mkdirSync(path.join(root, 'evidence'));
    writeFileSync(
      path.join(root, 'evidence', 'local-mainnet-promotion.json'),
      JSON.stringify({
        schemaVersion: 1,
        cohorts: {
          OOS: { closedBaskets: [{ realizedPnlUSDC: '999999' }], sharpe: 999 },
          SHADOW: { closedBaskets: [{ realizedPnlUSDC: '999999' }], sharpe: 999 },
        },
      }),
    );
  }
  return root;
}

function configureRuntime(root: string) {
  const oldProjectRoot = process.env.LOCAL_PROJECT_ROOT;
  const oldPythonExecutable = process.env.LOCAL_PYTHON_EXECUTABLE;
  process.env.LOCAL_PROJECT_ROOT = root;
  process.env.LOCAL_PYTHON_EXECUTABLE = path.join(root, process.platform === 'win32' ? 'python.exe' : 'python3');
  return () => {
    if (oldProjectRoot === undefined) delete process.env.LOCAL_PROJECT_ROOT;
    else process.env.LOCAL_PROJECT_ROOT = oldProjectRoot;
    if (oldPythonExecutable === undefined) delete process.env.LOCAL_PYTHON_EXECUTABLE;
    else process.env.LOCAL_PYTHON_EXECUTABLE = oldPythonExecutable;
  };
}

describe('Local promotion evidence subprocess gate', () => {
  beforeEach(() => vi.mocked(spawnSync).mockReset());
  afterEach(() => vi.restoreAllMocks());

  it('returns NOT_RUN and does not launch Python when the bundle is missing', () => {
    const root = setupRuntimeRoot(false);
    const restoreRuntime = configureRuntime(root);
    try {
      const result = verifyLocalPromotionBundle(root, sourceGitSha, sourceFingerprint);
      expect(result.status).toBe('NOT_RUN');
      expect(result.passed).toBe(false);
      expect(spawnSync).not.toHaveBeenCalled();
    } finally {
      restoreRuntime();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('returns NOT_RUN when the Python executable is not configured as an absolute path', () => {
    const root = setupRuntimeRoot();
    const restoreRuntime = configureRuntime(root);
    delete process.env.LOCAL_PYTHON_EXECUTABLE;
    try {
      const result = verifyLocalPromotionBundle(root, sourceGitSha, sourceFingerprint);
      expect(result.status).toBe('NOT_RUN');
      expect(result.failures).toContain('Validated absolute LOCAL_PYTHON_EXECUTABLE is unavailable');
      expect(spawnSync).not.toHaveBeenCalled();
    } finally {
      restoreRuntime();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('returns NOT_RUN when no validated absolute project root can be identified', () => {
    const root = setupRuntimeRoot();
    const restoreRuntime = configureRuntime(root);
    const oldEntry = process.argv[1];
    process.argv[1] = path.join(root, 'unrecognized-entry.js');
    delete process.env.LOCAL_PROJECT_ROOT;
    writeFileSync(process.argv[1], '// not a recognized server entry\n');
    try {
      const result = verifyLocalPromotionBundle(root, sourceGitSha, sourceFingerprint);
      expect(result.status).toBe('NOT_RUN');
      expect(result.failures).toContain('Validated absolute Local project root or recognized server entry point is unavailable');
      expect(spawnSync).not.toHaveBeenCalled();
    } finally {
      if (oldEntry === undefined) process.argv.splice(1, 1);
      else process.argv[1] = oldEntry;
      restoreRuntime();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('delegates evidence evaluation to the fixed Python module without shell or caller metrics', () => {
    const root = setupRuntimeRoot();
    const restoreRuntime = configureRuntime(root);
    vi.mocked(spawnSync).mockReturnValue(spawnResult(verifierOutput(root, {
      status: 'FAIL',
      passed: false,
      failures: ['OOS: replay evidence is not reproducible'],
      totalClosedBaskets: 0,
      cohorts: {},
    })));
    try {
      const result = verifyLocalPromotionBundle(root, sourceGitSha, sourceFingerprint);
      expect(result.status).toBe('FAIL');
      expect(result.passed).toBe(false);
      expect(result.totalClosedBaskets).toBe(0);
      const [executable, args, options] = vi.mocked(spawnSync).mock.calls[0];
      expect(executable).toBe(path.join(root, process.platform === 'win32' ? 'python.exe' : 'python3'));
      expect(args).toEqual(expect.arrayContaining([
        '-m',
        'apps.trading_worker.backtest.local_promotion_verifier',
        '--bundle-path',
        'evidence/local-mainnet-promotion.json',
      ]));
      expect(options).toMatchObject({ shell: false, timeout: 120_000, windowsHide: true });
      expect(options?.cwd).toBe(root);
      expect(options?.env?.PATH).toBe(root);
      expect(JSON.stringify(options?.env)).not.toContain('BINANCE_SECRET_SENTINEL');
    } finally {
      restoreRuntime();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('fails closed when the verifier is unavailable or exits nonzero', () => {
    const root = setupRuntimeRoot();
    const restoreRuntime = configureRuntime(root);
    vi.mocked(spawnSync).mockReturnValue({
      ...spawnResult('', 2),
      error: new Error('python missing'),
    } as unknown as ReturnType<typeof spawnSync>);
    try {
      const result = verifyLocalPromotionBundle(root, sourceGitSha, sourceFingerprint);
      expect(result.status).toBe('NOT_RUN');
      expect(result.passed).toBe(false);
    } finally {
      restoreRuntime();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('rejects verifier output whose status, pass bit, and bundle digest disagree', () => {
    const root = setupRuntimeRoot();
    const restoreRuntime = configureRuntime(root);
    vi.mocked(spawnSync).mockReturnValue(spawnResult(verifierOutput(root, {
      status: 'PASS',
      passed: false,
      failures: [],
      bundleSha256: 'c'.repeat(64),
    })));
    try {
      const result = verifyLocalPromotionBundle(root, sourceGitSha, sourceFingerprint);
      expect(result.status).toBe('NOT_RUN');
      expect(result.passed).toBe(false);
      expect(result.failures).toContain('Independent OOS/Shadow verifier returned an invalid result schema');
    } finally {
      restoreRuntime();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('rejects a bundle changed while the independent verifier is running', () => {
    const root = setupRuntimeRoot();
    const restoreRuntime = configureRuntime(root);
    const output = verifierOutput(root, {
      status: 'FAIL',
      passed: false,
      failures: ['evidence incomplete'],
      totalClosedBaskets: 0,
      cohorts: {},
    });
    vi.mocked(spawnSync).mockImplementation(() => {
      mkdirSync(path.join(root, 'evidence'), { recursive: true });
      writeFileSync(
        path.join(root, 'evidence', 'local-mainnet-promotion.json'),
        JSON.stringify({ schemaVersion: 2, cohorts: {} }),
      );
      return spawnResult(output);
    });
    try {
      const result = verifyLocalPromotionBundle(root, sourceGitSha, sourceFingerprint);
      expect(result.status).toBe('NOT_RUN');
      expect(result.failures).toContain('Promotion evidence changed while the independent verifier was running');
    } finally {
      restoreRuntime();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('fails closed on malformed verifier output', () => {
    const root = setupRuntimeRoot();
    const restoreRuntime = configureRuntime(root);
    vi.mocked(spawnSync).mockReturnValue(spawnResult('{not-json'));
    try {
      const result = verifyLocalPromotionBundle(root, sourceGitSha, sourceFingerprint);
      expect(result.status).toBe('NOT_RUN');
      expect(result.passed).toBe(false);
      expect(result.failures).toContain('Independent OOS/Shadow verifier returned malformed JSON');
    } finally {
      restoreRuntime();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('does not accept a PASS-shaped response without independently returned metrics and source binding', () => {
    const root = setupRuntimeRoot();
    const restoreRuntime = configureRuntime(root);
    vi.mocked(spawnSync).mockReturnValue(spawnResult(verifierOutput(root, {
      status: 'PASS',
      passed: true,
      failures: [],
      totalClosedBaskets: 50,
      cohorts: {},
    })));
    try {
      const result = verifyLocalPromotionBundle(root, sourceGitSha, sourceFingerprint);
      expect(result.status).toBe('FAIL');
      expect(result.passed).toBe(false);
    } finally {
      restoreRuntime();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('rejects a PASS response when event-level drawdown breaches the threshold', () => {
    const root = setupRuntimeRoot();
    const restoreRuntime = configureRuntime(root);
    const cohort = {
      status: 'PASS',
      closedBaskets: 25,
      maxDrawdownPct: 2,
      eventMaxDrawdownPct: 4,
      sharpe: 1.2,
      winRatePct: 60,
      rule0Violations: 0,
    };
    vi.mocked(spawnSync).mockReturnValue(spawnResult(verifierOutput(root, {
      status: 'PASS',
      passed: true,
      failures: [],
      sourceGitSha,
      sourceFingerprint,
      totalClosedBaskets: 50,
      cohorts: { OOS: cohort, SHADOW: cohort },
    })));
    try {
      const result = verifyLocalPromotionBundle(root, sourceGitSha, sourceFingerprint);
      expect(result.status).toBe('FAIL');
      expect(result.passed).toBe(false);
    } finally {
      restoreRuntime();
      rmSync(root, { recursive: true, force: true });
    }
  });

  it('rejects path traversal before invoking the verifier', () => {
    const root = setupRuntimeRoot();
    const restoreRuntime = configureRuntime(root);
    try {
      const result = verifyLocalPromotionBundle(
        root,
        sourceGitSha,
        sourceFingerprint,
        '../docs/research/evidence_artifact_btcusdt.json',
      );
      expect(result.status).toBe('NOT_RUN');
      expect(spawnSync).not.toHaveBeenCalled();
    } finally {
      restoreRuntime();
      rmSync(root, { recursive: true, force: true });
    }
  });
});
