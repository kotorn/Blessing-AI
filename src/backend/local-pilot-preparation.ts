import crypto from 'node:crypto';

/** Evidence for a single, supervised LIVE/DISARMED Worker process. */
export interface PreparedLocalPilot {
  campaignId: string;
  runId: string;
  sourceFingerprint: string;
  approvalId: string;
  workerGeneration: number;
  supervisorInstanceId: string;
  preflightEvidence: {
    observedAt: string;
    executionMode: string;
    engineState: string;
    mainnetLiveApproved: boolean;
    preflightOnly: boolean;
    preflightPassed: boolean;
    canArm: boolean;
    orderSubmissionAttempts: number;
    orderEndpointAttempts: number;
    checks: Array<{ id: string; status: string; required: boolean }>;
  };
  preflightSha256: string;
  preflightObservedAt: string;
  preparedAt: string;
}

export const LOCAL_PILOT_PREFLIGHT_MAX_AGE_MS = 120_000;

const REQUIRED_PREFLIGHT_CHECKS = [
  'CHK-PREFLIGHT-LOCAL-RISK-LIFECYCLE',
  'CHK-PREFLIGHT-PILOT-MONITOR',
  'CHK-PREFLIGHT-PILOT-FLAT-ACCOUNT',
  'CHK-PREFLIGHT-CREDENTIALS',
  'CHK-PREFLIGHT-PERSISTENCE',
  'CHK-PREFLIGHT-DURABLE-LEDGER',
  'CHK-PREFLIGHT-KILL-SWITCH',
  'CHK-PREFLIGHT-CONNECTION',
  'CHK-PREFLIGHT-AUTH',
  'CHK-PREFLIGHT-CAN-TRADE',
  'CHK-PREFLIGHT-POSITION-MODE',
  'CHK-PREFLIGHT-RULES',
  'CHK-PREFLIGHT-RECONCILIATION',
  'CHK-PREFLIGHT-PRIVATE-STREAM',
  'CHK-PREFLIGHT-ACCOUNT-RISK',
  'CHK-PREFLIGHT-MARKET',
  'CHK-PREFLIGHT-WORKER-STATE',
  'CHK-PREFLIGHT-NO-ORDER-ENDPOINT',
  'CHK-PREFLIGHT-NO-ORDER-SUBMISSION',
] as const;

