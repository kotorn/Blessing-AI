import crypto from 'node:crypto';
import type { PreparedLocalPilot } from './local-pilot-preparation.js';

export const LOCAL_LIVE_PILOT_RUNTIME_TARGET = 'LOCAL' as const;
export const LOCAL_LIVE_PILOT_SYMBOL = 'ETHUSDC' as const;
export const LOCAL_LIVE_PILOT_MARKET = 'USD_M_FUTURES' as const;
export const LOCAL_LIVE_PILOT_PENDING_WINDOW_MS = 60 * 60 * 1_000;
export const LOCAL_LIVE_PILOT_DURATION_MS = 7 * 24 * 60 * 60 * 1_000;
export const LOCAL_LIVE_PILOT_POSITION_NOTIONAL_USDC = 50 as const;
export const LOCAL_LIVE_PILOT_ORDER_NOTIONAL_USDC = 50 as const;
export const LOCAL_LIVE_PILOT_TOTAL_EXPOSURE_USDC = 50 as const;
export const LOCAL_LIVE_PILOT_PLANNED_RISK_USDC = 2 as const;
export const LOCAL_LIVE_PILOT_DRAWDOWN_USDC = 5 as const;
export const LOCAL_LIVE_PILOT_MAX_LEVERAGE = 10 as const;
export const LOCAL_LIVE_PILOT_QUICK_TARGET_NET_USDC = 0.25 as const;
export const LOCAL_LIVE_PILOT_QUICK_MAX_HOLD_MS = 24 * 60 * 60 * 1_000;
export const LOCAL_LIVE_PILOT_COLLECTION = 'local_live_pilot_campaigns' as const;

export type LocalLivePilotManagementMode = 'QUICK';
export type LocalLivePilotStrategyId = 'grid' | 'trend' | 'shock' | 'carry';
export type LocalLivePilotStatus =
  | 'PENDING_APPROVAL'
  | 'APPROVED'
  | 'ACTIVE'
  | 'CLOSE_ONLY'
  | 'EXPIRED'
  | 'COMPLETED'
  | 'REVOKED';

export interface LocalLivePilotActor {
  uid: string;
  role: 'trading_admin';
}

export interface LocalLivePilotInput {
  campaignId: string;
  runId: string;
  adminUid: string;
  role: 'trading_admin';
  sourceHash: string;
  gitSha: string;
  dependencyHash: string;
  migrationHash: string;
  strategyHash: string;
  riskPolicyHash: string;
  strategyId: LocalLivePilotStrategyId;
  secretManagerProjectId: string;
  apiKeyVersion: string;
  apiSecretVersion: string;
  managementMode: LocalLivePilotManagementMode;
}

export interface LocalLivePilotBinding {
  runtimeTarget: typeof LOCAL_LIVE_PILOT_RUNTIME_TARGET;
  campaignId: string;
  runId: string;
  adminUid: string;
  sourceHash: string;
  gitSha: string;
  dependencyHash: string;
  migrationHash: string;
  strategyHash: string;
  riskPolicyHash: string;
  strategyId: LocalLivePilotStrategyId;
  secretManagerProjectId: string;
  apiKeyVersion: string;
  apiSecretVersion: string;
  nonce: string;
  symbol: typeof LOCAL_LIVE_PILOT_SYMBOL;
  market: typeof LOCAL_LIVE_PILOT_MARKET;
  managementMode: LocalLivePilotManagementMode;
  limits: {
    positionNotionalUsdc: typeof LOCAL_LIVE_PILOT_POSITION_NOTIONAL_USDC;
    orderNotionalUsdc: typeof LOCAL_LIVE_PILOT_ORDER_NOTIONAL_USDC;
    totalExposureUsdc: typeof LOCAL_LIVE_PILOT_TOTAL_EXPOSURE_USDC;
    plannedRiskUsdc: typeof LOCAL_LIVE_PILOT_PLANNED_RISK_USDC;
    campaignDrawdownUsdc: typeof LOCAL_LIVE_PILOT_DRAWDOWN_USDC;
    maxLeverage: typeof LOCAL_LIVE_PILOT_MAX_LEVERAGE;
    quickTargetNetUsdc: typeof LOCAL_LIVE_PILOT_QUICK_TARGET_NET_USDC;
    quickMaxHoldMs: typeof LOCAL_LIVE_PILOT_QUICK_MAX_HOLD_MS;
  };
}

export interface LocalLivePilotCampaign extends LocalLivePilotBinding {
  status: LocalLivePilotStatus;
  version: number;
  requestedAt: string;
  pendingExpiresAt: string;
  approvedByUid?: string;
  approvedAt?: string;
  campaignExpiresAt?: string;
  activatedAt?: string;
  closeOnlyAt?: string;
  completedAt?: string;
  revokedAt?: string;
  preparation?: PreparedLocalPilot;
  updatedAt: string;
}

