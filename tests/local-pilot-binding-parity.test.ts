import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  computeLocalReleaseFingerprint, computeTrackCBinding, TRACK_C_DEPENDENCY_PATHS, TRACK_C_MIGRATION_PATHS, TRACK_C_POLICY_PATH,
  TRACK_C_REVIEW_POLICY_PATH, TRACK_C_SOURCE_PATHS,
} from '../src/backend/local-release-runtime.js';

vi.setConfig({ testTimeout: 60_000 });
// Nothing here is mocked: the real Python verifier helpers are executed and compared byte for byte.
const repo = path.resolve(import.meta.dirname, '..');
const environment = Object.fromEntries(Object.entries(process.env).filter(([k]) =>
  ['PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'].includes(k.toUpperCase())));

function python(code: string, root: string, args: string[] = []): any {
  const output = execFileSync('python', ['-I', '-c', code, root, ...args], {
    cwd: root, env: environment, shell: false, encoding: 'utf8', maxBuffer: 1 << 20,
  });
  return JSON.parse(output);
}
const PY_HEADER = `import sys,json,hashlib
from pathlib import Path
sys.path.insert(0,${JSON.stringify(path.join(repo, 'scripts'))})
import local_pilot_track_c_source as s
root=Path(sys.argv[1])
`;
const pythonHashes = (root: string) => python(`${PY_HEADER}
print(json.dumps({'sourceSha256':s.hash_files(root,s.SOURCE_PATHS),'dependencySha256':s.hash_files(root,s.DEPENDENCY_PATHS),
'migrationSha256':s.hash_files(root,('infra/postgres/migrations',)),
'pilotPolicySha256':hashlib.sha256((root/'config/risk/live_research_pilot.json').read_bytes()).hexdigest()}))`, root);
const pythonBinding = (root: string) => python(`${PY_HEADER}
print(json.dumps(s.source_binding(root)))`, root);
const tsHashes = (root: string) => {
  const { gitSha: _ignored, ...hashes } = computeTrackCBinding(root);
  return hashes;
};

const roots: string[] = [];
afterEach(() => { for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true }); });
const git = (root: string, ...args: string[]) => execFileSync('git', ['-c', 'user.name=Fixture',
  '-c', 'user.email=fixture@example.invalid', '-c', 'core.autocrlf=false', '-c', 'commit.gpgsign=false', ...args],
{ cwd: root, encoding: 'utf8' });

/** Minimal repo containing at least one tracked file for every hashed path. */
function fixtureRepo(): string {
  const root = mkdtempSync(path.join(tmpdir(), 'blessing-binding-parity-'));
  roots.push(root);
  git(root, 'init', '-q');
  const write = (relative: string, content: string) => {
    mkdirSync(path.dirname(path.join(root, relative)), { recursive: true });
    writeFileSync(path.join(root, relative), content);
  };
  for (const item of [...TRACK_C_SOURCE_PATHS, ...TRACK_C_DEPENDENCY_PATHS, ...TRACK_C_MIGRATION_PATHS]) {
    const isDirectory = !path.posix.basename(item).includes('.');
    write(isDirectory ? `${item}/b-file.txt` : item, `content of ${item}\n`);
    if (isDirectory) write(`${item}/a-file.txt`, `first ${item}\n`);
  }
  write(TRACK_C_POLICY_PATH, '{"version":"fixture"}\n');
  write(TRACK_C_REVIEW_POLICY_PATH, '{"mode":"SOLO_OPERATOR","operator_github_id":12929483}\n');
  git(root, 'add', '.');
  git(root, 'commit', '-qm', 'fixture');
  return root;
}

