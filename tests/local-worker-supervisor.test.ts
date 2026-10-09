import { EventEmitter } from 'node:events';
import { spawn, type ChildProcess } from 'node:child_process';
import { PassThrough } from 'node:stream';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  LocalWorkerSupervisor,
  buildLocalWorkerParentEnvironment,
  localPilotPaperWorkerIsDisarmed,
  localPilotWorkerStateMatchesCampaign,
} from '../src/backend/local-worker-supervisor';

class FakeChild extends EventEmitter {
  exitCode: number | null = null;
  signalCode: NodeJS.Signals | null = null;
  readonly signals: NodeJS.Signals[] = [];

  constructor(private readonly exitsOn: NodeJS.Signals | null) {
    super();
  }

  kill(signal: NodeJS.Signals = 'SIGTERM'): boolean {
    this.signals.push(signal);
    if (signal === this.exitsOn) {
      this.signalCode = signal;
      this.emit('exit', null, signal);
    }
    return true;
  }

  asChildProcess(): ChildProcess {
    return this as unknown as ChildProcess;
  }
}

const approvedSecrets = {
  approvalId: 'local-approval-12345678-1234-1234-1234-123456789abc',
  sourceFingerprint: 'a'.repeat(64),
  apiKey: 'mock-key',
  apiSecret: 'mock-secret',
  apiKeyVersion: '1',
  apiSecretVersion: '1',
};

const approvedPilotSecrets = {
  ...approvedSecrets,
  campaignId: 'pilot-2026-test-0001',
  gitSha: '1'.repeat(40),
  sourceHash: '2'.repeat(64),
  dependencyHash: '3'.repeat(64),
  migrationHash: '4'.repeat(64),
  strategyHash: '5'.repeat(64),
  riskPolicyHash: '6'.repeat(64),
  secretProjectId: 'test-project',
  strategyId: 'trend' as const,
  expiresAt: new Date(Date.now() + 60_000).toISOString(),
};

function readyResponse(payload: Record<string, unknown>): Response {
  return { ok: true, json: async () => payload } as Response;
}

afterEach(() => {
  vi.useRealTimers();
});

describe('Local Worker environment isolation', () => {
  it('passes only operating-system, PostgreSQL, and explicit runtime settings', () => {
    expect(buildLocalWorkerParentEnvironment({
      PATH: 'system-path',
      POSTGRES_HOST: '127.0.0.1',
      POSTGRES_PASSWORD: 'db-password',
      GOOGLE_APPLICATION_CREDENTIALS: 'C:/private/adc.json',
      FIREBASE_CONFIG: '{secret-ish}',
      BINANCE_TESTNET_API_SECRET: 'testnet-secret',
      UNRELATED_SECRET: 'must-not-inherit',
    })).toEqual({
      PATH: 'system-path',
      POSTGRES_HOST: '127.0.0.1',
      POSTGRES_PASSWORD: 'db-password',
    });
  });
});

describe('Local Pilot worker campaign read-back', () => {
  it('accepts only a LIVE runtime whose active configuration names the requested campaign', () => {
    expect(localPilotWorkerStateMatchesCampaign({
      execution_mode: 'LIVE',
      active_configuration: { pilotCampaignId: 'pilot-2026-test-0001' },
    }, 'pilot-2026-test-0001')).toBe(true);
    expect(localPilotWorkerStateMatchesCampaign({
      execution_mode: 'LIVE',
      active_configuration: { pilotCampaignId: 'pilot-another-campaign-0001' },
    }, 'pilot-2026-test-0001')).toBe(false);
    expect(localPilotWorkerStateMatchesCampaign({
      execution_mode: 'PAPER',
      active_configuration: { pilotCampaignId: 'pilot-2026-test-0001' },
    }, 'pilot-2026-test-0001')).toBe(false);
    expect(localPilotWorkerStateMatchesCampaign({
      execution_mode: 'LIVE',
      active_configuration: {},
    }, 'pilot-2026-test-0001')).toBe(false);
  });
});

