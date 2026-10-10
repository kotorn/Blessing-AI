import { describe, expect, it, vi } from 'vitest';
import {
  LOCAL_LIVE_PILOT_DURATION_MS,
  LOCAL_LIVE_PILOT_DRAWDOWN_USDC,
  LOCAL_LIVE_PILOT_MAX_LEVERAGE,
  LOCAL_LIVE_PILOT_ORDER_NOTIONAL_USDC,
  LOCAL_LIVE_PILOT_ENTRY_TARGET_NOTIONAL_USDC,
  LOCAL_LIVE_PILOT_EXECUTION_RISK_BUFFER_USDC,
  LOCAL_LIVE_PILOT_SESSION_ENTRY_CUTOFF_SECONDS,
  LOCAL_LIVE_PILOT_SESSION_CLOSE_AFTER_SECONDS,
  LOCAL_LIVE_PILOT_SESSION_END_SECONDS,
  LOCAL_LIVE_PILOT_PENDING_WINDOW_MS,
  LOCAL_LIVE_PILOT_PLANNED_RISK_USDC,
  LOCAL_LIVE_PILOT_POSITION_NOTIONAL_USDC,
  LOCAL_LIVE_PILOT_QUICK_MAX_HOLD_MS,
  LOCAL_LIVE_PILOT_QUICK_TARGET_NET_USDC,
  LOCAL_LIVE_PILOT_SYMBOL,
  LOCAL_LIVE_PILOT_TOTAL_EXPOSURE_USDC,
  localLivePilotCanPrepare,
  localLivePilotCanStart,
  localLivePilotCanIncreaseRisk,
  assertLocalLivePilotRecoveryReleaseAllowed,
  localLivePilotBinding,
  newLocalLivePilotCampaign,
  mapLocalPilotSession,
  type LocalLivePilotInput,
} from '../src/backend/local-live-pilot.js';
import { InMemoryLocalLivePilotStore } from '../src/backend/local-live-pilot-store.js';
import { revokeLocalPilotWithWorkerGuard } from '../src/backend/local-worker-supervisor.js';

const NOW = new Date('2026-09-26T03:00:00.000Z');
const ADMIN = { uid: 'admin-pilot-1', role: 'trading_admin' as const };
const HASHES = {
  sourceHash: 'a'.repeat(64),
  dependencyHash: 'b'.repeat(64),
  migrationHash: 'c'.repeat(64),
  strategyHash: 'd'.repeat(64),
  riskPolicyHash: 'e'.repeat(64),
};

function input(overrides: Partial<LocalLivePilotInput> = {}): LocalLivePilotInput {
  return {
    campaignId: 'pilot-2026-test-0001',
    runId: 'run-2026-test-0001',
    adminUid: ADMIN.uid,
    role: ADMIN.role,
    ...HASHES,
    gitSha: 'f'.repeat(40),
    strategyId: 'grid',
    secretManagerProjectId: 'blessing-project-123',
    apiKeyVersion: '1',
    apiSecretVersion: '1',
    managementMode: 'QUICK',
    ...overrides,
  };
}

async function pending() {
  const store = new InMemoryLocalLivePilotStore();
  const campaign = await store.create(input(), NOW);
  return { store, campaign, expected: localLivePilotBinding(campaign) };
}

