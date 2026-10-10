import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { resetTrustedPythonVerificationCache, verifyPython } from '../src/backend/local-python-runtime.js';

describe('trusted Python tree verification cache', () => {
  let dir = '';
  let exe = '';
  beforeEach(() => {
    resetTrustedPythonVerificationCache();
    dir = mkdtempSync(path.join(os.tmpdir(), 'blessing-py-'));
    exe = path.join(dir, 'python.exe');
    writeFileSync(exe, 'interpreter-v1');
  });
  afterEach(() => rmSync(dir, { recursive: true, force: true }));

  const trustedRunner = () => {
    const calls: string[] = [];
    const run = ((file: string) => { calls.push(file); return 'TRUSTED\r\n'; }) as never;
    return { calls, run };
  };

  it('reuses a successful tree verification inside the window', () => {
    const { calls, run } = trustedRunner();
    let t = 1_000;
    const deps = { run, now: () => t };
    const first = verifyPython(exe, {}, true, deps);
    t += 30_000;
    const second = verifyPython(exe, {}, true, deps);
    expect(second).toBe(first);
    expect(calls).toHaveLength(1);
  });

  it('verifies again once the window has passed', () => {
    const { calls, run } = trustedRunner();
    let t = 1_000;
    const deps = { run, now: () => t };
    verifyPython(exe, {}, true, deps);
    t += 61_000;
    verifyPython(exe, {}, true, deps);
    expect(calls).toHaveLength(2);
  });

  it('never accepts a replaced interpreter from the cache', () => {
    const { calls, run } = trustedRunner();
    const deps = { run, now: () => 1_000 };
    const before = verifyPython(exe, {}, true, deps);
    writeFileSync(exe, 'interpreter-v2-tampered');
    const after = verifyPython(exe, {}, true, deps);
    expect(after).not.toBe(before);
    expect(calls).toHaveLength(2);
  });

  it('can be disabled so every call verifies', () => {
    const { calls, run } = trustedRunner();
    const deps = { run, now: () => 1_000 };
    const env = { BLESSING_TRUSTED_PYTHON_CACHE_SECONDS: '0' };
    verifyPython(exe, env, true, deps);
    verifyPython(exe, env, true, deps);
    expect(calls).toHaveLength(2);
  });

  it('does not cache a failed verification', () => {
    const failing = (() => { throw new Error('exit 1'); }) as never;
    expect(() => verifyPython(exe, {}, true, { run: failing, now: () => 1 }))
      .toThrow('LOCAL_PILOT_TRUSTED_PYTHON_EXECUTABLE_NOT_VERIFIED');
    const { calls, run } = trustedRunner();
    verifyPython(exe, {}, true, { run, now: () => 2 });
    expect(calls).toHaveLength(1);
  });
});