describe('Local Pilot PAPER approval read-back', () => {
  it('requires the current responsive supervisor and exact Worker identity', () => {
    const supervisor = {
      ...new LocalWorkerSupervisor({
        workerUrl: 'http://127.0.0.1:8000',
        workerIdentityToken: 'mock-worker-token',
        runId: 'run-pilot-test',
      }).status(),
      mode: 'PAPER',
      workerRunning: true,
      workerResponsiveness: 'RESPONSIVE' as const,
    };
    const state = {
      execution_mode: 'PAPER',
      engine_state: 'DISARMED',
      mainnet_live_approved: false,
      local_run_id: 'run-pilot-test',
      local_supervisor_instance_id: supervisor.supervisorInstanceId,
      order_submission_attempts: 0,
    };
    expect(localPilotPaperWorkerIsDisarmed(state, supervisor, 'run-pilot-test')).toBe(true);
    expect(localPilotPaperWorkerIsDisarmed({ ...state, local_run_id: 'run-other-test' }, supervisor, 'run-pilot-test')).toBe(false);
    expect(localPilotPaperWorkerIsDisarmed({ ...state, local_supervisor_instance_id: 'other' }, supervisor, 'run-pilot-test')).toBe(false);
    expect(localPilotPaperWorkerIsDisarmed({ ...state, order_submission_attempts: '0' }, supervisor, 'run-pilot-test')).toBe(false);
    expect(localPilotPaperWorkerIsDisarmed(state, { ...supervisor, workerResponsiveness: 'STALE' }, 'run-pilot-test')).toBe(false);
  });
});

