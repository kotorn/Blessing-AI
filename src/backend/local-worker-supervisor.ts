import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import {
  resolveTrustedLocalPythonRuntime,
  type TrustedLocalPythonRuntime,
} from './local-python-runtime.js';
import {
  resolveTrustedLocalDockerRuntime,
  type TrustedLocalDockerRuntime,
} from './local-docker-runtime.js';
import type { PilotReadinessVerdict } from './local-pilot-verdict.js';

export interface LocalWorkerSupervisorOptions {
  root?: string;
  workerUrl: string;
  workerIdentityToken: string;
  runId: string;
  environment?: NodeJS.ProcessEnv;
  spawnProcess?: typeof spawn;
  pythonRuntimeResolver?: () => TrustedLocalPythonRuntime;
  workerRuntime?: 'HOST_PYTHON' | 'DOCKER';
  workerImageId?: string;
  dockerExecutable?: string;
  dockerRuntimeResolver?: (imageId: string) => TrustedLocalDockerRuntime;
  dockerExecFileSync?: typeof execFileSync;
  fetcher?: typeof fetch;
}

export interface ApprovedLocalWorkerSecrets {
  approvalId: string;
  sourceFingerprint: string;
  apiKey: string;
  apiSecret: string;
  apiKeyVersion: string;
  apiSecretVersion: string;
}

export interface ApprovedLocalPilotSecrets extends ApprovedLocalWorkerSecrets {
  campaignId: string;
  gitSha: string;
  sourceHash: string;
  dependencyHash: string;
  migrationHash: string;
  strategyHash: string;
  riskPolicyHash: string;
  secretProjectId: string;
  strategyId: 'grid' | 'trend' | 'shock' | 'carry';
  expiresAt: string;
}

const WORKER_INHERITED_ENV_KEYS = [
  'PATH', 'Path', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE',
  'APPDATA', 'LOCALAPPDATA', 'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE',
  'POSTGRES_HOST', 'POSTGRES_PORT', 'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD',
  'PERSISTENCE_MODE', 'MAX_MARGIN_UTILIZATION_PCT', 'ACCOUNT_SNAPSHOT_MAX_AGE_SEC',
  'MAX_MARKET_DATA_AGE_SEC', 'PRIVATE_STREAM_MAX_AGE_SEC', 'EXECUTION_LEASE_TTL_SECONDS',
  'BINANCE_REQUEST_TIMEOUT_SEC', 'BINANCE_PORTFOLIO_MARGIN', 'LOG_LEVEL', 'PYTHONUNBUFFERED',
  'WORKER_REVISION', 'WORKER_IMAGE_DIGEST',
] as const;

export function buildLocalWorkerParentEnvironment(
  source: NodeJS.ProcessEnv,
): NodeJS.ProcessEnv {
  const result: NodeJS.ProcessEnv = {};
  for (const key of WORKER_INHERITED_ENV_KEYS) {
    const value = source[key];
    if (typeof value === 'string') result[key] = value;
  }
  return result;
}

export function localPilotWorkerStateMatchesCampaign(
  state: Record<string, unknown>,
  campaignId: string,
): boolean {
  const configuration = state.active_configuration && typeof state.active_configuration === 'object'
    && !Array.isArray(state.active_configuration)
    ? state.active_configuration as Record<string, unknown>
    : state.activeConfiguration && typeof state.activeConfiguration === 'object'
      && !Array.isArray(state.activeConfiguration)
      ? state.activeConfiguration as Record<string, unknown>
      : {};
  const runtimeCampaignId = state.pilot_campaign_id
    ?? state.pilotCampaignId
    ?? configuration.pilotCampaignId
    ?? configuration.pilot_campaign_id;
  return state.execution_mode === 'LIVE'
    && typeof runtimeCampaignId === 'string'
    && runtimeCampaignId === campaignId;
}

export function localPilotPaperWorkerIsDisarmed(
  state: Record<string, unknown>,
  supervisor: ReturnType<LocalWorkerSupervisor['status']> | null,
  runId: string,
): boolean {
  return state.execution_mode === 'PAPER'
    && state.engine_state === 'DISARMED'
    && state.mainnet_live_approved === false
    && state.local_run_id === runId
    && state.local_supervisor_instance_id === supervisor?.supervisorInstanceId
    && state.order_submission_attempts === 0
    && supervisor?.runtimeTarget === 'LOCAL'
    && supervisor.runId === runId
    && supervisor.mode === 'PAPER'
    && supervisor.workerRunning
    && supervisor.workerResponsiveness === 'RESPONSIVE';
}

