import { randomUUID, createHash } from 'node:crypto';

export type PilotOfflineCheck = 'TYPESCRIPT_TESTS' | 'LINT' | 'BUILD';
export interface PilotAcceptanceBinding {
  gitSha: string;
  sourceSha256: string;
  dependencySha256: string;
  migrationSha256: string;
  policySha256: string;
}
export interface PilotAcceptanceAudit {
  // Implementations must atomically reserve and consume a server-created run ID.
  begin(id: string, check: PilotOfflineCheck, binding: PilotAcceptanceBinding): Promise<void>;
  finish(id: string, result: {
    status: 'PASS' | 'FAIL'; observedAt: string; outputSha256: string; reason: string;
  }): Promise<void>;
}

export class PilotAcceptanceAcknowledgementError extends Error {
  constructor(readonly runId: string) {
    super('LOCAL_PILOT_ACCEPTANCE_ACKNOWLEDGEMENT_UNKNOWN');
    this.name = 'PilotAcceptanceAcknowledgementError';
  }
}

/** Internal server operation only. This receipt is not campaign/start authority. */
export async function runPilotOfflineAcceptance(options: {
  root: string; isolatedHome: string; check: PilotOfflineCheck;
  binding: PilotAcceptanceBinding; audit: PilotAcceptanceAudit;
  assertBindingUnchanged: () => void;
  /** Internal supervisor operation, never supplied by browser JSON. No host fallback. */
  executeIsolated?: (runId: string, check: PilotOfflineCheck, binding: PilotAcceptanceBinding) => Promise<{
    error: unknown; stdout: Buffer; stderr: Buffer;
  }>;
}): Promise<{ runId: string; status: 'PASS' | 'FAIL' }> {
  const scripts = { TYPESCRIPT_TESTS: 'test', LINT: 'lint', BUILD: 'build' } as const;
  if (!Object.hasOwn(scripts, options.check)) throw new Error('LOCAL_PILOT_CHECK_NOT_ALLOWED');
  if (!/^[a-f0-9]{40}$/.test(options.binding.gitSha)
    || ['sourceSha256', 'dependencySha256', 'migrationSha256', 'policySha256'].some(
      (key) => !/^[a-f0-9]{64}$/.test(options.binding[key as keyof PilotAcceptanceBinding] || ''),
    )) {
    throw new Error('LOCAL_PILOT_ACCEPTANCE_BINDING_INVALID');
  }
  options.assertBindingUnchanged();
  if (!options.executeIsolated) throw new Error('LOCAL_PILOT_ISOLATED_RUNNER_NOT_CONFIGURED');
  const runId = randomUUID();
  const binding = structuredClone(options.binding);
  try {
    await options.audit.begin(runId, options.check, binding);
  } catch {
    throw new PilotAcceptanceAcknowledgementError(runId);
  }
  let status: 'PASS' | 'FAIL' = 'FAIL';
  let reason = 'LOCAL_PILOT_CHECK_FAILED';
  let outputSha256 = createHash('sha256').update('').digest('hex');
  try {
    options.assertBindingUnchanged();
    const result = await options.executeIsolated(runId, options.check, structuredClone(binding));
    outputSha256 = createHash('sha256').update(Buffer.concat([
      result.stdout || Buffer.alloc(0), result.stderr || Buffer.alloc(0),
    ])).digest('hex');
    options.assertBindingUnchanged();
    if (!result.error) {
      status = 'PASS'; reason = 'LOCAL_PILOT_CHECK_COMPLETED';
    }
  } catch {
    reason = 'LOCAL_PILOT_CHECK_OR_BINDING_FAILED';
  }
  // A lost audit acknowledgement never returns PASS to the caller.
  try {
    await options.audit.finish(runId, { status, reason, outputSha256, observedAt: new Date().toISOString() });
  } catch {
    throw new PilotAcceptanceAcknowledgementError(runId);
  }
  return { runId, status };
}
