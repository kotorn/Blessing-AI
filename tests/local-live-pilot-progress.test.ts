import { createHash } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import { progressCheckFromResult } from '../scripts/collect_local_pilot_progress.js';

describe('Local Pilot diagnostic progress records', () => {
  it('records a successful process with output digest and no raw output', () => {
    const check = progressCheckFromResult({
      id: 'BUILD',
      startedAt: '2026-09-29T00:00:00.000Z',
      completedAt: '2026-09-29T00:00:01.000Z',
      durationMs: 1_000,
      exitCode: 0,
      output: 'build passed',
    });

    expect(check).toEqual({
      id: 'BUILD',
      status: 'PASS',
      startedAt: '2026-09-29T00:00:00.000Z',
      completedAt: '2026-09-29T00:00:01.000Z',
      durationMs: 1_000,
      exitCode: 0,
      outputSha256: createHash('sha256').update('build passed').digest('hex'),
    });
    expect(JSON.stringify(check)).not.toContain('build passed');
  });

  it('records failures without exposing command output', () => {
    const check = progressCheckFromResult({
      id: 'POSTGRES_17_MIGRATIONS_RESTART',
      startedAt: '2026-09-29T00:00:00.000Z',
      completedAt: '2026-09-29T00:00:02.000Z',
      durationMs: 2_000,
      exitCode: 1,
      output: 'password=must-not-be-stored',
    });

    expect(check.status).toBe('FAIL');
    expect(check.outputSha256).toBeTruthy();
    expect(JSON.stringify(check)).not.toContain('must-not-be-stored');
  });

  it('treats a missing process exit status as a failure, never as a pass', () => {
    const check = progressCheckFromResult({
      id: 'PYTHON_TESTS',
      startedAt: '2026-09-29T00:00:00.000Z',
      completedAt: '2026-09-29T00:00:03.000Z',
      durationMs: 3_000,
      exitCode: null,
      output: '',
    });

    expect(check.status).toBe('FAIL');
    expect(check.exitCode).toBeNull();
  });
});
