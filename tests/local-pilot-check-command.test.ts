import { describe, expect, it, vi } from 'vitest';

vi.mock('node:fs', () => ({
  existsSync: vi.fn(() => true),
  realpathSync: vi.fn((value: string) => value),
}));

import { existsSync, realpathSync } from 'node:fs';
import { localPilotNpmCommand } from '../src/backend/local-pilot-check-command.js';

describe('Local pilot shell-free check commands', () => {
  it('uses Node plus a fixed adjacent npm CLI and separate argv', () => {
    const [command, args] = localPilotNpmCommand('test');
    expect(command).toBe(process.execPath);
    expect(args.slice(1)).toEqual(['run', 'test']);
    expect(args[0]).toMatch(/npm[\\/]bin[\\/]npm-cli\.js$/);
    expect(args).not.toContain('/c');
  });
  it('rejects non-allowlisted scripts', () => {
    expect(() => localPilotNpmCommand('test && other' as 'test')).toThrow('NOT_ALLOWED');
  });
  it('fails closed when npm is missing', () => {
    vi.mocked(existsSync).mockReturnValueOnce(false);
    expect(() => localPilotNpmCommand('lint')).toThrow('UNAVAILABLE');
  });
  it('rejects a redirected npm CLI', () => {
    vi.mocked(realpathSync).mockImplementationOnce((value) => String(value))
      .mockImplementationOnce(() => 'untrusted-cli');
    expect(() => localPilotNpmCommand('build')).toThrow('UNTRUSTED');
  });
});
