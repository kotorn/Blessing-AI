import { execFile } from 'node:child_process';
import path from 'node:path';
import { afterEach, expect, it, vi } from 'vitest';
import { executePilotContainer } from '../src/backend/local-pilot-container-executor.js';
import { verifyPilotCheckContainer } from '../src/backend/local-pilot-container-check.js';

vi.mock('node:child_process', () => ({ execFile: vi.fn() }));
vi.mock('../src/backend/local-pilot-container-check.js', () => ({
  pilotContainerCheckArgs: () => ['create', '--network=none'],
  verifyPilotCheckContainer: vi.fn(),
}));
afterEach(() => vi.resetAllMocks());
const id = 'f'.repeat(64);
const configuration = { runId: '11111111-1111-4111-8111-111111111111',
  snapshotId: '22222222-2222-4222-8222-222222222222', imageId: 'sha256:' + 'a'.repeat(64),
  sealSha256: 'b'.repeat(64), check: 'BUILD' as const };
function input() {
  return { dockerExecutable: path.resolve('docker-fixture'), configuration,
    assertApprovedSnapshot: vi.fn(async () => {}) };
}
function transport(exitCode = 0) {
  let inspected = 0;
  vi.mocked(execFile).mockImplementation((...parameters: unknown[]) => {
    const args = parameters[1] as string[];
    const callback = parameters.at(-1) as (error: null, stdout: Buffer, stderr: Buffer) => void;
    let output = '';
    if (args[0] === 'create') output = id;
    if (args[0] === 'inspect') {
      inspected++;
      output = JSON.stringify([{ Id: id, Name: `/blessing-acceptance-${configuration.runId}`,
        State: inspected === 1 ? { Status: 'created' } : { Status: 'exited', ExitCode: exitCode, OOMKilled: false, Error: '' } }]);
    }
    if (args[0] === 'start') output = 'check output';
    queueMicrotask(() => callback(null, Buffer.from(output), Buffer.alloc(0)));
    return {} as ReturnType<typeof execFile>;
  });
}
it('inspects isolation before starting and checks terminal exit before returning success', async () => {
  transport();
  const options = input();
  expect((await executePilotContainer(options)).error).toBeNull();
  expect(verifyPilotCheckContainer).toHaveBeenCalledOnce();
  expect(options.assertApprovedSnapshot).toHaveBeenCalledTimes(3);
  expect(vi.mocked(execFile).mock.calls.map((call) => call[1])).toEqual([
    ['create', '--network=none'], ['inspect', id], ['start', '--attach', id], ['inspect', id], ['rm', '--force', id],
  ]);
});
it('does not treat successful Docker attach as a passing failed checker', async () => {
  transport(1);
  await expect(executePilotContainer(input())).rejects.toThrow('CHECK_FAILED');
  expect(vi.mocked(execFile).mock.calls.at(-1)?.[1]).toEqual(['rm', '--force', id]);
});
it('never starts when isolation verification fails and cleans only its returned container ID', async () => {
  transport();
  vi.mocked(verifyPilotCheckContainer).mockImplementationOnce(() => { throw new Error('ISOLATION_MISMATCH'); });
  await expect(executePilotContainer(input())).rejects.toThrow('ISOLATION_MISMATCH');
  expect(vi.mocked(execFile).mock.calls.some((call) => (call[1] as string[])[0] === 'start')).toBe(false);
  expect(vi.mocked(execFile).mock.calls.at(-1)?.[1]).toEqual(['rm', '--force', id]);
});
it('does not create any container when snapshot policy approval fails', async () => {
  const options = input();
  options.assertApprovedSnapshot.mockRejectedValueOnce(new Error('NOT_APPROVED'));
  await expect(executePilotContainer(options)).rejects.toThrow('NOT_APPROVED');
  expect(execFile).not.toHaveBeenCalled();
});
