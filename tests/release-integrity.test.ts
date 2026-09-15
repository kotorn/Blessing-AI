import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const dockerfile = readFileSync(resolve(process.cwd(), 'Dockerfile.worker'), 'utf8');
const deployScript = readFileSync(resolve(process.cwd(), 'infra/cloudrun/deploy.ps1'), 'utf8');
const workerManifest = readFileSync(resolve(process.cwd(), 'infra/cloudrun/worker.yaml'), 'utf8');
const cloudBuild = readFileSync(resolve(process.cwd(), 'cloudbuild.yaml'), 'utf8');
const disarmedVerification = readFileSync(resolve(process.cwd(), 'infra/cloudrun/verify-disarmed.ps1'), 'utf8');

describe('release integrity contract', () => {
  it('verifies the pinned Binance CLI archive before extraction', () => {
    expect(dockerfile).toContain('ARG BINANCE_CLI_VERSION=2.1.1');
    expect(dockerfile).toContain('ARG BINANCE_CLI_SHA256=6b836a24f281abf590988207b0d19d4933971ca66dcf45a9e255cdc237c4deee');
    expect(dockerfile).toContain('sha256sum --check --strict -');
    expect(dockerfile).toContain('curl --fail --silent --show-error --location');
    expect(dockerfile).not.toMatch(/curl[^\n]*\|\s*tar/);
  });

  it('requires immutable images and explicit secret versions for Cloud Run', () => {
    expect(deployScript).toContain("@sha256:[0-9a-fA-F]{64}$");
    expect(deployScript).toContain('CloudSqlPasswordVersion');
    expect(deployScript).toContain('BinanceMainnetApiKeyVersion');
    expect(deployScript).toContain('BinanceMainnetApiSecretVersion');
    expect(deployScript).not.toContain(':latest');
    expect(workerManifest).not.toContain('key: latest');
    expect(workerManifest).toContain('key: "1"');
    expect(workerManifest).toContain('REPLACE_WITH_IMMUTABLE_IMAGE_DIGEST');
    expect(workerManifest).toContain('value: "false"');
  });

  it('keeps build tags out of deployment selection', () => {
    expect(cloudBuild).toContain('_IMAGE_TAG');
    expect(cloudBuild).toContain('disarmed-$BUILD_ID');
    expect(cloudBuild).not.toContain('v0.2-disarmed');
  });

  it('provides disarmed revision read-back for digest, secrets, and launch flags', () => {
    expect(disarmedVerification).toContain('latestReadyRevisionName');
    expect(disarmedVerification).toContain('@sha256:[0-9a-fA-F]{64}$');
    expect(disarmedVerification).toContain('EXECUTION_MODE');
    expect(disarmedVerification).toContain('PERSISTENCE_MODE');
    expect(disarmedVerification).toContain('MAINNET_LIVE_APPROVED');
    expect(disarmedVerification).toContain('numeric Secret Manager version');
  });
});
