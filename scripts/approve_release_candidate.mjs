import { getTradingAdminIdToken } from './mint_trading_admin_token.mjs';

const CONTROL_PLANE_URL = process.env.CONTROL_PLANE_URL || 'https://blessing-control-plane-hrybwxl4ra-as.a.run.app';
const candidateId = process.argv[2] || process.env.CANDIDATE_ID || 'rc-4c2542f0-1108-4294-b779-d1d7246f642f';

async function approve() {
  console.log(`Minting trading_admin ID token...`);
  const { idToken, decoded } = await getTradingAdminIdToken();
  console.log(`Token obtained for UID: ${decoded.uid}, role: ${decoded.role}`);

  const url = `${CONTROL_PLANE_URL}/api/release/mainnet/approve`;
  console.log(`Submitting approval to: ${url}`);
  console.log(`Candidate ID: ${candidateId}`);

  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${idToken}`,
    },
    body: JSON.stringify({ candidateId }),
  });

  const body = await res.text();
  console.log(`Response Status: ${res.status}`);
  try {
    const json = JSON.parse(body);
    console.log(`Response JSON:`, JSON.stringify(json, null, 2));
  } catch {
    console.log(`Response Body:`, body);
  }
}

approve().catch((err) => {
  console.error('Approval failed with error:', err);
  process.exit(1);
});
