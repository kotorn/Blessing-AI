import { createHash } from 'node:crypto';

/** Validate Git tree entries before exporting an acceptance-only source snapshot.
 * This is an export guard, not executable/image provenance or readiness authority.
 */
export function validatePilotSourceTree(entries: Array<{ path: string; mode: string; type: string }>): void {
  const seen = new Set<string>();
  for (const entry of entries) {
    const name = entry.path;
    const parts = name.split('/');
    const folded = name.toLowerCase();
    if (entry.type !== 'blob' || !['100644', '100755'].includes(entry.mode)
      || !name || name.includes('\\') || name.includes(':') || name.startsWith('/')
      || [...name].some((character) => character.charCodeAt(0) < 32)
      || parts.some((part) => !part || part === '.' || part === '..' || /[. ]$/.test(part)
        || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(part))
      || parts.some((part) => ['.git', 'node_modules', '.aws', '.config', '.ssh'].includes(part.toLowerCase()))
      || parts.some((part) => /^\.env(?:\.|$)/i.test(part) && part.toLowerCase() !== '.env.example')
      || parts.some((part) => /^\.(npmrc|pypirc|netrc)$/i.test(part))
      || /(?:^|\/)(?:application_default_credentials|credentials|service[-_]account[^/]*)\.json$/i.test(name)
      || /\.(?:pem|key|p12|pfx)$/i.test(name) || seen.has(folded)) {
      throw new Error('LOCAL_PILOT_SOURCE_EXPORT_UNSAFE');
    }
    seen.add(folded);
  }
  if (!entries.length) throw new Error('LOCAL_PILOT_SOURCE_EXPORT_EMPTY');
}

/** Canonical export identity includes every validated file, bytes and executable mode.
 * The caller must supply blobs read from the selected Git commit, not the checkout.
 */
export function pilotSourceManifest(files: Array<{ path: string; mode: string; bytes: Buffer }>): {
  schemaVersion: 1; files: Array<{ path: string; mode: string; size: number; sha256: string }>; sha256: string;
} {
  validatePilotSourceTree(files.map((file) => ({ ...file, type: 'blob' })));
  const entries = files.map((file) => {
    if (!Buffer.isBuffer(file.bytes)) throw new Error('LOCAL_PILOT_SOURCE_BYTES_INVALID');
    return { path: file.path, mode: file.mode, size: file.bytes.length,
      sha256: createHash('sha256').update(file.bytes).digest('hex') };
  }).sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  const manifest = { schemaVersion: 1 as const, files: entries };
  return { ...manifest, sha256: createHash('sha256').update(JSON.stringify(manifest)).digest('hex') };
}
