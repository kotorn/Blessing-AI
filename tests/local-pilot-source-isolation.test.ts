import { expect, it } from 'vitest';
import { pilotSourceManifest, validatePilotSourceTree } from '../src/backend/local-pilot-source-isolation.js';

const file = (path: string, mode = '100644') => ({ path, mode, type: 'blob' });
it('allows ordinary tracked source and environment examples', () => {
  expect(() => validatePilotSourceTree([file('src/app.ts'), file('.env.example'), file('package-lock.json')])).not.toThrow();
});

it('binds all file bytes and executable modes in a deterministic manifest', () => {
  const files = [{ path: 'src/app.ts', mode: '100644', bytes: Buffer.from('source') },
    { path: 'tests/app.test.ts', mode: '100644', bytes: Buffer.from('test') }];
  const original = pilotSourceManifest(files);
  expect(pilotSourceManifest([...files].reverse())).toEqual(original);
  expect(pilotSourceManifest([files[0], { ...files[1], bytes: Buffer.from('changed') }]).sha256)
    .not.toBe(original.sha256);
  expect(pilotSourceManifest([{ ...files[0], mode: '100755' }, files[1]]).sha256).not.toBe(original.sha256);
  expect(original.files).toHaveLength(2);
  expect(JSON.stringify(original)).not.toContain('source');
});
it('rejects credentials and installed tooling instead of copying them', () => {
  for (const name of ['.env', '.env.local', 'sub/.env.production', '.npmrc', '.ssh/id_rsa',
    'node_modules/tool/index.js', 'credentials.json', 'service-account-admin.json', 'key.pem']) {
    expect(() => validatePilotSourceTree([file(name)])).toThrow('UNSAFE');
  }
});
it('rejects symlinks, submodules and ambiguous Windows paths', () => {
  for (const entry of [file('link', '120000'), { path: 'submodule', mode: '160000', type: 'commit' },
    file('../escape'), file('C:/escape'), file('dir\\escape'), file('CON.txt'), file('name.'), file('file:stream')]) {
    expect(() => validatePilotSourceTree([entry])).toThrow('UNSAFE');
  }
  expect(() => validatePilotSourceTree([file('Config.ts'), file('config.ts')])).toThrow('UNSAFE');
  expect(() => validatePilotSourceTree([])).toThrow('EMPTY');
});
