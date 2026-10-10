import { describe, expect, it, vi } from 'vitest';
import type { execFileSync } from 'node:child_process';

import { resolveTrustedLocalDockerEngine, resolveTrustedLocalDockerRuntime } from '../src/backend/local-docker-runtime';

describe('trusted Local Docker runtime', () => {
  const imageId = `sha256:${'a'.repeat(64)}`;
  const expectedLabels = { 'org.blessing.git.sha': '1'.repeat(40),
    'org.blessing.source.sha256': '2'.repeat(64) };

  it('provides a verified Docker engine transport for server-owned acceptance containers', () => {
    let engineName = 'docker-desktop';
    const run = vi.fn((_executable: string, args: readonly string[]) => {
      if (args[0] === 'info' && args[2] === '{{.ServerVersion}}') return '29.6.2';
      if (args[0] === 'info' && args[2] === '{{.OSType}}') return 'linux';
      if (args[0] === 'info' && args[2] === '{{.Name}}') return engineName;
      throw new Error('unexpected Docker invocation');
    }) as unknown as typeof execFileSync;
    const engine = resolveTrustedLocalDockerEngine({ dockerExecutable: 'trusted-test-docker',
      execFileSync: run, environment: {} });
    expect(engine.engineHost).toBe('npipe:////./pipe/dockerDesktopLinuxEngine');
    expect(() => engine.assertUnchanged()).not.toThrow();
    engineName = 'other-engine';
    expect(() => engine.assertUnchanged()).toThrow('LOCAL_DOCKER_ENGINE_IDENTITY_CHANGED');
  });

  it('pins the expected Docker Desktop engine and immutable image identity', () => {
    let currentImage = imageId;
    let currentLabels = { ...expectedLabels };
    let currentName = 'docker-desktop';
    const run = vi.fn((
      _executable: string,
      args: readonly string[],
    ) => {
      if (args[0] === 'info' && args[2] === '{{.ServerVersion}}') return '29.6.2';
      if (args[0] === 'info' && args[2] === '{{.OSType}}') return 'linux';
      if (args[0] === 'info' && args[2] === '{{.Name}}') return currentName;
      if (args[0] === 'image' && args[1] === 'inspect') {
        return JSON.stringify([{ Id: currentImage, Config: { Labels: currentLabels } }]);
      }
      throw new Error('unexpected Docker invocation');
    }) as unknown as typeof execFileSync;
    const runtime = resolveTrustedLocalDockerRuntime({
      imageId,
      expectedLabels,
      dockerExecutable: 'trusted-test-docker',
      execFileSync: run,
      environment: {},
    });

    expect(runtime.imageId).toBe(imageId);
    expect(runtime.serverVersion).toBe('29.6.2');
    expect(runtime.engineHost).toBe('npipe:////./pipe/dockerDesktopLinuxEngine');
    expect(() => runtime.assertUnchanged()).not.toThrow();
    currentName = 'another-engine';
    expect(() => runtime.assertUnchanged()).toThrow('LOCAL_DOCKER_ENGINE_IDENTITY_CHANGED');
    currentName = 'docker-desktop';
    currentImage = `sha256:${'b'.repeat(64)}`;
    expect(() => runtime.assertUnchanged()).toThrow('LOCAL_WORKER_IMAGE_CHANGED');
    currentImage = imageId;
    currentLabels = { ...expectedLabels, 'org.blessing.source.sha256': '3'.repeat(64) };
    expect(() => runtime.assertUnchanged()).toThrow('LOCAL_WORKER_IMAGE_ATTESTATION_CHANGED');
  });

  it('rejects an image whose pinned ID has mismatched source-attestation labels', () => {
    const run = vi.fn((_executable: string, args: readonly string[]) => {
      if (args[0] === 'info' && args[2] === '{{.ServerVersion}}') return '29.6.2';
      if (args[0] === 'info' && args[2] === '{{.OSType}}') return 'linux';
      if (args[0] === 'info' && args[2] === '{{.Name}}') return 'docker-desktop';
      if (args[0] === 'image' && args[1] === 'inspect') {
        return JSON.stringify([{ Id: imageId, Config: { Labels: { ...expectedLabels,
          'org.blessing.git.sha': '4'.repeat(40) } } }]);
      }
      throw new Error('unexpected Docker invocation');
    }) as unknown as typeof execFileSync;
    expect(() => resolveTrustedLocalDockerRuntime({ imageId, expectedLabels,
      dockerExecutable: 'trusted-test-docker', execFileSync: run, environment: {},
    })).toThrow('LOCAL_DOCKER_RUNTIME_NOT_VERIFIED');
  });

  it('rejects tags and non-immutable image references', () => {
    expect(() => resolveTrustedLocalDockerRuntime({
      imageId: 'blessing-worker:latest',
      expectedLabels,
      dockerExecutable: 'trusted-test-docker',
      execFileSync: vi.fn() as unknown as typeof execFileSync,
      environment: {},
    })).toThrow('LOCAL_WORKER_IMAGE_ID_INVALID');
  });
});