describe('Local 7-day live research pilot domain', () => {
  it('binds the approved entry sizing and bounded session timing while keeping the hard caps', async () => {
    const { campaign } = await pending();
    expect(campaign.limits).toMatchObject({
      orderNotionalUsdc: LOCAL_LIVE_PILOT_ORDER_NOTIONAL_USDC,
      entryTargetNotionalUsdc: LOCAL_LIVE_PILOT_ENTRY_TARGET_NOTIONAL_USDC,
      executionRiskBufferUsdc: LOCAL_LIVE_PILOT_EXECUTION_RISK_BUFFER_USDC,
      sessionEntryCutoffSeconds: LOCAL_LIVE_PILOT_SESSION_ENTRY_CUTOFF_SECONDS,
      sessionCloseAfterSeconds: LOCAL_LIVE_PILOT_SESSION_CLOSE_AFTER_SECONDS,
      sessionEndSeconds: LOCAL_LIVE_PILOT_SESSION_END_SECONDS,
      positionNotionalUsdc: 50, plannedRiskUsdc: 2, campaignDrawdownUsdc: 5, maxLeverage: 10,
    });
  });

  it('rejects strategies outside the approved grid-only pilot', () => {
    expect(() => newLocalLivePilotCampaign(input({ strategyId: 'trend' }), NOW))
      .toThrow('LOCAL_PILOT_GRID_STRATEGY_REQUIRED');
  });

  it('maps only server-owned worker session timestamps into the control-plane response shape', () => {
    expect(mapLocalPilotSession({ armed_at: '2026-10-10T00:00:00.000Z',
      entry_cutoff_at: '2026-10-10T01:30:00.000Z', close_after_at: '2026-10-10T01:50:00.000Z',
      end_at: '2026-10-10T02:00:00.000Z', stage: 'ENTRY_CUTOFF', browserValue: true })).toEqual({
      armedAt: '2026-10-10T00:00:00.000Z', entryCutoffAt: '2026-10-10T01:30:00.000Z',
      closeAfterAt: '2026-10-10T01:50:00.000Z', endAt: '2026-10-10T02:00:00.000Z', stage: 'ENTRY_CUTOFF',
    });
    expect(mapLocalPilotSession({ armed_at: 'invalid', stage: '<script>' })).toMatchObject({
      armedAt: null, stage: 'UNKNOWN',
    });
    expect(mapLocalPilotSession(null)).toBeNull();
  });
  it('permits a current approved campaign to prepare without granting risk authority', async () => {
    const { store, campaign, expected } = await pending();
    const approved = await store.approve(campaign.campaignId, ADMIN, expected, NOW);
    expect(localLivePilotCanPrepare(approved, NOW)).toBe(true);
    expect(localLivePilotCanStart(approved, NOW)).toBe(true);
    expect(localLivePilotCanIncreaseRisk(approved, NOW)).toBe(false);
    expect(localLivePilotCanPrepare(approved, new Date(Date.parse(approved.campaignExpiresAt || '') + 1))).toBe(false);
    const active = await store.activate(approved.campaignId, expected, NOW);
    expect(localLivePilotCanPrepare(active, NOW)).toBe(true);
    expect(localLivePilotCanStart(active, NOW)).toBe(true);
    expect(localLivePilotCanPrepare({ ...active, status: 'CLOSE_ONLY' }, NOW)).toBe(false);
    expect(localLivePilotCanStart(active, new Date(Date.parse(approved.campaignExpiresAt || '') + 1))).toBe(false);
  });

  it('allows recovery-only release only for the bound admin of an ACTIVE unexpired campaign', async () => {
    const { store, campaign, expected } = await pending();
    const approved = await store.approve(campaign.campaignId, ADMIN, expected, NOW);
    expect(() => assertLocalLivePilotRecoveryReleaseAllowed(approved, ADMIN.uid, NOW))
      .toThrow('LOCAL_PILOT_NOT_ACTIVE');
    const active = await store.activate(campaign.campaignId, expected, NOW);
    expect(() => assertLocalLivePilotRecoveryReleaseAllowed(active, ADMIN.uid, NOW)).not.toThrow();
    expect(() => assertLocalLivePilotRecoveryReleaseAllowed(null, ADMIN.uid, NOW))
      .toThrow('LOCAL_PILOT_NOT_FOUND');
    expect(() => assertLocalLivePilotRecoveryReleaseAllowed(active, 'someone-else', NOW))
      .toThrow('LOCAL_PILOT_RELEASE_UID_MISMATCH');
    expect(() => assertLocalLivePilotRecoveryReleaseAllowed(active, '', NOW))
      .toThrow('LOCAL_PILOT_RELEASE_UID_MISMATCH');
    const afterExpiry = new Date(Date.parse(active.campaignExpiresAt || '') + 1);
    expect(() => assertLocalLivePilotRecoveryReleaseAllowed(active, ADMIN.uid, afterExpiry))
      .toThrow('LOCAL_PILOT_NOT_ACTIVE');
    const closeOnly = await store.enterCloseOnly(campaign.campaignId, expected, NOW);
    expect(() => assertLocalLivePilotRecoveryReleaseAllowed(closeOnly, ADMIN.uid, NOW))
      .toThrow('LOCAL_PILOT_NOT_ACTIVE');
    const revoked = await store.revoke(campaign.campaignId, ADMIN, expected, NOW);
    expect(() => assertLocalLivePilotRecoveryReleaseAllowed(revoked, ADMIN.uid, NOW))
      .toThrow('LOCAL_PILOT_NOT_ACTIVE');
  });

  it('does not revoke an eligible campaign when worker recovery-only is rejected', async () => {
    const { store, campaign, expected } = await pending();
    await store.approve(campaign.campaignId, ADMIN, expected, NOW);
    const active = await store.activate(campaign.campaignId, expected, NOW);
    const readWorkerState = vi.fn();
    const commitRevocation = vi.fn(() => store.revoke(campaign.campaignId, ADMIN, expected, NOW));

    await expect(revokeLocalPilotWithWorkerGuard({
      campaign: active,
      actorUid: ADMIN.uid,
      worker: { workerRunning: true, mode: 'LIVE', pilotCampaignId: campaign.campaignId },
      requestRecoveryOnly: async () => ({ ok: false, active: false }),
      readWorkerState,
      commitRevocation,
    })).rejects.toThrow('LOCAL_PILOT_WORKER_CLOSE_ONLY_UNCONFIRMED');

    await expect(store.get(campaign.campaignId)).resolves.toMatchObject({ status: 'ACTIVE' });
    expect(await store.canIncreaseRisk(campaign.campaignId, NOW)).toBe(true);
    expect(readWorkerState).not.toHaveBeenCalled();
    expect(commitRevocation).not.toHaveBeenCalled();
  });

  it('does not affect a worker when campaign status is ineligible for revocation', async () => {
    const { store, campaign, expected } = await pending();
    await store.approve(campaign.campaignId, ADMIN, expected, NOW);
    await store.activate(campaign.campaignId, expected, NOW);
    const completed = await store.complete(campaign.campaignId, expected, NOW);
    const requestRecoveryOnly = vi.fn(async () => ({ ok: true, active: true }));
    const readWorkerState = vi.fn(async () => ({
      execution_mode: 'LIVE',
      active_configuration: { pilotCampaignId: campaign.campaignId },
      recovery_only: true,
      engine_state: 'RECOVERY_ONLY',
    }));
    const commitRevocation = vi.fn(() => store.revoke(campaign.campaignId, ADMIN, expected, NOW));

    await expect(revokeLocalPilotWithWorkerGuard({
      campaign: completed,
      actorUid: ADMIN.uid,
      worker: { workerRunning: true, mode: 'LIVE', pilotCampaignId: campaign.campaignId },
      requestRecoveryOnly,
      readWorkerState,
      commitRevocation,
    })).rejects.toThrow('LOCAL_PILOT_ALREADY_COMPLETED');

    expect(requestRecoveryOnly).not.toHaveBeenCalled();
    expect(readWorkerState).not.toHaveBeenCalled();
    expect(commitRevocation).not.toHaveBeenCalled();
    await expect(store.get(campaign.campaignId)).resolves.toMatchObject({ status: 'COMPLETED' });
  });

  it('creates an immutable server-capped ETHUSDC USD-M campaign with one-hour pending window', () => {
    const campaign = newLocalLivePilotCampaign(input(), NOW);
    expect(campaign).toMatchObject({
      runtimeTarget: 'LOCAL',
      symbol: LOCAL_LIVE_PILOT_SYMBOL,
      market: 'USD_M_FUTURES',
      status: 'PENDING_APPROVAL',
      adminUid: ADMIN.uid,
      managementMode: 'QUICK',
    });
    expect(Date.parse(campaign.pendingExpiresAt) - Date.parse(campaign.requestedAt)).toBe(LOCAL_LIVE_PILOT_PENDING_WINDOW_MS);
    expect(campaign.limits).toEqual({
      positionNotionalUsdc: LOCAL_LIVE_PILOT_POSITION_NOTIONAL_USDC,
      orderNotionalUsdc: LOCAL_LIVE_PILOT_ORDER_NOTIONAL_USDC,
      entryTargetNotionalUsdc: LOCAL_LIVE_PILOT_ENTRY_TARGET_NOTIONAL_USDC,
      executionRiskBufferUsdc: LOCAL_LIVE_PILOT_EXECUTION_RISK_BUFFER_USDC,
      sessionEntryCutoffSeconds: LOCAL_LIVE_PILOT_SESSION_ENTRY_CUTOFF_SECONDS,
      sessionCloseAfterSeconds: LOCAL_LIVE_PILOT_SESSION_CLOSE_AFTER_SECONDS,
      sessionEndSeconds: LOCAL_LIVE_PILOT_SESSION_END_SECONDS,
      totalExposureUsdc: LOCAL_LIVE_PILOT_TOTAL_EXPOSURE_USDC,
      plannedRiskUsdc: LOCAL_LIVE_PILOT_PLANNED_RISK_USDC,
      campaignDrawdownUsdc: LOCAL_LIVE_PILOT_DRAWDOWN_USDC,
      maxLeverage: LOCAL_LIVE_PILOT_MAX_LEVERAGE,
      quickTargetNetUsdc: LOCAL_LIVE_PILOT_QUICK_TARGET_NET_USDC,
      quickMaxHoldMs: LOCAL_LIVE_PILOT_QUICK_MAX_HOLD_MS,
    });
    expect(campaign.nonce).toMatch(/^[a-f0-9]{48}$/);
    expect(JSON.stringify(campaign)).not.toMatch(/"apiKey"\s*:|"apiSecret"\s*:|password|credential|token/i);
  });

  it('rejects browser-supplied limits, arbitrary fields, bad hashes, and non-admin role', () => {
    expect(() => newLocalLivePilotCampaign({ ...input(), limits: { maxLeverage: 100 } } as never, NOW))
      .toThrow(/server-controlled|unsupported/i);
    expect(() => newLocalLivePilotCampaign({ ...input(), apiSecret: 'secret' } as never, NOW))
      .toThrow(/server-controlled|unsupported/i);
    expect(() => newLocalLivePilotCampaign(input({ role: 'operator' } as never), NOW)).toThrow(/trading_admin/i);
    expect(() => newLocalLivePilotCampaign(input({ strategyHash: 'not-a-hash' }), NOW)).toThrow(/SHA-256/i);
  });

  it('binds all code, data, strategy, risk, admin, nonce, and management-mode identity', () => {
    const campaign = newLocalLivePilotCampaign(input(), NOW);
    const binding = localLivePilotBinding(campaign);
    expect(binding).toMatchObject({
      campaignId: campaign.campaignId,
      adminUid: ADMIN.uid,
      sourceHash: HASHES.sourceHash,
      dependencyHash: HASHES.dependencyHash,
      migrationHash: HASHES.migrationHash,
      strategyHash: HASHES.strategyHash,
      riskPolicyHash: HASHES.riskPolicyHash,
      managementMode: 'QUICK',
      nonce: campaign.nonce,
    });
  });

  it('rejects HOLD until its no-fixed-target lifecycle is implemented', () => {
    expect(() => newLocalLivePilotCampaign(input({ managementMode: 'HOLD' } as never), NOW))
      .toThrow(/only supports QUICK/i);
  });

  it('approves once, fixes the seven-day expiry at approval time, and is idempotent on retry', async () => {
    const { store, campaign, expected } = await pending();
    const approvedAt = new Date(NOW.getTime() + 30_000);
    const approved = await store.approve(campaign.campaignId, ADMIN, expected, approvedAt);
    expect(approved.status).toBe('APPROVED');
    expect(approved.approvedByUid).toBe(ADMIN.uid);
    expect(Date.parse(approved.campaignExpiresAt!) - Date.parse(approved.approvedAt!)).toBe(LOCAL_LIVE_PILOT_DURATION_MS);
    expect(await store.approve(campaign.campaignId, ADMIN, expected, new Date(approvedAt.getTime() + 1_000))).toEqual(approved);
  });

  it('rejects approval after the pending window, from another UID, or with a mismatched binding', async () => {
    const a = await pending();
    await expect(a.store.approve(a.campaign.campaignId, ADMIN, a.expected,
      new Date(NOW.getTime() + LOCAL_LIVE_PILOT_PENDING_WINDOW_MS))).rejects.toThrow(/expired/i);
    await expect(a.store.approve(a.campaign.campaignId, { ...ADMIN, uid: 'other-admin' }, a.expected, NOW)).rejects.toThrow(/UID/i);
    await expect(a.store.approve(a.campaign.campaignId, ADMIN, { ...a.expected, strategyHash: HASHES.sourceHash }, NOW))
      .rejects.toThrow(/binding mismatch/i);
    await expect(a.store.approve(a.campaign.campaignId, { uid: ADMIN.uid, role: 'operator' } as never, a.expected, NOW))
      .rejects.toThrow(/trading_admin/i);
  });

  it('requires explicit activation before risk-increase and makes activation idempotent', async () => {
    const { store, campaign, expected } = await pending();
    expect(await store.canIncreaseRisk(campaign.campaignId, NOW)).toBe(false);
    await store.approve(campaign.campaignId, ADMIN, expected, NOW);
    expect(await store.canIncreaseRisk(campaign.campaignId, NOW)).toBe(false);
    const active = await store.activate(campaign.campaignId, expected, new Date(NOW.getTime() + 10));
    expect(active.status).toBe('ACTIVE');
    expect(await store.activate(campaign.campaignId, expected, new Date(NOW.getTime() + 20))).toEqual(active);
    expect(await store.canIncreaseRisk(campaign.campaignId, new Date(NOW.getTime() + 20))).toBe(true);
    expect(localLivePilotCanIncreaseRisk({ ...active, sourceHash: 'bad' }, NOW)).toBe(false);
  });

  it('campaign expiry blocks new risk while allowing close-only/completion transitions', async () => {
    const { store, campaign, expected } = await pending();
    await store.approve(campaign.campaignId, ADMIN, expected, NOW);
    await store.activate(campaign.campaignId, expected, NOW);
    const expiry = new Date(NOW.getTime() + LOCAL_LIVE_PILOT_DURATION_MS);
    const expired = await store.expire(campaign.campaignId, expiry);
    expect(expired.status).toBe('EXPIRED');
    expect(await store.canIncreaseRisk(campaign.campaignId, expiry)).toBe(false);
    const closeOnly = await store.enterCloseOnly(campaign.campaignId, expected, expiry);
    expect(closeOnly.status).toBe('CLOSE_ONLY');
    expect(await store.enterCloseOnly(campaign.campaignId, expected, expiry)).toEqual(closeOnly);
    const completed = await store.complete(campaign.campaignId, expected, expiry);
    expect(completed.status).toBe('COMPLETED');
    expect(await store.complete(campaign.campaignId, expected, expiry)).toEqual(completed);
  });

  it('expires an unapproved request and will not resurrect/reapprove it', async () => {
    const { store, campaign, expected } = await pending();
    const expired = await store.expire(campaign.campaignId, new Date(NOW.getTime() + LOCAL_LIVE_PILOT_PENDING_WINDOW_MS));
    expect(expired.status).toBe('EXPIRED');
    await expect(store.approve(campaign.campaignId, ADMIN, expected, new Date(NOW.getTime() + LOCAL_LIVE_PILOT_PENDING_WINDOW_MS)))
      .rejects.toThrow(/not pending|expired/i);
  });

  it('moves to close-only or revokes idempotently and never permits risk increase afterward', async () => {
    const { store, campaign, expected } = await pending();
    await store.approve(campaign.campaignId, ADMIN, expected, NOW);
    await store.activate(campaign.campaignId, expected, NOW);
    const closeOnly = await store.enterCloseOnly(campaign.campaignId, expected, NOW);
    expect(await store.enterCloseOnly(campaign.campaignId, expected, NOW)).toEqual(closeOnly);
    expect(await store.canIncreaseRisk(campaign.campaignId, NOW)).toBe(false);

    const { store: secondStore, campaign: second, expected: secondBinding } = await pending();
    await secondStore.approve(second.campaignId, ADMIN, secondBinding, NOW);
    const revoked = await secondStore.revoke(second.campaignId, ADMIN, secondBinding, NOW);
    expect(revoked.status).toBe('REVOKED');
    expect(await secondStore.revoke(second.campaignId, ADMIN, secondBinding, NOW)).toEqual(revoked);
    expect(await secondStore.canIncreaseRisk(second.campaignId, NOW)).toBe(false);
  });

  it('serializes concurrent approval so only one transition/version is committed', async () => {
    const { store, campaign, expected } = await pending();
    const results = await Promise.all([
      store.approve(campaign.campaignId, ADMIN, expected, NOW),
      store.approve(campaign.campaignId, ADMIN, expected, NOW),
    ]);
    expect(results[0]).toEqual(results[1]);
    expect(results[0].version).toBe(2);
    expect((await store.get(campaign.campaignId))?.version).toBe(2);
  });
});
