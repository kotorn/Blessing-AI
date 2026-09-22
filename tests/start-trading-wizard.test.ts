import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const wizard = readFileSync(resolve(process.cwd(), 'src/components/StartTradingWizard.tsx'), 'utf8');

describe('Start Trading wizard readiness UI', () => {
  it('binds the ARM control to both server and local readiness', () => {
    expect(wizard).toContain(
      "const canArm = Boolean(preflightResult?.canArm) && localReadinessChecks.every((check) => !check.required || check.status === 'PASS');",
    );
    expect(wizard).toContain('if (!canArm) return;');
    expect(wizard).toContain('disabled={loading || !canArm}');
  });
});
