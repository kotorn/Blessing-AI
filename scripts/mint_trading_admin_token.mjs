import { initializeApp, applicationDefault } from 'firebase-admin/app';
import { getAuth } from 'firebase-admin/auth';
import { readFileSync } from 'fs';
import { resolve } from 'path';

export async function getTradingAdminIdToken() {
  const app = initializeApp({
    credential: applicationDefault(),
    serviceAccountId: 'blessing-control-plane@gen-lang-client-0730128480.iam.gserviceaccount.com',
    projectId: 'gen-lang-client-0730128480',
  });
  const auth = getAuth(app);
  const uid = 'trading-admin-operator';
  
  // Also ensure custom user claims are set on the UID
  try {
    await auth.getUser(uid);
  } catch (e) {
    if (e.code === 'auth/user-not-found') {
      await auth.createUser({ uid, email: 'trading-admin@blessing.ai' });
    }
  }
  await auth.setCustomUserClaims(uid, { role: 'trading_admin', admin: true });

  const customToken = await auth.createCustomToken(uid, { role: 'trading_admin', admin: true });

  const configPath = resolve(process.cwd(), 'firebase-applet-config.json');
  const config = JSON.parse(readFileSync(configPath, 'utf8'));
  const apiKey = config.apiKey;

  const url = `https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key=${apiKey}`;
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: customToken, returnSecureToken: true }),
  });

  const data = await res.json();
  if (!res.ok) {
    throw new Error(`Exchange failed: ${JSON.stringify(data)}`);
  }

  const decoded = await auth.verifyIdToken(data.idToken);
  return { idToken: data.idToken, decoded };
}

if (process.argv[1].endsWith('mint_trading_admin_token.mjs')) {
  try {
    const { idToken, decoded } = await getTradingAdminIdToken();
    console.log(JSON.stringify({
      success: true,
      uid: decoded.uid,
      role: decoded.role,
      admin: decoded.admin,
      tokenLength: idToken.length,
    }));
  } catch (err) {
    console.error('Error minting token:', err);
    process.exit(1);
  }
}
