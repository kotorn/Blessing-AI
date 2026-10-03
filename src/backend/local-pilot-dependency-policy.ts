/** Pre-install validation only. Registry allowlisting is not a network firewall.
 * npm ci must still verify integrity in an isolated installer container.
 */
export function validatePilotDependencyLock(lock: unknown, sourcePaths: readonly string[]): void {
  const fail = () => { throw new Error('LOCAL_PILOT_DEPENDENCY_LOCK_UNSAFE'); };
  if (!lock || typeof lock !== 'object' || Array.isArray(lock)) return fail();
  const value = lock as Record<string, unknown>;
  if (value.lockfileVersion !== 3 || !value.packages || typeof value.packages !== 'object'
    || Array.isArray(value.packages)) return fail();
  const packages = value.packages as Record<string, any>;
  if (!Object.hasOwn(packages, '') || !Object.keys(packages).some((name) => name.startsWith('node_modules/'))) return fail();
  const safeRelative = (name: string) => !name.includes('\\') && !name.includes(':')
    && name.split('/').every((part) => part && part !== '.' && part !== '..');
  for (const [name, entry] of Object.entries(packages)) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return fail();
    if (!name) continue;
    if (!safeRelative(name)) return fail();
    if (!name.startsWith('node_modules/')) {
      if (!sourcePaths.includes(`${name}/package.json`)) return fail();
      continue;
    }
    if (entry.link === true) {
      if (typeof entry.resolved !== 'string' || !safeRelative(entry.resolved)
        || entry.resolved.startsWith('node_modules/') || !Object.hasOwn(packages, entry.resolved)
        || !sourcePaths.includes(`${entry.resolved}/package.json`)) return fail();
      continue;
    }
    if (typeof entry.resolved !== 'string' || typeof entry.integrity !== 'string'
      || !/^sha512-[A-Za-z0-9+/]{86}==$/.test(entry.integrity)) return fail();
    let url: URL;
    try { url = new URL(entry.resolved); } catch { return fail(); }
    if (url.protocol !== 'https:' || url.hostname !== 'registry.npmjs.org' || url.port
      || url.username || url.password || url.search || url.hash || !url.pathname.endsWith('.tgz')) return fail();
  }
}
