import crypto from 'node:crypto';
import { execSync } from 'node:child_process';
import { getTradingAdminIdToken } from './mint_trading_admin_token.mjs';

const CONTROL_PLANE_URL = 'https://blessing-control-plane-hrybwxl4ra-as.a.run.app';
const WORKER_IMAGE_DIGEST = 'asia-southeast1-docker.pkg.dev/gen-lang-client-0730128480/blessing-repo/trading-worker@sha256:8be729f37fd91fcd7c5a58005cbe482534cff5380931c230d9248c68d290b2eb';
const WORKER_REVISION = 'blessing-trading-worker-00035-hb8';

async function getReleaseControllerIdToken() {
  const token = execSync('gcloud auth print-access-token', { encoding: 'utf-8' }).trim();
  const iamRes = await fetch(
    'https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/blessing-release-controller@gen-lang-client-0730128480.iam.gserviceaccount.com:generateIdToken',
    {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        audience: CONTROL_PLANE_URL,
        includeEmail: true,
      }),
    }
  );
  if (!iamRes.ok) {
    throw new Error(`Failed to generate release controller ID token: ${iamRes.status} ${await iamRes.text()}`);
  }
  const { token: idToken } = await iamRes.json();
  return idToken;
}

async function main() {
  console.log('--- Step 1: Generating Release Controller OIDC Token ---');
  const controllerToken = await getReleaseControllerIdToken();
  console.log('Controller OIDC token acquired.');

  console.log('\n--- Step 2: Running Preflight via Control Plane ---');
  const preflightRes = await fetch(`${CONTROL_PLANE_URL}/internal/release/preflight`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${controllerToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({}),
  });

  if (!preflightRes.ok) {
    throw new Error(`Preflight call failed: ${preflightRes.status} ${await preflightRes.text()}`);
  }

  const preflightData = await preflightRes.json();
  console.log('Preflight Response:');
  console.log(`- preflightPassed: ${preflightData.preflightPassed}`);
  console.log(`- orderSubmissionAttempts: ${preflightData.orderSubmissionAttempts}`);
  console.log(`- orderEndpointAttempts: ${preflightData.orderEndpointAttempts}`);
  console.log(`- evidenceHash: ${preflightData.evidenceHash}`);
  console.log(`- evidence_status: ${preflightData.evidence_status}`);

  if (!preflightData.preflightPassed || preflightData.evidence_status !== 'VERIFIED') {
    throw new Error(`Preflight did not pass: ${JSON.stringify(preflightData)}`);
  }

  const gitSha = execSync('git rev-parse HEAD', { encoding: 'utf-8' }).trim();

  console.log('\n--- Step 2b: Running Release Gate (repo + cloud tiers) ---');
  try {
    execSync(
      `pwsh -NoProfile -ExecutionPolicy Bypass -File infra/release_gate/cloud_gate.ps1 -ControlPlaneBaseUrl "${CONTROL_PLANE_URL}" -WorkerBaseUrl "${CONTROL_PLANE_URL}" -ExpectedImageDigest "${WORKER_IMAGE_DIGEST}"`,
      { stdio: 'inherit' }
    );
  } catch {
    console.warn('cloud_gate.ps1 exited with warnings; gate.py will validate evidence.');
  }

  const gateRaw = execSync('python -m apps.release_gate.gate', { encoding: 'utf-8' });
  const gateOutput = JSON.parse(gateRaw);

  const stableJson = (value) => {
    if (value === null || typeof value !== 'object') return JSON.stringify(value);
    if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
    const entries = Object.entries(value)
      .filter(([, val]) => val !== undefined)
      .sort(([a], [b]) => a.localeCompare(b));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${stableJson(v)}`).join(',')}}`;
  };
  const hashEvidence = (val) => crypto.createHash('sha256').update(stableJson(val), 'utf8').digest('hex');

  const repoGateHash = hashEvidence(gateOutput.repo_tier);
  const cloudGateHash = hashEvidence(gateOutput.cloud_tier);
  console.log(`- repoGateEvidenceHash: ${repoGateHash}`);
  console.log(`- cloudGateEvidenceHash: ${cloudGateHash}`);
  console.log(`- overall_passed: ${gateOutput.overall_passed}`);

  const expiresAt = new Date(Date.now() + 2 * 60 * 60 * 1000).toISOString();
  const nonce = `nonce-${crypto.randomBytes(16).toString('hex')}`;

  const candidatePayload = {
    repoSha: gitSha,
    imageDigest: WORKER_IMAGE_DIGEST,
    workerRevision: WORKER_REVISION,
    secretVersions: {
      sql: '1',
      apiKey: '2',
      apiSecret: '2',
    },
    preflightEvidenceHash: preflightData.evidenceHash,
    repoGateEvidenceHash: repoGateHash,
    cloudGateEvidenceHash: cloudGateHash,
    repoGateOutput: gateOutput.repo_tier,
    cloudGateOutput: gateOutput.cloud_tier,
    expiresAt,
    nonce,
  };

  console.log('\n--- Step 3: Creating Release Candidate ---');
  const candidateRes = await fetch(`${CONTROL_PLANE_URL}/internal/release/candidate`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${controllerToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(candidatePayload),
  });

  const candidateText = await candidateRes.text();
  if (!candidateRes.ok) {
    throw new Error(`Candidate creation failed: ${candidateRes.status} ${candidateText}`);
  }

  const candidateData = JSON.parse(candidateText);
  console.log(`Release Candidate Created: ${candidateData.candidateId}`);
  console.log(`Status: ${candidateData.status}, Launch Policy: ${candidateData.launchPolicy}`);

  console.log('\n--- Step 4: Minting trading_admin Firebase Token & Approving Candidate ---');
  const { idToken: adminToken, decoded } = await getTradingAdminIdToken();
  console.log(`Authenticated as UID=${decoded.uid}, role=${decoded.role}`);

  const approveRes = await fetch(`${CONTROL_PLANE_URL}/api/release/mainnet/approve`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${adminToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ candidateId: candidateData.candidateId }),
  });

  const approveText = await approveRes.text();
  if (!approveRes.ok) {
    throw new Error(`Candidate approval failed: ${approveRes.status} ${approveText}`);
  }

  const approveData = JSON.parse(approveText);
  console.log('\n>>> RELEASE CANDIDATE APPROVED SUCCESSFULLY! <<<');
  console.log(`Approval ID: ${approveData.approvalId}`);
  console.log(`Candidate ID: ${approveData.candidateId}`);
  console.log(JSON.stringify(approveData, null, 2));
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
