import { TradingSystemState } from '../types';

export type EvidenceStatus = 'VERIFIED' | 'SIMULATED' | 'STALE' | 'UNAVAILABLE';

export const DEFAULT_EVIDENCE_MAX_AGE_MS = 10_000;

export interface EvidenceStateInput {
  dataSource?: TradingSystemState['dataSource'];
  workerResponsive?: boolean;
  updatedAt?: string;
  now?: number;
  maxAgeMs?: number;
}

/**
 * Derive a display-only provenance state from authoritative server fields.
 * Missing, invalid, future, or explicitly unhealthy state must never look
 * fresh. This helper does not grant execution authority; the server remains
 * the sole source of truth for arm/disarm decisions.
 */
export function deriveEvidenceStatus({
  dataSource,
  workerResponsive,
  updatedAt,
  now = Date.now(),
  maxAgeMs = DEFAULT_EVIDENCE_MAX_AGE_MS,
}: EvidenceStateInput): EvidenceStatus {
  if (workerResponsive !== true || !updatedAt) return 'UNAVAILABLE';

  const updatedAtMs = Date.parse(updatedAt);
  if (!Number.isFinite(updatedAtMs)) return 'UNAVAILABLE';

  const ageMs = now - updatedAtMs;
  if (ageMs < 0 || ageMs > maxAgeMs) return 'STALE';
  if (dataSource === 'SIMULATED') return 'SIMULATED';
  return 'VERIFIED';
}

export function evidenceStatusLabel(status: EvidenceStatus): string {
  switch (status) {
    case 'VERIFIED':
      return 'VERIFIED';
    case 'SIMULATED':
      return 'SIMULATED';
    case 'STALE':
      return 'STALE';
    case 'UNAVAILABLE':
      return 'UNAVAILABLE';
  }
}
