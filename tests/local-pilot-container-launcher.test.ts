import { promises as fs } from 'node:fs';
import { execFile } from 'node:child_process';
import { afterEach, expect, it, vi } from 'vitest';
import { checkerEnvironment, checkCommand, launch, launcherArguments, parseApprovedSeal, NODE, NPM, sha256, verifySeal } from '../scripts/local-pilot-container-launcher.mjs';

vi.mock('node:child_process', () => ({ execFile: vi.fn() }));
afterEach(() => { vi.restoreAllMocks(); vi.clearAllMocks(); });

function fixture() {
  return {
    'package.json': { type: 'file', sha256: sha256('{}'), mode: 0o644 },
    'package-lock.json': { type: 'file', sha256: sha256('lock'), mode: 0o644 },
    'src': { type: 'directory', mode: 0o755 },
    'src/index.ts': { type: 'file', sha256: sha256('source'), mode: 0o644 },
    'node_modules': { type: 'directory', mode: 0o755 },
    'node_modules/tool': { type: 'directory', mode: 0o755 },
    'node_modules/tool/cli.js': { type: 'file', sha256: sha256('dependency'), mode: 0o755 },
    'node_modules/.bin': { type: 'directory', mode: 0o755 },
    'node_modules/.bin/tool': { type: 'symlink', target: '../tool/cli.js' },
  };
}
const seal = (entries: object) => ({ version: 1, entries });

it('rejects traversal escaping after an intermediate symlink expansion', () => {
  const entries = {
    ...fixture(),
    'node_modules/deep': { type: 'directory', mode: 0o755 },
    'node_modules/deep/link': { type: 'symlink', target: '../../src' },
    'etc': { type: 'directory', mode: 0o755 },
    'etc/passwd': { type: 'file', mode: 0o644, sha256: sha256('synthetic') },
    'leak': { type: 'symlink', target: 'node_modules/deep/link/../../../etc/passwd' },
  };
  expect(() => verifySeal(seal(entries), entries)).toThrow('SEAL_INVALID');
});

it('requires exact supervisor arguments and a lowercase 64 hex seal hash', () => {
  const digest = 'a'.repeat(64);
  expect(launcherArguments(['BUILD', '--seal-sha256', digest])).toEqual({ check: 'BUILD', sealSha256: digest });
  for (const args of [[], ['BUILD'], ['BUILD', digest], ['BUILD', '--seal-sha256', digest, 'extra'],
    ['BUILD', '--seal-sha256', digest.toUpperCase()], ['BUILD', '--seal-sha256', 'sha256:' + digest],
    ['BUILD', '--seal-sha256', 'a'.repeat(63)], ['BUILD', '--seal-sha256', 'g'.repeat(64)],
    ['BUILD', '--hash', digest]]) {
    expect(() => launcherArguments(args)).toThrow('ARGUMENTS_INVALID');
  }
});

it('compares the raw manifest bytes with the independent hash before parsing', () => {
  const bytes = Buffer.from(JSON.stringify(seal(fixture())));
  expect(parseApprovedSeal(bytes, sha256(bytes))).toEqual(seal(fixture()));
  expect(() => parseApprovedSeal(Buffer.concat([bytes, Buffer.from(' ')]), sha256(bytes))).toThrow('SEAL_INVALID');
  expect(() => parseApprovedSeal(Buffer.from('not JSON'), sha256(bytes))).toThrow('SEAL_INVALID');
  expect(() => parseApprovedSeal(bytes, undefined)).toThrow('SEAL_INVALID');
  const forged = Buffer.from(JSON.stringify({ ...seal(fixture()), approved: true, sealSha256: sha256(bytes) }));
  expect(() => parseApprovedSeal(forged, sha256(bytes))).toThrow('SEAL_INVALID');
});