export async function revokeLocalPilotWithWorkerGuard<T>(input: {
  campaign: { campaignId: string; adminUid: string; status: string };
  actorUid: string;
  worker: { workerRunning: boolean; mode: string; pilotCampaignId: string | null };
  requestRecoveryOnly: () => Promise<{ ok: boolean; active?: unknown }>;
  readWorkerState: () => Promise<Record<string, unknown>>;
  commitRevocation: () => Promise<T>;
  onWorkerRecoveryConfirmed?: () => void;
}): Promise<T> {
  if (input.campaign.adminUid !== input.actorUid) {
    throw new Error('LOCAL_PILOT_REVOCER_UID_MISMATCH');
  }
  if (input.campaign.status === 'COMPLETED') {
    throw new Error('LOCAL_PILOT_ALREADY_COMPLETED');
  }
  if (!['PENDING_APPROVAL', 'APPROVED', 'ACTIVE', 'CLOSE_ONLY', 'EXPIRED', 'REVOKED']
    .includes(input.campaign.status)) {
    throw new Error('LOCAL_PILOT_CANNOT_BE_REVOKED_FROM_STATUS');
  }

  if (input.worker.workerRunning && input.worker.mode === 'LIVE') {
    if (input.worker.pilotCampaignId !== input.campaign.campaignId) {
      throw new Error('LOCAL_PILOT_WORKER_CAMPAIGN_MISMATCH');
    }
    const recovery = await input.requestRecoveryOnly();
    if (!recovery.ok || recovery.active !== true) {
      throw new Error('LOCAL_PILOT_WORKER_CLOSE_ONLY_UNCONFIRMED');
    }
    const state = await input.readWorkerState();
    if (!localPilotWorkerStateMatchesCampaign(state, input.campaign.campaignId)
      || state.recovery_only !== true
      || state.engine_state !== 'RECOVERY_ONLY') {
      throw new Error('LOCAL_PILOT_WORKER_CLOSE_ONLY_UNCONFIRMED');
    }
    input.onWorkerRecoveryConfirmed?.();
  }

  return input.commitRevocation();
}

export class LocalWorkerSupervisor {
  private readonly root: string;
  private readonly environment: NodeJS.ProcessEnv;
  private readonly spawnProcess: typeof spawn;
  private readonly pythonRuntimeResolver: () => TrustedLocalPythonRuntime;
  private readonly workerRuntime: 'HOST_PYTHON' | 'DOCKER';
  private readonly workerImageId: string;
  private readonly dockerExecutable: string | undefined;
  private readonly dockerRuntimeResolver: (imageId: string) => TrustedLocalDockerRuntime;
  private readonly dockerExecFileSync: typeof execFileSync;
  private readonly fetcher: typeof fetch;
  private pythonRuntime: TrustedLocalPythonRuntime | null = null;
  private dockerRuntime: TrustedLocalDockerRuntime | null = null;
  private worker: ChildProcess | null = null;
  private workerContainerName: string | null = null;
  private workerApprovalId: string | null = null;
  private workerSourceFingerprint: string | null = null;
  private workerApiKeyVersion: string | null = null;
  private workerApiSecretVersion: string | null = null;
  private workerPilotCampaignId: string | null = null;
  private workerGeneration = 0;
  private lastWorkerStderr = '';
  private readonly supervisorInstanceId = randomUUID();
  private mode: 'STOPPED' | 'PAPER' | 'LIVE' = 'STOPPED';
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private stateMonitorTimer: ReturnType<typeof setInterval> | null = null;
  private activePilotVerdict: PilotReadinessVerdict | null = null;
  private stateMonitorInFlightFor: ChildProcess | null = null;
  private lastStateObservedAt: string | null = null;
  private workerHeartbeatAt: string | null = null;
  private pilotLifecycleMonitorStatus = 'UNKNOWN';

