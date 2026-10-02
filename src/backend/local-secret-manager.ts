import { GoogleAuth } from 'google-auth-library';

const SECRET_IDS = {
  apiKey: 'blessing-binance-mainnet-api-key',
  apiSecret: 'blessing-binance-mainnet-api-secret',
} as const;

export function localSecretCrc32c(raw: Uint8Array): number {
  let checksum = 0xffffffff;
  for (const byte of raw) {
    checksum ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      checksum = (checksum >>> 1) ^ ((checksum & 1) ? 0x82f63b78 : 0);
    }
  }
  return (checksum ^ 0xffffffff) >>> 0;
}

export function decodeVerifiedLocalSecretPayload(payload: unknown): string {
  if (!payload || typeof payload !== 'object') throw new Error('LOCAL_SECRET_MANAGER_RESPONSE_INVALID');
  const { data, dataCrc32c } = payload as { data?: unknown; dataCrc32c?: unknown };
  if (typeof data !== 'string' || !/^[A-Za-z0-9+/]*={0,2}$/.test(data)
    || data.length > 8_192 || typeof dataCrc32c !== 'string' || !/^(0|[1-9][0-9]*)$/.test(dataCrc32c)) {
    throw new Error('LOCAL_SECRET_MANAGER_RESPONSE_INVALID');
  }
  const raw = Buffer.from(data, 'base64');
  try {
    if (raw.toString('base64') !== data || Number(dataCrc32c) !== localSecretCrc32c(raw)) {
      throw new Error('LOCAL_SECRET_MANAGER_RESPONSE_INVALID');
    }
    if (raw.length >= 3 && raw[0] === 0xef && raw[1] === 0xbb && raw[2] === 0xbf) {
      throw new Error('LOCAL_SECRET_MANAGER_RESPONSE_INVALID');
    }
    const value = new TextDecoder('utf-8', { fatal: true }).decode(raw);
    if (!value.trim() || /[\r\n]/.test(value) || value.includes('\u0000')) {
      throw new Error('LOCAL_SECRET_MANAGER_RESPONSE_INVALID');
    }
    return value;
  } catch {
    throw new Error('LOCAL_SECRET_MANAGER_RESPONSE_INVALID');
  } finally {
    raw.fill(0);
  }
}

export interface LocalSecretSourceIdentity {
  secretManagerProjectId: string;
  apiKeySecretVersionResource: string;
  apiSecretSecretVersionResource: string;
}

export function localSecretSourceIdentity(
  projectId: string,
  apiKeyVersion: string,
  apiSecretVersion: string,
): LocalSecretSourceIdentity {
  if (!/^[a-z][a-z0-9-]{4,28}[a-z0-9]$/.test(projectId)) {
    throw new Error('LOCAL_SECRET_MANAGER_PROJECT_INVALID');
  }
  if (!/^[1-9][0-9]*$/.test(apiKeyVersion) || !/^[1-9][0-9]*$/.test(apiSecretVersion)) {
    throw new Error('LOCAL_RELEASE_SECRET_VERSIONS_INVALID');
  }
  return {
    secretManagerProjectId: projectId,
    apiKeySecretVersionResource: `projects/${projectId}/secrets/${SECRET_IDS.apiKey}/versions/${apiKeyVersion}`,
    apiSecretSecretVersionResource: `projects/${projectId}/secrets/${SECRET_IDS.apiSecret}/versions/${apiSecretVersion}`,
  };
}

export interface LocalMainnetSecrets {
  apiKey: string;
  apiSecret: string;
  apiKeyVersion: string;
  apiSecretVersion: string;
}

export interface ApprovedLocalPilotSecretBinding {
  campaignId: string;
  status: 'APPROVED' | 'ACTIVE';
  adminUid: string;
  approvedByUid: string;
  campaignExpiresAt: string;
  secretManagerProjectId: string;
  apiKeyVersion: string;
  apiSecretVersion: string;
}

export async function accessPinnedLocalMainnetSecrets(options: {
  projectId: string;
  apiKeyVersion: string;
  apiSecretVersion: string;
  identity: LocalSecretSourceIdentity;
  campaign: ApprovedLocalPilotSecretBinding;
  auth?: GoogleAuth;
  fetcher?: typeof fetch;
}): Promise<LocalMainnetSecrets> {
  const identity = localSecretSourceIdentity(options.projectId, options.apiKeyVersion, options.apiSecretVersion);
  if (identity.secretManagerProjectId !== options.identity.secretManagerProjectId
    || identity.apiKeySecretVersionResource !== options.identity.apiKeySecretVersionResource
    || identity.apiSecretSecretVersionResource !== options.identity.apiSecretSecretVersionResource) {
    throw new Error('LOCAL_SECRET_SOURCE_BINDING_MISMATCH');
  }
  const campaign = options.campaign;
  if (!/^pilot-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$/.test(campaign.campaignId)
    || !['APPROVED', 'ACTIVE'].includes(campaign.status)
    || !campaign.adminUid.trim()
    || campaign.approvedByUid !== campaign.adminUid
    || !Number.isFinite(Date.parse(campaign.campaignExpiresAt))
    || Date.parse(campaign.campaignExpiresAt) <= Date.now()
    || campaign.secretManagerProjectId !== options.projectId
    || campaign.apiKeyVersion !== options.apiKeyVersion
    || campaign.apiSecretVersion !== options.apiSecretVersion) {
    throw new Error('LOCAL_PILOT_SECRET_AUTHORIZATION_INVALID');
  }
  const auth = options.auth || new GoogleAuth({ scopes: ['https://www.googleapis.com/auth/cloud-platform'] });
  const token = await auth.getAccessToken();
  if (!token) throw new Error('LOCAL_SECRET_MANAGER_AUTH_UNAVAILABLE');
  const fetcher = options.fetcher || fetch;

  const read = async (resource: string): Promise<string> => {
    const url = `https://secretmanager.googleapis.com/v1/${resource}:access`;
    let response: Response;
    try {
      response = await fetcher(url, {
        method: 'GET',
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(15_000),
      });
    } catch {
      throw new Error('LOCAL_SECRET_MANAGER_ACCESS_FAILED');
    }
    if (!response.ok) throw new Error('LOCAL_SECRET_MANAGER_ACCESS_FAILED');
    let payload: unknown;
    try {
      const body = await response.json() as { payload?: unknown };
      payload = body.payload;
    } catch {
      throw new Error('LOCAL_SECRET_MANAGER_RESPONSE_INVALID');
    }
    return decodeVerifiedLocalSecretPayload(payload);
  };

  const apiKey = await read(identity.apiKeySecretVersionResource);
  try {
    const apiSecret = await read(identity.apiSecretSecretVersionResource);
    return {
      apiKey,
      apiSecret,
      apiKeyVersion: options.apiKeyVersion,
      apiSecretVersion: options.apiSecretVersion,
    };
  } catch {
    // Do not include the returned key or Secret Manager response in an error.
    throw new Error('LOCAL_SECRET_MANAGER_ACCESS_FAILED');
  }
}
