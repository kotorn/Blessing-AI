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
const gitignore = readFileSync(resolve(process.cwd(), '.gitignore'), 'utf8');

describe('cloud release gate static assertions', () => {
  it('cloud_gate.ps1 references all three verify sub-scripts', () => {
    expect(cloudGate).toContain('verify-iam.ps1');
    expect(cloudGate).toContain('verify-disarmed.ps1');
    expect(cloudGate).toContain('verify-runtime-access.ps1');
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