it('fails before inventory, copy or process execution when raw seal approval mismatches', async () => {
  const bytes = Buffer.from(JSON.stringify(seal(fixture())));
  vi.spyOn(fs, 'lstat').mockImplementation(async (name) => {
    const normalized = String(name).replaceAll('\\', '/');
    return { isDirectory: () => normalized === '/sealed' || normalized === '/work',
      isFile: () => normalized === '/sealed/seal-manifest.json' } as any;
  });
  vi.spyOn(fs, 'realpath').mockImplementation(async (name) => String(name));
  const readDirectory = vi.spyOn(fs, 'readdir').mockResolvedValue([] as any);
  vi.spyOn(fs, 'readFile').mockResolvedValue(bytes);
  const copy = vi.spyOn(fs, 'copyFile').mockResolvedValue();
  const mkdir = vi.spyOn(fs, 'mkdir').mockResolvedValue(undefined);
  await expect(launch(['BUILD', '--seal-sha256', '0'.repeat(64)])).rejects.toThrow('SEAL_INVALID');
  expect(readDirectory).toHaveBeenCalledExactlyOnceWith('/work');
  expect(copy).not.toHaveBeenCalled();
  expect(mkdir).not.toHaveBeenCalled();
});

it('rejects a missing seal before any copy', async () => {
  vi.spyOn(fs, 'lstat').mockImplementation(async (name) => {
    if (String(name).replaceAll('\\', '/') === '/sealed/seal-manifest.json') throw new Error('ENOENT');
    return { isDirectory: () => true } as any;
  });
  vi.spyOn(fs, 'realpath').mockImplementation(async (name) => String(name));
  vi.spyOn(fs, 'readdir').mockResolvedValue([] as any);
  const copy = vi.spyOn(fs, 'copyFile').mockResolvedValue();
  await expect(launch(['LINT', '--seal-sha256', 'a'.repeat(64)])).rejects.toThrow('ENOENT');
  expect(copy).not.toHaveBeenCalled();
});

it('imports without execution and allows only fixed offline npm commands', () => {
  for (const [check, script] of [['TYPESCRIPT_TESTS', 'test'], ['LINT', 'lint'], ['BUILD', 'build']]) {
    expect(checkCommand(check)).toEqual([NODE, [NPM, '--ignore-scripts', '--offline', 'run', script]]);
  }
  expect(NODE).toBe('/usr/local/bin/node');
  expect(NPM).toBe('/usr/local/lib/node_modules/npm/bin/npm-cli.js');
  for (const bad of ['test', 'BUILD; curl x', '__proto__', 'constructor', '', undefined]) {
    expect(() => checkCommand(bad)).toThrow('NOT_ALLOWED');
  }
});

it('constructs a fresh environment without inheriting secrets or execution overrides', () => {
  expect(checkerEnvironment()).toMatchObject({ EXECUTION_MODE: 'PAPER', MAINNET_LIVE_APPROVED: 'false',
    BLESSING_DISABLE_TEST_DOTENV: '1', NPM_CONFIG_IGNORE_SCRIPTS: 'true', NPM_CONFIG_OFFLINE: 'true',
    DOTENV_CONFIG_PATH: '/dev/null' });
  expect(checkerEnvironment()).not.toHaveProperty('NODE_OPTIONS');
  expect(checkerEnvironment()).not.toHaveProperty('BINANCE_MAINNET_API_KEY');
  const changed = checkerEnvironment();
  changed.EXECUTION_MODE = 'LIVE';
  expect(checkerEnvironment().EXECUTION_MODE).toBe('PAPER');
});

it('verifies an exact source and installed dependency tree including internal bin links', () => {
  expect(verifySeal(seal(fixture()), fixture())).toBe(true);
});

it('rejects absent, malformed and incomplete seals', () => {
  for (const bad of [undefined, null, {}, { version: 2, entries: fixture() }, { version: 1, entries: [] }]) {
    expect(() => verifySeal(bad, fixture())).toThrow('SEAL_INVALID');
  }
  for (const key of ['package.json', 'package-lock.json', 'src/index.ts', 'node_modules/tool/cli.js', 'src']) {
    const entries: Record<string, any> = fixture();
    delete entries[key];
    expect(() => verifySeal(seal(entries), entries)).toThrow('SEAL_INVALID');
  }
});