  constructor(private readonly options: LocalWorkerSupervisorOptions) {
    this.root = path.resolve(options.root || process.cwd());
    this.environment = options.environment || process.env;
    this.spawnProcess = options.spawnProcess || spawn;
    this.pythonRuntimeResolver = options.pythonRuntimeResolver
      || (() => resolveTrustedLocalPythonRuntime(this.environment, this.root));
    this.workerRuntime = options.workerRuntime || (this.environment.LOCAL_WORKER_RUNTIME === 'DOCKER'
      ? 'DOCKER' : 'HOST_PYTHON');
    this.workerImageId = options.workerImageId || this.environment.LOCAL_WORKER_IMAGE_ID || '';
    this.dockerExecutable = options.dockerExecutable;
    this.dockerRuntimeResolver = options.dockerRuntimeResolver || ((imageId) =>
      resolveTrustedLocalDockerRuntime({
        imageId, environment: this.environment, dockerExecutable: this.dockerExecutable,
        execFileSync: this.dockerExecFileSync,
      }));
    this.dockerExecFileSync = options.dockerExecFileSync || execFileSync;
    this.fetcher = options.fetcher || fetch;
  }

  public setPilotReadinessVerdict(verdict: PilotReadinessVerdict | null): void {
    this.activePilotVerdict = verdict;
  }

  status(): {
    runtimeTarget: 'LOCAL';
    runId: string;
    mode: string;
    approvalId: string | null;
    sourceFingerprint: string | null;
    secretVersions: { apiKey: string; apiSecret: string } | null;
    pilotCampaignId: string | null;
    workerGeneration: number | null;
    workerRuntime: 'HOST_PYTHON' | 'DOCKER';
    workerContainerName: string | null;
    supervisorInstanceId: string;
    workerRunning: boolean;
    workerResponsiveness: 'STOPPED' | 'UNKNOWN' | 'RESPONSIVE' | 'STALE';
    workerStateObservedAt: string | null;
    workerHeartbeatAt: string | null;
    pilotLifecycleMonitorStatus: string;
  } {
    const workerRunning = this.worker !== null && this.childIsRunning(this.worker);
    const heartbeatMs = this.workerHeartbeatAt ? Date.parse(this.workerHeartbeatAt) : NaN;
    const heartbeatAgeMs = Date.now() - heartbeatMs;
    const stateObservedMs = this.lastStateObservedAt ? Date.parse(this.lastStateObservedAt) : NaN;
    const stateObservationAgeMs = Date.now() - stateObservedMs;
    const workerResponsiveness = !workerRunning
      ? 'STOPPED'
      : !Number.isFinite(heartbeatMs) || !Number.isFinite(stateObservedMs)
        ? 'UNKNOWN'
        : heartbeatAgeMs >= -2_000 && heartbeatAgeMs <= 10_000
          && stateObservationAgeMs >= 0 && stateObservationAgeMs <= 10_000
          ? 'RESPONSIVE'
          : 'STALE';
    const lifecycleMonitorStatus = !workerRunning ? 'UNKNOWN' : Number.isFinite(stateObservationAgeMs)
      && stateObservationAgeMs >= 0 && stateObservationAgeMs <= 10_000
      ? this.pilotLifecycleMonitorStatus
      : this.pilotLifecycleMonitorStatus === 'UNKNOWN' ? 'UNKNOWN' : 'STALE';
    return {
      runtimeTarget: 'LOCAL',
      runId: this.options.runId,
      mode: workerRunning ? this.mode : 'STOPPED',
      approvalId: workerRunning ? this.workerApprovalId : null,
      sourceFingerprint: workerRunning ? this.workerSourceFingerprint : null,
      secretVersions: workerRunning && this.workerApiKeyVersion && this.workerApiSecretVersion
        ? { apiKey: this.workerApiKeyVersion, apiSecret: this.workerApiSecretVersion }
        : null,
      pilotCampaignId: workerRunning ? this.workerPilotCampaignId : null,
      workerGeneration: workerRunning ? this.workerGeneration : null,
      workerRuntime: this.workerRuntime,
      workerContainerName: this.workerContainerName,
      supervisorInstanceId: this.supervisorInstanceId,
      workerRunning,
      workerResponsiveness,
      workerStateObservedAt: this.lastStateObservedAt,
      workerHeartbeatAt: this.workerHeartbeatAt,
      pilotLifecycleMonitorStatus: lifecycleMonitorStatus,
    };
  }

