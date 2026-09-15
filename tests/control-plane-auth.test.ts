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
  requiredControlPlaneRole,
} from '../src/backend/control-plane-auth.js';

const server = readFileSync(resolve(process.cwd(), 'server.ts'), 'utf8');
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

  it('requires verified control-plane authorization for system and quant routes', () => {
    expect(server).toContain('authorizeOperatorRequest');
    expect(server).toContain("app.use(['/api/system', '/api/quant', '/api/binance']");
    expect(server).toContain('CONTROL_PLANE_AUTH_REQUIRED');
    expect(server).toContain('CONTROL_PLANE_AUTH_FORBIDDEN');
    expect(server).toContain("requiredRole: ControlPlaneRole");
    expect(server.indexOf("app.use(['/api/system', '/api/quant', '/api/binance']")).toBeLessThan(
      server.indexOf("app.get('/api/binance/verify-key'")
    );
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
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/system/kill-switch' })).toBe('trading_admin');
    expect(requiredControlPlaneRole({ method: 'POST', path: '/api/system/preflight/read-only' })).toBe('trading_admin');
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
