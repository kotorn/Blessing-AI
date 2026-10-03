import { describe, expect, it } from 'vitest';
import {
  buildTrustedPythonVerificationEnvironment,
  resolveTrustedLocalPythonRuntime,
} from '../src/backend/local-python-runtime.js';

describe('trusted Local Python runtime', () => {
  it('normalizes environment names and strips inherited credential variables', () => {
    expect(buildTrustedPythonVerificationEnvironment({
      Path: 'C:\\Windows\\System32', SystemRoot: 'C:\\Windows',
      BINANCE_MAINNET_API_SECRET: 'must-not-forward',
    }, 'C:\\Python\\python.exe')).toMatchObject({
      PATH: 'C:\\Windows\\System32', SYSTEMROOT: 'C:\\Windows',
      BLESSING_PYTHON_PATH: 'C:\\Python\\python.exe',
    });
    expect(buildTrustedPythonVerificationEnvironment({ BINANCE_MAINNET_API_SECRET: 'x' }, '')
      .BINANCE_MAINNET_API_SECRET).toBeUndefined();
  });

  it.runIf(process.platform === 'win32' && process.env.LOCAL_WORKER_RUNTIME === 'HOST_PYTHON')(
    'verifies the signer and protected runtime installation before explicit host-Python use', () => {
    const runtime = resolveTrustedLocalPythonRuntime(process.env, process.cwd());

    expect(runtime.executable).toMatch(/^[A-Z]:\\.+\\python\.exe$/i);
    expect(() => runtime.assertUnchanged()).not.toThrow();
    },
  );
});
