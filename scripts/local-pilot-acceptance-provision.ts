import { execFileSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { copyFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { computeTrackCBinding, localLivePilotPolicySha256 } from '../src/backend/local-release-runtime.js';
import { parsePilotAcceptanceImagePolicy } from '../src/backend/local-pilot-acceptance-approval.js';
import { resolveTrustedLocalDockerEngine } from '../src/backend/local-docker-runtime.js';

const ROOT = process.cwd();
const POLICY_PATH = 'config/local_pilot_acceptance_policy.json';
const DOCKERFILE = 'infra/acceptance/Dockerfile';
const LAUNCHER = 'scripts/local-pilot-container-launcher.mjs';
const sha256 = (value: string | Buffer) => createHash('sha256').update(value).digest('hex');
const digestRef = (value: string) => /^.+@sha256:[a-f0-9]{64}$/.test(value);

function run(executable: string, args: string[], cwd = ROOT): string {
  return execFileSync(executable, args, {
    cwd, env: { PATH: process.env.PATH, SYSTEMROOT: process.env.SYSTEMROOT,
      TEMP: process.env.TEMP, TMP: process.env.TMP,
      DOCKER_HOST: 'npipe:////./pipe/dockerDesktopLinuxEngine' },
    encoding: 'utf8', shell: false, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    timeout: 30 * 60 * 1000, maxBuffer: 8 * 1024 * 1024,
  }).trim();
}

function assertCleanCommittedSource(): string {
  const sha = run('git', ['rev-parse', '--verify', 'HEAD']);
  if (!/^[a-f0-9]{40}$/.test(sha) || run('git', ['status', '--porcelain', '--untracked-files=all'])) {
    throw new Error('LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN');
  }
  return sha;
}

function assertPolicy(binding: ReturnType<typeof computeTrackCBinding>) {
  const value = JSON.parse(readFileSync(path.join(ROOT, POLICY_PATH), 'utf8')) as unknown;
  const policy = parsePilotAcceptanceImagePolicy(value);
  return { binding, policy };
}

function dockerEnv(engine: ReturnType<typeof resolveTrustedLocalDockerEngine>) {
  return { PATH: process.env.PATH, SYSTEMROOT: process.env.SYSTEMROOT,
    TEMP: process.env.TEMP, TMP: process.env.TMP, DOCKER_HOST: engine.engineHost };
}

function docker(engine: ReturnType<typeof resolveTrustedLocalDockerEngine>, args: string[]): string {
  engine.assertUnchanged();
  const result = run(engine.executable, args);
  engine.assertUnchanged();
  return result;
}

function assertCheckerImage(engine: ReturnType<typeof resolveTrustedLocalDockerEngine>, policy: {
  checker: { imageId: string; baseImageDigest: string; launcherSha256: string };
}): void {
  const image = JSON.parse(docker(engine, ['image', 'inspect', policy.checker.imageId, '--format', '{{json .}}'])) as {
    Id?: string; Config?: { Labels?: Record<string, string>; Env?: string[] };
  };
  const labels = image.Config?.Labels || {};
  if (image.Id !== policy.checker.imageId
    || labels['org.blessing.acceptance.role'] !== 'offline-checker'
    || labels['org.blessing.acceptance.base-image'] !== policy.checker.baseImageDigest
    || labels['org.blessing.acceptance.launcher-sha256'] !== policy.checker.launcherSha256
    || !Array.isArray(image.Config?.Env)
    || image.Config.Env.some((entry) => !/^PATH=.+$/.test(entry)
      && !/^(NODE_VERSION|YARN_VERSION)=\d+\.\d+\.\d+$/.test(entry))) {
    throw new Error('LOCAL_PILOT_ACCEPTANCE_IMAGE_ATTESTATION_MISMATCH');
  }
}

function checker(engine: ReturnType<typeof resolveTrustedLocalDockerEngine>, base: string): void {
  if (!digestRef(base)) throw new Error('ACCEPTANCE_BASE_MUST_BE_IMMUTABLE_DIGEST');
  const sha = assertCleanCommittedSource();
  engine.assertUnchanged();
  const temp = mkdtempSync(path.join(os.tmpdir(), 'blessing-acceptance-image-'));
  try {
    copyFileSync(path.join(ROOT, DOCKERFILE), path.join(temp, 'Dockerfile'));
    copyFileSync(path.join(ROOT, LAUNCHER), path.join(temp, 'local-pilot-container-launcher.mjs'));
    const launcherSha256 = sha256(readFileSync(path.join(temp, 'local-pilot-container-launcher.mjs')));
    const tag = `blessing-acceptance-checker-candidate:${randomUUID()}`;
    docker(engine, ['build', '--pull=false', '--network=none', '--tag', tag,
      '--build-arg', `ACCEPTANCE_BASE=${base}`, '--build-arg', `LAUNCHER_SHA256=${launcherSha256}`, temp]);
    const image = JSON.parse(docker(engine, ['image', 'inspect', tag, '--format', '{{json .}}'])) as {
      Id?: string; Config?: { Labels?: Record<string, string>; Env?: string[] };
    };
    if (!image.Id || !/^sha256:[a-f0-9]{64}$/.test(image.Id)
      || image.Config?.Labels?.['org.blessing.acceptance.role'] !== 'offline-checker'
      || image.Config?.Labels?.['org.blessing.acceptance.base-image'] !== base
      || image.Config?.Labels?.['org.blessing.acceptance.launcher-sha256'] !== launcherSha256
      || !Array.isArray(image.Config.Env)
      || image.Config.Env.some((entry) => !/^PATH=.+$/.test(entry)
        && !/^(NODE_VERSION|YARN_VERSION)=\d+\.\d+\.\d+$/.test(entry))) {
      throw new Error('LOCAL_PILOT_ACCEPTANCE_IMAGE_BUILD_ATTESTATION_INVALID');
    }
    if (assertCleanCommittedSource() !== sha) throw new Error('LOCAL_PILOT_ACCEPTANCE_SOURCE_CHANGED');
    process.stdout.write(`${JSON.stringify({ status: 'CANDIDATE_ONLY', gitSha: sha,
      checker: { imageId: image.Id, baseImageDigest: base, launcherSha256 } }, null, 2)}\n`);
  } finally {
    rmSync(temp, { recursive: true, force: true });
  }
}

function snapshot(engine: ReturnType<typeof resolveTrustedLocalDockerEngine>): void {
  const gitSha = assertCleanCommittedSource();
  const { binding, policy } = assertPolicy(computeTrackCBinding(ROOT));
  assertCheckerImage(engine, policy);
  const policySha256 = localLivePilotPolicySha256(ROOT);
  engine.assertUnchanged();
  const snapshotId = randomUUID();
  const stagingVolume = `blessing-acceptance-stage-${snapshotId}`;
  const volumeName = `blessing-acceptance-source-${snapshotId}`;
  const labels = {
    'org.blessing.acceptance.snapshot-id': snapshotId,
    'org.blessing.acceptance.git-sha': binding.gitSha,
    'org.blessing.acceptance.source-sha256': binding.sourceSha256,
    'org.blessing.acceptance.dependency-sha256': binding.dependencySha256,
  };
  const temp = mkdtempSync(path.join(os.tmpdir(), 'blessing-acceptance-source-'));
  let finalVolumeCreated = false;
  let completed = false;
  try {
    const archive = path.join(temp, 'source.tar');
    const archiveBytes = execFileSync('git', ['archive', '--format=tar', 'HEAD'], {
      cwd: ROOT, env: { PATH: process.env.PATH, PATHEXT: process.env.PATHEXT,
        SYSTEMROOT: process.env.SYSTEMROOT, TEMP: process.env.TEMP, TMP: process.env.TMP,
        GIT_CONFIG_NOSYSTEM: '1', GIT_NO_REPLACE_OBJECTS: '1', GIT_CONFIG_GLOBAL: 'NUL' },
      encoding: 'buffer', shell: false, windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 30_000, maxBuffer: 256 * 1024 * 1024,
    });
    writeFileSync(archive, archiveBytes, { flag: 'wx' });
    docker(engine, ['volume', 'create', '--name', stagingVolume]);
    const registry = 'https://registry.npmjs.org/';
    const build = "tar -xf /input/source.tar -C /snapshot && cd /snapshot && "
      + `npm ci --ignore-scripts --registry=${registry} --audit=false --fund=false && `
      + "chmod -R a-w /snapshot && node --input-type=module -e \"import {createSealManifest} from './scripts/local-pilot-container-launcher.mjs';import {writeFileSync} from 'node:fs';writeFileSync('/snapshot/seal-manifest.json',JSON.stringify(await createSealManifest('/snapshot')),{flag:'wx'})\"";
    const mountSource = temp.replaceAll('\\', '/');
    const environment = dockerEnv(engine);
    engine.assertUnchanged();
    execFileSync(engine.executable, ['run', '--rm', '--network', 'bridge', '--user', '0:0',
      '--env', 'HOME=/tmp/acceptance-provision-home', '--env', 'PATH=/usr/local/bin:/usr/bin:/bin',
      '--env', 'NPM_CONFIG_USERCONFIG=/dev/null', '--env', 'NPM_CONFIG_GLOBALCONFIG=/dev/null',
      '--env', 'NPM_CONFIG_IGNORE_SCRIPTS=true', '--env', 'NPM_CONFIG_REGISTRY=https://registry.npmjs.org/',
      '--env', 'NPM_CONFIG_CACHE=/tmp/acceptance-provision-home/.npm', '--env', 'NPM_CONFIG_AUDIT=false',
      '--env', 'NPM_CONFIG_FUND=false',
      '--mount', `type=bind,source=${mountSource},target=/input,readonly`,
      '--mount', `type=volume,source=${stagingVolume},target=/snapshot`,
      '--entrypoint', '/bin/sh', policy.checker.imageId, '-ceu', build], {
      cwd: ROOT, env: environment, encoding: 'utf8', shell: false, windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'], timeout: 30 * 60 * 1000, maxBuffer: 8 * 1024 * 1024,
    });
    engine.assertUnchanged();
    const manifestSha256 = docker(engine, ['run', '--rm', '--network', 'none',
      '--mount', `type=volume,source=${stagingVolume},target=/snapshot,readonly`,
      '--entrypoint', '/usr/local/bin/node', policy.checker.imageId, '-e',
      "process.stdout.write(require('crypto').createHash('sha256').update(require('fs').readFileSync('/snapshot/seal-manifest.json')).digest('hex'))"]);
    if (!/^[a-f0-9]{64}$/.test(manifestSha256)) throw new Error('LOCAL_PILOT_ACCEPTANCE_SEAL_BUILD_FAILED');
    const volumeLabels = { ...labels,
      'org.blessing.acceptance.manifest-sha256': manifestSha256,
      'org.blessing.acceptance.seal-sha256': manifestSha256 };
    const labelArgs = Object.entries(volumeLabels).flatMap(([key, value]) => ['--label', `${key}=${value}`]);
    docker(engine, ['volume', 'create', '--name', volumeName, ...labelArgs]);
    finalVolumeCreated = true;
    engine.assertUnchanged();
    execFileSync(engine.executable, ['run', '--rm', '--network', 'none', '--user', '0:0',
      '--mount', `type=volume,source=${stagingVolume},target=/stage,readonly`,
      '--mount', `type=volume,source=${volumeName},target=/snapshot`,
      '--entrypoint', '/bin/sh', policy.checker.imageId, '-ceu', 'cp -a /stage/. /snapshot/'], {
      cwd: ROOT, env: environment, encoding: 'utf8', shell: false, windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'], timeout: 30_000, maxBuffer: 8 * 1024 * 1024,
    });
    engine.assertUnchanged();
    const verified = docker(engine, ['run', '--rm', '--network', 'none', '--read-only',
      '--mount', `type=volume,source=${volumeName},target=/sealed,readonly`,
      '--entrypoint', '/usr/local/bin/blessing-acceptance-check', policy.checker.imageId,
      'verify-seal', '--seal-sha256', manifestSha256]);
    if (verified !== 'LOCAL_PILOT_SEAL_VERIFIED') throw new Error('LOCAL_PILOT_ACCEPTANCE_SEAL_VERIFY_FAILED');
    const observed = JSON.parse(docker(engine, ['volume', 'inspect', volumeName, '--format', '{{json .}}'])) as {
      Name?: string; Labels?: Record<string, string>;
    };
    if (observed.Name !== volumeName || Object.entries({ ...labels,
      'org.blessing.acceptance.manifest-sha256': manifestSha256,
      'org.blessing.acceptance.seal-sha256': manifestSha256 }).some(([key, value]) => observed.Labels?.[key] !== value)) {
      throw new Error('LOCAL_PILOT_ACCEPTANCE_VOLUME_ATTESTATION_INVALID');
    }
    if (assertCleanCommittedSource() !== gitSha) throw new Error('LOCAL_PILOT_ACCEPTANCE_SOURCE_CHANGED');
    completed = true;
    process.stdout.write(`${JSON.stringify({ status: 'CANDIDATE_ONLY', schemaVersion: 1,
      gitSha: binding.gitSha, sourceSha256: binding.sourceSha256,
      dependencySha256: binding.dependencySha256, migrationSha256: binding.migrationSha256,
      policySha256, snapshotId, volumeName, manifestSha256, sealSha256: manifestSha256,
      checkerImageId: policy.checker.imageId,
      checkerBaseImageDigest: policy.checker.baseImageDigest,
      checkerLauncherSha256: policy.checker.launcherSha256 }, null, 2)}\n`);
  } finally {
    try { docker(engine, ['volume', 'rm', '--force', stagingVolume]); } catch { /* staging cleanup is best-effort */ }
    if (!completed && finalVolumeCreated) {
      try { docker(engine, ['volume', 'rm', '--force', volumeName]); } catch { /* candidate was not emitted */ }
    }
    rmSync(temp, { recursive: true, force: true });
  }
}

const [command, ...args] = process.argv.slice(2);
try {
  const engine = resolveTrustedLocalDockerEngine();
  if (command === 'checker' && args.length === 1) checker(engine, args[0]);
  else if (command === 'snapshot' && args.length === 0) snapshot(engine);
  else throw new Error('USAGE: checker <immutable-base-image-ref> | snapshot');
} catch (error) {
  process.stderr.write(`${error instanceof Error ? error.message : 'LOCAL_PILOT_ACCEPTANCE_PROVISION_FAILED'}\n`);
  process.exitCode = 1;
}
