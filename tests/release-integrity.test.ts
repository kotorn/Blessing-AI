import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const dockerfile = readFileSync(resolve(process.cwd(), 'Dockerfile.worker'), 'utf8');
const deployScript = readFileSync(resolve(process.cwd(), 'infra/cloudrun/deploy.ps1'), 'utf8');
const workerManifest = readFileSync(resolve(process.cwd(), 'infra/cloudrun/worker.yaml'), 'utf8');
const cloudBuild = readFileSync(resolve(process.cwd(), 'cloudbuild.yaml'), 'utf8');
const controlPlaneDockerfile = readFileSync(resolve(process.cwd(), 'Dockerfile.control-plane'), 'utf8');
const controlPlaneCloudBuild = readFileSync(resolve(process.cwd(), 'cloudbuild-control-plane.yaml'), 'utf8');
const controlPlaneDeploy = readFileSync(resolve(process.cwd(), 'infra/cloudrun/deploy-control-plane.ps1'), 'utf8');
const releaseControllerCloudBuild = readFileSync(resolve(process.cwd(), 'cloudbuild-release-controller.yaml'), 'utf8');
const releaseController = readFileSync(resolve(process.cwd(), 'infra/cloudrun/release_controller.py'), 'utf8');
const candidateCreator = readFileSync(resolve(process.cwd(), 'infra/cloudrun/create-release-candidate.ps1'), 'utf8');
const disarmedVerification = readFileSync(resolve(process.cwd(), 'infra/cloudrun/verify-disarmed.ps1'), 'utf8');
const liveDisarmedVerification = readFileSync(resolve(process.cwd(), 'infra/cloudrun/verify-live-disarmed.ps1'), 'utf8');
const server = readFileSync(resolve(process.cwd(), 'server.ts'), 'utf8');

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

  it('verifies both LIVE-disarmed states without activating execution', () => {
    expect(liveDisarmedVerification).toContain('ExpectedMainnetLiveApproved = "false"');
    expect(liveDisarmedVerification).toContain('ExpectedMainnetLiveApproved $ExpectedMainnetLiveApproved');
    expect(liveDisarmedVerification).toContain('engineState -ne "DISARMED"');
    expect(liveDisarmedVerification).toContain('orderSubmissionAttempts');
    expect(liveDisarmedVerification).not.toContain('/arm');
  });

  it('builds the Control Plane without Binance or SQL secret material', () => {
    expect(controlPlaneDockerfile).toContain('npm ci');
    expect(controlPlaneDockerfile).toContain('COPY src/dataconnect-generated/ ./src/dataconnect-generated/');
    expect(controlPlaneDockerfile).toContain('CONTROL_PLANE_ONLY=true');
    expect(controlPlaneDockerfile).toContain('CONTROL_PLANE_AUTH_REQUIRED=true');
    expect(controlPlaneDockerfile).not.toMatch(/BINANCE_(?:MAINNET_)?API_(?:KEY|SECRET)/);
    expect(controlPlaneDockerfile).not.toContain('POSTGRES_PASSWORD');
    expect(controlPlaneCloudBuild).toContain('_IMAGE_TAG');
    expect(controlPlaneDeploy).toContain('@sha256:[0-9a-fA-F]{64}$');
    expect(controlPlaneDeploy).toContain('CONTROL_PLANE_ALLOWED_SERVICE_ACCOUNTS');
    expect(controlPlaneDeploy).not.toContain('BINANCE_MAINNET_API_SECRET');
  });

  it('runs the release controller only from an immutable attached-identity build', () => {
    expect(releaseControllerCloudBuild).toContain('_CLOUD_SDK_IMAGE');
    expect(releaseControllerCloudBuild).toContain('_RELEASE_CONTROLLER_SCRIPT_SHA256');
    expect(releaseControllerCloudBuild).toContain('_RELEASE_CONTROLLER_SERVICE_ACCOUNT');
    expect(releaseControllerCloudBuild).toContain('serviceAccount:');
    expect(releaseControllerCloudBuild).toContain('projects/${_PROJECT_ID}/serviceAccounts/${_RELEASE_CONTROLLER_SERVICE_ACCOUNT}');
    expect(releaseControllerCloudBuild).toContain('CONTROLLER_IMAGE=${_CLOUD_SDK_IMAGE}');
    expect(releaseControllerCloudBuild).toContain('python3 infra/cloudrun/release_controller.py');
    expect(releaseControllerCloudBuild).not.toContain('--impersonate-service-account');
    expect(releaseController).toContain('auth", "print-identity-token');
    expect(releaseController).toContain('auth",\n        "list"');
    expect(releaseController).toContain('RELEASE_CONTROLLER_SERVICE_ACCOUNT');
    expect(releaseController).toContain('active Cloud Build identity is not the dedicated Release Controller');
    expect(releaseController).toContain('MAINNET_LIVE_APPROVED=true');
    expect(releaseController).toContain('WORKER_REVISION={worker_revision}');
    expect(releaseController).toContain('expected_live_approved=False');
    expect(releaseController).toContain('expected_live_approved=True');
    expect(releaseController).toContain('containerConcurrency');
    expect(releaseController).toContain('cloudsql-instances');
    expect(releaseController).toContain('promoted Worker revision');
    expect(releaseController).toContain('MAINNET_LAUNCH_POLICY=STAGED_FIRST_ORDER');
    expect(releaseController).toContain('--no-allow-unauthenticated');
    expect(releaseController).toContain('/internal/release/readiness');
    expect(releaseController).toContain('engineState');
    expect(releaseController).toContain('DISARMED');
    expect(releaseController).not.toContain('--impersonate-service-account');
    expect(releaseController).not.toContain('place-order');
  });

  it('creates release candidates only through the fixed controller boundary', () => {
    expect(candidateCreator).toContain('/internal/release/candidate');
    expect(candidateCreator).toContain('auth print-identity-token');
    expect(candidateCreator).toContain('blessing-release-controller@');
    expect(candidateCreator).toContain('PENDING_APPROVAL');
    expect(candidateCreator).toContain('ETHUSDC');
    expect(candidateCreator).not.toContain('--impersonate-service-account');
    expect(candidateCreator).not.toContain('place-order');
    expect(candidateCreator).not.toContain('BINANCE_MAINNET_API_SECRET=');
  });

  it('requires a server-consumed approval before forwarding a LIVE ARM', () => {
    expect(server).toContain('getConsumedApproval(');
    expect(server).toContain('RELEASE_APPROVAL_NOT_CONSUMED');
    expect(server).toContain('RELEASE_APPROVAL_SCOPE_MISMATCH');
    expect(server).toContain('WORKER_NOT_LIVE_DISARMED');
    expect(server).toContain('requestedConfig.releaseApprovalId = consumedApproval.approvalId');
    expect(server).toContain("workerState.mainnet_live_approved !== true");
    expect(server).toContain("workerState.engine_state !== 'DISARMED'");
    expect(server).toContain("workerState.worker_image_digest");
    expect(server).toContain("workerState.worker_revision");
    expect(server).toContain('RELEASE_CANDIDATE_RUNTIME_MISMATCH');
  });
});
