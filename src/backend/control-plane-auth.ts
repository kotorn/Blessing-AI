/**
 * Server-side authorization policy for the control plane.
 *
 * Firebase ID tokens establish the browser user's identity.  These helpers
 * interpret only trusted custom claims verified by firebase-admin; the client
 * cannot select a role by changing request JSON or local state.
 */

export type ControlPlaneRole = 'viewer' | 'operator' | 'trading_admin';

const ROLE_RANK: Record<ControlPlaneRole, number> = {
  viewer: 1,
  operator: 2,
  trading_admin: 3,
};

function normalizeRole(value: unknown): ControlPlaneRole | null {
  if (typeof value !== 'string') return null;
  const normalized = value.trim().toLowerCase().replace(/[-\s]+/g, '_');
  if (normalized === 'viewer' || normalized === 'read_only' || normalized === 'readonly') {
    return 'viewer';
  }
  if (normalized === 'operator') return 'operator';
  if (
    normalized === 'trading_admin' ||
    normalized === 'admin' ||
    normalized === 'trader'
  ) {
    return 'trading_admin';
  }
  return null;
}

/** Return roles derived from verified Firebase custom claims only. */
export function controlPlaneRoles(claims: Record<string, unknown>): ControlPlaneRole[] {
  const roles = new Set<ControlPlaneRole>();
  const roleClaims = [claims.role, claims.roles];
  for (const claim of roleClaims) {
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

  // Legacy boolean claims remain accepted only as trusted, server-verified
  // custom claims while deployments migrate to the explicit role names.
  if (claims.viewer === true) roles.add('viewer');
  if (claims.operator === true) roles.add('operator');
  if (claims.trading_admin === true || claims.tradingAdmin === true || claims.admin === true) {
    roles.add('trading_admin');
  }

  return [...roles].sort((left, right) => ROLE_RANK[left] - ROLE_RANK[right]);
}

export function highestControlPlaneRole(
  roles: ControlPlaneRole[],
): ControlPlaneRole | undefined {
  return roles.length ? roles[roles.length - 1] : undefined;
}

export function hasControlPlaneRole(
  roles: ControlPlaneRole[],
  required: ControlPlaneRole,
): boolean {
  const highest = highestControlPlaneRole(roles);
  return Boolean(highest && ROLE_RANK[highest] >= ROLE_RANK[required]);
}

export interface ControlPlaneRequestLike {
  method: string;
  originalUrl?: string;
  path?: string;
  body?: unknown;
}

/**
 * Choose the minimum trusted role for a route.  LIVE arm and kill-switch
 * transitions are intentionally restricted to trading_admin; all other
 * control mutations require operator or better, and reads require viewer.
 */
export function requiredControlPlaneRole(req: ControlPlaneRequestLike): ControlPlaneRole {
  const route = (req.originalUrl || req.path || '').split('?')[0];
  const method = String(req.method || 'GET').toUpperCase();
  if (method === 'GET' || method === 'HEAD') return 'viewer';

  if (route.endsWith('/preflight/read-only')) return 'trading_admin';

  if (route.endsWith('/arm')) {
    const mode = String((req.body as { executionMode?: unknown } | undefined)?.executionMode || '')
      .trim()
      .toUpperCase();
    return mode === 'LIVE' ? 'trading_admin' : 'operator';
  }

  if (route.endsWith('/kill-switch') || route.endsWith('/killswitch')) {
    return 'trading_admin';
  }

  return 'operator';
}
