import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { expect, it } from 'vitest';
import { createSealManifest, sha256, verifySealAt } from '../scripts/local-pilot-container-launcher.mjs';

it('detects a changed dependency in the accepted snapshot before execution', async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'pilot-seal-test-'));
  try {
    await mkdir(path.join(root, 'node_modules', 'locked-package'), { recursive: true });
    await mkdir(path.join(root, 'src'));
    await writeFile(path.join(root, 'package.json'), '{}');
    await writeFile(path.join(root, 'package-lock.json'), '{"lockfileVersion":3}');
    await writeFile(path.join(root, 'src', 'entry.ts'), 'export const value = 1;');
    await writeFile(path.join(root, 'node_modules', 'locked-package', 'index.js'), 'module.exports = 1;');
    const manifest = await createSealManifest(root);
    const sealBytes = Buffer.from(JSON.stringify(manifest));
    await writeFile(path.join(root, 'seal-manifest.json'), sealBytes);
    const sealHash = sha256(sealBytes);
    await expect(verifySealAt(root, sealHash)).resolves.toBe(true);
    await writeFile(path.join(root, 'node_modules', 'locked-package', 'index.js'), 'module.exports = 2;');
    await expect(verifySealAt(root, sealHash)).rejects.toThrow('LOCAL_PILOT_SEAL_INVALID');
    expect((await readFile(path.join(root, 'seal-manifest.json'))).equals(sealBytes)).toBe(true);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
