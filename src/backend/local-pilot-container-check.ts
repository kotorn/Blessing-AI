import type { PilotOfflineCheck } from './local-pilot-acceptance-runner.js';

const DIGEST = /^sha256:[a-f0-9]{64}$/;
const UUID = /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/;

/** Construct checker arguments only. Image approval and sealed-volume provenance
 * must be verified by the trusted supervisor before executing these arguments.
 * Do not accept this object from an HTTP request or use it as readiness evidence.
 */
export function pilotContainerCheckArgs(options: {
  runId: string; imageId: string; snapshotId: string; sealSha256: string; check: PilotOfflineCheck;
  /** Exact non-secret metadata from the independently approved image policy. */
  approvedImageEnvironment?: readonly string[];
}): string[] {
  const { runId, imageId, snapshotId, check } = options;
  if (!UUID.test(runId) || !UUID.test(snapshotId) || !DIGEST.test(imageId)
    || !/^[a-f0-9]{64}$/.test(options.sealSha256)
    || !['TYPESCRIPT_TESTS', 'LINT', 'BUILD'].includes(check)) {
    throw new Error('LOCAL_PILOT_CHECK_CONTAINER_INVALID');
  }
  const metadata = options.approvedImageEnvironment ?? [];
  if (!Array.isArray(metadata) || metadata.length > 2
    || metadata.some((entry) => typeof entry !== 'string' || !/^(NODE_VERSION|YARN_VERSION)=\d+\.\d+\.\d+$/.test(entry))
    || new Set(metadata.map((entry) => entry.split('=')[0])).size !== metadata.length) {
    throw new Error('LOCAL_PILOT_CHECK_CONTAINER_INVALID');
  }
  return [
    'create', '--pull=never', '--name', `blessing-acceptance-${runId}`,
    '--network=none', '--read-only', '--user=10001:10001', '--cap-drop=ALL',
    '--security-opt=no-new-privileges', '--pids-limit=256', '--memory=4g', '--cpus=2',
    '--mount', `type=volume,src=blessing-acceptance-source-${snapshotId},dst=/sealed,readonly,volume-nocopy`,
    '--tmpfs', '/work:rw,exec,nosuid,nodev,size=2147483648,uid=10001,gid=10001,mode=0700',
    '--tmpfs', '/tmp:rw,noexec,nosuid,nodev,size=67108864,uid=10001,gid=10001,mode=0700',
    '--workdir=/work', '--env=HOME=/work/home', '--env=PATH=/usr/local/bin:/usr/bin:/bin',
    '--env=EXECUTION_MODE=PAPER', '--env=MAINNET_LIVE_APPROVED=false',
    '--env=BLESSING_DISABLE_TEST_DOTENV=1', '--env=NPM_CONFIG_IGNORE_SCRIPTS=true',
    '--entrypoint=/usr/local/bin/blessing-acceptance-check', imageId, check, '--seal-sha256', options.sealSha256,
  ];
}

/** Validate host-observed docker inspect data BEFORE starting the container. */
export function verifyPilotCheckContainer(value: unknown, options: Parameters<typeof pilotContainerCheckArgs>[0]): void {
  pilotContainerCheckArgs(options);
  const fail = () => { throw new Error('LOCAL_PILOT_CHECK_CONTAINER_ISOLATION_MISMATCH'); };
  if (!value || typeof value !== 'object') return fail();
  const item = value as Record<string, any>;
  const host = item.HostConfig;
  const config = item.Config;
  const env = [
    'HOME=/work/home', 'PATH=/usr/local/bin:/usr/bin:/bin', 'EXECUTION_MODE=PAPER',
    'MAINNET_LIVE_APPROVED=false', 'BLESSING_DISABLE_TEST_DOTENV=1', 'NPM_CONFIG_IGNORE_SCRIPTS=true',
    ...(options.approvedImageEnvironment ?? []),
  ];
  const same = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right);
  if (!host || !config || item.Image !== options.imageId || item.State?.Status !== 'created'
    || item.Name !== `/blessing-acceptance-${options.runId}` || config.User !== '10001:10001'
    || config.WorkingDir !== '/work' || !same(config.Entrypoint, ['/usr/local/bin/blessing-acceptance-check'])
    || !same(config.Cmd, [options.check, '--seal-sha256', options.sealSha256]) || !Array.isArray(config.Env)
    || !same([...config.Env].sort(), [...env].sort())
    || host.NetworkMode !== 'none' || host.ReadonlyRootfs !== true || host.Privileged !== false
    || !same(host.CapDrop, ['ALL']) || (host.CapAdd?.length || 0) !== 0
    || !same(host.SecurityOpt, ['no-new-privileges']) || host.PidsLimit !== 256
    || host.Memory !== 4294967296 || host.NanoCpus !== 2000000000
    || (host.Binds?.length || 0) !== 0 || (host.Devices?.length || 0) !== 0
    || Object.keys(host.PortBindings || {}).length !== 0
    || host.PidMode || host.IpcMode === 'host' || host.UTSMode || host.UsernsMode === 'host'
    || !Array.isArray(item.Mounts) || item.Mounts.length !== 1
    || item.Mounts[0].Type !== 'volume' || item.Mounts[0].RW !== false
    || item.Mounts[0].Name !== `blessing-acceptance-source-${options.snapshotId}`
    || item.Mounts[0].Destination !== '/sealed'
    || !same(host.Tmpfs, {
      '/work': 'rw,exec,nosuid,nodev,size=2147483648,uid=10001,gid=10001,mode=0700',
      '/tmp': 'rw,noexec,nosuid,nodev,size=67108864,uid=10001,gid=10001,mode=0700',
    })) return fail();
}
