import { execFile } from 'node:child_process';
import path from 'node:path';
import { buildLocalPilotGitEnvironment } from './local-live-pilot-readiness.js';
import { pilotContainerCheckArgs, verifyPilotCheckContainer } from './local-pilot-container-check.js';

type Configuration = Parameters<typeof pilotContainerCheckArgs>[0];
type Result = { error: unknown; stdout: Buffer; stderr: Buffer };

/** Server-owned Docker transport. The caller must verify approved image policy,
 * snapshot provenance and absence of concurrent writers; these are not inferred
 * from a passing test or an immutable image identifier. */
export async function executePilotContainer(options: {
  dockerExecutable: string; dockerHost?: string; configuration: Configuration;
  assertApprovedSnapshot: () => Promise<void>;
}): Promise<Result> {
  if (!path.isAbsolute(options.dockerExecutable)) throw new Error('LOCAL_PILOT_DOCKER_EXECUTABLE_INVALID');
  const args = pilotContainerCheckArgs(options.configuration);
  const name = `blessing-acceptance-${options.configuration.runId}`;
  const invoke = (argv: string[], timeout = 30_000): Promise<Result> => new Promise((resolve) => {
    execFile(options.dockerExecutable, argv, {
      env: { ...buildLocalPilotGitEnvironment(process.env),
        ...(options.dockerHost ? { DOCKER_HOST: options.dockerHost } : {}) }, shell: false, windowsHide: true,
      encoding: 'buffer', timeout, maxBuffer: 16 * 1024 * 1024,
    }, (error, stdout, stderr) => resolve({ error, stdout, stderr }));
  });
  await options.assertApprovedSnapshot();
  const created = await invoke(args);
  if (created.error) throw new Error('LOCAL_PILOT_CONTAINER_CREATE_UNKNOWN');
  // Only cleanup a container whose identity was returned by this create call.
  const id = created.stdout.toString('utf8').trim();
  if (!/^[a-f0-9]{64}$/.test(id)) throw new Error('LOCAL_PILOT_CONTAINER_CREATE_UNKNOWN');
  const run = async (): Promise<Result> => {
    const inspected = await invoke(['inspect', id]);
    if (inspected.error) throw new Error('LOCAL_PILOT_CONTAINER_INSPECT_FAILED');
    const values = JSON.parse(inspected.stdout.toString('utf8'));
    if (!Array.isArray(values) || values.length !== 1 || values[0].Id !== id || values[0].Name !== `/${name}`) {
      throw new Error('LOCAL_PILOT_CONTAINER_IDENTITY_MISMATCH');
    }
    verifyPilotCheckContainer(values[0], options.configuration);
    await options.assertApprovedSnapshot();
    const execution = await invoke(['start', '--attach', id], 15 * 60 * 1000);
    if (execution.error) return execution;
    const terminal = await invoke(['inspect', id]);
    if (terminal.error) throw new Error('LOCAL_PILOT_CONTAINER_TERMINAL_UNKNOWN');
    const final = JSON.parse(terminal.stdout.toString('utf8'));
    if (!Array.isArray(final) || final.length !== 1 || final[0].Id !== id
      || final[0].State?.Status !== 'exited' || final[0].State?.ExitCode !== 0
      || final[0].State?.OOMKilled !== false || final[0].State?.Error !== '') {
      throw new Error('LOCAL_PILOT_CONTAINER_CHECK_FAILED');
    }
    await options.assertApprovedSnapshot();
    return execution;
  };
  let result: Result | undefined;
  let failed = false;
  let failure: unknown;
  try { result = await run(); } catch (error) { failed = true; failure = error; }
  const cleaned = await invoke(['rm', '--force', id]);
  if (cleaned.error) throw new Error('LOCAL_PILOT_CONTAINER_CLEANUP_UNKNOWN');
  if (failed) throw failure;
  if (!result) throw new Error('LOCAL_PILOT_CONTAINER_TERMINAL_UNKNOWN');
  return result;
}
