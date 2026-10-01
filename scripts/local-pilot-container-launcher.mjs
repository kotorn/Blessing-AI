#!/usr/local/bin/node
import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

export const SEAL_NAME = 'seal-manifest.json';
export const NODE = '/usr/local/bin/node';
export const NPM = '/usr/local/lib/node_modules/npm/bin/npm-cli.js';
const CHECKS = Object.freeze({ TYPESCRIPT_TESTS: 'test', LINT: 'lint', BUILD: 'build' });
const fail = () => { throw new Error('LOCAL_PILOT_SEAL_INVALID'); };
export const sha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');

export function launcherArguments(args) {
  if (!Array.isArray(args) || args.length !== 3 || args[1] !== '--seal-sha256'
    || typeof args[2] !== 'string' || !/^[a-f0-9]{64}$/.test(args[2])) {
    throw new Error('LOCAL_PILOT_ARGUMENTS_INVALID');
  }
  checkCommand(args[0]);
  return { check: args[0], sealSha256: args[2] };
}

export function parseApprovedSeal(bytes, expectedSha256) {
  if (typeof expectedSha256 !== 'string' || !/^[a-f0-9]{64}$/.test(expectedSha256)
    || sha256(bytes) !== expectedSha256) fail();
  return JSON.parse(bytes.toString('utf8'));
}

export function checkCommand(check) {
  if (!Object.hasOwn(CHECKS, check)) throw new Error('LOCAL_PILOT_CHECK_NOT_ALLOWED');
  return [NODE, [NPM, '--ignore-scripts', '--offline', 'run', CHECKS[check]]];
}

// Construct from nothing: inherited credentials, NODE_OPTIONS, npm config and
// dotenv settings must never reach the checker or its children.
export function checkerEnvironment() {
  return {
    HOME: '/work/home', PATH: '/usr/local/bin:/usr/bin:/bin', TMPDIR: '/tmp',
    EXECUTION_MODE: 'PAPER', MAINNET_LIVE_APPROVED: 'false',
    BLESSING_DISABLE_TEST_DOTENV: '1', DOTENV_CONFIG_PATH: '/dev/null',
    NPM_CONFIG_IGNORE_SCRIPTS: 'true', NPM_CONFIG_OFFLINE: 'true',
    NPM_CONFIG_AUDIT: 'false', NPM_CONFIG_FUND: 'false',
    NPM_CONFIG_USERCONFIG: '/dev/null', NPM_CONFIG_GLOBALCONFIG: '/dev/null',
    NPM_CONFIG_CACHE: '/work/home/.npm',
  };
}

function safePath(value) {
  return typeof value === 'string' && value.length > 0 && !value.includes('\\')
    && !value.includes('\0') && !path.posix.isAbsolute(value)
    && value.split('/').every((part) => part && part !== '.' && part !== '..');
}

// Schema: {version:1, entries:{relativePath:{type:'file',sha256,mode},
//   directory:{type:'directory',mode}, link:{type:'symlink',target}}}.
// mode is the integer stat.mode & 0o777, including executable permissions.
// Entries include every directory, source file and installed dependency.
// This is integrity checking, NOT approval: the trusted caller must independently
// approve both the image hash and the raw manifest hash before container start.
export function verifySeal(manifest, observed) {
  if (!manifest || manifest.version !== 1 || !manifest.entries
    || typeof manifest.entries !== 'object' || Array.isArray(manifest.entries)
    || !observed || typeof observed !== 'object') fail();
  const expected = manifest.entries;
  const names = Object.keys(expected).sort();
  if (JSON.stringify(names) !== JSON.stringify(Object.keys(observed).sort())) fail();
  for (const name of names) {
    if (!safePath(name) || name === SEAL_NAME || name.split('/')[0] === 'home') fail();
    if (!name.startsWith('node_modules/') && name.split('/').some((part) =>
      part === '.npmrc' || part === '.env' || (part.startsWith('.env.') && part !== '.env.example'))) fail();
    const entry = expected[name];
    const actual = observed[name];
    if (!entry || !actual || entry.type !== actual.type) fail();
    if (entry.type === 'file' || entry.type === 'directory') {
      if (!Number.isInteger(entry.mode) || entry.mode < 0 || entry.mode > 0o777
        || entry.mode !== actual.mode) fail();
    }
    for (let parent = path.posix.dirname(name); parent !== '.'; parent = path.posix.dirname(parent)) {
      if (expected[parent]?.type !== 'directory') fail();
    }
    if (entry.type === 'file') {
      if (!/^[a-f0-9]{64}$/.test(entry.sha256) || entry.sha256 !== actual.sha256) fail();
    } else if (entry.type === 'symlink') {
      const target = entry.target;
      if (typeof target !== 'string' || !target || target.includes('\\') || target.includes('\0')
        || path.posix.isAbsolute(target) || target !== actual.target) fail();
      // Expand links before subsequent '..', matching filesystem traversal.
      const parts = path.posix.dirname(name) === '.' ? [] : path.posix.dirname(name).split('/');
      const pending = target.split('/');
      let expansions = 0;
      while (pending.length) {
        const component = pending.shift();
        if (!component || component === '.') continue;
        if (component === '..') {
          if (!parts.length) fail();
          parts.pop();
          continue;
        }
        const candidate = [...parts, component].join('/');
        const item = expected[candidate];
        if (!item) fail();
        if (item.type === 'symlink') {
          if (++expansions > 40) fail();
          const next = item.target;
          if (typeof next !== 'string' || !next || next.includes('\\') || next.includes('\0') || path.posix.isAbsolute(next)) fail();
          pending.unshift(...next.split('/'));
        } else {
          if (pending.length && item.type !== 'directory') fail();
          parts.push(component);
        }
      }
      if (!parts.length || !Object.hasOwn(expected, parts.join('/'))) fail();
    } else if (entry.type !== 'directory') fail();
  }
  if (expected['package.json']?.type !== 'file'
    || expected['package-lock.json']?.type !== 'file'
    || expected.node_modules?.type !== 'directory'
    || !names.some((name) => name.startsWith('node_modules/') && expected[name].type === 'file')
    || !names.some((name) => !name.startsWith('node_modules/')
      && !['package.json', 'package-lock.json'].includes(name) && expected[name].type === 'file')) fail();
  return true;
}

