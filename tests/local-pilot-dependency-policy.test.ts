import { readFileSync } from 'node:fs';
import { expect, it } from 'vitest';
import { validatePilotDependencyLock } from '../src/backend/local-pilot-dependency-policy.js';
const packageEntry = { resolved: 'https://registry.npmjs.org/tool/-/tool-1.0.0.tgz',
  integrity: 'sha512-' + Buffer.alloc(64).toString('base64') };
const fixture = (entry = packageEntry) => ({ lockfileVersion: 3, packages: { '': {}, 'node_modules/tool': entry } });
it('validates the repaired current lock with its source-bound local dependency', () => {
  const current = JSON.parse(readFileSync('package-lock.json', 'utf8'));
  expect(() => validatePilotDependencyLock(current,
    ['src/dataconnect-generated/package.json'])).not.toThrow();
});
it('rejects unverified integrity and arbitrary fetch targets', () => {
  for (const entry of [{ ...packageEntry, integrity: '' },
    { ...packageEntry, resolved: 'https://evil.invalid/tool.tgz' },
    { ...packageEntry, resolved: 'https://user:token@registry.npmjs.org/tool.tgz' },
    { ...packageEntry, resolved: 'file:../../outside' }]) {
    expect(() => validatePilotDependencyLock(fixture(entry), [])).toThrow('UNSAFE');
  }
});
it('allows only local links fully represented in the source snapshot', () => {
  const lock = { lockfileVersion: 3, packages: { '': {},
    'node_modules/generated': { link: true, resolved: 'src/generated' }, 'src/generated': {} } };
  expect(() => validatePilotDependencyLock(lock, ['src/generated/package.json'])).not.toThrow();
  expect(() => validatePilotDependencyLock(lock, [])).toThrow('UNSAFE');
});