describe('LocalWorkerSupervisor process shutdown', () => {
  it('rejects unbound LIVE startup before stopping or spawning any Worker', async () => {
    const spawnMock = vi.fn();
    const supervisor = new LocalWorkerSupervisor({
      workerUrl: 'http://127.0.0.1:8000',
      workerIdentityToken: 'mock-worker-token',
      runId: 'local-run',
      spawnProcess: spawnMock as unknown as typeof spawn,
      pythonRuntimeResolver: () => ({ executable: 'trusted-test-python', assertUnchanged: vi.fn() }),
    });

    await expect(supervisor.startApprovedLive(approvedSecrets)).rejects.toThrow('LOCAL_PILOT_REQUIRED');
    expect(spawnMock).not.toHaveBeenCalled();
    expect(supervisor.status()).toMatchObject({ mode: 'STOPPED', workerRunning: false });
  });

  it('waits for the old child to exit before spawning a replacement', async () => {
    vi.useFakeTimers();
    const oldChild = new FakeChild('SIGKILL');
    const newChild = new FakeChild('SIGTERM');
    let statePollAvailable = true;
    let wrongSupervisorIdentity = false;
    const spawnMock = vi.fn((_command: string, _args: string[], _options: object) => {
      expect(oldChild.signalCode).toBe('SIGKILL');
      return newChild.asChildProcess();
    });
    const supervisorRef: { current: LocalWorkerSupervisor | null } = { current: null };
    const supervisor = new LocalWorkerSupervisor({
      workerUrl: 'http://127.0.0.1:8000',
      workerIdentityToken: 'mock-worker-token',
      runId: 'local-run',
      spawnProcess: spawnMock as unknown as typeof spawn,
      pythonRuntimeResolver: () => ({ executable: 'trusted-test-python', assertUnchanged: vi.fn() }),
      fetcher: async (input) => {
        if (String(input).endsWith('/ready')) return readyResponse({ status: 'ready' });
        if (String(input).endsWith('/state') && !statePollAvailable) throw new Error('worker state unavailable');
        return readyResponse({
          execution_mode: 'LIVE',
          engine_state: 'DISARMED',
          mainnet_live_approved: true,
          order_submission_attempts: 0,
          local_run_id: 'local-run',
          local_supervisor_instance_id: wrongSupervisorIdentity
            ? 'different-supervisor'
            : supervisorRef.current?.status().supervisorInstanceId,
          local_source_fingerprint: approvedSecrets.sourceFingerprint,
          pilot_campaign_id: approvedPilotSecrets.campaignId,
          heartbeat_at: new Date().toISOString(),
          pilot_lifecycle_monitor: { status: 'DEGRADED' },
        });
      },
    });
    supervisorRef.current = supervisor;
    const internal = supervisor as unknown as {
      worker: ChildProcess | null;
      mode: 'STOPPED' | 'PAPER' | 'LIVE';
    };
    internal.worker = oldChild.asChildProcess();
    internal.mode = 'LIVE';

    const starting = supervisor.startApprovedPilotLive(approvedPilotSecrets);
    await vi.advanceTimersByTimeAsync(5_000);
    await starting;

    expect(oldChild.signals).toEqual(['SIGTERM', 'SIGKILL']);
    expect(spawnMock).toHaveBeenCalledTimes(1);
    expect(spawnMock.mock.calls[0]?.[1]).toEqual(expect.arrayContaining(['-I', '-c']));
    expect(supervisor.status()).toMatchObject({
      workerRunning: true,
      workerResponsiveness: 'RESPONSIVE',
      pilotLifecycleMonitorStatus: 'DEGRADED',
    });
    statePollAvailable = false;
    await vi.advanceTimersByTimeAsync(11_000);
    expect(supervisor.status()).toMatchObject({
      workerResponsiveness: 'STALE',
      pilotLifecycleMonitorStatus: 'STALE',
    });
    statePollAvailable = true;
    wrongSupervisorIdentity = true;
    await vi.advanceTimersByTimeAsync(2_500);
    expect(supervisor.status()).toMatchObject({
      workerResponsiveness: 'UNKNOWN',
      pilotLifecycleMonitorStatus: 'UNKNOWN',
    });
    await supervisor.close();
  });

  it('fails closed and does not spawn a replacement if child exit is unconfirmed', async () => {
    vi.useFakeTimers();
    const stuckChild = new FakeChild(null);
    const spawnMock = vi.fn();
    const supervisor = new LocalWorkerSupervisor({
      workerUrl: 'http://127.0.0.1:8000',
      workerIdentityToken: 'mock-worker-token',
      runId: 'local-run',
      spawnProcess: spawnMock as unknown as typeof spawn,
      pythonRuntimeResolver: () => ({ executable: 'trusted-test-python', assertUnchanged: vi.fn() }),
    });
    const internal = supervisor as unknown as {
      worker: ChildProcess | null;
      mode: 'STOPPED' | 'PAPER' | 'LIVE';
    };
    internal.worker = stuckChild.asChildProcess();
    internal.mode = 'LIVE';

    const starting = supervisor.startApprovedPilotLive(approvedPilotSecrets);
    const outcome = starting.then(
      () => new Error('expected shutdown failure was not raised'),
      (error: unknown) => error as Error,
    );
    await vi.advanceTimersByTimeAsync(5_000);
    await vi.advanceTimersByTimeAsync(5_000);

    expect((await outcome).message).toBe('LOCAL_WORKER_SHUTDOWN_UNCONFIRMED');
    expect(stuckChild.signals).toEqual(['SIGTERM', 'SIGKILL']);
    expect(spawnMock).not.toHaveBeenCalled();
    expect(supervisor.status()).toMatchObject({ mode: 'LIVE', workerRunning: true });
  });
});

