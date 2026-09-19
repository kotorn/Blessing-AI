import { execSync } from 'node:child_process';
import { getTradingAdminIdToken } from './mint_trading_admin_token.mjs';

const CONTROL_PLANE_URL = process.env.CONTROL_PLANE_URL || 'https://blessing-control-plane-hrybwxl4ra-as.a.run.app';
const CANDIDATE_ID = process.env.CANDIDATE_ID || 'rc-a90d1009-d6ba-4e3c-9bcd-fb155f39fed4';
const LAUNCH_ID = process.env.LAUNCH_ID || 'launch-approval-36ab457f-cf1d-44c2-ae37-2f4aac33b7dc';

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
  console.log('===============================================================');
  console.log('  BLESSING AI - GATE 6: MAINNET AUTONOMOUS CONTINUATION');
  console.log('===============================================================');
  console.log(`Control Plane: ${CONTROL_PLANE_URL}`);
  console.log(`Launch ID:     ${LAUNCH_ID}`);
  console.log(`Candidate ID:  ${CANDIDATE_ID}`);

  // Step 1: Release Controller Token & Continuation Readiness Check
  console.log('\n--- Step 1: Generating Release Controller OIDC Token ---');
  const controllerToken = await getReleaseControllerIdToken();
  console.log('Release Controller OIDC token acquired.');

  console.log('\n--- Step 2: Probing Continuation Readiness ---');
  const readinessRes = await fetch(`${CONTROL_PLANE_URL}/internal/release/continuation-readiness`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${controllerToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ launchId: LAUNCH_ID }),
  });

  const readinessText = await readinessRes.text();
  console.log(`Readiness Status: ${readinessRes.status}`);
  if (!readinessRes.ok) {
    throw new Error(`Continuation readiness rejected: ${readinessRes.status} ${readinessText}`);
  }

  const readiness = JSON.parse(readinessText);
  console.log('Continuation Readiness Evidence:');
  console.log(`- continuationReady: ${readiness.continuationReady}`);
  console.log(`- evidence_status:   ${readiness.evidence_status}`);
  console.log(`- executionMode:     ${readiness.executionMode}`);
  console.log(`- launchState:       ${readiness.launchState}`);
  console.log(`- engineState:       ${readiness.engineState}`);
  console.log(`- submittedOrders:   ${readiness.submittedOrders}`);
  console.log(`- secretVersions:    ${JSON.stringify(readiness.secretVersions)}`);

  if (readiness.evidence_status !== 'VERIFIED' || readiness.continuationReady !== true) {
    throw new Error(`Continuation readiness is not verified: ${readinessText}`);
  }
  if (readiness.executionMode !== 'LIVE') {
    throw new Error(`Execution mode must be LIVE: ${readiness.executionMode}`);
  }
  if (!['PAUSED_NEW_RISK', 'DISARMED'].includes(readiness.engineState)) {
    throw new Error(`Engine state must be PAUSED_NEW_RISK or DISARMED: ${readiness.engineState}`);
  }
  if (Number(readiness.submittedOrders) < 1) {
    throw new Error(`Submitted orders must be >= 1 for continuation: ${readiness.submittedOrders}`);
  }
  console.log('>>> Continuation Readiness VERIFIED! <<<');

  // Step 3: Mint trading_admin Firebase Token
  console.log('\n--- Step 3: Minting Verified trading_admin Token ---');
  const { idToken: adminToken, decoded } = await getTradingAdminIdToken();
  console.log(`Authenticated as UID=${decoded.uid}, role=${decoded.role}`);

  // Step 4: Approve Continuation
  console.log('\n--- Step 4: Requesting Continuation Approval ---');
  const approveRes = await fetch(`${CONTROL_PLANE_URL}/api/release/mainnet/continuation/approve`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${adminToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      candidateId: CANDIDATE_ID,
      launchId: LAUNCH_ID,
    }),
  });

  const approveText = await approveRes.text();
  console.log(`Approval Status: ${approveRes.status}`);
  if (!approveRes.ok) {
    throw new Error(`Continuation approval failed: ${approveRes.status} ${approveText}`);
  }

  const approveData = JSON.parse(approveText);
  console.log(`Continuation Approval Issued: ${approveData.continuationId}`);
  console.log(JSON.stringify(approveData, null, 2));

  // Step 5: Execute Autonomous Continuation
  console.log('\n--- Step 5: Triggering Autonomous Continuation via Control Plane ---');
  const continuePayload = {
    executionMode: 'LIVE',
    riskProfile: 'CONSERVATIVE',
    instruments: ['ETHUSDC'],
    strategies: { grid: true, trend: false, shock: false, carry: false },
    enforcePreflight: true,
    continuationApprovalId: approveData.continuationId,
    launchId: LAUNCH_ID,
  };

  const continueRes = await fetch(`${CONTROL_PLANE_URL}/api/system/continue`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${adminToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(continuePayload),
  });

  const continueText = await continueRes.text();
  console.log(`Continue Response Status: ${continueRes.status}`);
  if (!continueRes.ok) {
    throw new Error(`Autonomous continuation failed: ${continueRes.status} ${continueText}`);
  }

  const continueResult = JSON.parse(continueText);
  console.log('\n>>> AUTONOMOUS CONTINUATION ACTIVATED! <<<');
  console.log(JSON.stringify(continueResult, null, 2));

  // Step 6: Verify Live Autonomous State
  console.log('\n--- Step 6: Verifying Active Autonomous Runtime State ---');
  const stateRes = await fetch(`${CONTROL_PLANE_URL}/api/system/state`, {
    headers: { 'Authorization': `Bearer ${adminToken}` },
  });
  if (stateRes.ok) {
    const finalState = await stateRes.json();
    console.log(`Engine State:       ${finalState.engineState}`);
    console.log(`Launch State:       ${finalState.mainnetLaunchState}`);
    console.log(`Pause New Risk:     ${finalState.pauseNewRisk}`);
    console.log(`Orders Submitted:   ${finalState.orderSubmissionAttempts ?? finalState.workerState?.order_submission_attempts}`);
    console.log(`Reconciliation:     ${finalState.reconciliationStatus}`);
  }
}

main().catch((err) => {
  console.error('\n[FATAL ERROR]', err);
  process.exit(1);
});
