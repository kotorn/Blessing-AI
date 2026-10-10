import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { verifierDiagnosticCode } from '../src/backend/local-pilot-attestation.js';
import { localLivePilotReadiness, localPilotCiAttestation } from '../src/backend/local-live-pilot-readiness.js';
import type { LocalReleaseFingerprint } from '../src/backend/local-release-runtime.js';

const GENERIC = 'LOCAL_PILOT_VERIFIER_RUNTIME_UNAVAILABLE';
const RESOLVER_CODES = [
  'LOCAL_PILOT_TRUSTED_PYTHON_EXECUTABLE_NOT_VERIFIED',
  'LOCAL_PILOT_PYTHON_DEPENDENCIES_MISSING',
  'LOCAL_PILOT_TRUSTED_PYTHON_EXECUTABLE_CHANGED',
];

const state = vi.hoisted(() => ({ resolverError: '', verifierError: '', verifierOutput: '' }));
vi.mock('../src/backend/local-python-runtime.js', async (original) => ({
  ...await original<typeof import('../src/backend/local-python-runtime.js')>(),
  resolveTrustedLocalPythonRuntime: () => {
    if (state.resolverError) throw new Error(state.resolverError);
    return { executable: 'fixture-python', assertUnchanged() {} };
  },
}));
vi.mock('node:child_process', async (original) => {
  const actual = await original<typeof import('node:child_process')>();
  return { ...actual, execFileSync: (...args: any[]) => {
    if (args[0] !== 'fixture-python') return (actual.execFileSync as any)(...args);
    if (state.verifierError) throw new Error(state.verifierError);
    return state.verifierOutput;
  } };
});