async function inventory(root) {
  const result = Object.create(null);
  async function walk(relative = '') {
    for (const name of await fs.readdir(path.join(root, relative))) {
      const key = relative ? `${relative}/${name}` : name;
      if (key === SEAL_NAME) continue;
      const full = path.join(root, key);
      const stat = await fs.lstat(full);
      if (stat.isSymbolicLink()) result[key] = { type: 'symlink', target: await fs.readlink(full) };
      else if (stat.isDirectory()) {
        result[key] = { type: 'directory', mode: stat.mode & 0o777 };
        await walk(key);
      } else if (stat.isFile()) result[key] = { type: 'file', sha256: sha256(await fs.readFile(full)), mode: stat.mode & 0o777 };
      else fail();
    }
  }
  await walk();
  return result;
}

// Fixed image paths and mount roots; no caller-selected commands or locations.
// /sealed must stay immutable throughout verification and copy (supervisor gate).
export async function launch(argv) {
  const { check, sealSha256 } = launcherArguments(argv);
  const [command, args] = checkCommand(check);
  const sealed = '/sealed';
  const work = '/work';
  if (!(await fs.lstat(sealed)).isDirectory() || (await fs.realpath(sealed)) !== sealed
    || !(await fs.lstat(work)).isDirectory() || (await fs.realpath(work)) !== work
    || (await fs.readdir(work)).length !== 0) fail();
  const sealPath = path.join(sealed, SEAL_NAME);
  if (!(await fs.lstat(sealPath)).isFile()) fail();
  const manifest = parseApprovedSeal(await fs.readFile(sealPath), sealSha256);
  const entries = await inventory(sealed);
  verifySeal(manifest, entries);
  // Complete verification precedes every write. Copy only inventoried entries;
  // retain relative links and never follow them while copying.
  const directories = Object.keys(entries).filter((key) => entries[key].type === 'directory')
    .sort((a, b) => a.split('/').length - b.split('/').length);
  for (const key of directories) await fs.mkdir(path.join(work, key));
  for (const [key, entry] of Object.entries(entries)) {
    if (entry.type === 'file') {
      await fs.copyFile(path.join(sealed, key), path.join(work, key), 1);
      await fs.chmod(path.join(work, key), entry.mode);
    }
    else if (entry.type === 'symlink') await fs.symlink(entry.target, path.join(work, key));
  }
  // Set directories last so sealed read-only directory permissions do not
  // prevent population, and apply children before their parents.
  for (const key of [...directories].reverse()) await fs.chmod(path.join(work, key), entries[key].mode);
  // Also detect copy corruption or a changed source before executing anything.
  verifySeal(manifest, await inventory(work));
  await fs.mkdir('/work/home', { recursive: true });
  return new Promise((resolve, reject) => {
    execFile(command, args, {
      cwd: work, env: checkerEnvironment(), shell: false,
      timeout: 600_000, maxBuffer: 16 * 1024 * 1024,
    }, (error, stdout, stderr) => {
      if (error) reject(error);
      else resolve({ stdout, stderr });
    });
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
    launch(process.argv.slice(2)).then(({ stdout, stderr }) => {
      process.stdout.write(stdout);
      process.stderr.write(stderr);
    }).catch(() => {
      process.stderr.write('LOCAL_PILOT_CHECK_FAILED\n');
      process.exitCode = 1;
    });
}
