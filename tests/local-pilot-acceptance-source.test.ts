import { expect, it } from 'vitest';
import { validatePilotAcceptanceSource } from '../src/backend/local-pilot-committed-source.js';
function fixture() {
  const lock = { lockfileVersion: 3, packages: { '': {},
    'node_modules/generated': { link: true, resolved: 'src/generated' }, 'src/generated': {} } };
  return ['package.json', 'package-lock.json', 'tsconfig.json', 'vite.config.ts',
    'eslint.config.js', 'server.ts', 'src/generated/package.json', 'tests/app.test.ts'].map((name) => ({
    path: name, mode: '100644', bytes: Buffer.from(name === 'package-lock.json' ? JSON.stringify(lock) : '{}'),
  }));
}
it('validates dependencies only against files in the selected export', () => {
  expect(() => validatePilotAcceptanceSource(fixture())).not.toThrow();
  expect(() => validatePilotAcceptanceSource(fixture().filter((file) => file.path !== 'src/generated/package.json')))
    .toThrow('SOURCE_INCOMPLETE');
});
it('rejects incomplete check inputs or malformed lock before installation', () => {
  expect(() => validatePilotAcceptanceSource(fixture().filter((file) => file.path !== 'tsconfig.json')))
    .toThrow('SOURCE_INCOMPLETE');
  const malformed = fixture().map((file) => file.path === 'package-lock.json'
    ? { ...file, bytes: Buffer.from('not-json') } : file);
  expect(() => validatePilotAcceptanceSource(malformed)).toThrow('LOCK_INVALID');
});
