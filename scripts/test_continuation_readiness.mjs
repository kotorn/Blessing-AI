import { execSync } from 'child_process';

const CONTROL_PLANE_URL = 'https://blessing-control-plane-hrybwxl4ra-as.a.run.app';
const LAUNCH_ID = 'launch-approval-36ab457f-cf1d-44c2-ae37-2f4aac33b7dc';

async function main() {
  const token = execSync('gcloud auth print-access-token').toString().trim();
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
  const { token: idToken } = await iamRes.json();

  console.log('Got Release Controller ID token.');
  const res = await fetch(`${CONTROL_PLANE_URL}/internal/release/continuation-readiness`, {
    method: 'POST',
    headers: {
      'Authorization': `Bearer ${idToken}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      launchId: LAUNCH_ID,
    }),
  });

  console.log('Status:', res.status);
  const body = await res.json();
  console.log('Body:\n', JSON.stringify(body, null, 2));
}

main().catch(console.error);
