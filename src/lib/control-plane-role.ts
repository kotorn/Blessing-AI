import { User } from 'firebase/auth';

export type ControlPlaneRole = 'viewer' | 'operator' | 'trading_admin' | 'unknown';

const ROLE_RANK: Record<Exclude<ControlPlaneRole, 'unknown'>, number> = {
  viewer: 1,
  operator: 2,
  trading_admin: 3,
};

function normalizeRole(value: unknown): Exclude<ControlPlaneRole, 'unknown'> | null {
  if (typeof value !== 'string') return null;
  const normalized = value.trim().toLowerCase().replace(/[-\s]+/g, '_');
  if (normalized === 'viewer' || normalized === 'read_only' || normalized === 'readonly') return 'viewer';
  if (normalized === 'operator') return 'operator';
  if (normalized === 'trading_admin' || normalized === 'admin' || normalized === 'trader') return 'trading_admin';
  return null;
}

export function highestControlPlaneRoleFromClaims(claims: Record<string, unknown>): ControlPlaneRole {
  const roles = new Set<Exclude<ControlPlaneRole, 'unknown'>>();
  for (const claim of [claims.role, claims.roles]) {
    const values = Array.isArray(claim)
      ? claim
      : typeof claim === 'string'
        ? claim.split(/[\s,]+/)
        : [claim];
    for (const value of values) {
      const role = normalizeRole(value);
      if (role) roles.add(role);
    }
  }
  if (claims.viewer === true) roles.add('viewer');
  if (claims.operator === true) roles.add('operator');
  if (claims.trading_admin === true || claims.tradingAdmin === true || claims.admin === true) {
    roles.add('trading_admin');
  }

  return [...roles].sort((left, right) => ROLE_RANK[left] - ROLE_RANK[right]).at(-1) || 'unknown';
}

export async function resolveControlPlaneRole(user: User | null): Promise<ControlPlaneRole> {
  if (!user) return 'unknown';
  try {
    const token = await user.getIdTokenResult();
    return highestControlPlaneRoleFromClaims(token.claims as Record<string, unknown>);
  } catch {
    return 'unknown';
  }
}

export function requiredRoleForExecutionMode(mode: 'PAPER' | 'TESTNET' | 'LIVE'): Exclude<ControlPlaneRole, 'unknown'> {
  return mode === 'LIVE' ? 'trading_admin' : 'operator';
}

export function roleSatisfies(
  actual: ControlPlaneRole,
  required: Exclude<ControlPlaneRole, 'unknown'>,
): boolean {
  return actual !== 'unknown' && ROLE_RANK[actual] >= ROLE_RANK[required];
}