  async startPaper(): Promise<void> {
    await this.stopWorker();
    this.workerApprovalId = null;
    this.workerPilotCampaignId = null;
    this.workerSourceFingerprint = null;
    this.workerApiKeyVersion = null;
    this.workerApiSecretVersion = null;
    await this.startWorker({ mode: 'PAPER', approved: false });
    await this.waitForState((state) =>
      state.execution_mode === 'PAPER'
      && state.engine_state === 'DISARMED'
      && state.mainnet_live_approved === false
      && state.order_submission_attempts === 0,
    );
  }

  async startApprovedLive(input: ApprovedLocalWorkerSecrets): Promise<void> {
    void input;
    throw new Error('LOCAL_PILOT_REQUIRED');
  }

  async startApprovedPilotLive(input: ApprovedLocalPilotSecrets): Promise<void> {
    if (!/^local-approval-[0-9a-f-]{36}$/i.test(input.approvalId)) throw new Error('LOCAL_APPROVAL_ID_INVALID');
    if (!/^pilot-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$/.test(input.campaignId)) throw new Error('LOCAL_PILOT_ID_INVALID');
    if (!/^[0-9a-f]{64}$/i.test(input.sourceFingerprint)
      || !/^[0-9a-f]{40,64}$/i.test(input.gitSha)
      || !/^[0-9a-f]{64}$/i.test(input.sourceHash)
      || !/^[0-9a-f]{64}$/i.test(input.dependencyHash)
      || !/^[0-9a-f]{64}$/i.test(input.migrationHash)
      || !/^[0-9a-f]{64}$/i.test(input.strategyHash)
      || !/^[0-9a-f]{64}$/i.test(input.riskPolicyHash)) throw new Error('LOCAL_PILOT_FINGERPRINT_INVALID');
    if (!Number.isFinite(Date.parse(input.expiresAt)) || Date.parse(input.expiresAt) <= Date.now()) {
      throw new Error('LOCAL_PILOT_APPROVAL_EXPIRED');
    }
    if (!input.apiKey.trim() || !input.apiSecret.trim()) throw new Error('LOCAL_MAINNET_SECRET_UNAVAILABLE');
    if (!/^[1-9][0-9]*$/.test(input.apiKeyVersion) || !/^[1-9][0-9]*$/.test(input.apiSecretVersion)) {
      throw new Error('LOCAL_RELEASE_SECRET_VERSIONS_INVALID');
    }
    await this.stopWorker();
    this.workerApprovalId = input.approvalId;
    this.workerPilotCampaignId = input.campaignId;
    this.workerSourceFingerprint = input.sourceFingerprint;
    this.workerApiKeyVersion = input.apiKeyVersion;
    this.workerApiSecretVersion = input.apiSecretVersion;
    await this.startWorker({
      mode: 'LIVE', approved: true,
      apiKey: input.apiKey, apiSecret: input.apiSecret,
      apiKeyVersion: input.apiKeyVersion, apiSecretVersion: input.apiSecretVersion,
      approvalId: input.approvalId, sourceFingerprint: input.sourceFingerprint,
      pilot: input,
    });
    try {
      await this.waitForState((state) =>
        state.execution_mode === 'LIVE'
        && state.engine_state === 'DISARMED'
        && state.mainnet_live_approved === true
        && state.pilot_campaign_id === input.campaignId
        && state.local_run_id === this.options.runId
        && state.local_source_fingerprint === input.sourceFingerprint
        && state.order_submission_attempts === 0,
      );
    } catch (error) {
      await this.startPaper();
      throw error;
    }
  }

  async stopWorker(): Promise<void> {
    const current = this.worker;
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = null;
    if (this.stateMonitorTimer) clearInterval(this.stateMonitorTimer);
    this.stateMonitorTimer = null;
    if (this.workerContainerName) {
      await this.stopDockerContainer(this.workerContainerName, current);
      if (current) this.clearWorkerState(current);
      else this.workerContainerName = null;
      return;
    }
    if (!current) {
      this.mode = 'STOPPED';
      return;
    }
    if (this.childIsRunning(current)) {
      try {
        current.kill('SIGTERM');
      } catch {
        // Continue to the bounded force-stop path and require exit read-back.
      }
      if (!(await this.waitForChildExit(current, 5_000))) {
        try {
          current.kill('SIGKILL');
        } catch {
          // The process is not considered stopped unless its exit is observed.
        }
        if (!(await this.waitForChildExit(current, 5_000))) {
          throw new Error('LOCAL_WORKER_SHUTDOWN_UNCONFIRMED');
        }
      }
    }
    this.clearWorkerState(current);
  }

