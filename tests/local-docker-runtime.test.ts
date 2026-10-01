import { describe, expect, it, vi } from 'vitest';
import type { execFileSync } from 'node:child_process';

import { resolveTrustedLocalDockerRuntime } from '../src/backend/local-docker-runtime';

describe('trusted Local Docker runtime', () => {
  const imageId = `sha256:${'a'.repeat(64)}`;

  it('pins the expected Docker Desktop engine and immutable image identity', () => {
    let currentImage = imageId;
    let currentName = 'docker-desktop';
    const run = vi.fn((
      _executable: string,
      args: readonly string[],
    ) => {
      if (args[0] === 'info' && args[2] === '{{.ServerVersion}}') return '29.6.2';
      if (args[0] === 'info' && args[2] === '{{.OSType}}') return 'linux';
      if (args[0] === 'info' && args[2] === '{{.Name}}') return currentName;
      if (args[0] === 'image' && args[1] === 'inspect') return currentImage;
      throw new Error('unexpected Docker invocation');
    }) as unknown as typeof execFileSync;
    const runtime = resolveTrustedLocalDockerRuntime({
      imageId,
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
  });

  it('rejects tags and non-immutable image references', () => {
    expect(() => resolveTrustedLocalDockerRuntime({
      imageId: 'blessing-worker:latest',
      dockerExecutable: 'trusted-test-docker',
      execFileSync: vi.fn() as unknown as typeof execFileSync,
      environment: {},
    })).toThrow('LOCAL_WORKER_IMAGE_ID_INVALID');
  });
});