export function attestPreparedLocalPilot(input: {
  campaignId: string;
  runId: string;
  sourceFingerprint: string;
  approvalId: string;
  workerGeneration: number | null;
  supervisorInstanceId: string;
  workerState: Record<string, unknown>;
  preflight: Record<string, unknown>;
  now?: Date;
}): PreparedLocalPilot {
  const now = input.now || new Date();
  const state = input.workerState;
  const preflight = input.preflight;
  const monitor = state.pilot_lifecycle_monitor;
  const monitorSuccessAt = monitor && typeof monitor === 'object'
    ? Date.parse(String((monitor as Record<string, unknown>).last_success_at || ''))
    : NaN;
  const monitorStatus = monitor && typeof monitor === 'object'
    ? (monitor as Record<string, unknown>).status
    : undefined;
  const checksForMonitor = Array.isArray(preflight.checks)
    ? preflight.checks.filter((check): check is Record<string, unknown> =>
      Boolean(check && typeof check === 'object' && !Array.isArray(check)))
    : [];
  const monitorCheckPassed = checksForMonitor.some((check) =>
    check.id === 'CHK-PREFLIGHT-PILOT-MONITOR' && check.required === true && check.status === 'PASS');
  const flatAccountCheckPassed = checksForMonitor.some((check) =>
    check.id === 'CHK-PREFLIGHT-PILOT-FLAT-ACCOUNT' && check.required === true && check.status === 'PASS');
  const monitorFresh = monitorStatus === 'HEALTHY'
    && Number.isFinite(monitorSuccessAt)
    && monitorSuccessAt <= now.getTime() + 2_000
    && now.getTime() - monitorSuccessAt <= 15_000;
  const monitorCorrectlyDeferredWhileDisarmed = monitorStatus === 'NOT_RUN'
    && state.engine_state === 'DISARMED'
    && monitorCheckPassed
    && flatAccountCheckPassed;
  if (!monitorFresh && !monitorCorrectlyDeferredWhileDisarmed) {
    throw new Error('LOCAL_PILOT_LIFECYCLE_MONITOR_UNHEALTHY');
  }
  if (!Number.isInteger(input.workerGeneration) || (input.workerGeneration || 0) < 1
    || state.execution_mode !== 'LIVE'
    || state.engine_state !== 'DISARMED'
    || state.mainnet_live_approved !== true
    || state.pilot_campaign_id !== input.campaignId
    || state.local_run_id !== input.runId
    || state.local_source_fingerprint !== input.sourceFingerprint
    || state.local_supervisor_instance_id !== input.supervisorInstanceId
    || state.order_submission_attempts !== 0
    || state.kill_switch_active !== false) {
    throw new Error('LOCAL_PILOT_PREPARED_WORKER_IDENTITY_UNVERIFIED');
  }
  if (preflight.executionMode !== 'LIVE'
    || preflight.engineState !== 'DISARMED'
    || preflight.mainnetLiveApproved !== true
    || preflight.preflightOnly !== true
    || preflight.preflightPassed !== true
    || preflight.canArm !== false
    || preflight.orderSubmissionAttempts !== 0
    || preflight.orderEndpointAttempts !== 0) {
    throw new Error('LOCAL_PILOT_SIGNED_PREFLIGHT_FAILED');
  }
  const checks = preflight.checks;
  if (!Array.isArray(checks)) throw new Error('LOCAL_PILOT_SIGNED_PREFLIGHT_INCOMPLETE');
  const byId = new Map<string, string>();
  for (const raw of checks) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      throw new Error('LOCAL_PILOT_SIGNED_PREFLIGHT_INCOMPLETE');
    }
    const check = raw as Record<string, unknown>;
    if (typeof check.id !== 'string' || byId.has(check.id)) {
      throw new Error('LOCAL_PILOT_SIGNED_PREFLIGHT_INCOMPLETE');
    }
    byId.set(check.id, String(check.status || 'NOT_RUN'));
    if (check.required === true && check.status !== 'PASS') {
      throw new Error('LOCAL_PILOT_SIGNED_PREFLIGHT_FAILED');
    }
  }
  if (REQUIRED_PREFLIGHT_CHECKS.some((id) => byId.get(id) !== 'PASS')) {
    throw new Error('LOCAL_PILOT_SIGNED_PREFLIGHT_INCOMPLETE');
  }
  const observedAt = typeof preflight.observedAt === 'string' ? Date.parse(preflight.observedAt) : NaN;
  if (!Number.isFinite(observedAt) || !Number.isFinite(now.getTime())
    || observedAt > now.getTime() + 2_000
    || now.getTime() - observedAt > LOCAL_PILOT_PREFLIGHT_MAX_AGE_MS) {
    throw new Error('LOCAL_PILOT_SIGNED_PREFLIGHT_STALE');
  }
  const preflightEvidence: PreparedLocalPilot['preflightEvidence'] = {
    observedAt: new Date(observedAt).toISOString(),
    executionMode: preflight.executionMode,
    engineState: preflight.engineState,
    mainnetLiveApproved: preflight.mainnetLiveApproved,
    preflightOnly: preflight.preflightOnly,
    preflightPassed: preflight.preflightPassed,
    canArm: preflight.canArm,
    orderSubmissionAttempts: preflight.orderSubmissionAttempts,
    orderEndpointAttempts: preflight.orderEndpointAttempts,
    checks: checks.map((check) => check as Record<string, unknown>)
      .map((check) => ({ id: String(check.id), status: String(check.status || 'NOT_RUN'), required: check.required === true }))
      .sort((a, b) => String(a.id).localeCompare(String(b.id))),
  };
  return {
    campaignId: input.campaignId,
    runId: input.runId,
    sourceFingerprint: input.sourceFingerprint,
    approvalId: input.approvalId,
    workerGeneration: input.workerGeneration as number,
    supervisorInstanceId: input.supervisorInstanceId,
    preflightEvidence,
    preflightSha256: preparedLocalPilotEvidenceHash(preflightEvidence),
    preflightObservedAt: new Date(observedAt).toISOString(),
    preparedAt: now.toISOString(),
  };
}