describe('Track C binding: TypeScript equals the Python verifier on the same tree', () => {
  it('uses exactly the Python source and dependency path lists', () => {
    const lists = python(`${PY_HEADER}
print(json.dumps({'source':list(s.SOURCE_PATHS),'dependency':list(s.DEPENDENCY_PATHS)}))`, repo);
    expect([...TRACK_C_SOURCE_PATHS].sort()).toEqual([...lists.source].sort());
    expect([...TRACK_C_DEPENDENCY_PATHS].sort()).toEqual([...lists.dependency].sort());
    expect(TRACK_C_SOURCE_PATHS).toContain('scripts/run_local_pilot_ci_regressions.py');
    expect(TRACK_C_DEPENDENCY_PATHS).toContain('requirements-worker.lock');
  });

  it('matches Python hash_files for every binding field on the real repository', () => {
    expect(tsHashes(repo)).toEqual(pythonHashes(repo));
  });

  it('matches the complete Python source_binding on a clean checkout (fixture repo)', () => {
    const root = fixtureRepo();
    expect(computeTrackCBinding(root)).toEqual(pythonBinding(root));
  });

  it('matches the complete Python source_binding on the real repository when it is clean', () => {
    // Python scrubs the VCS config (including global excludes), so judge cleanliness the way it does.
    const pythonDirty = python(`${PY_HEADER}
print(json.dumps(s.git(root,'status','--porcelain','--untracked-files=all')))`, repo);
    if (pythonDirty) return; // source_binding() would refuse a dirty tree; the hash tests above still apply
    expect(computeTrackCBinding(repo)).toEqual(pythonBinding(repo));
  });

  it('the fingerprint production actually sends as --binding carries exactly the Track C hashes', () => {
    const identifiers = { runId: 'run-parity-check-0001', apiKeyVersion: '1', apiSecretVersion: '1',
      secretManagerProjectId: 'fixture-project-id' };
    for (const root of [repo]) {
      const fingerprint = computeLocalReleaseFingerprint({ root, ...identifiers });
      const binding = computeTrackCBinding(root);
      expect({ gitSha: fingerprint.gitSha, sourceSha256: fingerprint.sourceSha256,
        dependencySha256: fingerprint.dependencySha256, migrationSha256: fingerprint.migrationSha256 })
        .toEqual({ gitSha: binding.gitSha, sourceSha256: binding.sourceSha256,
          dependencySha256: binding.dependencySha256, migrationSha256: binding.migrationSha256 });
      expect({ ...pythonHashes(root) }).toMatchObject({ sourceSha256: fingerprint.sourceSha256,
        dependencySha256: fingerprint.dependencySha256, migrationSha256: fingerprint.migrationSha256 });
    }
  });

  it('changing one hashed file changes the TypeScript and Python hash identically', () => {
    const root = fixtureRepo();
    const before = { ts: tsHashes(root), py: pythonHashes(root) };
    expect(before.ts).toEqual(before.py);
    writeFileSync(path.join(root, 'domain/b-file.txt'), 'tampered\n');
    const after = { ts: tsHashes(root), py: pythonHashes(root) };
    expect(after.ts).toEqual(after.py);
    expect(after.ts.sourceSha256).not.toBe(before.ts.sourceSha256);
    expect(after.ts.dependencySha256).toBe(before.ts.dependencySha256);
  });

  it('requirements-worker.lock and the CI regression runner are part of the binding', () => {
    for (const [file, field] of [['requirements-worker.lock', 'dependencySha256'],
      ['scripts/run_local_pilot_ci_regressions.py', 'sourceSha256']] as const) {
      const root = fixtureRepo();
      const before = tsHashes(root);
      writeFileSync(path.join(root, file), 'changed\n');
      const after = tsHashes(root);
      expect(after[field]).not.toBe(before[field]);
      expect(after).toEqual(pythonHashes(root));
    }
  });

  it('ignores untracked files exactly like git ls-files in Python', () => {
    const root = fixtureRepo();
    const before = tsHashes(root);
    writeFileSync(path.join(root, 'domain/untracked.txt'), 'not tracked\n');
    expect(tsHashes(root)).toEqual(before);
    expect(pythonHashes(root)).toEqual(before);
  });

  it('the real Python verifier accepts the TypeScript binding and rejects any altered field', () => {
    const root = fixtureRepo();
    const verify = (binding: object) => JSON.parse(execFileSync('python', ['-I',
      path.join(repo, 'scripts/verify_local_pilot_track_c.py'), '--root', root, '--binding', JSON.stringify(binding),
      '--now', '2026-01-01T00:00:00+00:00'], { cwd: root, env: environment, shell: false, encoding: 'utf8' }));
    const binding = computeTrackCBinding(root);
    const accepted = verify(binding);
    expect(accepted.sourceClean).toBe(true);
    expect(accepted.binding).toEqual(binding);
    // No signed evidence exists in the fixture, so nothing may pass even though the binding matches.
    expect(accepted.classes.some((entry: any) => entry.status === 'PASS')).toBe(false);
    for (const key of Object.keys(binding) as Array<keyof typeof binding>) {
      const rejected = verify({ ...binding, [key]: key === 'gitSha' ? 'a'.repeat(40) : 'a'.repeat(64) });
      expect(rejected.reason).toBe('TRACK_C_BINDING_MISMATCH');
      expect(rejected.sourceClean).toBe(false);
    }
  });

  it('fails closed when hashed inputs are missing', () => {
    const root = mkdtempSync(path.join(tmpdir(), 'blessing-binding-empty-'));
    roots.push(root);
    git(root, 'init', '-q');
    writeFileSync(path.join(root, 'README.md'), 'x\n');
    git(root, 'add', '.');
    git(root, 'commit', '-qm', 'empty');
    expect(() => computeTrackCBinding(root)).toThrow('LOCAL_RELEASE_FINGERPRINT_INPUT_MISSING');
  });
});