export type LocalLivePilotExpectedBinding = LocalLivePilotBinding;

const HASH_RE = /^[a-f0-9]{64}$/i;
const UID_RE = /^[A-Za-z0-9:_-]{1,256}$/;
const CAMPAIGN_ID_RE = /^pilot-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$/;
const NONCE_RE = /^[a-f0-9]{48}$/;

function clean(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function requireNoExtraFields(value: unknown, allowed: readonly string[]): asserts value is Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Local live pilot input must be an object');
  }
  const allow = new Set(allowed);
  if (Object.keys(value).some((key) => !allow.has(key))) {
    throw new Error('Local live pilot input contains unsupported fields; risk limits are server-controlled');
  }
}

export function localLivePilotBinding(campaign: LocalLivePilotCampaign): LocalLivePilotBinding {
  return {
    runtimeTarget: campaign.runtimeTarget,
    campaignId: campaign.campaignId,
    runId: campaign.runId,
    adminUid: campaign.adminUid,
    sourceHash: campaign.sourceHash,
    gitSha: campaign.gitSha,
    dependencyHash: campaign.dependencyHash,
    migrationHash: campaign.migrationHash,
    strategyHash: campaign.strategyHash,
    riskPolicyHash: campaign.riskPolicyHash,
    strategyId: campaign.strategyId,
    secretManagerProjectId: campaign.secretManagerProjectId,
    apiKeyVersion: campaign.apiKeyVersion,
    apiSecretVersion: campaign.apiSecretVersion,
    nonce: campaign.nonce,
    symbol: campaign.symbol,
    market: campaign.market,
    managementMode: campaign.managementMode,
    limits: { ...campaign.limits },
  };
}

export function validateLocalLivePilotActor(actor: LocalLivePilotActor): void {
  if (!actor || actor.role !== 'trading_admin') throw new Error('Local live pilot requires the trading_admin role');
  if (!UID_RE.test(clean(actor.uid))) throw new Error('Local live pilot trading_admin UID is invalid');
}

export function newLocalLivePilotCampaign(input: LocalLivePilotInput, now = new Date()): LocalLivePilotCampaign {
  requireNoExtraFields(input, [
    'campaignId', 'runId', 'adminUid', 'role', 'sourceHash', 'gitSha', 'dependencyHash', 'migrationHash',
    'strategyHash', 'riskPolicyHash', 'strategyId', 'secretManagerProjectId', 'apiKeyVersion',
    'apiSecretVersion', 'managementMode',
  ]);
  const actor = { uid: input.adminUid, role: input.role } as LocalLivePilotActor;
  validateLocalLivePilotActor(actor);
  if (!CAMPAIGN_ID_RE.test(clean(input.campaignId))) throw new Error('campaignId must be a pilot-prefixed opaque id');
  if (!/^run-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$/.test(clean(input.runId))) throw new Error('runId is invalid');
  for (const field of ['sourceHash', 'dependencyHash', 'migrationHash', 'strategyHash', 'riskPolicyHash'] as const) {
    if (!HASH_RE.test(clean(input[field]))) throw new Error(`${field} must be a SHA-256 hash`);
  }
  if (!/^[a-f0-9]{40,64}$/i.test(clean(input.gitSha))) throw new Error('gitSha must identify the approved commit');
  if (!['grid', 'trend', 'shock', 'carry'].includes(input.strategyId)) throw new Error('strategyId is invalid');
  if (input.managementMode !== 'QUICK') {
    throw new Error('the first Local live research pilot only supports QUICK management');
  }
  if (!/^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(clean(input.secretManagerProjectId))) throw new Error('secretManagerProjectId is invalid');
  if (!/^[1-9][0-9]*$/.test(clean(input.apiKeyVersion)) || !/^[1-9][0-9]*$/.test(clean(input.apiSecretVersion))) {
    throw new Error('pinned Secret Manager versions are required');
  }
  if (!Number.isFinite(now.getTime())) throw new Error('request time is invalid');
  const requestedAt = now.toISOString();
  const campaign: LocalLivePilotCampaign = {
    runtimeTarget: LOCAL_LIVE_PILOT_RUNTIME_TARGET,
    campaignId: clean(input.campaignId),
    runId: clean(input.runId),
    adminUid: clean(input.adminUid),
    sourceHash: clean(input.sourceHash).toLowerCase(),
    gitSha: clean(input.gitSha).toLowerCase(),
    dependencyHash: clean(input.dependencyHash).toLowerCase(),
    migrationHash: clean(input.migrationHash).toLowerCase(),
    strategyHash: clean(input.strategyHash).toLowerCase(),
    riskPolicyHash: clean(input.riskPolicyHash).toLowerCase(),
    strategyId: input.strategyId,
    secretManagerProjectId: clean(input.secretManagerProjectId),
    apiKeyVersion: clean(input.apiKeyVersion),
    apiSecretVersion: clean(input.apiSecretVersion),
    nonce: crypto.randomBytes(24).toString('hex'),
    symbol: LOCAL_LIVE_PILOT_SYMBOL,
    market: LOCAL_LIVE_PILOT_MARKET,
    managementMode: input.managementMode,
    limits: {
      positionNotionalUsdc: LOCAL_LIVE_PILOT_POSITION_NOTIONAL_USDC,
      orderNotionalUsdc: LOCAL_LIVE_PILOT_ORDER_NOTIONAL_USDC,
      totalExposureUsdc: LOCAL_LIVE_PILOT_TOTAL_EXPOSURE_USDC,
      plannedRiskUsdc: LOCAL_LIVE_PILOT_PLANNED_RISK_USDC,
      campaignDrawdownUsdc: LOCAL_LIVE_PILOT_DRAWDOWN_USDC,
      maxLeverage: LOCAL_LIVE_PILOT_MAX_LEVERAGE,
      quickTargetNetUsdc: LOCAL_LIVE_PILOT_QUICK_TARGET_NET_USDC,
      quickMaxHoldMs: LOCAL_LIVE_PILOT_QUICK_MAX_HOLD_MS,
    },
    status: 'PENDING_APPROVAL',
    version: 1,
    requestedAt,
    pendingExpiresAt: new Date(now.getTime() + LOCAL_LIVE_PILOT_PENDING_WINDOW_MS).toISOString(),
    updatedAt: requestedAt,
  };
  return campaign;
}