const roots: string[] = [];
afterEach(() => {
  state.resolverError = '';
  state.verifierError = '';
  state.verifierOutput = '';
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

function fixture(options: { attestations: boolean; ciArtifacts: boolean }) {
  const root = mkdtempSync(path.join(tmpdir(), 'blessing-attestation-diag-'));
  roots.push(root);
  writeFileSync(path.join(root, '.gitignore'), 'artifacts/\n');
  execFileSync('git', ['init', '-q'], { cwd: root });
  execFileSync('git', ['add', '.gitignore'], { cwd: root });
  execFileSync('git', ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
    'commit', '-qm', 'diagnostics fixture'], { cwd: root });
  const gitSha = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim();
  const binding = {
    gitSha, sourceSha256: 'a'.repeat(64), dependencySha256: 'b'.repeat(64),
    migrationSha256: 'c'.repeat(64), pilotPolicySha256: 'd'.repeat(64),
  };
  mkdirSync(path.join(root, 'artifacts', 'local-pilot-attestations'), { recursive: true });
  if (options.attestations) {
    writeFileSync(path.join(root, 'artifacts', 'local-pilot-attestations', 'CHECKS.json'), JSON.stringify({ status: 'PASS' }));
  }
  if (options.ciArtifacts) {
    writeFileSync(path.join(root, 'artifacts', 'local-pilot-ci-evidence.json'), '{}');
    writeFileSync(path.join(root, 'artifacts', 'local-pilot-ci-attestation.bundle.json'), '{}');
  }
  return { root, binding, gitSha };
}

function evaluate(root: string, binding: ReturnType<typeof fixture>['binding']) {
  return localLivePilotReadiness({
    root,
    fingerprint: binding as unknown as LocalReleaseFingerprint,
    pilotPolicySha256: binding.pilotPolicySha256,
    now: new Date('2026-10-03T00:00:00Z'),
  });
}

describe('verifier diagnostic codes', () => {
  it('passes only the fixed resolver codes through and collapses everything else to the generic code', () => {
    for (const code of RESOLVER_CODES) {
      expect(verifierDiagnosticCode(new Error(code))).toBe(code);
    }
    expect(verifierDiagnosticCode(new Error(`${RESOLVER_CODES[1]}: C:\\Users\\Kan\\python.exe`))).toBe(GENERIC);
    expect(verifierDiagnosticCode(new Error('spawn C:\\Users\\Kan\\python.exe ENOENT'))).toBe(GENERIC);
    expect(verifierDiagnosticCode(RESOLVER_CODES[1])).toBe(GENERIC);
    expect(verifierDiagnosticCode(undefined)).toBe(GENERIC);
  });
});

describe('track C readiness surfaces why the verifier runtime refused', () => {
  it.each(RESOLVER_CODES)('adds %s as a blocker and stays BLOCKED', (code) => {
    const { root, binding } = fixture({ attestations: true, ciArtifacts: true });
    state.resolverError = code;
    const readiness = evaluate(root, binding);
    expect(readiness).toMatchObject({ status: 'BLOCKED', canApprove: false, canStart: false });
    expect(readiness.blockers).toContain(code);
    expect(readiness.blockers).toEqual(expect.arrayContaining([
      'LOCAL_PILOT_CHECK_PROVENANCE_UNVERIFIED',
      'LOCAL_PILOT_REVIEW_PROVENANCE_UNVERIFIED',
      'LOCAL_PILOT_TESTNET_PROVENANCE_UNVERIFIED',
    ]));
    expect(readiness.ciAttestation).toEqual({
      status: 'FAIL', reason: 'CI_ATTESTATION_INVALID', diagnostic: code,
    });
  });

  it('reports the generic code and never echoes raw verifier text', () => {
    const { root, binding } = fixture({ attestations: true, ciArtifacts: false });
    state.verifierError = 'Command failed: C:\\Users\\Kan\\AppData\\python.exe --token secret-token-123';
    const readiness = evaluate(root, binding);
    expect(readiness).toMatchObject({ status: 'BLOCKED', canApprove: false, canStart: false });
    expect(readiness.blockers).toContain(GENERIC);
    const serialized = JSON.stringify(readiness);
    expect(serialized).not.toContain('C:\\\\Users\\\\Kan');
    expect(serialized).not.toContain('secret-token-123');
    expect(serialized).not.toContain('python.exe');
  });

  it('keeps a shape-rejected verifier result BLOCKED without a runtime diagnostic', () => {
    const { root, binding } = fixture({ attestations: true, ciArtifacts: false });
    state.verifierOutput = JSON.stringify({ sourceClean: false, classes: [] });
    const readiness = evaluate(root, binding);
    expect(readiness).toMatchObject({ status: 'BLOCKED', canApprove: false, canStart: false });
    expect(readiness.blockers).not.toContain(GENERIC);
  });

  it('adds no diagnostic when no attestation files exist', () => {
    const { root, binding } = fixture({ attestations: false, ciArtifacts: false });
    state.resolverError = RESOLVER_CODES[1];
    const readiness = evaluate(root, binding);
    expect(readiness.status).toBe('BLOCKED');
    expect(readiness.blockers).not.toContain(RESOLVER_CODES[1]);
    expect(readiness.blockers).not.toContain(GENERIC);
  });
});

describe('CI attestation keeps CI_ATTESTATION_INVALID and adds a fixed diagnostic', () => {
  it('propagates the resolver code alongside the existing reason', () => {
    const { root, gitSha } = fixture({ attestations: false, ciArtifacts: true });
    state.resolverError = RESOLVER_CODES[1];
    expect(localPilotCiAttestation(root, gitSha)).toEqual({
      status: 'FAIL', reason: 'CI_ATTESTATION_INVALID', diagnostic: RESOLVER_CODES[1],
    });
  });

  it('uses the generic code for raw verifier failures without leaking them', () => {
    const { root, gitSha } = fixture({ attestations: false, ciArtifacts: true });
    state.verifierError = 'boom at C:\\Users\\Kan\\secret-path';
    const result = localPilotCiAttestation(root, gitSha);
    expect(result).toEqual({ status: 'FAIL', reason: 'CI_ATTESTATION_INVALID', diagnostic: GENERIC });
    expect(JSON.stringify(result)).not.toContain('Kan');
  });
});