  async close(): Promise<void> {
    await this.stopWorker();
    this.workerApprovalId = null;
    this.workerPilotCampaignId = null;
    this.workerSourceFingerprint = null;
    this.workerApiKeyVersion = null;
    this.workerApiSecretVersion = null;
  }

  private async startWorker(config: {
    mode: 'PAPER' | 'LIVE';
    approved: boolean;
    apiKey?: string;
    apiSecret?: string;
    apiKeyVersion?: string;
    apiSecretVersion?: string;
    approvalId?: string;
    sourceFingerprint?: string;
    pilot?: ApprovedLocalPilotSecrets;
  }): Promise<void> {
    if (this.workerRuntime === 'HOST_PYTHON') {
      this.pythonRuntime ||= this.pythonRuntimeResolver();
      this.pythonRuntime.assertUnchanged();
    }
    const env: NodeJS.ProcessEnv = {
      ...buildLocalWorkerParentEnvironment(this.environment),
      LOCAL_ONLY: 'true',
      LOCAL_RUNTIME_TARGET: 'LOCAL',
      LOCAL_RUN_ID: this.options.runId,
      LOCAL_SUPERVISOR_INSTANCE_ID: this.supervisorInstanceId,
      LOCAL_SOURCE_FINGERPRINT: config.sourceFingerprint || '',
      LOCAL_WORKER_AUTH_REQUIRED: 'true',
      WORKER_IDENTITY_TOKEN: this.options.workerIdentityToken,
      BIND_HOST: '127.0.0.1',
      PORT: '8000',
      EXECUTION_MODE: config.mode,
      MAINNET_LIVE_APPROVED: String(config.approved),
      MAINNET_RELEASE_APPROVAL_ID: config.approvalId || '',
      MAINNET_CONTINUATION_APPROVAL_ID: '',
      BINANCE_API_KEY: '',
      BINANCE_API_SECRET: '',
      BINANCE_TESTNET_API_KEY: '',
      BINANCE_TESTNET_API_SECRET: '',
      BINANCE_MAINNET_API_KEY: config.apiKey || '',
      BINANCE_MAINNET_API_SECRET: config.apiSecret || '',
      BINANCE_MAINNET_API_KEY_VERSION: config.apiKeyVersion || '',
      BINANCE_MAINNET_API_SECRET_VERSION: config.apiSecretVersion || '',
      PERSISTENCE_MODE: 'REQUIRED',
      EXECUTION_LEASE_REQUIRED: 'true',
      DATABASE_URL: '',
      LOCAL_LIVE_PILOT_CAMPAIGN_ID: config.pilot?.campaignId || '',
      LOCAL_LIVE_PILOT_GIT_SHA: config.pilot?.gitSha || '',
      LOCAL_LIVE_PILOT_SOURCE_HASH: config.pilot?.sourceHash || '',
      LOCAL_LIVE_PILOT_DEPENDENCY_HASH: config.pilot?.dependencyHash || '',
      LOCAL_LIVE_PILOT_MIGRATION_HASH: config.pilot?.migrationHash || '',
      LOCAL_LIVE_PILOT_STRATEGY_HASH: config.pilot?.strategyHash || '',
      LOCAL_LIVE_PILOT_RISK_POLICY_HASH: config.pilot?.riskPolicyHash || '',
      LOCAL_LIVE_PILOT_SECRET_PROJECT_ID: config.pilot?.secretProjectId || '',
      LOCAL_LIVE_PILOT_STRATEGY_ID: config.pilot?.strategyId || '',
      LOCAL_LIVE_PILOT_EXPIRES_AT: config.pilot?.expiresAt || '',
      LOCAL_LIVE_PILOT_MANAGEMENT_MODE: config.pilot ? 'QUICK' : '',
    };
    let child: ChildProcess;
    if (this.workerRuntime === 'DOCKER') {
      child = this.spawnDockerWorker(env, config);
    } else {
      const isolatedBootstrap = `import runpy,sys;sys.path.insert(0,${JSON.stringify(path.resolve(this.root))});runpy.run_module('apps.trading_worker.main',run_name='__main__')`;
      child = this.spawnProcess(this.pythonRuntime!.executable, ['-I', '-c', isolatedBootstrap], {
        cwd: this.root, env, stdio: 'ignore', windowsHide: true,
      });
    }
    this.worker = child;
    this.workerGeneration += 1;
    this.mode = config.mode;
    this.startSupervisorHeartbeat(child);
    this.startWorkerStateMonitor(child);
    child.once('exit', () => {
      this.clearWorkerState(child);
    });
    child.once('error', () => this.clearWorkerState(child));
  }