export function validateLocalLivePilotCampaign(campaign: LocalLivePilotCampaign, now = new Date()): string[] {
  const errors: string[] = [];
  if (!campaign || campaign.runtimeTarget !== LOCAL_LIVE_PILOT_RUNTIME_TARGET) errors.push('runtimeTarget must be LOCAL');
  if (!CAMPAIGN_ID_RE.test(clean(campaign?.campaignId))) errors.push('campaignId is invalid');
  if (!UID_RE.test(clean(campaign?.adminUid))) errors.push('adminUid is invalid');
  if (!NONCE_RE.test(clean(campaign?.nonce))) errors.push('nonce is invalid');
  for (const field of ['sourceHash', 'dependencyHash', 'migrationHash', 'strategyHash', 'riskPolicyHash'] as const) {
    if (!HASH_RE.test(clean(campaign?.[field]))) errors.push(`${field} is invalid`);
  }
  if (!/^[a-f0-9]{40,64}$/i.test(clean(campaign?.gitSha))) errors.push('gitSha is invalid');
  if (campaign?.symbol !== LOCAL_LIVE_PILOT_SYMBOL || campaign?.market !== LOCAL_LIVE_PILOT_MARKET) errors.push('pilot market binding is invalid');
  if (campaign?.managementMode !== 'QUICK') errors.push('first pilot management mode must be QUICK');
  if (!/^run-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$/.test(clean(campaign?.runId))) errors.push('runId is invalid');
  if (!['grid', 'trend', 'shock', 'carry'].includes(String(campaign?.strategyId))) errors.push('strategyId is invalid');
  if (!/^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(clean(campaign?.secretManagerProjectId))) errors.push('secretManagerProjectId is invalid');
  if (!/^[1-9][0-9]*$/.test(clean(campaign?.apiKeyVersion)) || !/^[1-9][0-9]*$/.test(clean(campaign?.apiSecretVersion))) errors.push('secret version binding is invalid');
  const expectedLimits = {
    positionNotionalUsdc: LOCAL_LIVE_PILOT_POSITION_NOTIONAL_USDC,
    orderNotionalUsdc: LOCAL_LIVE_PILOT_ORDER_NOTIONAL_USDC,
    totalExposureUsdc: LOCAL_LIVE_PILOT_TOTAL_EXPOSURE_USDC,
    plannedRiskUsdc: LOCAL_LIVE_PILOT_PLANNED_RISK_USDC,
    campaignDrawdownUsdc: LOCAL_LIVE_PILOT_DRAWDOWN_USDC,
    maxLeverage: LOCAL_LIVE_PILOT_MAX_LEVERAGE,
    quickTargetNetUsdc: LOCAL_LIVE_PILOT_QUICK_TARGET_NET_USDC,
    quickMaxHoldMs: LOCAL_LIVE_PILOT_QUICK_MAX_HOLD_MS,
  };
  if (JSON.stringify(campaign?.limits) !== JSON.stringify(expectedLimits)) errors.push('pilot limits do not match server policy');
  if (!Number.isInteger(campaign?.version) || campaign.version < 1) errors.push('version is invalid');
  for (const field of ['requestedAt', 'pendingExpiresAt', 'updatedAt'] as const) {
    if (!Number.isFinite(Date.parse(clean(campaign?.[field])))) errors.push(`${field} is invalid`);
  }
  if (Date.parse(campaign.pendingExpiresAt) - Date.parse(campaign.requestedAt) !== LOCAL_LIVE_PILOT_PENDING_WINDOW_MS) {
    errors.push('pending approval window must be exactly one hour');
  }
  const unapprovedExpiry = campaign.status === 'EXPIRED'
    && !campaign.approvedAt
    && !campaign.approvedByUid
    && !campaign.campaignExpiresAt;
  if (campaign.status === 'PENDING_APPROVAL' || unapprovedExpiry) {
    if (campaign.approvedAt || campaign.approvedByUid || campaign.campaignExpiresAt) errors.push('pending campaign cannot include approval metadata');
    if (campaign.status === 'PENDING_APPROVAL' && Date.parse(campaign.pendingExpiresAt) <= now.getTime()) {
      errors.push('pending approval window has expired');
    }
  } else {
    if (!campaign.approvedAt || !campaign.approvedByUid || !campaign.campaignExpiresAt) errors.push('approved campaign metadata is incomplete');
    if (campaign.approvedByUid !== campaign.adminUid) errors.push('approvedByUid does not match bound trading_admin');
    const approvedAt = Date.parse(clean(campaign.approvedAt));
    const expiresAt = Date.parse(clean(campaign.campaignExpiresAt));
    if (!Number.isFinite(approvedAt) || !Number.isFinite(expiresAt)) errors.push('campaign approval timestamps are invalid');
    else if (expiresAt - approvedAt !== LOCAL_LIVE_PILOT_DURATION_MS) errors.push('campaign expiry must be exactly seven days after approval');
  }
  if (!['PENDING_APPROVAL', 'APPROVED', 'ACTIVE', 'CLOSE_ONLY', 'EXPIRED', 'COMPLETED', 'REVOKED'].includes(String(campaign?.status))) {
    errors.push('campaign status is invalid');
  }
  return errors;
}

