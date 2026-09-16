import { OAuth2Client } from 'google-auth-library';

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

export interface InternalServiceRequestLike {
  header(name: string): string | undefined;
}

export interface InternalServiceAuthResult {
  ok: boolean;
  serviceAccount?: string;
  subject?: string;
  error?: string;
}

export interface InternalServiceAuthOptions {
  /** Exact Cloud Run URL used as the OIDC audience. */
  audience?: string;
  /** Exact service-account emails allowed to call the internal route. */
  allowedServiceAccounts?: string[];
  /** Injectable verifier for deterministic unit tests. */
  verifyIdToken?: (token: string, audience: string) => Promise<{
    iss?: string;
    aud?: string | string[];
    email?: string;
    email_verified?: boolean;
    sub?: string;
  } | undefined>;
}

const googleOidcClient = new OAuth2Client();
const GOOGLE_ISSUER = 'https://accounts.google.com';

function configuredInternalAudience(): string {
  return (
    process.env.CONTROL_PLANE_URL?.trim() ||
    process.env.CONTROL_PLANE_BASE_URL?.trim() ||
    ''
  ).replace(/\/+$/, '');
}

function configuredServiceAccounts(): string[] {
  return (process.env.CONTROL_PLANE_ALLOWED_SERVICE_ACCOUNTS || '')
    .split(',')
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean);
}

async function verifyGoogleIdToken(
  token: string,
  audience: string,
): Promise<{
  iss?: string;
  aud?: string | string[];
  email?: string;
  email_verified?: boolean;
  sub?: string;
} | undefined> {
  const ticket = await googleOidcClient.verifyIdToken({ idToken: token, audience });
  return ticket.getPayload() || undefined;
}

/**
 * Verify a Google-signed Cloud Run service identity.
 *
 * This is deliberately separate from Firebase user authorization. A Firebase
 * ID token proves the browser user to this service; it must never be reused as
 * the service-to-service credential sent to the Worker.
 */
export async function authorizeInternalServiceRequest(
  req: InternalServiceRequestLike,
  options: InternalServiceAuthOptions = {},
): Promise<InternalServiceAuthResult> {
  const authorization = req.header('authorization') || '';
  const match = /^Bearer\s+([^\s]+)$/i.exec(authorization);
  if (!match) return { ok: false, error: 'Google service identity is required' };

  const audience = (options.audience || configuredInternalAudience()).replace(/\/+$/, '');
  if (!audience) return { ok: false, error: 'Internal OIDC audience is not configured' };

  const allowed = (options.allowedServiceAccounts?.length
    ? options.allowedServiceAccounts
    : configuredServiceAccounts())
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean);
  if (!allowed.length) return { ok: false, error: 'Internal service allowlist is not configured' };

  try {
    const payload = await (options.verifyIdToken || verifyGoogleIdToken)(match[1], audience);
    const tokenAudience = payload?.aud;
    const audienceMatches = Array.isArray(tokenAudience)
      ? tokenAudience.includes(audience)
      : tokenAudience === audience;
    const email = payload?.email?.trim().toLowerCase() || '';
    if (
      !payload ||
      payload.iss !== GOOGLE_ISSUER ||
      !audienceMatches ||
      !payload.email_verified ||
      !allowed.includes(email)
    ) {
      return { ok: false, error: 'Google service identity is not authorized' };
    }
    return { ok: true, serviceAccount: email, subject: payload.sub };
  } catch {
    // Never echo a token, issuer response, or verifier internals.
    return { ok: false, error: 'Google service identity could not be verified' };
  }
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

  if (route.endsWith('/release/mainnet/approve')) return 'trading_admin';

  if (route.endsWith('/release/mainnet/continuation/approve')) return 'trading_admin';

  if (route.endsWith('/system/continue')) return 'trading_admin';

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
