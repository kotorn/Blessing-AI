import { getTradingAdminIdToken } from './mint_trading_admin_token.mjs';

const CONTROL_PLANE_URL = process.env.CONTROL_PLANE_URL || 'https://blessing-control-plane-hrybwxl4ra-as.a.run.app';

async function main() {
  const { idToken, decoded } = await getTradingAdminIdToken();
  console.log(`[AUTH] Authenticated as UID=${decoded.uid}, role=${decoded.role}`);

  const stateRes = await fetch(`${CONTROL_PLANE_URL}/api/system/state`, {
    headers: { 'Authorization': `Bearer ${idToken}` },
  });

  if (!stateRes.ok) {
    console.error(`Failed to fetch state: ${stateRes.status} ${await stateRes.text()}`);
    return;
  }

  const state = await stateRes.json();
  console.log('\n=== CURRENT CONTROL PLANE / SYSTEM STATE ===');
  console.log(JSON.stringify(state, null, 2));

  const healthRes = await fetch(`${CONTROL_PLANE_URL}/api/monitoring/health`, {
    headers: { 'Authorization': `Bearer ${idToken}` },
  });
  if (healthRes.ok) {
    const health = await healthRes.json();
    console.log('\n=== MONITORING HEALTH ===');
    console.log(JSON.stringify(health, null, 2));
  }
}

main().catch(console.error);