export function assertLocalLivePilotBinding(campaign: LocalLivePilotCampaign, expected: LocalLivePilotExpectedBinding): void {
  if (JSON.stringify(localLivePilotBinding(campaign)) !== JSON.stringify(expected)) {
    throw new Error('Local live pilot binding mismatch');
  }
}

export function localLivePilotCanIncreaseRisk(campaign: LocalLivePilotCampaign, now = new Date()): boolean {
  return validateLocalLivePilotCampaign(campaign, now).length === 0
    && campaign.status === 'ACTIVE'
    && Date.parse(campaign.campaignExpiresAt || '') > now.getTime();
}

/**
 * Releasing recovery-only re-opens new-risk authority on the live Worker, so it
 * requires the bound admin and a still-ACTIVE, unexpired campaign.
 */
export function assertLocalLivePilotRecoveryReleaseAllowed(
  campaign: LocalLivePilotCampaign | null | undefined,
  actorUid: string,
  now = new Date(),
): void {
  if (!campaign) throw new Error('LOCAL_PILOT_NOT_FOUND');
  if (!actorUid || campaign.adminUid !== actorUid) throw new Error('LOCAL_PILOT_RELEASE_UID_MISMATCH');
  if (!localLivePilotCanIncreaseRisk(campaign, now)) throw new Error('LOCAL_PILOT_NOT_ACTIVE');
}

/** An approved/active campaign may prepare a DISARMED Worker; only start permits new risk. */
export function localLivePilotCanPrepare(campaign: LocalLivePilotCampaign, now = new Date()): boolean {
  return validateLocalLivePilotCampaign(campaign, now).length === 0
    && ['APPROVED', 'ACTIVE'].includes(campaign.status)
    && Date.parse(campaign.campaignExpiresAt || '') > now.getTime();
}

/** Allows a crash-retry of the durable ACTIVE-before-ARM transition only while unexpired. */
export function localLivePilotCanStart(campaign: LocalLivePilotCampaign, now = new Date()): boolean {
  return ['APPROVED', 'ACTIVE'].includes(campaign.status)
    && campaign.runtimeTarget === LOCAL_LIVE_PILOT_RUNTIME_TARGET
    && campaign.approvedByUid === campaign.adminUid
    && Number.isFinite(Date.parse(campaign.campaignExpiresAt || ''))
    && Date.parse(campaign.campaignExpiresAt || '') > now.getTime();
}
