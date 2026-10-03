import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { TextDecoder } from 'node:util';
import path from 'node:path';
import { buildLocalPilotGitEnvironment } from './local-live-pilot-readiness.js';
import { pilotSourceManifest, validatePilotSourceTree } from './local-pilot-source-isolation.js';
import { validatePilotDependencyLock } from './local-pilot-dependency-policy.js';

/** Read exact committed blobs only. Never execute checkout filters or read working-tree files.
 * Caller still must verify the Git executable and approved commit independently.
 */
export function readPilotCommittedSource(root: string, gitSha: string) {
  if (!/^[a-f0-9]{40}$/.test(gitSha)) throw new Error('LOCAL_PILOT_SOURCE_COMMIT_INVALID');
  const run = (args: string[]) => execFileSync('git', args, {
    cwd: path.resolve(root), env: buildLocalPilotGitEnvironment(process.env),
    shell: false, windowsHide: true, timeout: 30_000, maxBuffer: 32 * 1024 * 1024,
    stdio: ['ignore', 'pipe', 'ignore'],
  });
  const decoder = new TextDecoder('utf-8', { fatal: true });
  const decode = (value: Buffer) => decoder.decode(value);
  const resolved = decode(run(['rev-parse', '--verify', `${gitSha}^{commit}`])).trim();
  if (resolved !== gitSha) throw new Error('LOCAL_PILOT_SOURCE_COMMIT_MISMATCH');
  const treeId = decode(run(['rev-parse', '--verify', `${gitSha}^{tree}`])).trim();
  if (!/^[a-f0-9]{40}$/.test(treeId)) throw new Error('LOCAL_PILOT_SOURCE_TREE_INVALID');
  const rawTree = decode(run(['ls-tree', '-rz', '--full-tree', gitSha]));
  if (!rawTree.endsWith('\0')) throw new Error('LOCAL_PILOT_SOURCE_TREE_INCOMPLETE');
  const entries = rawTree.slice(0, -1).split('\0').map((entry) => {
    const match = /^(\d{6}) (blob|commit) ([a-f0-9]{40})\t(.+)$/.exec(entry);
    if (!match) throw new Error('LOCAL_PILOT_SOURCE_TREE_INVALID');
    return { mode: match[1], type: match[2], blobId: match[3], path: match[4] };
  });
  validatePilotSourceTree(entries);
  let totalBytes = 0;
  const files = entries.map((entry) => {
    const bytes = run(['cat-file', 'blob', entry.blobId]);
    const blobId = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
    if (blobId !== entry.blobId) throw new Error('LOCAL_PILOT_SOURCE_BLOB_MISMATCH');
    totalBytes += bytes.length;
    if (totalBytes > 128 * 1024 * 1024) throw new Error('LOCAL_PILOT_SOURCE_EXPORT_TOO_LARGE');
    return { path: entry.path, mode: entry.mode, bytes };
  });
  return { gitSha, treeId, manifest: pilotSourceManifest(files), files };
}

export function validatePilotAcceptanceSource(files: Array<{ path: string; mode: string; bytes: Buffer }>): void {
  validatePilotSourceTree(files.map((file) => ({ ...file, type: 'blob' })));
  const names = files.map((file) => file.path);
  const required = ['package.json', 'package-lock.json', 'tsconfig.json', 'vite.config.ts',
    'eslint.config.js', 'server.ts'];
  if (required.some((name) => !names.includes(name)) || !names.some((name) => name.startsWith('src/'))
    || !names.some((name) => name.startsWith('tests/') && name.endsWith('.test.ts'))) {
    throw new Error('LOCAL_PILOT_ACCEPTANCE_SOURCE_INCOMPLETE');
  }
  let lock: unknown;
  try {
    const bytes = files.find((file) => file.path === 'package-lock.json')!.bytes;
    lock = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
  } catch { throw new Error('LOCAL_PILOT_ACCEPTANCE_LOCK_INVALID'); }
  validatePilotDependencyLock(lock, names);
}

/** Acceptance-facing export: validate dependencies against exactly the committed files. */
export function readPilotAcceptanceSource(root: string, gitSha: string) {
  const source = readPilotCommittedSource(root, gitSha);
  validatePilotAcceptanceSource(source.files);
  return source;
}
