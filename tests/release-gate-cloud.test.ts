import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const cloudGate = readFileSync(
  resolve(process.cwd(), 'infra/release_gate/cloud_gate.ps1'),
  'utf8',
);
const disarmedVerification = readFileSync(
  resolve(process.cwd(), 'infra/cloudrun/verify-disarmed.ps1'),
  'utf8',
);
const cloudGateAuth = readFileSync(
  resolve(process.cwd(), 'infra/cloudrun/verify-control-plane-auth.ps1'),
  'utf8',
);
const identityVerification = readFileSync(
  resolve(process.cwd(), 'infra/cloudrun/verify-release-identities.ps1'),
  'utf8',
);
const monitoringVerification = readFileSync(
  resolve(process.cwd(), 'infra/monitoring/verify-release-monitoring.ps1'),
  'utf8',
);
const budgetVerification = readFileSync(
  resolve(process.cwd(), 'infra/monitoring/verify-budget.ps1'),
  'utf8',
);
const controlPlaneDeployment = readFileSync(
  resolve(process.cwd(), 'infra/cloudrun/deploy-control-plane.ps1'),
  'utf8',
);
const controlPlaneBootstrap = readFileSync(
  resolve(process.cwd(), 'infra/cloudrun/deploy-control-plane-bootstrap.ps1'),
  'utf8',
);
const controlPlaneVerification = readFileSync(
  resolve(process.cwd(), 'infra/cloudrun/verify-control-plane.ps1'),
  'utf8',
);
const artifactAudit = readFileSync(
  resolve(process.cwd(), 'infra/artifact-registry/audit-images.ps1'),
  'utf8',
);
const protectedDigestVerification = readFileSync(
  resolve(process.cwd(), 'infra/artifact-registry/verify-protected-digests.ps1'),
  'utf8',
);
const cleanupPolicy = JSON.parse(readFileSync(
  resolve(process.cwd(), 'infra/artifact-registry/cleanup-policy.json'),
  'utf8',
)) as Array<Record<string, unknown>>;
const gitignore = readFileSync(resolve(process.cwd(), '.gitignore'), 'utf8');

