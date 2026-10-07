import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const compose = readFileSync(resolve(process.cwd(), 'docker-compose.yml'), 'utf8');
const launcher = readFileSync(resolve(process.cwd(), 'scripts/start-local.ps1'), 'utf8');
const batchLauncher = readFileSync(resolve(process.cwd(), 'scripts/start-local.bat'), 'utf8');
const server = readFileSync(resolve(process.cwd(), 'server.ts'), 'utf8');
const worker = readFileSync(resolve(process.cwd(), 'apps/trading_worker/main.py'), 'utf8');
const supervisor = readFileSync(resolve(process.cwd(), 'src/backend/local-worker-supervisor.ts'), 'utf8');
const ciWorkflow = readFileSync(resolve(process.cwd(), '.github/workflows/ci.yml'), 'utf8');

describe('local runtime safety contract', () => {
  it('publishes every Compose host port on loopback only', () => {
    const publishedPorts = [...compose.matchAll(/^\s+- "([^"]+:\d+)"\s*$/gm)]
      .map((match) => match[1]);

    expect(publishedPorts.length).toBeGreaterThan(0);
    expect(publishedPorts.every((mapping) => mapping.startsWith('127.0.0.1:'))).toBe(true);
  });

  it('keeps the existing Compose Postgres port while isolating local runtime ports', () => {
    const postgresBlock = compose.split('\n  postgres:\n')[1]?.split('\n\n  # Isolated')[0] ?? '';
    const localPostgresBlock = compose.split('\n  postgres-local:\n')[1]?.split('\n\n  redis:')[0] ?? '';

    expect(postgresBlock).toContain('127.0.0.1:5432:5432');
    expect(localPostgresBlock).toContain('127.0.0.1:5433:5432');
    expect(launcher).toContain('$env:POSTGRES_PORT = "5433"');
    expect(launcher).toContain('$env:PORT = "3001"');
    expect(launcher).toContain('http://127.0.0.1:3001');
    expect(launcher).toContain('$env:WORKER_URL = "http://127.0.0.1:8000"');
  });

  it('runs local services with durable persistence, local auth, and a disarmed PAPER default', () => {
    expect(launcher).toContain('$env:LOCAL_ONLY = "true"');
    expect(launcher).toContain('$env:PERSISTENCE_MODE = "REQUIRED"');
    expect(launcher).toContain('$env:EXECUTION_LEASE_REQUIRED = "true"');
    expect(launcher).toContain('$env:EXECUTION_MODE = "PAPER"');
    expect(launcher).toContain('$env:MAINNET_LIVE_APPROVED = "false"');
    expect(launcher).toContain('$env:NODE_ENV = "development"');
    expect(launcher).toContain('$env:LOCAL_MAINNET_API_KEY_VERSION = Get-LocalConfigValue');
    expect(launcher).toContain('$env:LOCAL_MAINNET_API_SECRET_VERSION = Get-LocalConfigValue');
    expect(launcher).not.toContain('Get-SecretVersion');
    expect(launcher).not.toContain('LoadMainnetSecrets');
    expect(launcher).not.toContain('CONTROL_PLANE_ALLOW_UNAUTHENTICATED_LOCAL');
    expect(launcher).toContain('$env:LOCAL_WORKER_AUTH_REQUIRED = "true"');
    expect(launcher).toContain('$env:LOCAL_WORKER_RUNTIME = "DOCKER"');
    expect(launcher).toContain('--file Dockerfile.worker --tag $workerImageTag');
    expect(launcher).toContain('image inspect --format "{{.Id}}" $workerImageTag');
    expect(launcher).toContain('[System.Security.Cryptography.ProtectedData]::Protect');
    expect(launcher).toContain('[System.Security.Cryptography.ProtectedData]::Unprotect');
    expect(launcher).toContain('DataProtectionScope]::CurrentUser');
    expect(launcher).toContain('com.docker.compose.project.config_files');
    expect(launcher).toContain('com.docker.compose.volume');
    expect(launcher).toContain('postgres_local_data');
    expect(launcher).not.toContain('ConvertTo-SecureString');
    expect(launcher).toContain('compose --profile local up -d postgres-local');
    expect(launcher).toContain('apply_local_postgres_migrations.py');
    expect(launcher).toContain('Get-NetTCPConnection -State Listen -ErrorAction Stop');
    expect(launcher).toContain('Show-UnmanagedPortStatus 8888');
    expect(launcher).toContain('Get-LocalDockerContext');
    expect(launcher).toContain('--context $script:dockerContext');
    expect(launcher.match(/Assert-RequiredPortsAvailable/g)?.length).toBeGreaterThanOrEqual(4);
    expect(launcher).toContain('127.0.0.1:5433');
    expect(batchLauncher).toContain('start-local.ps1');
    expect(server).toContain('app.listen(PORT, BIND_HOST');
    expect(server).toContain("process.env.NODE_ENV !== 'production' || LOCAL_ONLY");
    expect(worker).toContain('host=worker_bind_host()');
  });

  it('rejects the legacy unbound Local LIVE route without retrieving Mainnet secrets', () => {
    const candidateRoute = server
      .split("app.post('/api/local/mainnet/candidate'")[1]
      ?.split("app.get('/api/local/mainnet/:candidateId'")[0] ?? '';
    const approvalRoute = server
      .split("app.post('/api/local/mainnet/approve'")[1]
      ?.split("app.get('/api/system/readiness'")[0] ?? '';

    expect(candidateRoute).toContain('localMainnetRiskLifecycleStatus()');
    expect(candidateRoute).toContain('LOCAL_MAINNET_RISK_LIFECYCLE_UNAVAILABLE');
    expect(approvalRoute).toContain("error: 'LOCAL_PILOT_REQUIRED'");
    expect(approvalRoute).toContain('campaign-bound Local Pilot request, approval, and prepare flow');
    expect(approvalRoute).not.toContain('accessPinnedLocalMainnetSecrets(');
    expect(approvalRoute).not.toContain('startApprovedLive(');
    expect(approvalRoute).not.toContain('startApprovedPilotLive(');
    expect(server).toContain("app.post('/api/local/pilot/prepare'");
    expect(server).toContain("app.post('/api/local/pilot/start'");
    const legacyContinuation = server
      .split("app.post('/api/system/continue'")[1]
      ?.split("app.post('/api/system/kill-switch'")[0] ?? '';
    expect(legacyContinuation).toContain("error: 'LOCAL_PILOT_CAMPAIGN_REQUIRED'");
    expect(legacyContinuation).toContain('if (LOCAL_ONLY)');
    expect(supervisor).toContain("throw new Error('LOCAL_PILOT_REQUIRED')");
  });

  it('attests only successful exact-SHA CI runs pushed to main', () => {
    const job = ciWorkflow.split('  local_pilot_ci_attestation:')[1] ?? '';
    expect(job).toContain('needs: [build_and_test]');
    expect(job).toContain("if: github.repository == 'kotorn/Blessing-AI' && github.event_name == 'push' && github.ref == 'refs/heads/main'");
    expect(job).toContain("github.repository == 'kotorn/Blessing-AI'");
    expect(job).toContain('1366161771');
    expect(job).toContain('kotorn/Blessing-AI/.github/workflows/ci.yml@refs/heads/main');
    expect(job).toContain('id-token: write');
    expect(job).toContain('attestations: write');
    expect(job).toContain('COMMIT_SHA: ${{ github.sha }}');
    expect(job).toContain('WORKFLOW_SHA: ${{ github.workflow_sha }}');
    expect(job).toContain('if os.environ["WORKFLOW_SHA"] != sha:');
    expect(job).toContain('actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6');
    expect(job).toContain('actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a');
    expect(job).toContain('steps.attest.outputs.bundle-path');
    expect(job).toContain('local-pilot-ci-attestation.bundle.json');
    expect(job).toContain('artifacts/local-pilot-ci-evidence.json');
    expect(job).not.toContain('BINANCE_MAINNET_API_KEY');
    expect(job).not.toContain('accessPinnedLocalMainnetSecrets');
  });

  it('requires authenticated supervisor heartbeats and exits Local LIVE after heartbeat loss', () => {
    expect(supervisor).toContain('/supervisor/heartbeat');
    expect(supervisor).toContain('AbortSignal.timeout(2_500)');
    expect(worker).toContain('@app.post("/supervisor/heartbeat")');
    expect(worker).toContain('LOCAL_SUPERVISOR_HEARTBEAT_TTL_SECONDS = 15.0');
    expect(worker).toContain('action=disarm_and_exit');
    expect(worker).toContain('MAINNET_LIVE_APPROVED"] = "false"');
    expect(worker).toContain('await server.serve()');
  });

  it('reads back ambiguous ARM and gates revocation on recovery before the campaign transition', () => {
    const pilotStart = server
      .split("app.post('/api/local/pilot/start'")[1]
      ?.split("app.post('/api/local/pilot/close-only'")[0] ?? '';
    const pilotRevoke = server
      .split("app.post('/api/local/pilot/revoke'")[1]
      ?.split('async function inspectLocalContinuationContext')[0] ?? '';
    const recoveryRoute = server
      .split("app.post('/api/system/recovery-only'")[1]
      ?.split("app.post('/api/system/kill-switch'")[0] ?? '';
    const ambiguousArmRecovery = pilotStart
      .split('if (armAttempted && campaign)')[1] ?? '';

    expect(ambiguousArmRecovery.indexOf('confirmRunningLocalPilotCampaign'))
      .toBeLessThan(ambiguousArmRecovery.indexOf("forwardWorkerRequest('/recovery-only'"));
    expect(ambiguousArmRecovery).not.toContain('startPaper');
    expect(pilotRevoke.indexOf('campaign.adminUid !== uid'))
      .toBeLessThan(pilotRevoke.indexOf('revokeLocalPilotWithWorkerGuard'));
    expect(pilotRevoke.indexOf("campaign.status === 'COMPLETED'"))
      .toBeLessThan(pilotRevoke.indexOf('revokeLocalPilotWithWorkerGuard'));
    expect(pilotRevoke.indexOf('revokeLocalPilotWithWorkerGuard'))
      .toBeLessThan(pilotRevoke.indexOf('commitRevocation: () => store.revoke('));
    expect(pilotRevoke).toContain('workerRecoveryOnly: workerRecoveryConfirmed');
    expect(recoveryRoute).toContain('campaignId !== supervisor.pilotCampaignId');
    expect(recoveryRoute.indexOf('confirmRunningLocalPilotCampaign'))
      .toBeLessThan(recoveryRoute.indexOf("forwardWorkerRequest('/recovery-only'"));
  });

  it('keeps the Local Pilot /start route blocked before any ARM when readiness is unverified', () => {
    const pilotStart = server
      .split("app.post('/api/local/pilot/start'")[1]
      ?.split("app.post('/api/local/pilot/close-only'")[0] ?? '';
    const unready = pilotStart.indexOf('rejectUnreadyLocalPilot(res)');
    const reserveTransition = pilotStart.indexOf('reserveLocalPilotTransition()');
    const armRequest = pilotStart.indexOf("forwardWorkerRequest('/arm'");

    expect(unready).toBeGreaterThanOrEqual(0);
    expect(unready).toBeLessThan(reserveTransition);
    expect(reserveTransition).toBeLessThan(armRequest);
  });

  it('does not expose campaign identity or Secret Manager references in pilot responses', () => {
    const sanitizer = server
      .split('function safeLocalLivePilotCampaign(')[1]
      ?.split('\n}', 1)[0] ?? '';

    expect(sanitizer).toContain('adminUid: _adminUid');
    expect(sanitizer).toContain('approvedByUid: _approvedByUid');
    expect(sanitizer).toContain('secretManagerProjectId: _secretManagerProjectId');
    expect(sanitizer).toContain('apiKeyVersion: _apiKeyVersion');
    expect(sanitizer).toContain('apiSecretVersion: _apiSecretVersion');
    expect(sanitizer).toContain('nonce: _nonce');
  });

  it('serializes close-only and revoke with prepare/start transitions', () => {
    const closeOnly = server
      .split("app.post('/api/local/pilot/close-only'")[1]
      ?.split("app.post('/api/local/pilot/revoke'")[0] ?? '';
    const revoke = server
      .split("app.post('/api/local/pilot/revoke'")[1]
      ?.split('async function inspectLocalContinuationContext')[0] ?? '';

    expect(closeOnly).toContain('reserveLocalPilotTransition()');
    expect(closeOnly).toContain('finally {\n    localPilotTransitionBusy = false;');
    expect(revoke).toContain('reserveLocalPilotTransition()');
    expect(revoke).toContain('finally {\n    localPilotTransitionBusy = false;');
  });

  it('binds recovery-only release and close-only to the campaign admin and an ACTIVE campaign', () => {
    const closeOnly = server
      .split("app.post('/api/local/pilot/close-only'")[1]
      ?.split("app.post('/api/local/pilot/revoke'")[0] ?? '';
    const recoveryRoute = server
      .split("app.post('/api/system/recovery-only'")[1]
      ?.split("app.post('/api/system/kill-switch'")[0] ?? '';

    expect(closeOnly).toContain('res.locals.firebaseUid');
    expect(closeOnly).toContain("'FIREBASE_IDENTITY_MISSING'");
    expect(closeOnly).toContain('campaign.adminUid !== uid');
    expect(closeOnly).toContain("'LOCAL_PILOT_CLOSER_UID_MISMATCH'");
    expect(closeOnly.indexOf('campaign.adminUid !== uid'))
      .toBeLessThan(closeOnly.indexOf("forwardWorkerRequest('/recovery-only'"));
    expect(recoveryRoute).toContain('reserveLocalPilotTransition');
    expect(recoveryRoute).toContain('LOCAL_PILOT_TRANSITION_IN_PROGRESS');
    expect(recoveryRoute).toContain('assertLocalLivePilotRecoveryReleaseAllowed(');
    expect(recoveryRoute).toContain('res.locals.firebaseUid');
    expect(recoveryRoute).toContain('LOCAL_PILOT_RELEASE_UID_MISMATCH');
    expect(recoveryRoute).toContain('LOCAL_PILOT_NOT_ACTIVE');
    expect(recoveryRoute.indexOf('assertLocalLivePilotRecoveryReleaseAllowed('))
      .toBeLessThan(recoveryRoute.indexOf("forwardWorkerRequest('/recovery-only'"));
  });

  it('permits ACTIVE campaign re-prepare only through the DISARMED flat-account preflight', () => {
    const prepareRoute = server
      .split("app.post('/api/local/pilot/prepare'")[1]
      ?.split("app.post('/api/local/pilot/start'")[0] ?? '';

    expect(prepareRoute).toContain("!['APPROVED', 'ACTIVE'].includes(campaign.status)");
    expect(prepareRoute).toContain('localLivePilotCanPrepare(campaign)');
    expect(prepareRoute).toContain('localWorkerIsPaperDisarmed(workerBeforeStart)');
    expect(prepareRoute).toContain("forwardWorkerRequest('/preflight/read-only'");
    expect(prepareRoute).toContain("forwardWorkerRequest('/state')");
    expect(prepareRoute).toContain("status: campaign.status");
    expect(prepareRoute).not.toContain("forwardWorkerRequest('/arm'");
  });

  it('strictly validates BINANCE_PORTFOLIO_MARGIN in start-local.ps1 without a silent default', () => {
    expect(launcher).not.toContain('Get-LocalConfigValue "BINANCE_PORTFOLIO_MARGIN" "true"');
    expect(launcher).toContain("BINANCE_PORTFOLIO_MARGIN must be set to 'true' or 'false'");
  });

  it('skips dotenv.config() in LOCAL_ONLY mode or when SKIP_DOTENV sentinel is set', () => {
    expect(server).toContain('if (!skipDotenv) {');
    expect(server).toContain('dotenv.config();');
    expect(server).toContain("const skipDotenv = isLocalOnlyBoot || ['1', 'true', 'yes', 'on'].includes(");
    expect(server).toContain("process.env.SKIP_DOTENV");
    expect(server).toContain("process.env.LOCAL_ONLY");
  });
});

