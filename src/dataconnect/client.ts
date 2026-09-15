/**
 * Feature-flagged Firebase SQL Connect client boundary.
 *
 * Firestore remains the default until a generated SDK, migration verification,
 * and two-user ownership tests have passed. When cutover is enabled, callers
 * use SQL Connect exclusively; this module never dual-writes authoritative
 * baskets or risk settings.
 */

const GENERATED_SDK_PACKAGE = '@dataconnect/generated';
const clientEnv = (import.meta as ImportMeta & {
  env?: Record<string, string | undefined>;
}).env || {};

export function isDataConnectCutoverEnabled(): boolean {
  const enabled = String(clientEnv.VITE_DATA_CONNECT_CUTOVER || '').toLowerCase() === 'true';
  const rollback = String(clientEnv.VITE_DATA_CONNECT_ROLLBACK || '').toLowerCase() === 'true';
  return enabled && !rollback;
}

type GeneratedSdk = Record<string, unknown>;
let sdkPromise: Promise<GeneratedSdk> | null = null;

async function generatedSdk(): Promise<GeneratedSdk> {
  if (!isDataConnectCutoverEnabled()) {
    throw new Error('Firebase SQL Connect cutover is disabled');
  }
  if (!sdkPromise) {
    sdkPromise = import(/* @vite-ignore */ GENERATED_SDK_PACKAGE) as Promise<GeneratedSdk>;
  }
  return sdkPromise;
}

async function invokeGenerated(operation: string, variables: Record<string, unknown>): Promise<unknown> {
  const sdk = await generatedSdk();
  const candidate = sdk[operation];
  if (typeof candidate !== 'function') {
    throw new Error(`Generated SQL Connect operation ${operation} is unavailable`);
  }
  return (candidate as (input: Record<string, unknown>) => Promise<unknown>)(variables);
}

function requiredBasketId(basket: Record<string, unknown>): string | null {
  const value = String(basket.basket_id || basket.basketId || '').trim();
  if (!value) return null;
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
    throw new Error('SQL Connect basket ids must be UUIDs during the guarded cutover');
  }
  return value;
}

function basketVariables(basket: Record<string, unknown>) {
  return {
    name: String(basket.name || basket.basket_id || basket.basketId || 'Trading basket'),
    symbol: String(basket.instrument || basket.symbol || '').toUpperCase(),
    enabled: basket.enabled !== false && String(basket.state || '').toUpperCase() !== 'CLOSED',
    configuration: JSON.stringify({
      direction: basket.direction,
      state: basket.state,
      gridDepth: basket.grid_depth ?? basket.gridDepth ?? 0,
      maxGridLevels: basket.max_grid_levels ?? basket.maxGridLevels ?? 5,
      totalSize: basket.total_size ?? basket.totalSize ?? 0,
      averageEntry: basket.average_entry ?? basket.averageEntry ?? 0,
      currentMarkPrice: basket.current_mark_price ?? basket.currentMarkPrice ?? 0,
      netPnl: basket.net_pnl ?? basket.netPnl ?? 0,
    }),
  };
}

/** Return true when SQL Connect handled the write; false means Firestore is authoritative. */
export async function saveCloudBasketWithDataConnect(basket: Record<string, unknown>): Promise<boolean> {
  if (!isDataConnectCutoverEnabled()) return false;
  const id = requiredBasketId(basket);
  const variables = basketVariables(basket);
  if (id) {
    await invokeGenerated('updateMyBasket', { id, ...variables });
  } else {
    await invokeGenerated('createMyBasket', variables);
  }
  return true;
}

/** Return true when SQL Connect handled the write; false means Firestore is authoritative. */
export async function saveRiskSettingsWithDataConnect(settings: {
  maxPortfolioLeverage: number;
  emergencyDrawdownPct: number;
  marginStressThresholdPct?: number;
}): Promise<boolean> {
  if (!isDataConnectCutoverEnabled()) return false;
  await invokeGenerated('upsertMyRiskSettings', {
    riskProfile: 'BALANCED',
    maxPortfolioDrawdownPct: settings.emergencyDrawdownPct,
    maxGrossLeverage: settings.maxPortfolioLeverage,
    maxMarginUtilizationPct: settings.marginStressThresholdPct ?? 30,
  });
  return true;
}
