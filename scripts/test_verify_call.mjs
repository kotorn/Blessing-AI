import { execSync } from 'child_process';

async function main() {
  const token = execSync('gcloud auth print-access-token').toString().trim();
  const iamRes = await fetch('https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/blessing-release-controller@gen-lang-client-0730128480.iam.gserviceaccount.com:generateIdToken', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      audience: 'https://blessing-control-plane-hrybwxl4ra-as.a.run.app',
      includeEmail: true,
    }),
  });
  const { token: idToken } = await iamRes.json();

  console.log('Got ID token length:', idToken?.length);
  const verifyRes = await fetch('https://blessing-control-plane-hrybwxl4ra-as.a.run.app/internal/release/verify', {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${idToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      candidateId: 'rc-4c2542f0-1108-4294-b779-d1d7246f642f',
    }),
  });

  const text = await verifyRes.text();
  console.log('Status:', verifyRes.status);
  console.log('Body:', text);
}

main().catch(console.error);
