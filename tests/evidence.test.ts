import { describe, expect, it } from 'vitest';
import { deriveEvidenceStatus } from '../src/lib/evidence';
import {
  highestControlPlaneRoleFromClaims,
  requiredRoleForExecutionMode,
  roleSatisfies,
} from '../src/lib/control-plane-role';

describe('authoritative evidence status', () => {
  const now = Date.parse('2026-09-22T06:00:00.000Z');

  it('marks a fresh Binance worker state as verified', () => {
    expect(deriveEvidenceStatus({
      dataSource: 'BINANCE',
      workerResponsive: true,
      updatedAt: '2026-09-22T05:59:55.000Z',
      now,
    })).toBe('VERIFIED');
  });

  it('keeps simulated data visibly separate from verified data', () => {
    expect(deriveEvidenceStatus({
      dataSource: 'SIMULATED',
      workerResponsive: true,
      updatedAt: '2026-09-22T05:59:55.000Z',
      now,
    })).toBe('SIMULATED');
  });

  it('marks old, future, missing, and unhealthy state as unsafe to trust', () => {
    expect(deriveEvidenceStatus({ dataSource: 'BINANCE', workerResponsive: true, updatedAt: '2026-09-22T05:59:00.000Z', now })).toBe('STALE');
    expect(deriveEvidenceStatus({ dataSource: 'BINANCE', workerResponsive: true, updatedAt: '2026-09-22T06:00:01.000Z', now })).toBe('STALE');
    expect(deriveEvidenceStatus({ dataSource: 'BINANCE', workerResponsive: false, updatedAt: '2026-09-22T05:59:55.000Z', now })).toBe('UNAVAILABLE');
    expect(deriveEvidenceStatus({ dataSource: 'BINANCE', workerResponsive: true, updatedAt: 'not-a-date', now })).toBe('UNAVAILABLE');
  });
});

describe('control-plane role guidance', () => {
  it('matches the server role hierarchy and legacy trusted claims', () => {
    expect(highestControlPlaneRoleFromClaims({ role: 'viewer' })).toBe('viewer');
    expect(highestControlPlaneRoleFromClaims({ roles: ['viewer', 'operator'] })).toBe('operator');
    expect(highestControlPlaneRoleFromClaims({ tradingAdmin: true })).toBe('trading_admin');
    expect(highestControlPlaneRoleFromClaims({})).toBe('unknown');
  });

  it('requires trading_admin for LIVE and operator for non-live arming', () => {
    expect(requiredRoleForExecutionMode('PAPER')).toBe('operator');
    expect(requiredRoleForExecutionMode('TESTNET')).toBe('operator');
    expect(requiredRoleForExecutionMode('LIVE')).toBe('trading_admin');
    expect(roleSatisfies('trading_admin', 'operator')).toBe(true);
    expect(roleSatisfies('viewer', 'operator')).toBe(false);
    expect(roleSatisfies('unknown', 'viewer')).toBe(false);
  });
});
