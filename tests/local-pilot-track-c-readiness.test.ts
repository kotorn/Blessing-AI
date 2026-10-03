import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, expect, it, vi } from 'vitest';
import { localLivePilotReadiness } from '../src/backend/local-live-pilot-readiness.js';
import type { LocalReleaseFingerprint } from '../src/backend/local-release-runtime.js';

const fixtureVerifier = vi.hoisted(() => ({ output: '', fail: false }));
vi.mock('../src/backend/local-python-runtime.js', async (original) => ({
  ...await original<typeof import('../src/backend/local-python-runtime.js')>(),
  resolveTrustedLocalPythonRuntime: () => ({ executable: 'fixture-python', assertUnchanged() {} }),
}));
vi.mock('node:child_process', async (original) => {
  const actual = await original<typeof import('node:child_process')>();
  return { ...actual, execFileSync: (...args: any[]) => {
    if (args[0] !== 'fixture-python') return (actual.execFileSync as any)(...args);
    if (fixtureVerifier.fail) throw new Error('fixture verifier error');
    return fixtureVerifier.output;
  } };
});

const roots: string[] = [];
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });

it('readiness consumes only live verifier results, rejects forged exports and verifier failure', () => {
  const root = mkdtempSync(path.join(tmpdir(), 'blessing-track-c-'));
  roots.push(root);
  writeFileSync(path.join(root, '.gitignore'), 'artifacts/\n');
  execFileSync('git', ['init', '-q'], { cwd: root });
  execFileSync('git', ['add', '.gitignore'], { cwd: root });
  execFileSync('git', ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], { cwd: root });
  const binding = { gitSha: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim(),
    sourceSha256: 'a'.repeat(64), dependencySha256: 'b'.repeat(64), migrationSha256: 'c'.repeat(64), pilotPolicySha256: 'd'.repeat(64) };
  const directory = path.join(root, 'artifacts/local-pilot-attestations');
  mkdirSync(directory, { recursive: true });
  writeFileSync(path.join(directory, 'CHECKS.json'), JSON.stringify({ status: 'PASS', canApprove: true }));
  const classes = ['CHECKS', 'REVIEW_AUTH_RELEASE', 'REVIEW_ORDER_RISK', 'REVIEW_PERSISTENCE', 'TESTNET_ETHUSDC']
    .map((evidenceClass) => ({ evidenceClass, status: 'PASS', reason: 'TRACK_C_ATTESTATION_VERIFIED' }));
  const evaluate = () => localLivePilotReadiness({ root, fingerprint: binding as unknown as LocalReleaseFingerprint,
    pilotPolicySha256: binding.pilotPolicySha256, now: new Date('2026-10-03T00:00:00Z') });
  fixtureVerifier.fail = false;
  fixtureVerifier.output = JSON.stringify({ sourceClean: true, binding, classes });
  expect(evaluate()).toMatchObject({ status: 'READY', canApprove: true, canStart: true, prepared: { status: 'NOT_RUN' } });
  fixtureVerifier.output = JSON.stringify({ sourceClean: true, binding, classes: [] });
  expect(evaluate().status).toBe('BLOCKED');
  fixtureVerifier.output = JSON.stringify({ sourceClean: true, binding: { ...binding, gitSha: 'f'.repeat(40) }, classes });
  expect(evaluate().status).toBe('BLOCKED');
  fixtureVerifier.fail = true;
  expect(evaluate().status).toBe('BLOCKED');
  fixtureVerifier.fail = false;
  fixtureVerifier.output = JSON.stringify({ sourceClean: true, binding, classes });
  writeFileSync(path.join(root, 'dirty.txt'), 'unreviewed');
  expect(evaluate().status).toBe('BLOCKED');
});