it('rejects missing, extra, tampered source and tampered installed dependencies', () => {
  for (const key of ['src/index.ts', 'node_modules/tool/cli.js']) {
    const changed: Record<string, any> = fixture();
    changed[key] = { ...changed[key], sha256: sha256('tampered') };
    expect(() => verifySeal(seal(fixture()), changed)).toThrow('SEAL_INVALID');
    delete changed[key];
    expect(() => verifySeal(seal(fixture()), changed)).toThrow('SEAL_INVALID');
  }
  expect(() => verifySeal(seal(fixture()), { ...fixture(), 'extra.js': { type: 'file', sha256: sha256('x') } }))
    .toThrow('SEAL_INVALID');
});

it('rejects traversal, escaping links, dangling links and link cycles even when sealed', () => {
  for (const target of ['/etc/passwd', '../../../outside', '..\\..\\outside', '../missing', 'tool']) {
    const entries = { ...fixture(), 'node_modules/.bin/tool': { type: 'symlink', target } };
    expect(() => verifySeal(seal(entries), entries)).toThrow('SEAL_INVALID');
  }
  for (const key of ['../escape', '/absolute', 'a\\b', 'a//b', 'seal-manifest.json']) {
    const entries = { ...fixture(), [key]: { type: 'file', sha256: sha256('x') } };
    expect(() => verifySeal(seal(entries), entries)).toThrow('SEAL_INVALID');
  }
  const cycle = { ...fixture(), 'node_modules/.bin/tool': { type: 'symlink', target: 'other' },
    'node_modules/.bin/other': { type: 'symlink', target: 'tool' } };
  expect(() => verifySeal(seal(cycle), cycle)).toThrow('SEAL_INVALID');
});

it('rejects unsupported special entries and a self-declared digest does not excuse tampering', () => {
  const entries = { ...fixture(), pipe: { type: 'fifo' } };
  expect(() => verifySeal(seal(entries), entries)).toThrow('SEAL_INVALID');
  const asserted = { ...seal(fixture()), approved: true, imageDigest: 'sha256:' + 'a'.repeat(64) };
  expect(() => verifySeal(asserted, { ...fixture(), 'src/index.ts': { ...fixture()['src/index.ts'], sha256: sha256('changed') } }))
    .toThrow('SEAL_INVALID');
});

it('requires sealed file/directory permission bits and rejects chmod substitution', () => {
  for (const key of ['src', 'src/index.ts', 'node_modules/tool/cli.js']) {
    const expected: Record<string, any> = fixture();
    const changed: Record<string, any> = fixture();
    changed[key] = { ...changed[key], mode: 0o777 };
    expect(() => verifySeal(seal(expected), changed)).toThrow('SEAL_INVALID');
    for (const mode of [undefined, -1, 0o1000, '755', 1.5]) {
      expected[key] = { ...expected[key], mode };
      expect(() => verifySeal(seal(expected), expected)).toThrow('SEAL_INVALID');
    }
  }
});

it('reserves runtime HOME and rejects source dotenv/npm config even when sealed', () => {
  for (const key of ['home', '.env', '.env.local', '.env.Example', '.env.example.local', '.npmrc', 'src/.env', 'src/.npmrc']) {
    const entries = { ...fixture(), [key]: { type: 'file', sha256: sha256('x'), mode: 0o644 } };
    expect(() => verifySeal(seal(entries), entries)).toThrow('SEAL_INVALID');
  }
  const entries = { ...fixture(), 'node_modules/tool/.env': { type: 'file', sha256: sha256('fixture'), mode: 0o644 } };
  expect(verifySeal(seal(entries), entries)).toBe(true);
  const example = { ...fixture(), '.env.example': { type: 'file', sha256: sha256('template'), mode: 0o644 } };
  expect(verifySeal(seal(example), example)).toBe(true);
});

