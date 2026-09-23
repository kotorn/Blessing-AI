import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it, vi } from 'vitest';

const firebaseAuthMock = vi.hoisted(() => ({
  applicationDefault: vi.fn(() => ({})),
  getApps: vi.fn(() => [{ name: 'test-app' }]),
  initializeApp: vi.fn(() => ({ name: 'test-app' })),
  getAuth: vi.fn(),
  verifyIdToken: vi.fn(),
}));

vi.mock('firebase-admin/app', () => ({
  applicationDefault: firebaseAuthMock.applicationDefault,
  getApps: firebaseAuthMock.getApps,
  initializeApp: firebaseAuthMock.initializeApp,
}));

vi.mock('firebase-admin/auth', () => ({
  getAuth: firebaseAuthMock.getAuth.mockImplementation(() => ({
    verifyIdToken: firebaseAuthMock.verifyIdToken,
  })),
}));
import {
  authorizeFirebaseRequest,
  authorizeOperatorRequest,
} from '../src/backend/bigquery.js';
import {
  controlPlaneRoles,
  hasControlPlaneRole,
  isEightDIncidentId,
  requiredControlPlaneRole,
} from '../src/backend/control-plane-auth.js';

const server = readFileSync(resolve(process.cwd(), 'server.ts'), 'utf8');
const wealthGrowthDeck = readFileSync(resolve(process.cwd(), 'src/components/WealthGrowthDeck.tsx'), 'utf8');
const workerDockerfile = readFileSync(resolve(process.cwd(), 'Dockerfile.worker'), 'utf8');
const envExample = readFileSync(resolve(process.cwd(), '.env.example'), 'utf8');
const iamVerification = readFileSync(resolve(process.cwd(), 'infra/cloudrun/verify-iam.ps1'), 'utf8');
const aiCopilot = readFileSync(resolve(process.cwd(), 'src/components/AIQuantCopilot.tsx'), 'utf8');
const backtestStudio = readFileSync(resolve(process.cwd(), 'src/components/BacktestReplayStudio.tsx'), 'utf8');
const accountOverview = readFileSync(resolve(process.cwd(), 'src/components/AccountOverview.tsx'), 'utf8');
const balanceAllocation = readFileSync(resolve(process.cwd(), 'src/components/BalanceAllocation.tsx'), 'utf8');
const header = readFileSync(resolve(process.cwd(), 'src/components/Header.tsx'), 'utf8');

