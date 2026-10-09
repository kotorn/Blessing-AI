import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { executePilotContainer } from './local-pilot-container-executor.js';
import type { PilotAcceptanceBinding, PilotOfflineCheck } from './local-pilot-acceptance-runner.js';
import {
  LOCAL_PILOT_ACCEPTANCE_POLICY_PATH,
  parsePilotAcceptanceImagePolicy,
  readProtectedPilotAcceptanceReceipt,
  type ApprovedPilotAcceptanceReceipt,
} from './local-pilot-acceptance-approval.js';
import { resolveTrustedLocalDockerEngine, type TrustedLocalDockerEngine } from './local-docker-runtime.js';

const LABELS = {
  role: 'org.blessing.acceptance.role',
  base: 'org.blessing.acceptance.base-image',
  launcher: 'org.blessing.acceptance.launcher-sha256',
  snapshot: 'org.blessing.acceptance.snapshot-id',
  git: 'org.blessing.acceptance.git-sha',
  source: 'org.blessing.acceptance.source-sha256',
  dependencies: 'org.blessing.acceptance.dependency-sha256',
  manifest: 'org.blessing.acceptance.manifest-sha256',
  seal: 'org.blessing.acceptance.seal-sha256',
} as const;

function canonical(value: unknown): string {
  if (!value || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonical(record[key])}`).join(',')}}`;
}

