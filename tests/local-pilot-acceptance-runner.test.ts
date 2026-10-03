import { afterEach, expect, it, vi } from 'vitest';
vi.mock('node:child_process', () => ({ execFile: vi.fn() }));
vi.mock('../src/backend/local-pilot-check-command.js', () => ({
  localPilotNpmCommand: () => ['node', ['npm-cli.js', 'run', 'test']],
}));
import { execFile } from 'node:child_process';
import { runPilotOfflineAcceptance, type PilotAcceptanceAudit } from '../src/backend/local-pilot-acceptance-runner.js';

afterEach(() => vi.resetAllMocks());
function successfulProcess() {
  vi.mocked(execFile).mockImplementation((...args: unknown[]) => {
    const callback = args.at(-1) as (error: null, stdout: Buffer, stderr: Buffer) => void;
    queueMicrotask(() => callback(null, Buffer.from('ok'), Buffer.alloc(0)));
    return {} as ReturnType<typeof execFile>;
  });
}
function options() {
  return {
    root: process.cwd(), isolatedHome: process.cwd(), check: 'TYPESCRIPT_TESTS' as const,
    binding: { gitSha: 'a'.repeat(40), sourceSha256: 'b'.repeat(64), dependencySha256: 'c'.repeat(64),
      migrationSha256: 'd'.repeat(64), policySha256: 'e'.repeat(64) },
    audit: { begin: vi.fn<PilotAcceptanceAudit['begin']>(async () => {}),
      finish: vi.fn<PilotAcceptanceAudit['finish']>(async () => {}) },
    assertBindingUnchanged: vi.fn(),
    executeIsolated: vi.fn(async () => await new Promise<{ error: unknown; stdout: Buffer; stderr: Buffer }>((resolve) => {
      execFile('isolated-fixture', [], { shell: false }, (error, stdout, stderr) => resolve({
        error, stdout: Buffer.from(stdout), stderr: Buffer.from(stderr),
      }));
    })),
  };
}
it('writes server-owned begin/finish around an isolated supervisor check', async () => {
  successfulProcess();
  const input = options();
  expect((await runPilotOfflineAcceptance(input)).status).toBe('PASS');
  expect(input.audit.begin).toHaveBeenCalledOnce();
  expect(input.audit.finish).toHaveBeenCalledOnce();
  expect(input.executeIsolated).toHaveBeenCalledWith(input.audit.begin.mock.calls[0][0], input.check, input.binding);
});

it('never falls back to host execution when isolation is not configured', async () => {
  const input = options();
  await expect(runPilotOfflineAcceptance({ ...input, executeIsolated: undefined }))
    .rejects.toThrow('ISOLATED_RUNNER_NOT_CONFIGURED');
  expect(execFile).not.toHaveBeenCalled();
  expect(input.audit.begin).not.toHaveBeenCalled();
});
it('never executes when durable begin fails', async () => {
  const input = options(); input.audit.begin.mockRejectedValueOnce(new Error('audit down'));
  await expect(runPilotOfflineAcceptance(input)).rejects.toMatchObject({
    message: 'LOCAL_PILOT_ACCEPTANCE_ACKNOWLEDGEMENT_UNKNOWN',
    runId: expect.stringMatching(/^[a-f0-9-]{36}$/),
  });
  expect(execFile).not.toHaveBeenCalled();
});
it('cannot return success when finish acknowledgment is lost', async () => {
  successfulProcess();
  const input = options(); input.audit.finish.mockRejectedValueOnce(new Error('lost ack'));
  await expect(runPilotOfflineAcceptance(input)).rejects.toMatchObject({
    message: 'LOCAL_PILOT_ACCEPTANCE_ACKNOWLEDGEMENT_UNKNOWN',
    runId: expect.stringMatching(/^[a-f0-9-]{36}$/),
  });
  expect(input.audit.begin.mock.calls[0][0]).toBe(input.audit.finish.mock.calls[0][0]);
});
it('records failure when the source changes after execution', async () => {
  const input = options();
  input.assertBindingUnchanged.mockImplementationOnce(() => {}).mockImplementationOnce(() => {})
    .mockImplementationOnce(() => { throw new Error('changed'); });
  successfulProcess();
  expect((await runPilotOfflineAcceptance(input)).status).toBe('FAIL');
});

it('leaves the event loop responsive while the subprocess is pending', async () => {
  let complete: ((error: null, stdout: Buffer, stderr: Buffer) => void) | undefined;
  vi.mocked(execFile).mockImplementation((...args: unknown[]) => {
    complete = args.at(-1) as typeof complete;
    return {} as ReturnType<typeof execFile>;
  });
  const input = options();
  const pending = runPilotOfflineAcceptance(input);
  await new Promise<void>((resolve) => setTimeout(resolve, 0));
  expect(input.audit.finish).not.toHaveBeenCalled();
  expect(complete).toBeTypeOf('function');
  complete!(null, Buffer.from('done'), Buffer.alloc(0));
  expect((await pending).status).toBe('PASS');
});

it('records process timeout/error as FAIL without exposing output', async () => {
  vi.mocked(execFile).mockImplementation((...args: unknown[]) => {
    const callback = args.at(-1) as (error: Error, stdout: Buffer, stderr: Buffer) => void;
    queueMicrotask(() => callback(new Error('timeout'), Buffer.alloc(0), Buffer.from('private output')));
    return {} as ReturnType<typeof execFile>;
  });
  const input = options();
  expect((await runPilotOfflineAcceptance(input)).status).toBe('FAIL');
  expect(JSON.stringify(input.audit.finish.mock.calls)).not.toContain('private output');
});