describe('LocalWorkerSupervisor Docker secret handoff', () => {
  it('keeps secrets out of Docker arguments/environment and sends a newline-delimited stdin envelope', async () => {
    const stdin = new PassThrough();
    const child = new EventEmitter() as ChildProcess;
    Object.assign(child, { stdin, exitCode: null, signalCode: null, kill: vi.fn(() => true) });
    const spawnMock = vi.fn(() => child);
    const captured: Buffer[] = [];
    const stdinEnded = new Promise<void>((resolve) => {
      stdin.on('data', (chunk: Buffer) => captured.push(Buffer.from(chunk)));
      stdin.on('end', resolve);
    });
    const apiKey = 'mainnet-api-key-sensitive';
    const apiSecret = 'mainnet-api-secret-sensitive';
    const workerToken = 'worker-identity-token-sensitive';
    const postgresPassword = 'postgres-password-sensitive';
    const expectedImageLabels = { 'org.blessing.git.sha': '1'.repeat(40),
      'org.blessing.source.sha256': '2'.repeat(64) };
    const assertImageUnchanged = vi.fn();
    const supervisor = new LocalWorkerSupervisor({
      workerUrl: 'http://127.0.0.1:8000',
      workerIdentityToken: workerToken,
      runId: 'local-docker-run',
      workerRuntime: 'DOCKER',
      workerImageId: `sha256:${'a'.repeat(64)}`,
      expectedWorkerImageLabels: expectedImageLabels,
      dockerExecutable: 'trusted-test-docker',
      dockerRuntimeResolver: (imageId, labels) => ({
        executable: 'trusted-test-docker',
        imageId,
        expectedLabels: labels!,
        serverVersion: 'test',
        engineHost: 'npipe:////./pipe/dockerDesktopLinuxEngine',
        assertUnchanged: assertImageUnchanged,
      }),
      spawnProcess: spawnMock as unknown as typeof spawn,
      environment: {
        POSTGRES_DB: 'blessing',
        POSTGRES_USER: 'worker',
        POSTGRES_PASSWORD: postgresPassword,
        WORKER_IDENTITY_TOKEN: workerToken,
      },
    });
    const sourceEnvironment = {
      LOCAL_ONLY: 'true',
      POSTGRES_DB: 'blessing',
      POSTGRES_USER: 'worker',
      POSTGRES_PASSWORD: postgresPassword,
      WORKER_IDENTITY_TOKEN: workerToken,
      BINANCE_MAINNET_API_KEY: apiKey,
      BINANCE_MAINNET_API_SECRET: apiSecret,
      BINANCE_TESTNET_API_KEY: '',
      BINANCE_TESTNET_API_SECRET: '',
    };
    const invoke = supervisor as unknown as {
      spawnDockerWorker: (environment: NodeJS.ProcessEnv, config: {
        mode: 'PAPER' | 'LIVE'; apiKey?: string; apiSecret?: string;
      }) => ChildProcess;
    };

    invoke.spawnDockerWorker(sourceEnvironment, { mode: 'LIVE', apiKey, apiSecret });
    supervisor.assertWorkerImageUnchanged();
    expect(assertImageUnchanged).toHaveBeenCalled();
    await stdinEnded;

    const [command, args, spawnOptions] = spawnMock.mock.calls[0] as unknown as [
      string, string[], { env: NodeJS.ProcessEnv },
    ];
    const envelope = Buffer.concat(captured).toString('utf8');
    expect(envelope.endsWith('\n')).toBe(true);
    expect(JSON.parse(envelope)).toEqual({
      apiKey,
      apiSecret,
      workerIdentityToken: workerToken,
      postgresPassword,
    });
    expect(command).toBe('trusted-test-docker');
    expect(args).not.toContain(apiKey);
    expect(args).not.toContain(apiSecret);
    expect(args).not.toContain(workerToken);
    expect(args).not.toContain(postgresPassword);
    for (const value of Object.values(spawnOptions.env)) {
      expect(value).not.toBe(apiKey);
      expect(value).not.toBe(apiSecret);
      expect(value).not.toBe(workerToken);
      expect(value).not.toBe(postgresPassword);
    }
    expect(spawnOptions.env.BINANCE_MAINNET_API_KEY).toBe('');
    expect(spawnOptions.env.BINANCE_MAINNET_API_SECRET).toBe('');
    expect(spawnOptions.env).not.toHaveProperty('WORKER_IDENTITY_TOKEN');
    expect(spawnOptions.env).not.toHaveProperty('POSTGRES_PASSWORD');
  });
});