export function createLocalPilotAcceptanceExecutor(options: {
  root: string;
  binding: PilotAcceptanceBinding;
  engine?: TrustedLocalDockerEngine;
  execFileSync?: typeof execFileSync;
}): (runId: string, check: PilotOfflineCheck, binding: PilotAcceptanceBinding) => Promise<{
  error: unknown; stdout: Buffer; stderr: Buffer;
}> {
  const engine = options.engine || resolveTrustedLocalDockerEngine();
  const run = options.execFileSync || execFileSync;
  const invoke = (args: string[]) => run(engine.executable, args, {
    cwd: options.root,
    env: { PATH: process.env.PATH, SYSTEMROOT: process.env.SYSTEMROOT, TEMP: process.env.TEMP,
      TMP: process.env.TMP, DOCKER_HOST: engine.engineHost },
    shell: false, windowsHide: true, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'],
    timeout: 20_000, maxBuffer: 1024 * 1024,
  }).trim();

  const readPolicy = () => parsePilotAcceptanceImagePolicy(JSON.parse(readFileSync(
    path.resolve(options.root, LOCAL_PILOT_ACCEPTANCE_POLICY_PATH), 'utf8')) as unknown);
  const verifyImage = (receipt: ApprovedPilotAcceptanceReceipt, policy: ReturnType<typeof readPolicy>) => {
    engine.assertUnchanged();
    const values = JSON.parse(invoke(['image', 'inspect', receipt.checkerImageId])) as unknown;
    if (!Array.isArray(values) || values.length !== 1 || !values[0] || typeof values[0] !== 'object') {
      throw new Error('LOCAL_PILOT_ACCEPTANCE_IMAGE_INSPECT_INVALID');
    }
    const image = values[0] as { Id?: unknown; Config?: { Labels?: unknown; Env?: unknown } };
    const labels = image.Config?.Labels;
    const expected = {
      [LABELS.role]: 'offline-checker', [LABELS.base]: policy.checker.baseImageDigest,
      [LABELS.launcher]: policy.checker.launcherSha256,
    };
    if (image.Id !== policy.checker.imageId || image.Id !== receipt.checkerImageId
      || !labels || typeof labels !== 'object' || Array.isArray(labels)
      || Object.entries(expected).some(([key, value]) => (labels as Record<string, unknown>)[key] !== value)) {
      throw new Error('LOCAL_PILOT_ACCEPTANCE_IMAGE_ATTESTATION_MISMATCH');
    }
    const rawEnvironment = image.Config?.Env ?? [];
    if (!Array.isArray(rawEnvironment) || rawEnvironment.some((entry) => typeof entry !== 'string')) {
      throw new Error('LOCAL_PILOT_ACCEPTANCE_IMAGE_ENVIRONMENT_INVALID');
    }
    const approvedEnvironment = rawEnvironment.filter((entry): entry is string => {
      if (/^PATH=.+$/.test(entry)) return false; // Runtime always replaces PATH with its fixed allowlist.
      const match = /^(NODE_VERSION|YARN_VERSION)=(\d+\.\d+\.\d+)$/.exec(entry);
      if (!match) throw new Error('LOCAL_PILOT_ACCEPTANCE_IMAGE_ENVIRONMENT_INVALID');
      return true;
    });
    if (new Set(approvedEnvironment.map((entry) => entry.split('=')[0])).size !== approvedEnvironment.length) {
      throw new Error('LOCAL_PILOT_ACCEPTANCE_IMAGE_ENVIRONMENT_INVALID');
    }
    return approvedEnvironment;
  };
  const verifyVolume = (receipt: ApprovedPilotAcceptanceReceipt) => {
    engine.assertUnchanged();
    const values = JSON.parse(invoke(['volume', 'inspect', receipt.volumeName])) as unknown;
    if (!Array.isArray(values) || values.length !== 1 || !values[0] || typeof values[0] !== 'object') {
      throw new Error('LOCAL_PILOT_ACCEPTANCE_SNAPSHOT_UNAVAILABLE');
    }
    const volume = values[0] as { Name?: unknown; Labels?: unknown };
    const labels = volume.Labels;
    const expected = {
      [LABELS.snapshot]: receipt.snapshotId,
      [LABELS.git]: receipt.gitSha,
      [LABELS.source]: receipt.sourceSha256,
      [LABELS.dependencies]: receipt.dependencySha256,
      [LABELS.manifest]: receipt.manifestSha256,
      [LABELS.seal]: receipt.sealSha256,
    };
    if (volume.Name !== receipt.volumeName || !labels || typeof labels !== 'object' || Array.isArray(labels)
      || Object.entries(expected).some(([key, value]) => (labels as Record<string, unknown>)[key] !== value)) {
      throw new Error('LOCAL_PILOT_ACCEPTANCE_SNAPSHOT_ATTESTATION_MISMATCH');
    }
    const sealed = invoke(['run', '--rm', '--network', 'none', '--read-only',
      '--mount', `type=volume,source=${receipt.volumeName},target=/sealed,readonly`,
      '--entrypoint', '/usr/local/bin/blessing-acceptance-check', receipt.checkerImageId,
      'verify-seal', '--seal-sha256', receipt.sealSha256]);
    if (sealed !== 'LOCAL_PILOT_SEAL_VERIFIED') {
      throw new Error('LOCAL_PILOT_ACCEPTANCE_SNAPSHOT_SEAL_INVALID');
    }
  };
  const readReceipt = (policy: ReturnType<typeof readPolicy>) =>
    readProtectedPilotAcceptanceReceipt(options.binding, policy);

  return async (runId, check, binding) => {
    if (canonical(binding) !== canonical(options.binding)) {
      throw new Error('LOCAL_PILOT_ACCEPTANCE_BINDING_CHANGED');
    }
    const policy = readPolicy();
    const approved = readReceipt(policy);
    const approvedImageEnvironment = verifyImage(approved, policy);
    verifyVolume(approved);
    const assertApprovedSnapshot = async () => {
      engine.assertUnchanged();
      const currentPolicy = readPolicy();
      const currentReceipt = readReceipt(currentPolicy);
      if (canonical(currentPolicy) !== canonical(policy) || canonical(currentReceipt) !== canonical(approved)) {
        throw new Error('LOCAL_PILOT_ACCEPTANCE_APPROVAL_CHANGED');
      }
      if (canonical(verifyImage(currentReceipt, currentPolicy)) !== canonical(approvedImageEnvironment)) {
        throw new Error('LOCAL_PILOT_ACCEPTANCE_IMAGE_ENVIRONMENT_CHANGED');
      }
      verifyVolume(currentReceipt);
    };
    return executePilotContainer({
      dockerExecutable: engine.executable,
      dockerHost: engine.engineHost,
      configuration: {
        runId, check, imageId: approved.checkerImageId, snapshotId: approved.snapshotId,
        sealSha256: approved.sealSha256,
        approvedImageEnvironment,
      },
      assertApprovedSnapshot,
    });
  };
}
