import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { expect, it } from 'vitest';
import { readPilotCommittedSource } from '../src/backend/local-pilot-committed-source.js';
import { buildLocalPilotGitEnvironment } from '../src/backend/local-live-pilot-readiness.js';

it('exports real committed bytes, excludes dirty files, and rejects committed credentials', () => {
  const fixture = mkdtempSync(path.join(tmpdir(), 'blessing-source-fixture-'));
  const git = (args: string[]) => execFileSync('git', args, {
    cwd: fixture, env: buildLocalPilotGitEnvironment(process.env), shell: false,
    encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 10_000,
  }).trim();
  try {
    git(['init']);
    git(['config', 'user.name', 'Acceptance Fixture']);
    git(['config', 'user.email', 'acceptance@example.invalid']);
    git(['config', 'core.autocrlf', 'false']);
    writeFileSync(path.join(fixture, 'source.ts'), 'export const value = "committed";\n');
    git(['add', '--', 'source.ts']);
    git(['-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=NUL', 'commit', '-m', 'isolated source fixture']);
    const sha = git(['rev-parse', 'HEAD']);
    writeFileSync(path.join(fixture, 'source.ts'), 'export const value = "dirty";\n');
    writeFileSync(path.join(fixture, '.env.local'), 'CANARY=synthetic-not-a-real-secret\n');
    writeFileSync(path.join(fixture, 'untracked.ts'), 'untracked');
    const exported = readPilotCommittedSource(fixture, sha);
    expect(exported.files.map((file) => file.path)).toEqual(['source.ts']);
    expect(exported.files[0].bytes.toString()).toContain('committed');
    expect(exported.files[0].bytes.toString()).not.toContain('dirty');
    expect(JSON.stringify(exported.manifest)).not.toContain('CANARY');
    git(['add', '--', 'source.ts']);
    git(['-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=NUL', 'commit', '-m', 'replacement tree fixture']);
    const replacement = git(['rev-parse', 'HEAD']);
    git(['replace', sha, replacement]);
    const replacedExport = readPilotCommittedSource(fixture, sha);
    expect(replacedExport.treeId).toBe(exported.treeId);
    expect(replacedExport.files[0].bytes.toString()).toContain('committed');
    expect(replacedExport.files[0].bytes.toString()).not.toContain('dirty');
    git(['add', '--', '.env.local']);
    git(['-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=NUL', 'commit', '-m', 'synthetic credential rejection fixture']);
    expect(() => readPilotCommittedSource(fixture, git(['rev-parse', 'HEAD']))).toThrow('SOURCE_EXPORT_UNSAFE');
  } finally {
    rmSync(fixture, { recursive: true, force: true });
  }
}, 30_000);