export function preparedLocalPilotMatches(
  prepared: PreparedLocalPilot | null,
  expected: Omit<PreparedLocalPilot, 'preflightObservedAt' | 'preparedAt' | 'preflightSha256' | 'preflightEvidence'>,
  now = new Date(),
  options: { requireFresh?: boolean } = {},
): boolean {
  // requireFresh=false keeps every identity/evidence check but not the 120 s age
  // bound. Only the start route uses it, because it refreshes the read-only
  // preflight (and fails closed if that fails) before it arms. Without it, a human
  // pause longer than 120 s after Prepare dead-ends the campaign: Prepare requires an
  // unused PAPER worker, but the worker is already LIVE/DISARMED.
  const requireFresh = options.requireFresh !== false;
  return prepared !== null
    && prepared.campaignId === expected.campaignId
    && prepared.runId === expected.runId
    && prepared.sourceFingerprint === expected.sourceFingerprint
    && prepared.approvalId === expected.approvalId
    && prepared.workerGeneration === expected.workerGeneration
    && prepared.supervisorInstanceId === expected.supervisorInstanceId
    && prepared.preflightSha256 === preparedLocalPilotEvidenceHash(prepared.preflightEvidence)
    && Number.isFinite(Date.parse(prepared.preflightObservedAt))
    && (!requireFresh || now.getTime() - Date.parse(prepared.preflightObservedAt) <= LOCAL_PILOT_PREFLIGHT_MAX_AGE_MS)
    && Date.parse(prepared.preflightObservedAt) <= now.getTime() + 2_000;
}

export function preparedLocalPilotEvidenceHash(evidence: PreparedLocalPilot['preflightEvidence']): string {
  if (!evidence || typeof evidence !== 'object' || Array.isArray(evidence)) {
    throw new Error('LOCAL_PILOT_PREFLIGHT_EVIDENCE_INVALID');
  }
  const allowedKeys = [
    'observedAt', 'executionMode', 'engineState', 'mainnetLiveApproved', 'preflightOnly',
    'preflightPassed', 'canArm', 'orderSubmissionAttempts', 'orderEndpointAttempts', 'checks',
  ];
  if (Object.keys(evidence).some((key) => !allowedKeys.includes(key))
    || allowedKeys.some((key) => !Object.hasOwn(evidence, key))) {
    throw new Error('LOCAL_PILOT_PREFLIGHT_EVIDENCE_INVALID');
  }
  if (typeof evidence.observedAt !== 'string' || !Number.isFinite(Date.parse(evidence.observedAt))
    || typeof evidence.executionMode !== 'string' || typeof evidence.engineState !== 'string'
    || typeof evidence.mainnetLiveApproved !== 'boolean' || typeof evidence.preflightOnly !== 'boolean'
    || typeof evidence.preflightPassed !== 'boolean' || typeof evidence.canArm !== 'boolean'
    || !Number.isSafeInteger(evidence.orderSubmissionAttempts) || evidence.orderSubmissionAttempts < 0
    || !Number.isSafeInteger(evidence.orderEndpointAttempts) || evidence.orderEndpointAttempts < 0
    || !Array.isArray(evidence.checks)) {
    throw new Error('LOCAL_PILOT_PREFLIGHT_EVIDENCE_INVALID');
  }
  const checks = evidence.checks.map((check) => {
    if (!check || typeof check !== 'object' || Array.isArray(check)
      || Object.keys(check).sort().join(',') !== 'id,required,status'
      || typeof check.id !== 'string' || !check.id
      || !['PASS', 'FAIL', 'NOT_RUN', 'UNKNOWN'].includes(check.status)
      || typeof check.required !== 'boolean') {
      throw new Error('LOCAL_PILOT_PREFLIGHT_EVIDENCE_INVALID');
    }
    return { id: check.id, status: check.status, required: check.required };
  }).sort((a, b) => a.id.localeCompare(b.id));
  if (new Set(checks.map((check) => check.id)).size !== checks.length) {
    throw new Error('LOCAL_PILOT_PREFLIGHT_EVIDENCE_INVALID');
  }
  const canonical = {
    observedAt: new Date(Date.parse(evidence.observedAt)).toISOString(),
    executionMode: evidence.executionMode,
    engineState: evidence.engineState,
    mainnetLiveApproved: evidence.mainnetLiveApproved,
    preflightOnly: evidence.preflightOnly,
    preflightPassed: evidence.preflightPassed,
    canArm: evidence.canArm,
    orderSubmissionAttempts: evidence.orderSubmissionAttempts,
    orderEndpointAttempts: evidence.orderEndpointAttempts,
    checks,
  };
  return crypto.createHash('sha256').update(JSON.stringify(canonical)).digest('hex');
}