it('copies only a verified tree, restores modes, verifies the copy, then executes without a shell', async () => {
  const source = fixture();
  const content: Record<string, string> = {
    'package.json': '{}', 'package-lock.json': 'lock', 'src/index.ts': 'source',
    'node_modules/tool/cli.js': 'dependency',
  };
  const copied: Record<string, any> = {};
  const bytes = Buffer.from(JSON.stringify(seal(source)));
  const normalize = (name: unknown) => String(name).replaceAll('\\', '/');
  const tree = (name: unknown) => normalize(name).startsWith('/sealed/') ? source : copied;
  const key = (name: unknown) => normalize(name).split('/').slice(2).join('/');
  vi.spyOn(fs, 'realpath').mockImplementation(async (name) => normalize(name));
  vi.spyOn(fs, 'lstat').mockImplementation(async (name) => {
    const full = normalize(name);
    const entry = full === '/sealed' || full === '/work' ? { type: 'directory', mode: 0o700 }
      : full === '/sealed/seal-manifest.json' ? { type: 'file', mode: 0o644 }
      : (tree(name) as any)[key(name)];
    if (!entry) throw new Error('ENOENT');
    return { mode: entry.mode, isDirectory: () => entry.type === 'directory',
      isFile: () => entry.type === 'file', isSymbolicLink: () => entry.type === 'symlink' } as any;
  });
  vi.spyOn(fs, 'readdir').mockImplementation(async (name) => {
    const prefix = key(name);
    const entries = normalize(name).startsWith('/sealed') ? source : copied;
    const children = Object.keys(entries).filter((item) => {
      const parent = item.includes('/') ? item.slice(0, item.lastIndexOf('/')) : '';
      return parent === prefix;
    }).map((item) => item.split('/').at(-1));
    if (normalize(name) === '/sealed') children.push('seal-manifest.json');
    return children as any;
  });
  vi.spyOn(fs, 'readFile').mockImplementation(async (name) =>
    normalize(name) === '/sealed/seal-manifest.json' ? bytes : Buffer.from(content[key(name)]));
  vi.spyOn(fs, 'readlink').mockImplementation(async (name) => (tree(name) as any)[key(name)].target);
  vi.spyOn(fs, 'mkdir').mockImplementation(async (name) => {
    copied[key(name)] = { type: 'directory', mode: 0o700 };
    return undefined;
  });
  const copy = vi.spyOn(fs, 'copyFile').mockImplementation(async (from, to) => {
    copied[key(to)] = { ...(source as any)[key(from)], mode: 0o600 };
  });
  vi.spyOn(fs, 'chmod').mockImplementation(async (name, mode) => { copied[key(name)].mode = mode; });
  vi.spyOn(fs, 'symlink').mockImplementation(async (target, name) => {
    copied[key(name)] = { type: 'symlink', target };
  });
  vi.mocked(execFile).mockImplementation(((command: string, args: string[], options: any, callback: any) => {
    // Called only after both tree verifications and HOME creation succeeded.
    const { home, ...sealedCopy } = copied;
    expect(home.type).toBe('directory');
    expect(sealedCopy).toEqual(source);
    callback(null, 'ok', '');
  }) as any);
  await expect(launch(['BUILD', '--seal-sha256', sha256(bytes)])).resolves.toEqual({ stdout: 'ok', stderr: '' });
  expect(copy).toHaveBeenCalledTimes(4);
  expect(execFile).toHaveBeenCalledWith(NODE, [NPM, '--ignore-scripts', '--offline', 'run', 'build'],
    expect.objectContaining({ cwd: '/work', env: checkerEnvironment(), shell: false }), expect.any(Function));
  // A chmod failure/substitution in the copied tree must stop execution.
  for (const name of Object.keys(copied)) delete copied[name];
  vi.mocked(execFile).mockClear();
  vi.mocked(fs.chmod).mockImplementation(async (name, mode) => {
    copied[key(name)].mode = key(name) === 'node_modules/tool/cli.js' ? 0o644 : mode;
  });
  await expect(launch(['BUILD', '--seal-sha256', sha256(bytes)])).rejects.toThrow('SEAL_INVALID');
  expect(execFile).not.toHaveBeenCalled();
});
