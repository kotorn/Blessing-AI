/** Mechanical lock metadata repair: pinned versions only, no npm execution. */
import { createHash } from 'node:crypto';
import { readFile, writeFile, mkdtemp, rename } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
export function lockedPackageName(location, entry) {
  const name = entry.name || location.split('node_modules/').at(-1);
  if (!/^(?:@[a-z0-9_.-]+\/)?[a-z0-9_.-]+$/i.test(name)
    || typeof entry.version !== 'string' || !/^\d+\.\d+\.\d+(?:[-+][a-z0-9.-]+)?$/i.test(entry.version)) {
    throw new Error('PINNED_PACKAGE_IDENTITY_INVALID');
  }
  return name;
}
export function verifiedDist(entry, metadata, archive) {
  if (metadata.version !== entry.version || !metadata.dist
    || !/^sha512-[A-Za-z0-9+/]{86}==$/.test(metadata.dist.integrity || '')) {
    throw new Error('REGISTRY_IDENTITY_OR_INTEGRITY_INVALID');
  }
  const url = new URL(metadata.dist.tarball);
  if (url.protocol !== 'https:' || url.hostname !== 'registry.npmjs.org'
    || url.port || url.username || url.password || url.search || url.hash
    || !url.pathname.endsWith('.tgz')) throw new Error('REGISTRY_TARBALL_INVALID');
  const actual = 'sha512-' + createHash('sha512').update(archive).digest('base64');
  if (actual !== metadata.dist.integrity) throw new Error('REGISTRY_TARBALL_INTEGRITY_MISMATCH');
  if ((/^sha512-[A-Za-z0-9+/]{86}==$/.test(entry.integrity || '') && entry.integrity !== actual)
    || (entry.resolved && entry.resolved !== url.href)) throw new Error('EXISTING_LOCK_METADATA_CONFLICT');
  return { resolved: url.href, integrity: actual };
}

async function fetchBytes(url) {
  const response = await fetch(url, { redirect: 'error', signal: AbortSignal.timeout(30_000) });
  if (!response.ok) throw new Error(`REGISTRY_HTTP_${response.status}`);
  const length = Number(response.headers.get('content-length') || 0);
  if (length > 64 * 1024 * 1024) throw new Error('REGISTRY_RESPONSE_TOO_LARGE');
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length > 64 * 1024 * 1024) throw new Error('REGISTRY_RESPONSE_TOO_LARGE');
  return bytes;
}

export async function repairLock(root, apply = false) {
  const target = path.join(root, 'package-lock.json');
  const original = await readFile(target);
  const lock = JSON.parse(original.toString('utf8'));
  if (lock.lockfileVersion !== 3 || !lock.packages) throw new Error('LOCK_SCHEMA_INVALID');
  const missing = Object.entries(lock.packages).filter(([name, entry]) =>
    name.startsWith('node_modules/') && !entry.link
      && (!entry.resolved || !/^sha512-[A-Za-z0-9+/]{86}==$/.test(entry.integrity || '')));
  const cache = new Map();
  let complete = 0;
  let cursor = 0;
  const results = new Map();
  async function worker() {
    while (cursor < missing.length) {
      const [location, entry] = missing[cursor++];
      const name = lockedPackageName(location, entry);
      const key = `${name}@${entry.version}`;
      if (!cache.has(key)) cache.set(key, (async () => {
        const metadata = JSON.parse((await fetchBytes(
          `https://registry.npmjs.org/${encodeURIComponent(name)}/${encodeURIComponent(entry.version)}`)).toString('utf8'));
        if (metadata.name !== name || metadata.version !== entry.version) throw new Error('REGISTRY_PACKAGE_MISMATCH');
        // Validate URL before fetching; metadata alone is not byte integrity evidence.
        const dist = metadata.dist;
        const url = new URL(dist?.tarball || '');
        if (url.protocol !== 'https:' || url.hostname !== 'registry.npmjs.org' || url.port
          || url.username || url.password || url.search || url.hash) throw new Error('REGISTRY_TARBALL_INVALID');
        const archive = await fetchBytes(url.href);
        return { metadata, archive };
      })());
      const { metadata, archive } = await cache.get(key);
      results.set(location, verifiedDist(entry, metadata, archive));
      complete++;
      if (complete % 25 === 0) process.stdout.write(`Verified ${complete}/${missing.length} pinned entries\n`);
    }
  }
  await Promise.all(Array.from({ length: 4 }, worker));
  // All changes are delayed until all artifacts have been verified.
  for (const [location, fields] of results) Object.assign(lock.packages[location], fields);
  if (hash(await readFile(target)) !== hash(original)) throw new Error('LOCK_CHANGED_DURING_REPAIR');
  if (apply && results.size) {
    const backupDir = await mkdtemp(path.join(tmpdir(), 'blessing-lock-backup-'));
    await writeFile(path.join(backupDir, 'package-lock.json'), original, { flag: 'wx' });
    const temporary = target + '.pilot-metadata-' + process.pid;
    await writeFile(temporary, JSON.stringify(lock, null, 2) + '\n', { flag: 'wx' });
    if (hash(await readFile(target)) !== hash(original)) throw new Error('LOCK_CHANGED_BEFORE_REPLACE');
    await rename(temporary, target);
    process.stdout.write(`Original lock backup: ${backupDir}\n`);
  }
  process.stdout.write(JSON.stringify({ verifiedEntries: results.size, applied: apply && results.size > 0,
    versionsChanged: 0, installScriptsExecuted: 0 }) + '\n');
}
if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  if (process.argv.slice(2).some((arg) => arg !== '--apply')) throw new Error('ARGUMENT_INVALID');
  repairLock(process.cwd(), process.argv.includes('--apply')).catch((error) => {
    process.stderr.write(`Lock metadata repair failed: ${error.message}\n`);
    process.exitCode = 1;
  });
}
