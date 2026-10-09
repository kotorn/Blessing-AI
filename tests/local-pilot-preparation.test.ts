import { describe, expect, it } from 'vitest';
import {
  attestPreparedLocalPilot,
  preparedLocalPilotMatches,
} from '../src/backend/local-pilot-preparation.js';

const checkIds = [
  'LOCAL-RISK-LIFECYCLE', 'PILOT-MONITOR', 'PILOT-FLAT-ACCOUNT', 'CREDENTIALS', 'PERSISTENCE', 'DURABLE-LEDGER',
  'KILL-SWITCH', 'CONNECTION', 'AUTH', 'CAN-TRADE', 'POSITION-MODE',
  'RULES', 'RECONCILIATION', 'PRIVATE-STREAM', 'ACCOUNT-RISK', 'MARKET',
  'WORKER-STATE', 'NO-ORDER-ENDPOINT', 'NO-ORDER-SUBMISSION',
];

function validInput(now = new Date('2026-09-27T08:00:00.000Z')) {
  const monitorSuccessAt = new Date(now.getTime() - 5_000).toISOString();
  return {
    campaignId: 'pilot-12345678-1234-1234-1234-123456789abc',
    runId: 'run-12345678-1234-1234-1234-123456789abc',
    sourceFingerprint: 'a'.repeat(64),
    approvalId: 'local-approval-12345678-1234-1234-1234-123456789abc',
    workerGeneration: 2,
    supervisorInstanceId: '12345678-1234-4234-8234-123456789abc',
    workerState: {
      execution_mode: 'LIVE', engine_state: 'DISARMED', mainnet_live_approved: true,
      pilot_campaign_id: 'pilot-12345678-1234-1234-1234-123456789abc',
      local_run_id: 'run-12345678-1234-1234-1234-123456789abc',
      local_source_fingerprint: 'a'.repeat(64), order_submission_attempts: 0,
      local_supervisor_instance_id: '12345678-1234-4234-8234-123456789abc',
      kill_switch_active: false,
      pilot_lifecycle_monitor: {
        status: 'HEALTHY',
        last_success_at: monitorSuccessAt,
      },
    },
    preflight: {
      executionMode: 'LIVE', engineState: 'DISARMED', mainnetLiveApproved: true,
      preflightOnly: true, preflightPassed: true, canArm: false,
      orderSubmissionAttempts: 0, orderEndpointAttempts: 0,
      checks: checkIds.map((id) => ({ id: `CHK-PREFLIGHT-${id}`, required: true, status: 'PASS' })),
      observedAt: '2026-09-27T07:59:30.000Z',
    },
    now,
  };
}

describe('Local pilot preparation attestation', () => {
  it('binds a fresh signed preflight to one DISARMED Worker generation', () => {
    const input = validInput();
    const prepared = attestPreparedLocalPilot(input);
    expect(preparedLocalPilotMatches(prepared, {
      campaignId: input.campaignId,
      runId: input.runId,
      sourceFingerprint: input.sourceFingerprint,
      approvalId: input.approvalId,
      workerGeneration: input.workerGeneration,
      supervisorInstanceId: input.supervisorInstanceId,
    }, input.now)).toBe(true);
    expect(preparedLocalPilotMatches(prepared, {
      campaignId: input.campaignId,
      runId: input.runId,
      sourceFingerprint: input.sourceFingerprint,
      approvalId: input.approvalId,
      workerGeneration: input.workerGeneration + 1,
      supervisorInstanceId: input.supervisorInstanceId,
    }, input.now)).toBe(false);
    expect(prepared.preflightSha256).toMatch(/^[a-f0-9]{64}$/);
    const wrongSupervisor = validInput();
    wrongSupervisor.workerState.local_supervisor_instance_id = '87654321-4321-4321-8321-cba987654321';
    expect(() => attestPreparedLocalPilot(wrongSupervisor)).toThrow('WORKER_IDENTITY_UNVERIFIED');
  });

  it('ignores only the age bound when requireFresh is false', () => {
    const input = validInput();
    const prepared = attestPreparedLocalPilot(input);
    const expected = {
      campaignId: input.campaignId, runId: input.runId, sourceFingerprint: input.sourceFingerprint,
      approvalId: input.approvalId, workerGeneration: input.workerGeneration,
      supervisorInstanceId: input.supervisorInstanceId,
    };
    const later = new Date(input.now.getTime() + 10 * 60_000);
    expect(preparedLocalPilotMatches(prepared, expected, later)).toBe(false);
    expect(preparedLocalPilotMatches(prepared, expected, later, { requireFresh: false })).toBe(true);
    // identity and evidence integrity still apply
    expect(preparedLocalPilotMatches(prepared, { ...expected, workerGeneration: input.workerGeneration + 1 },
      later, { requireFresh: false })).toBe(false);
    expect(preparedLocalPilotMatches(prepared, { ...expected, runId: 'another-run' },
      later, { requireFresh: false })).toBe(false);
    const tampered = { ...prepared, preflightSha256: '0'.repeat(64) };
    expect(preparedLocalPilotMatches(tampered, expected, later, { requireFresh: false })).toBe(false);
  });

  it('allows the monitor to be NOT_RUN only after signed flat-account evidence while DISARMED', () => {
    const input = validInput();
    input.workerState.pilot_lifecycle_monitor.status = 'NOT_RUN';
    input.workerState.pilot_lifecycle_monitor.last_success_at = '';
    const prepared = attestPreparedLocalPilot(input);
    expect(prepared.preflightEvidence.checks.find(
      (check) => check.id === 'CHK-PREFLIGHT-PILOT-FLAT-ACCOUNT',
    )).toEqual({
      id: 'CHK-PREFLIGHT-PILOT-FLAT-ACCOUNT', status: 'PASS', required: true,
    });

    const unknownFlatState = validInput();
    unknownFlatState.workerState.pilot_lifecycle_monitor.status = 'NOT_RUN';
    unknownFlatState.workerState.pilot_lifecycle_monitor.last_success_at = '';
    const flatCheck = unknownFlatState.preflight.checks.find(
      (check) => check.id === 'CHK-PREFLIGHT-PILOT-FLAT-ACCOUNT',
    );
    flatCheck.status = 'UNKNOWN';
    expect(() => attestPreparedLocalPilot(unknownFlatState)).toThrow('LIFECYCLE_MONITOR_UNHEALTHY');
  });

  it('rejects a different campaign, an order attempt, missing checks and stale evidence', () => {
    const wrongCampaign = validInput();
    wrongCampaign.workerState.pilot_campaign_id = 'pilot-another-campaign';
    expect(() => attestPreparedLocalPilot(wrongCampaign)).toThrow('WORKER_IDENTITY_UNVERIFIED');
    const attemptedOrder = validInput();
    attemptedOrder.workerState.order_submission_attempts = 1;
    expect(() => attestPreparedLocalPilot(attemptedOrder)).toThrow('WORKER_IDENTITY_UNVERIFIED');
    const stalledMonitor = validInput();
    stalledMonitor.workerState.pilot_lifecycle_monitor.status = 'STALLED';
    expect(() => attestPreparedLocalPilot(stalledMonitor)).toThrow('LIFECYCLE_MONITOR_UNHEALTHY');
    const missingCheck = validInput();
    missingCheck.preflight.checks.pop();
    expect(() => attestPreparedLocalPilot(missingCheck)).toThrow('PREFLIGHT_INCOMPLETE');
    const stale = validInput(new Date('2026-09-27T08:03:00.000Z'));
    expect(() => attestPreparedLocalPilot(stale)).toThrow('PREFLIGHT_STALE');
  });
});