describe('cloud release gate static assertions', () => {
  it('cloud_gate.ps1 references all three verify sub-scripts', () => {
    expect(cloudGate).toContain('verify-iam.ps1');
    expect(cloudGate).toContain('verify-disarmed.ps1');
    expect(cloudGate).toContain('verify-runtime-access.ps1');
    expect(cloudGate).toContain('verify-release-identities.ps1');
    expect(cloudGate).toContain('verify-control-plane-auth.ps1');
    expect(cloudGate).toContain('verify-release-monitoring.ps1');
    expect(cloudGate).toContain('verify-budget.ps1');
  });

  it('cloud read-back requires scoped IAM, monitoring, and budget evidence', () => {
    expect(cloudGate).toContain('$BillingAccount');
    expect(identityVerification).toContain('allUsers');
    expect(identityVerification).toContain('roles/secretmanager.secretAccessor');
    expect(identityVerification).toContain('roles/bigquery.dataEditor');
    expect(monitoringVerification).toContain('logging metrics list');
    expect(monitoringVerification).toContain('monitoring policies list');
    expect(budgetVerification).toContain('billing budgets list');
    expect(budgetVerification).toContain('alert only');
  });

  it('protected Control Plane probes require 401/403 and do not use valid tokens', () => {
    expect(cloudGateAuth).toContain('invalid-firebase-id-token');
    expect(cloudGateAuth).toContain('@(401, 403)');
    expect(cloudGateAuth).toContain('/internal/release/runtime');
    expect(cloudGateAuth).toContain('/internal/release/readiness');
    expect(cloudGateAuth).toContain('invalid-google-oidc-token');
    expect(cloudGateAuth).not.toContain('gcloud auth print-identity-token');
  });

  it('cloud_gate.ps1 has an ExpectedExecutionMode param defaulting to "PAPER"', () => {
    expect(cloudGate).toMatch(/\bExpectedExecutionMode\b/);
    // default value must be the string "PAPER"
    expect(cloudGate).toMatch(/ExpectedExecutionMode\s*=\s*["']PAPER["']/);
  });

  it('pins Control Plane runtime profiles to request-based CPU and bounded scaling', () => {
    for (const script of [cloudGate, controlPlaneDeployment, controlPlaneBootstrap, controlPlaneVerification]) {
      expect(script).toContain('DEV_PAPER_UI');
      expect(script).toContain('MAINNET_OPERATOR_UI');
      expect(script).toContain('RuntimeProfile');
    }
    expect(controlPlaneDeployment).toContain('"--cpu-throttling"');
    expect(controlPlaneDeployment).not.toContain('"--no-cpu-throttling"');
    expect(controlPlaneVerification).toContain('run.googleapis.com/cpu-throttling');
    expect(controlPlaneDeployment).toContain('run.googleapis.com/minScale');
    expect(controlPlaneDeployment).toContain('run.googleapis.com/maxScale');
    expect(controlPlaneVerification).toContain('run.googleapis.com/minScale');
    expect(controlPlaneVerification).toContain('run.googleapis.com/maxScale');
    expect(controlPlaneDeployment).toContain('latest Ready revision does not receive 100% traffic');
    expect(controlPlaneVerification).toContain('latest Ready revision does not receive 100% traffic');
  });

  it('cloud_gate.ps1 uses the cloud-gate- evidence file naming pattern', () => {
    expect(cloudGate).toContain('cloud-gate-');
  });

  it('cloud_gate.ps1 does not write $IdentityToken into the evidence object', () => {
    // The evidence object is built around the $evidence ordered hashtable.
    // Confirm $IdentityToken never appears as a property value in that block.
    // We split on the marker where evidence writing begins and check the
    // section from "Build evidence object" onwards.
    const evidenceSection = cloudGate.slice(
      cloudGate.indexOf('Build evidence object'),
    );
    expect(evidenceSection).not.toContain('$IdentityToken');
    expect(cloudGate).not.toContain('[string]$IdentityToken');
    expect(cloudGate).toContain('worker_image_digest');
    expect(cloudGate).toContain('control_plane_image_digest');
  });

  it('verify-disarmed.ps1 now has an ExpectedExecutionMode param defaulting to "PAPER"', () => {
    expect(disarmedVerification).toMatch(/\bExpectedExecutionMode\b/);
    expect(disarmedVerification).toMatch(/ExpectedExecutionMode\s*=\s*["']PAPER["']/);
  });

  it('verify-disarmed.ps1 no longer contains the hardcoded literal -ne "PAPER"', () => {
    expect(disarmedVerification).not.toContain('-ne "PAPER"');
  });

  it('.gitignore contains an entry for the evidence/ directory', () => {
    expect(gitignore).toContain('evidence/');
  });

  it('keeps Artifact Registry inventory and protected-digest checks read-only', () => {
    expect(artifactAudit).toContain('artifacts docker images list');
    expect(artifactAudit).toContain('destructive_action = $false');
    expect(artifactAudit).not.toMatch(/artifacts\s+docker\s+images\s+delete/i);
    expect(protectedDigestVerification).toContain('include-tags');
    expect(protectedDigestVerification).toContain('Protected image digests are missing');
    expect(protectedDigestVerification).not.toMatch(/gcloud\s+artifacts[\s\S]{0,120}\b(delete|remove)\b/i);
    expect(protectedDigestVerification).not.toContain('set-cleanup-policies');
    expect(cleanupPolicy.some((policy) => JSON.stringify(policy).includes('untagged'))).toBe(true);
    expect(cleanupPolicy.some((policy) => JSON.stringify(policy).includes('90d'))).toBe(true);
  });
});
