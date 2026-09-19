import { getTradingAdminIdToken } from './mint_trading_admin_token.mjs';

const CONTROL_PLANE_URL = 'https://blessing-control-plane-hrybwxl4ra-as.a.run.app';
const APPROVAL_ID = 'approval-f33f4d55-daaf-46b5-881a-a54d68633228';

async function main() {
  console.log('--- Minting verified trading_admin Firebase token ---');
  const { idToken, decoded } = await getTradingAdminIdToken();
  console.log(`Authenticated as UID=${decoded.uid}, role=${decoded.role}`);

  console.log('\n--- Sending STAGED_FIRST_ORDER LIVE ARM to Control Plane ---');
  const armPayload = {
    executionMode: 'LIVE',
    riskProfile: 'CONSERVATIVE',
    instruments: ['ETHUSDC'],
    strategies: { grid: true, trend: false, shock: false, carry: false },
    enforcePreflight: true,
    releaseApprovalId: APPROVAL_ID,
    launchPolicy: 'STAGED_FIRST_ORDER',
  };

  const armRes = await fetch(`${CONTROL_PLANE_URL}/api/system/arm`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${idToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(armPayload),
  });

  const armText = await armRes.text();
  console.log(`ARM Response Status: ${armRes.status}`);
  console.log(`ARM Response Body: ${armText}`);

  if (!armRes.ok) {
    throw new Error(`ARM request rejected: ${armRes.status} ${armText}`);
  }

  console.log('\n--- ARM Accepted! Monitoring worker state for staged first order execution ---');
  // Poll state for up to 120 seconds to observe order execution & auto-pause
  for (let i = 0; i < 60; i++) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    const stateRes = await fetch(`${CONTROL_PLANE_URL}/api/system/state`, {
      headers: { 'Authorization': `Bearer ${idToken}` },
    });
    if (!stateRes.ok) {
      console.log(`[${i * 2}s] Failed to fetch state: ${stateRes.status}`);
      continue;
    }
    const state = await stateRes.json();
    const workerState = state.workerState || {};
    console.log(`[${i * 2}s] engineState=${state.engineState} launchState=${workerState.mainnet_launch_state || state.mainnetLaunchState} pauseNewRisk=${state.pauseNewRisk} orders=${workerState.order_submission_attempts ?? state.orderSubmissionAttempts}`);
    
    if (
      workerState.order_submission_attempts > 0 ||
      state.pauseNewRisk === true ||
      workerState.mainnet_launch_state === 'PAUSED_NEW_RISK' ||
      state.engineState === 'PAUSED'
    ) {
      console.log('\n>>> STAGED FIRST ORDER EXECUTION COMPLETED! <<<');
      console.log(JSON.stringify(workerState, null, 2));
      return;
    }
  }
}

main().catch((err) => {
  console.error('Fatal error during staged ARM execution:', err);
  process.exit(1);
});