describe('control-plane authentication contract', () => {
  it('denies anonymous and malformed Firebase credentials before route logic', async () => {
    const request = (authorization?: string) => ({
      header: () => authorization,
    }) as any;

    const anonymous = await authorizeOperatorRequest(request(), {
      requiredRole: 'operator',
    });
    expect(anonymous.ok).toBe(false);
    expect(anonymous.forbidden).toBeUndefined();

    const malformed = await authorizeFirebaseRequest(request('Bearer not-a-firebase-id-token'), {
      localBypassEnv: '__CONTROL_PLANE_TEST_BYPASS_DISABLED__',
      requiredRole: 'viewer',
    });
    expect(malformed.ok).toBe(false);
    expect(malformed.error).toBeTruthy();
  });

  it('allows a verified trading_admin and denies a verified viewer for mutation', async () => {
    const request = (authorization: string) => ({
      header: () => authorization,
    }) as any;

    firebaseAuthMock.verifyIdToken
      .mockResolvedValueOnce({ uid: 'admin-user', role: 'trading_admin' })
      .mockResolvedValueOnce({ uid: 'viewer-user', role: 'viewer' });

    const admin = await authorizeOperatorRequest(request('Bearer verified-admin'), {
      requiredRole: 'trading_admin',
    });
    expect(admin.ok).toBe(true);
    expect(admin.uid).toBe('admin-user');
    expect(admin.role).toBe('trading_admin');

    const viewer = await authorizeOperatorRequest(request('Bearer verified-viewer'), {
      requiredRole: 'operator',
    });
    expect(viewer.ok).toBe(false);
    expect(viewer.forbidden).toBe(true);
    expect(viewer.role).toBe('viewer');
  });

  it('rejects custom tokens for trading_admin actions and requires interactive google sign-in', async () => {
    const request = (authorization: string) => ({
      header: () => authorization,
    }) as any;

    firebaseAuthMock.verifyIdToken
      .mockResolvedValueOnce({
        uid: 'admin-custom',
        role: 'trading_admin',
        firebase: { sign_in_provider: 'custom' },
      })
      .mockResolvedValueOnce({
        uid: 'admin-google',
        role: 'trading_admin',
        firebase: { sign_in_provider: 'google.com' },
      })
      .mockResolvedValueOnce({
        uid: 'operator-custom',
        role: 'operator',
        firebase: { sign_in_provider: 'custom' },
      });

    // trading_admin rejected when custom token
    const customAdmin = await authorizeOperatorRequest(request('Bearer custom-admin'), {
      requiredRole: 'trading_admin',
    });
    expect(customAdmin.ok).toBe(false);
    expect(customAdmin.forbidden).toBe(true);
    expect(customAdmin.error).toContain('Interactive Google sign-in is required');

    // trading_admin allowed with google sign-in
    const googleAdmin = await authorizeOperatorRequest(request('Bearer google-admin'), {
      requiredRole: 'trading_admin',
    });
    expect(googleAdmin.ok).toBe(true);
    expect(googleAdmin.role).toBe('trading_admin');

    // operator allowed even with custom token
    const customOperator = await authorizeOperatorRequest(request('Bearer custom-operator'), {
      requiredRole: 'operator',
    });
    expect(customOperator.ok).toBe(true);
    expect(customOperator.role).toBe('operator');
  });

  it('requires verified control-plane authorization for system and quant routes', () => {
    expect(server).toContain('authorizeOperatorRequest');
    const protectedApiMiddleware = server.slice(
      server.indexOf('app.use(['),
      server.indexOf('], (req, res, next)'),
    );
    expect(protectedApiMiddleware).toContain("'/api/wealth'");
    expect(protectedApiMiddleware).toContain("'/api/incidents'");
    expect(protectedApiMiddleware).toContain("'/api/learning'");
    expect(server).toContain('CONTROL_PLANE_AUTH_REQUIRED');
    expect(server).toContain('CONTROL_PLANE_AUTH_FORBIDDEN');
    expect(server).toContain("requiredRole: ControlPlaneRole");
    expect(server).toContain('req.baseUrl ||');
    expect(server).toContain('/api/release/mainnet/approve');
    expect(server.indexOf('app.use([')).toBeLessThan(
      server.indexOf("app.get('/api/binance/verify-key'")
    );
    expect(server.indexOf('app.use([')).toBeLessThan(
      server.indexOf("app.get('/api/google/products'")
    );
  });

  it('requires viewer/operator authorization for Wealth and 8D API routes', () => {
    expect(requiredControlPlaneRole({ method: 'GET', path: '/api/wealth/metrics' })).toBe('viewer');
    expect(requiredControlPlaneRole({ method: 'GET', path: '/api/incidents/8d' })).toBe('viewer');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/incidents/8d/8D-20260923-ABC123/close' })).toBe('operator');
    expect(server).toContain('encodeURIComponent(incidentId)');
    expect(isEightDIncidentId('8D-20260923-ABC123')).toBe(true);
    expect(isEightDIncidentId('../../kill-switch?')).toBe(false);
    expect(isEightDIncidentId('8D-20260923-ABC123/../kill-switch')).toBe(false);
  });

  it('keeps Wealth telemetry unavailable when the Worker has no verified evidence', () => {
    const wealthRoutes = server.slice(server.indexOf('// Wealth Growth & 8D Learning Engine Endpoints'));
    expect(wealthRoutes).toContain("evidence_status: 'UNAVAILABLE'");
    expect(wealthRoutes).not.toContain('is_capital_safe: true');
    expect(wealthRoutes).not.toContain('res.json([])');
    expect(wealthGrowthDeck).toContain('WEALTH_TELEMETRY_UNAVAILABLE');
    expect(wealthGrowthDeck).toContain('apiClient.get<WealthMetrics>');
    expect(wealthGrowthDeck).not.toContain('Shadow test verified mitigation.');
    expect(wealthGrowthDeck).not.toContain("signoff_agent: 'ChiefRiskOfficerAgent'");
    expect(workerDockerfile).toContain('COPY apps/learning_engine/ ./apps/learning_engine/');
  });

  it('uses a Google-signed Worker identity in production', () => {
    expect(server).toContain('WORKER_URL');
    expect(server).toContain('WORKER_IDENTITY_TOKEN');
    expect(server).toContain('WORKER_URL is not configured');
    expect(server).toContain('getIdTokenClient');
    expect(server).toContain('getRequestHeaders');
    expect(server).toContain('Firebase user tokens never cross this service boundary');
    expect(envExample).toContain('CONTROL_PLANE_SERVICE_ACCOUNT=blessing-control-plane@');
    expect(envExample).toContain('CONTROL_PLANE_ALLOW_UNAUTHENTICATED_LOCAL=false');
  });

  it('keeps Control Plane readiness behind the internal OIDC boundary', () => {
    expect(server).toContain("app.use('/internal/release'");
    expect(server).toContain("app.post('/internal/release/readiness'");
    expect(server).toContain('FIREBASE_ADMIN');
    expect(server).toContain('RELEASE_STORE');
    expect(server).toContain('WORKER_OIDC');
    expect(server).toContain("forwardWorkerRequest('/ready')");
  });

  it('maps only verified claims to hierarchical roles', () => {
    expect(controlPlaneRoles({})).toEqual([]);
    expect(controlPlaneRoles({ role: 'viewer' })).toEqual(['viewer']);
    expect(controlPlaneRoles({ roles: ['operator'] })).toEqual(['operator']);
    expect(controlPlaneRoles({ role: 'trading_admin' })).toEqual(['trading_admin']);
    expect(controlPlaneRoles({ admin: true })).toEqual(['trading_admin']);

    expect(hasControlPlaneRole(['viewer'], 'viewer')).toBe(true);
    expect(hasControlPlaneRole(['viewer'], 'operator')).toBe(false);
    expect(hasControlPlaneRole(['operator'], 'viewer')).toBe(true);
    expect(hasControlPlaneRole(['operator'], 'operator')).toBe(true);
    expect(hasControlPlaneRole(['operator'], 'trading_admin')).toBe(false);
    expect(hasControlPlaneRole(['trading_admin'], 'trading_admin')).toBe(true);
  });

  it('keeps the Cloud Run Worker invoker binding fail-closed', () => {
    expect(iamVerification).toContain('get-iam-policy');
    expect(iamVerification).toContain('roles/run.invoker');
    expect(iamVerification).toContain('allUsers');
    expect(iamVerification).toContain('allAuthenticatedUsers');
    expect(iamVerification).toContain('ControlPlaneServiceAccount');
  });

  it('assigns elevated roles to high-impact routes', () => {
    expect(requiredControlPlaneRole({ method: 'GET', path: '/api/system/state' })).toBe('viewer');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/system/disarm' })).toBe('operator');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/system/arm', body: { executionMode: 'TESTNET' } })).toBe('operator');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/system/arm', body: { executionMode: 'LIVE' } })).toBe('trading_admin');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/system/kill-switch', body: { active: true } })).toBe('operator');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/system/kill-switch', body: { active: false } })).toBe('trading_admin');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/system/preflight/read-only' })).toBe('trading_admin');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/quant/backtest/run' })).toBe('operator');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/quant/ai/research' })).toBe('operator');

    // Case-insensitivity verification matching Express default route dispatching
    expect(requiredControlPlaneRole({ method: 'POST', path: '/API/RELEASE/MAINNET/APPROVE' })).toBe('trading_admin');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/Release/Mainnet/Continuation/Approve' })).toBe('trading_admin');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/system/Continue' })).toBe('trading_admin');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/system/ARM', body: { executionMode: 'LIVE' } })).toBe('trading_admin');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/API/SYSTEM/KILL-SWITCH', body: { active: false } })).toBe('trading_admin');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/API/SYSTEM/KILL-SWITCH', body: { active: true } })).toBe('operator');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/system/preflight/READ-ONLY' })).toBe('trading_admin');
  });

  it('does not accept Mainnet credentials through the browser profile store', () => {
    expect(server).toContain('MAINNET_CREDENTIALS_MUST_USE_SECRET_MANAGER');
    expect(server).toContain('rejectBrowserMainnetCredentialStorage(res)');
    expect(server).toContain("if (!requestedTestnet || requestedEnvironment !== 'TESTNET')");
  });

  it('routes protected research calls through the authenticated API client', () => {
    expect(aiCopilot).toContain("apiClient.post<ResearchResponse>('/api/quant/ai/research'");
    expect(backtestStudio).toContain("apiClient.post<BacktestResponse>('/api/quant/backtest/run'");
    expect(aiCopilot).not.toContain("fetch('/api/quant/ai/research'");
    expect(backtestStudio).not.toContain("fetch('/api/quant/backtest/run'");
  });

  it('routes protected Binance browser calls through the authenticated API client', () => {
    for (const component of [accountOverview, balanceAllocation, header]) {
      expect(component).toContain("from '../api/client'");
      expect(component).not.toMatch(/fetch\(['"]\/api\/binance\//);
    }
  });
});
