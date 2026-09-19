import { getTradingAdminIdToken } from './mint_trading_admin_token.mjs';

const CONTROL_PLANE_URL = 'https://blessing-control-plane-hrybwxl4ra-as.a.run.app';

async function main() {
  console.log('--- Minting trading_admin token ---');
  const { idToken, decoded } = await getTradingAdminIdToken();
  console.log(`Authenticated as UID=${decoded.uid}, role=${decoded.role}`);

  console.log('\n--- Engaging kill switch (active=true) ---');
  const res = await fetch(`${CONTROL_PLANE_URL}/api/quant/risk/kill-switch`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${idToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ active: true }),
  });

  const text = await res.text();
  if (!res.ok) {
    throw new Error(`Kill switch request failed: ${res.status} ${text}`);
  }
  console.log('\n--- Kill switch response ---');
  console.log(text);
}

main().catch((err) => {
  console.error('Fatal error:', err);
  process.exit(1);
});
