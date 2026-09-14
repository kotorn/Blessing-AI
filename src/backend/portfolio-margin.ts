export type PortfolioMarginBalance = {
  asset: string;
  totalWalletBalance: number;
  crossMarginAsset: number;
  crossMarginFree: number;
};

export type PortfolioMarginObservation = {
  environment: 'BINANCE_TESTNET';
  status: 'UNAVAILABLE' | 'OBSERVED_READ_ONLY' | 'INVALID_RESPONSE';
  verified: false;
  includedInWorkerCollateral: false;
  balances: PortfolioMarginBalance[];
  message?: string;
};

export function unavailablePortfolioMarginObservation(
  message = 'Portfolio Margin is not an execution account for the USDⓈ-M Testnet worker.',
): PortfolioMarginObservation {
  return {
    environment: 'BINANCE_TESTNET',
    status: 'UNAVAILABLE',
    verified: false,
    includedInWorkerCollateral: false,
    balances: [],
    message,
  };
}

function requiredNumber(value: unknown, field: string): number {
  if (value === undefined || value === null || value === '') {
    throw new Error(`Portfolio Margin response is missing ${field}`);
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) {
    throw new Error(`Portfolio Margin response has invalid ${field}`);
  }
  return parsed;
}

export function parsePortfolioMarginResponse(raw: unknown): PortfolioMarginObservation {
  if (!Array.isArray(raw)) {
    return {
      ...unavailablePortfolioMarginObservation(
        'Portfolio Margin response is not an asset array.',
      ),
      status: 'INVALID_RESPONSE',
    };
  }

  try {
    const balances = raw.map((item: any): PortfolioMarginBalance => {
      if (!item || typeof item.asset !== 'string' || !item.asset.trim()) {
        throw new Error('Portfolio Margin response is missing asset');
      }
      return {
        asset: item.asset.trim().toUpperCase(),
        totalWalletBalance: requiredNumber(item.totalWalletBalance, 'totalWalletBalance'),
        crossMarginAsset: requiredNumber(item.crossMarginAsset, 'crossMarginAsset'),
        crossMarginFree: requiredNumber(item.crossMarginFree, 'crossMarginFree'),
      };
    });

    return {
      environment: 'BINANCE_TESTNET',
      status: 'OBSERVED_READ_ONLY',
      verified: false,
      includedInWorkerCollateral: false,
      balances,
      message:
        'Portfolio Margin balance observed read-only; not accepted as USDⓈ-M Worker collateral or readiness evidence.',
    };
  } catch (error: any) {
    return {
      ...unavailablePortfolioMarginObservation(
        error?.message || 'Portfolio Margin response could not be verified.',
      ),
      status: 'INVALID_RESPONSE',
    };
  }
}
