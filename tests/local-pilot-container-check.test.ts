import { expect, it } from 'vitest';
import { pilotContainerCheckArgs, verifyPilotCheckContainer } from '../src/backend/local-pilot-container-check.js';
const options = {
  runId: '11111111-1111-4111-8111-111111111111',
  snapshotId: '22222222-2222-4222-8222-222222222222',
  sealSha256: 'b'.repeat(64),
  imageId: 'sha256:' + 'a'.repeat(64), check: 'BUILD' as const,
};
it('uses a fixed launcher and offline unprivileged container with no host bind mounts', () => {
  const args = pilotContainerCheckArgs(options);
  for (const flag of ['--pull=never', '--network=none', '--read-only', '--user=10001:10001',
    '--cap-drop=ALL', '--security-opt=no-new-privileges', '--env=MAINNET_LIVE_APPROVED=false']) {
    expect(args).toContain(flag);
  }
  expect(args.slice(-4)).toEqual([options.imageId, 'BUILD', '--seal-sha256', options.sealSha256]);
  expect(args.filter((arg) => arg.startsWith('type='))).toEqual([
    `type=volume,src=blessing-acceptance-source-${options.snapshotId},dst=/sealed,readonly,volume-nocopy`,
  ]);
  expect(args.join(' ')).not.toMatch(/docker\.sock|type=bind|--privileged|--publish|--env-file/);
});

it('checks effective Docker configuration and rejects altered boundaries', () => {
  const inspect = {
    Image: options.imageId, Name: `/blessing-acceptance-${options.runId}`, State: { Status: 'created' },
    Config: { User: '10001:10001', WorkingDir: '/work', Entrypoint: ['/usr/local/bin/blessing-acceptance-check'],
      Cmd: ['BUILD', '--seal-sha256', options.sealSha256], Env: ['HOME=/work/home', 'PATH=/usr/local/bin:/usr/bin:/bin', 'EXECUTION_MODE=PAPER',
        'MAINNET_LIVE_APPROVED=false', 'BLESSING_DISABLE_TEST_DOTENV=1', 'NPM_CONFIG_IGNORE_SCRIPTS=true'] },
    HostConfig: { NetworkMode: 'none', ReadonlyRootfs: true, Privileged: false, CapDrop: ['ALL'],
      SecurityOpt: ['no-new-privileges'], PidsLimit: 256, Memory: 4294967296, NanoCpus: 2000000000,
      Tmpfs: { '/work': 'rw,exec,nosuid,nodev,size=2147483648,uid=10001,gid=10001,mode=0700',
        '/tmp': 'rw,noexec,nosuid,nodev,size=67108864,uid=10001,gid=10001,mode=0700' } },
    Mounts: [{ Type: 'volume', RW: false, Name: `blessing-acceptance-source-${options.snapshotId}`, Destination: '/sealed' }],
  };
  expect(() => verifyPilotCheckContainer(inspect, options)).not.toThrow();
  const metadata = ['NODE_VERSION=24.21.0', 'YARN_VERSION=1.22.22'];
  const withMetadata = { ...inspect, Config: { ...inspect.Config, Env: [...inspect.Config.Env, ...metadata] } };
  expect(() => verifyPilotCheckContainer(withMetadata, options)).toThrow('ISOLATION_MISMATCH');
  expect(() => verifyPilotCheckContainer(withMetadata, { ...options, approvedImageEnvironment: metadata })).not.toThrow();
  expect(() => verifyPilotCheckContainer(withMetadata, { ...options, approvedImageEnvironment: ['NODE_VERSION=24.20.0', metadata[1]] }))
    .toThrow('ISOLATION_MISMATCH');
  for (const change of [{ NetworkMode: 'host' }, { Privileged: true }, { CapAdd: ['SYS_ADMIN'] },
    { Binds: ['C:/Users/Kan:/host'] }, { PortBindings: { '3000/tcp': [{}] } }, { IpcMode: 'host' }]) {
    expect(() => verifyPilotCheckContainer({ ...inspect, HostConfig: { ...inspect.HostConfig, ...change } }, options))
      .toThrow('ISOLATION_MISMATCH');
  }
  expect(() => verifyPilotCheckContainer({ ...inspect, Config: { ...inspect.Config, Env: [...inspect.Config.Env, 'TOKEN=canary'] } }, options))
    .toThrow('ISOLATION_MISMATCH');
  expect(() => verifyPilotCheckContainer({ ...inspect, Mounts: [{ ...inspect.Mounts[0], RW: true }] }, options))
    .toThrow('ISOLATION_MISMATCH');
});
it('rejects tags, arbitrary volume paths, and command injection before container creation', () => {
  for (const invalid of [{ imageId: 'node:latest' }, { snapshotId: 'C:/Users/Kan' },
    { sealSha256: '' }, { sealSha256: 'b'.repeat(64) + ';other' },
    { runId: options.runId + ',dst=/host' }, { check: 'BUILD; curl x' }]) {
    expect(() => pilotContainerCheckArgs({ ...options, ...invalid } as typeof options)).toThrow('INVALID');
  }
});

it('does not allow image policy metadata to carry execution overrides or credentials', () => {
  for (const metadata of [['NODE_OPTIONS=--require=/host'], ['TOKEN=canary'], ['NODE_VERSION=24.21.0', 'NODE_VERSION=24.21.0'],
    ['NODE_VERSION=24.21.0\nTOKEN=canary'], ['YARN_VERSION=latest']]) {
    expect(() => pilotContainerCheckArgs({ ...options, approvedImageEnvironment: metadata })).toThrow('INVALID');
  }
});
