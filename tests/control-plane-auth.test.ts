import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const server = readFileSync(resolve(process.cwd(), 'server.ts'), 'utf8');
const envExample = readFileSync(resolve(process.cwd(), '.env.example'), 'utf8');
const aiCopilot = readFileSync(resolve(process.cwd(), 'src/components/AIQuantCopilot.tsx'), 'utf8');
const backtestStudio = readFileSync(resolve(process.cwd(), 'src/components/BacktestReplayStudio.tsx'), 'utf8');

describe('control-plane authentication contract', () => {
  it('requires operator authorization for system and quant control routes', () => {
    expect(server).toContain('authorizeOperatorRequest');
    expect(server).toContain("app.use(['/api/system', '/api/quant']");
    expect(server).toContain('OPERATOR_AUTH_REQUIRED');
  });

  it('requires a configured worker identity in production', () => {
    expect(server).toContain('WORKER_URL');
    expect(server).toContain('WORKER_IDENTITY_TOKEN');
    expect(server).toContain('WORKER_URL is not configured');
    expect(envExample).toContain('CONTROL_PLANE_ALLOW_UNAUTHENTICATED_LOCAL=false');
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
});