  private spawnDockerWorker(
    sourceEnvironment: NodeJS.ProcessEnv,
    config: {
      mode: 'PAPER' | 'LIVE';
      apiKey?: string;
      apiSecret?: string;
    },
  ): ChildProcess {
    if (!this.workerImageId) throw new Error('LOCAL_WORKER_IMAGE_ID_UNAVAILABLE');
    this.dockerRuntime ||= this.dockerRuntimeResolver(this.workerImageId);
    this.dockerRuntime.assertUnchanged();
    const token = String(sourceEnvironment.WORKER_IDENTITY_TOKEN || '');
    const databasePassword = String(sourceEnvironment.POSTGRES_PASSWORD || '');
    const databaseName = String(sourceEnvironment.POSTGRES_DB || '');
    const databaseUser = String(sourceEnvironment.POSTGRES_USER || '');
    if (!token || !databasePassword || !databaseName || !databaseUser) {
      throw new Error('LOCAL_DOCKER_WORKER_REQUIRED_SETTINGS_MISSING');
    }

    const env: NodeJS.ProcessEnv = { ...sourceEnvironment };
    for (const name of [
      'PATH', 'Path', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'USERPROFILE',
      'APPDATA', 'LOCALAPPDATA', 'POSTGRES_PASSWORD', 'WORKER_IDENTITY_TOKEN',
      'BINANCE_MAINNET_API_KEY', 'BINANCE_MAINNET_API_SECRET',
      'BINANCE_TESTNET_API_KEY', 'BINANCE_TESTNET_API_SECRET',
    ]) delete env[name];
    Object.assign(env, {
      LOCAL_ONLY: 'true',
      LOCAL_RUNTIME_TARGET: 'LOCAL',
      LOCAL_WORKER_CONTAINER: 'true',
      BIND_HOST: '0.0.0.0',
      PORT: '8000',
      POSTGRES_HOST: 'host.docker.internal',
      POSTGRES_PORT: '5433',
      POSTGRES_PASSWORD_FILE: '/run/secrets/postgres_password',
      WORKER_IDENTITY_TOKEN_FILE: '/run/secrets/worker_identity_token',
      BINANCE_MAINNET_API_KEY: '',
      BINANCE_MAINNET_API_SECRET: '',
      BINANCE_MAINNET_API_KEY_FILE: '/run/secrets/binance_mainnet_api_key',
      BINANCE_MAINNET_API_SECRET_FILE: '/run/secrets/binance_mainnet_api_secret',
    });
    const dockerClientEnv = buildLocalWorkerParentEnvironment(this.environment);
    delete dockerClientEnv.POSTGRES_PASSWORD;
    Object.assign(dockerClientEnv, env, { DOCKER_HOST: this.dockerRuntime.engineHost });
    const envNames = Object.keys(env).filter((name) => typeof env[name] === 'string');
    const containerName = `blessing-local-worker-${this.supervisorInstanceId.replaceAll('-', '')}-${this.workerGeneration + 1}`;
    const args = [
      'run', '--rm', '--init', '--interactive', '--name', containerName,
      '--publish', '127.0.0.1:8000:8000', '--restart=no', '--read-only',
      '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=32m',
      '--tmpfs', '/run/secrets:rw,noexec,nosuid,nodev,size=1m,uid=10001,gid=10001,mode=0700',
      '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user', '10001:10001',
    ];
    for (const name of envNames) args.push('--env', name);
    args.push('--entrypoint', 'python', this.dockerRuntime.imageId, '-m', 'apps.trading_worker.local_container_bootstrap');
    const child = this.spawnProcess(this.dockerRuntime.executable, args, {
      cwd: this.root,
      env: dockerClientEnv,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    });
    this.lastWorkerStderr = '';
    child.stderr?.on('data', (chunk) => {
      const text = chunk.toString();
      this.lastWorkerStderr = (this.lastWorkerStderr + text).slice(-4000);
      console.error('[Worker Container stderr]', text.trim());
    });
    child.stdout?.on('data', (chunk) => {
      console.log('[Worker Container stdout]', chunk.toString().trim());
    });
    this.workerContainerName = containerName;
    const payload = Buffer.from(JSON.stringify({
      apiKey: config.apiKey || '',
      apiSecret: config.apiSecret || '',
      workerIdentityToken: token,
      postgresPassword: databasePassword,
    }) + '\n', 'utf8');
    if (!child.stdin) {
      payload.fill(0);
      throw new Error('LOCAL_DOCKER_WORKER_SECRET_CHANNEL_UNAVAILABLE');
    }
    child.stdin.end(payload, () => payload.fill(0));
    return child;
  }

