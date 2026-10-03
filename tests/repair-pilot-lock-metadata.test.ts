import { createHash } from 'node:crypto';
import { expect, it } from 'vitest';
import { lockedPackageName, verifiedDist } from '../scripts/repair-pilot-lock-metadata.mjs';
it('verifies actual tarball bytes for the pinned version', () => {
  const archive = Buffer.from('synthetic package bytes');
  const metadata = { version: '1.2.3', dist: {
    tarball: 'https://registry.npmjs.org/tool/-/tool-1.2.3.tgz',
    integrity: 'sha512-' + createHash('sha512').update(archive).digest('base64'),
  } };
  expect(verifiedDist({ version: '1.2.3' }, metadata, archive)).toEqual({
    resolved: metadata.dist.tarball, integrity: metadata.dist.integrity,
  });
  expect(() => verifiedDist({ version: '1.2.4' }, metadata, archive)).toThrow('IDENTITY');
  expect(() => verifiedDist({ version: '1.2.3' }, metadata, Buffer.from('modified'))).toThrow('MISMATCH');
  expect(() => verifiedDist({ version: '1.2.3', integrity: 'sha512-' + Buffer.alloc(64).toString('base64') }, metadata, archive)).toThrow('CONFLICT');
  expect(verifiedDist({ version: '1.2.3', integrity: 'malformed' }, metadata, archive).integrity)
    .toBe(metadata.dist.integrity);
});
it('rejects arbitrary registry targets and package identity injection', () => {
  expect(lockedPackageName('node_modules/@scope/tool', { version: '1.2.3' })).toBe('@scope/tool');
  expect(() => lockedPackageName('node_modules/../../escape', { version: '1.2.3' })).toThrow('IDENTITY');
  expect(() => verifiedDist({ version: '1.2.3' }, { version: '1.2.3', dist: {
    tarball: 'https://user:token@registry.npmjs.org/tool.tgz',
    integrity: 'sha512-' + Buffer.alloc(64).toString('base64'),
  } }, Buffer.alloc(0))).toThrow('TARBALL_INVALID');
});
