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
});