  private async stopDockerContainer(name: string, child: ChildProcess | null): Promise<void> {
    const docker = this.dockerRuntime;
    if (!docker || !/^blessing-local-worker-[a-f0-9]+-[0-9]+$/.test(name)) {
      throw new Error('LOCAL_WORKER_SHUTDOWN_UNCONFIRMED');
    }
    docker.assertUnchanged();
    const env = buildLocalWorkerParentEnvironment(this.environment);
    delete env.POSTGRES_PASSWORD;
    env.DOCKER_HOST = docker.engineHost;
    try {
      const running = this.dockerExecFileSync(docker.executable, [
        'ps', '--quiet', '--filter', `name=^/${name}$`,
      ], { cwd: this.root, env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 10_000 });
      if (running.trim()) {
        this.dockerExecFileSync(docker.executable, ['stop', '--time', '10', name], {
          cwd: this.root, env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 15_000,
        });
      }
      const remaining = this.dockerExecFileSync(docker.executable, [
        'ps', '--quiet', '--filter', `name=^/${name}$`,
      ], { cwd: this.root, env, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'], timeout: 10_000 });
      if (remaining.trim()) throw new Error('LOCAL_WORKER_SHUTDOWN_UNCONFIRMED');
    } catch {
      throw new Error('LOCAL_WORKER_SHUTDOWN_UNCONFIRMED');
    }
    if (child && this.childIsRunning(child)) {
      if (!(await this.waitForChildExit(child, 5_000))) {
        try { child.kill('SIGTERM'); } catch { /* inspect remains authoritative */ }
        if (!(await this.waitForChildExit(child, 5_000))) throw new Error('LOCAL_WORKER_SHUTDOWN_UNCONFIRMED');
      }
    }
  }

  private startSupervisorHeartbeat(child: ChildProcess): void {
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
    const send = async () => {
      if (this.worker !== child || !this.childIsRunning(child)) return;
      try {
        const body = JSON.stringify({ pilotReadinessVerdict: this.activePilotVerdict });
        await this.fetcher(`${this.options.workerUrl.replace(/\/+$/, '')}/supervisor/heartbeat`, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${this.options.workerIdentityToken}`,
            'Content-Type': 'application/json',
          },
          body,
          signal: AbortSignal.timeout(2_500),
        });
      } catch {
        // A failed heartbeat is intentionally not masked by local state; the
        // Worker watchdog disarms and exits after its fixed bounded TTL.
      }
    };
    void send();
    this.heartbeatTimer = setInterval(() => { void send(); }, 3_000);
  }

  private startWorkerStateMonitor(child: ChildProcess): void {
    if (this.stateMonitorTimer) clearInterval(this.stateMonitorTimer);
    const observe = async () => {
      if (this.stateMonitorInFlightFor === child || this.worker !== child || !this.childIsRunning(child)) return;
      this.stateMonitorInFlightFor = child;
      try {
        const response = await this.fetcher(
          `${this.options.workerUrl.replace(/\/+$/, '')}/state`,
          {
            headers: { Authorization: `Bearer ${this.options.workerIdentityToken}` },
            signal: AbortSignal.timeout(2_500),
          },
        );
        if (!response.ok) return;
        const state = await response.json() as Record<string, unknown>;
        if (!state || typeof state !== 'object' || Array.isArray(state) || this.worker !== child) return;
        if (state.local_run_id !== this.options.runId
          || state.local_supervisor_instance_id !== this.supervisorInstanceId
          || state.execution_mode !== this.mode
          || (this.workerSourceFingerprint !== null
            && state.local_source_fingerprint !== this.workerSourceFingerprint)
          || (this.mode === 'LIVE' && state.mainnet_live_approved !== true)
          || (this.mode === 'PAPER' && state.mainnet_live_approved !== false)
          || (this.workerPilotCampaignId !== null
            && state.pilot_campaign_id !== this.workerPilotCampaignId)
          || (this.workerPilotCampaignId === null && Boolean(state.pilot_campaign_id))) {
          this.lastStateObservedAt = new Date().toISOString();
          this.workerHeartbeatAt = null;
          this.pilotLifecycleMonitorStatus = 'UNKNOWN';
          return;
        }
        this.lastStateObservedAt = new Date().toISOString();
        const heartbeat = state.heartbeat_at ?? state.heartbeatAt;
        this.workerHeartbeatAt = typeof heartbeat === 'string' && Number.isFinite(Date.parse(heartbeat))
          ? heartbeat
          : null;
        const monitor = state.pilot_lifecycle_monitor && typeof state.pilot_lifecycle_monitor === 'object'
          && !Array.isArray(state.pilot_lifecycle_monitor)
          ? state.pilot_lifecycle_monitor as Record<string, unknown>
          : null;
        this.pilotLifecycleMonitorStatus = typeof monitor?.status === 'string'
          ? monitor.status
          : 'UNKNOWN';
      } catch {
        // A failed read remains UNKNOWN/STALE; it never refreshes worker liveness.
      } finally {
        if (this.stateMonitorInFlightFor === child) this.stateMonitorInFlightFor = null;
      }
    };
    void observe();
    this.stateMonitorTimer = setInterval(() => { void observe(); }, 2_500);
  }

  private async waitForState(predicate: (state: Record<string, unknown>) => boolean): Promise<void> {
    const deadline = Date.now() + 90_000;
    const headers = { Authorization: `Bearer ${this.options.workerIdentityToken}` };
    while (Date.now() < deadline) {
      if (!this.worker || !this.childIsRunning(this.worker)) {
        throw new Error(`LOCAL_WORKER_EXITED_BEFORE_READINESS: exitCode=${this.worker?.exitCode ?? 'none'} signalCode=${this.worker?.signalCode ?? 'none'} stderr=${this.lastWorkerStderr || 'none'}`);
      }
      try {
        const [readyResponse, stateResponse] = await Promise.all([
          this.fetcher(`${this.options.workerUrl.replace(/\/+$/, '')}/ready`, { headers, signal: AbortSignal.timeout(5_000) }),
          this.fetcher(`${this.options.workerUrl.replace(/\/+$/, '')}/state`, { headers, signal: AbortSignal.timeout(5_000) }),
        ]);
        if (readyResponse.ok && stateResponse.ok) {
          const ready = await readyResponse.json() as Record<string, unknown>;
          const state = await stateResponse.json() as Record<string, unknown>;
          if (ready.status === 'ready' && predicate(state)) return;
        }
      } catch {
        // Readiness may lag process creation. No worker response is trusted as approval.
      }
      await new Promise((resolve) => setTimeout(resolve, 1_000));
    }
    throw new Error('LOCAL_WORKER_READINESS_TIMEOUT');
  }

  private childIsRunning(child: ChildProcess): boolean {
    return child.exitCode === null && child.signalCode === null;
  }

  private waitForChildExit(child: ChildProcess, timeoutMs: number): Promise<boolean> {
    if (!this.childIsRunning(child)) return Promise.resolve(true);
    return new Promise((resolve) => {
      const finish = (exited: boolean) => {
        clearTimeout(timer);
        child.removeListener('exit', onExit);
        resolve(exited || !this.childIsRunning(child));
      };
      const onExit = () => finish(true);
      const timer = setTimeout(() => finish(false), timeoutMs);
      child.once('exit', onExit);
      if (!this.childIsRunning(child)) finish(true);
    });
  }

  private clearWorkerState(child: ChildProcess): void {
    if (this.worker !== child) return;
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = null;
    if (this.stateMonitorTimer) clearInterval(this.stateMonitorTimer);
    this.stateMonitorTimer = null;
    this.activePilotVerdict = null;
    this.worker = null;
    this.mode = 'STOPPED';
    this.workerApprovalId = null;
    this.workerPilotCampaignId = null;
    this.workerSourceFingerprint = null;
    this.workerApiKeyVersion = null;
    this.workerApiSecretVersion = null;
    this.lastStateObservedAt = null;
    this.workerHeartbeatAt = null;
    this.pilotLifecycleMonitorStatus = 'UNKNOWN';
  }
}
