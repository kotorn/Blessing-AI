/**
 * Binance verification service — extracted from server.ts.
 *
 * Contains the internals of `verifyBinanceCredentials` and
 * `fetchBinanceLiveBalances`: HMAC request signing, the Binance REST probes
 * (spot, USDⓈ-M futures, cross/isolated margin, Portfolio Margin, Simple Earn),
 * and the pure response parsing/valuation logic.  server.ts keeps the
 * original wrapper names and delegates here, so route handlers, startup
 * orchestration, and call sites are unchanged.
 *
 * Read-only capability probes only: nothing in this module authorizes the
 * worker or mutates engine state.
 */
import crypto from 'crypto';
import {
  parsePortfolioMarginResponse,
  unavailablePortfolioMarginObservation,
} from './portfolio-margin.js';

export async function verifyBinanceCredentialsInternal(apiKey: string, apiSecret: string, isTestnet: boolean) {
  const environment = isTestnet ? 'TESTNET' : 'MAINNET';
  if (!apiKey || !apiSecret) {
    return {
      configured: false,
      isTestnet,
      environment,
      message: `Binance ${environment} credentials are missing from configuration.`,
      spot: { authenticated: false, canTrade: false, message: 'No API credentials configured' },
      futures: { authenticated: false, canTrade: false, hedgeMode: false, message: 'No API credentials configured' },
      restrictions: {},
    };
  }

  const maskedKey = apiKey.length >= 8 ? `${apiKey.slice(0, 4)}...${apiKey.slice(-4)}` : '***';
  const spotBase = isTestnet ? 'https://testnet.binance.vision' : 'https://api.binance.com';
  const futuresBase = isTestnet ? 'https://testnet.binancefuture.com' : 'https://fapi.binance.com';

  const results: any = {
    configured: true,
    maskedKey,
    isTestnet,
    environment,
    spot: { authenticated: false, canTrade: false, message: '' },
    futures: { authenticated: false, canTrade: false, hedgeMode: false, message: '' },
    restrictions: {},
  };

  const sign = (secret: string, queryStr: string) => {
    return crypto.createHmac('sha256', secret).update(queryStr).digest('hex');
  };

  try {
    // 1. Check Spot Account
    const spotTs = Date.now();
    const spotQuery = `timestamp=${spotTs}`;
    const spotSig = sign(apiSecret, spotQuery);
    const spotResp = await fetch(`${spotBase}/api/v3/account?${spotQuery}&signature=${spotSig}`, {
      headers: { 'X-MBX-APIKEY': apiKey },
    });
    const spotData: any = await spotResp.json();
    if (spotResp.ok) {
      results.spot.authenticated = true;
      results.spot.canTrade = spotData.canTrade;
      results.spot.message = 'Authenticated successfully on Binance Spot.';
    } else {
      results.spot.message = spotData.msg || `HTTP ${spotResp.status}`;
    }

    // 2. Check Futures Position Mode on the selected fixed environment. This is a capability probe;
    // the Python worker remains the sole execution/readiness authority.
    const fTs = Date.now();
    const fQuery = `timestamp=${fTs}`;
    const fSig = sign(apiSecret, fQuery);
    const fResp = await fetch(`${futuresBase}/fapi/v1/positionSide/dual?${fQuery}&signature=${fSig}`, {
      headers: { 'X-MBX-APIKEY': apiKey },
    });
    const fData: any = await fResp.json();
    if (fResp.ok) {
      results.futures.authenticated = true;
      results.futures.hedgeMode = fData.dualSidePosition;
      results.futures.message = fData.dualSidePosition ? 'Hedge Mode Active' : 'One-Way Mode (Hedge Mode required)';
    } else {
      results.futures.message = fData.msg || `HTTP ${fResp.status}`;
    }

    // 3. Probe Margin Account Mode (Cross Margin / Portfolio Margin)
    results.margin = { authenticated: false, mode: 'CLASSIC', canTrade: false, message: '' };
    try {
      const marginTs = Date.now();
      const marginQuery = `timestamp=${marginTs}`;
      const marginSig = sign(apiSecret, marginQuery);
      const cmResp = await fetch(`${spotBase}/sapi/v1/margin/account?${marginQuery}&signature=${marginSig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      });
      if (cmResp.ok) {
        const cmData: any = await cmResp.json();
        results.margin.authenticated = true;
        results.margin.canTrade = cmData.borrowEnabled ?? true;
        results.margin.mode = 'CROSS_MARGIN';
        results.margin.marginLevel = cmData.marginLevel;
        results.margin.message = `Cross Margin Active (Level: ${cmData.marginLevel || 'N/A'})`;
      } else {
        const pmResp = await fetch(`${spotBase}/sapi/v1/portfolio/account?${marginQuery}&signature=${marginSig}`, {
          headers: { 'X-MBX-APIKEY': apiKey },
        });
        if (pmResp.ok) {
          results.margin.authenticated = true;
          results.margin.mode = 'PORTFOLIO_MARGIN';
          results.margin.message = 'Portfolio Margin Mode Active';
        } else {
          results.margin.message = 'Classic Mode (Spot & Futures standard)';
        }
      }
    } catch {
      results.margin.message = 'Margin mode probe not available';
    }

    return results;
  } catch (err: any) {
    return {
      configured: true,
      maskedKey,
      isTestnet,
      environment,
      spot: { authenticated: false, canTrade: false, message: err.message },
      futures: { authenticated: false, canTrade: false, hedgeMode: false, message: err.message },
      restrictions: {},
      error: err.message,
    };
  }
}

export async function fetchBinanceLiveBalancesInternal(apiKey: string, apiSecret: string, isTestnet: boolean) {
  if (!apiKey || !apiSecret) {
    return {
      success: false,
      configured: false,
      message: 'No Binance API credentials configured.',
    };
  }

  const environment = isTestnet ? 'TESTNET' : 'MAINNET';
  const spotBase = isTestnet ? 'https://testnet.binance.vision' : 'https://api.binance.com';
  const futuresBase = isTestnet ? 'https://testnet.binancefuture.com' : 'https://fapi.binance.com';
  const papiBase = isTestnet ? 'https://testnet.binancefuture.com' : 'https://papi.binance.com';

  const sign = (secret: string, queryStr: string) => {
    return crypto.createHmac('sha256', secret).update(queryStr).digest('hex');
  };

  try {
    const ts = Date.now();
    const query = `timestamp=${ts}`;
    const sig = sign(apiSecret, query);

    // Fetch in parallel: Spot Account, Prices, Wallet Balances, Cross Margin, Isolated Margin, Portfolio Margin, Simple Earn, and Futures
    let spotTotalUsd = 0;
    let spotSuccess = false;
    let spotError = '';
    const holdings: Array<{ asset: string; qty: number; unitPrice: number; usdVal: number }> = [];

    const [
      spotResp,
      tickerResp,
      walletsResp,
      cmResp,
      isoResp,
      pmResp,
      pmAccountResp,
      papiResp,
      earnFlexResp,
      earnLockedResp,
      fResp,
    ] = await Promise.all([
      fetch(`${spotBase}/api/v3/account?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch((e) => ({ ok: false, status: 500, json: async () => ({ msg: e.message }) })),
      fetch(`${spotBase}/api/v3/ticker/price`).catch(() => null),
      fetch(`${spotBase}/sapi/v1/asset/wallet/balance?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
      fetch(`${spotBase}/sapi/v1/margin/account?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
      fetch(`${spotBase}/sapi/v1/margin/isolated/account?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
      fetch(`${spotBase}/sapi/v1/portfolio/balance?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
      fetch(`${spotBase}/sapi/v1/portfolio/account?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
      fetch(`${papiBase}/papi/v1/balance?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
      fetch(`${spotBase}/sapi/v1/simple-earn/flexible/position?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
      fetch(`${spotBase}/sapi/v1/simple-earn/locked/position?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
      fetch(`${futuresBase}/fapi/v2/account?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
      }).catch(() => null),
    ]);

    // Build price map
    const priceMap: Record<string, number> = {};
    if (tickerResp && (tickerResp as any).ok) {
      const tickerList = await (tickerResp as any).json();
      if (Array.isArray(tickerList)) {
        tickerList.forEach((p: any) => {
        const price = Number(p.price);
        if (typeof p.symbol === 'string' && Number.isFinite(price) && price > 0) {
          priceMap[p.symbol] = price;
        }
        });
      }
    }
    const btcPrice = priceMap['BTCUSDT'];
    const stableCoins = new Set(['USDT', 'USDC', 'BUSD', 'FDUSD', 'USDE', 'TUSD', 'DAI']);

    // Helper to get unit price
    const getAssetPrice = (assetName: string): number => {
      const clean = assetName.startsWith('LD') ? assetName.slice(2) : assetName;
      if (stableCoins.has(clean) || stableCoins.has(assetName)) return 1.0;
      if (priceMap[`${clean}USDT`]) return priceMap[`${clean}USDT`];
      if (priceMap[`${assetName}USDT`]) return priceMap[`${assetName}USDT`];
      if ((clean === 'BTC' || assetName === 'BTC') && Number.isFinite(btcPrice) && btcPrice > 0) return btcPrice;
      return 0;
    };

    // Helper to add or update holdings
    const addOrMergeHolding = (asset: string, qty: number, unitPrice: number, usdVal: number) => {
      if (qty <= 0.0001 && usdVal <= 0.0001) return;
      const existing = holdings.find((h) => h.asset === asset);
      if (existing) {
        existing.qty = parseFloat((existing.qty + qty).toFixed(8));
        existing.usdVal = parseFloat((existing.usdVal + usdVal).toFixed(4));
        if (existing.unitPrice <= 0 && unitPrice > 0) existing.unitPrice = unitPrice;
      } else {
        holdings.push({
          asset,
          qty: parseFloat(qty.toFixed(8)),
          unitPrice: parseFloat(unitPrice.toFixed(4)),
          usdVal: parseFloat(usdVal.toFixed(4)),
        });
      }
    };

    // Parse Spot
    const spotData = await (spotResp as any).json();
    let valuationComplete = true;
    if ((spotResp as any).ok && Array.isArray(spotData.balances)) {
      spotSuccess = true;
      spotData.balances.forEach((b: any) => {
        const qty = parseFloat(b.free || '0') + parseFloat(b.locked || '0');
        if (qty <= 0) return;

        const asset = b.asset;
        const unitPrice = getAssetPrice(asset);

        if (qty > 0 && (!Number.isFinite(unitPrice) || unitPrice <= 0)) {
          valuationComplete = false;
          return;
        }

        const usdVal = qty * unitPrice;
        if (usdVal > 0.0001 || qty > 0.0001) {
          addOrMergeHolding(asset, qty, unitPrice, usdVal);
          spotTotalUsd += usdVal;
        }
      });
      holdings.sort((a, b) => b.usdVal - a.usdVal);
    } else {
      spotError = spotData?.msg || `Spot error HTTP ${(spotResp as any).status}`;
    }

    // Parse Wallets Balance (all sub-accounts)
    let walletsData: any[] = [];
    if (walletsResp && (walletsResp as any).ok) {
      try {
        walletsData = await (walletsResp as any).json();
      } catch {}
    }

    const sub_wallets: Array<{
      walletName: string;
      category: 'TRADING_BOT' | 'PORTFOLIO_MARGIN' | 'EARN' | 'SPOT' | 'FUNDING';
      btcVal: number;
      usdVal: number;
      pctOfTotal: number;
    }> = [];

    let totalWalletsUsd = 0;
    if (Array.isArray(walletsData)) {
      if (walletsData.some((wallet) => Number(wallet.balance) > 0) && (!Number.isFinite(btcPrice) || btcPrice <= 0)) {
        valuationComplete = false;
      }
      walletsData.forEach((w: any) => {
        const btc = parseFloat(w.balance || '0');
        const usdVal = Number.isFinite(btcPrice) ? parseFloat((btc * btcPrice).toFixed(2)) : 0;
        if (usdVal > 0.001 || btc > 0) {
          let category: 'TRADING_BOT' | 'PORTFOLIO_MARGIN' | 'EARN' | 'SPOT' | 'FUNDING' = 'SPOT';
          if (w.walletName.includes('Trading Bot')) category = 'TRADING_BOT';
          else if (w.walletName.includes('Cross Margin') || w.walletName.includes('Margin') || w.walletName.includes('Portfolio') || w.walletName.includes('PM')) category = 'PORTFOLIO_MARGIN';
          else if (w.walletName.includes('Earn')) category = 'EARN';
          else if (w.walletName.includes('Funding')) category = 'FUNDING';

          sub_wallets.push({
            walletName: w.walletName,
            category,
            btcVal: parseFloat(btc.toFixed(8)),
            usdVal,
            pctOfTotal: 0,
          });
          totalWalletsUsd += usdVal;
        }
      });
    }

    // ==========================================
    // PARSE MARGIN ACCOUNTS (Cross Margin, Isolated Margin, Portfolio Margin)
    // ==========================================
    let crossMarginSuccess = false;
    let crossMarginNetBtc = 0;
    let crossMarginLiabilityBtc = 0;
    let crossMarginLevel = 0;
    let crossMarginTotalNetUsd = 0;
    const crossMarginAssets: Array<{
      asset: string;
      free: number;
      locked: number;
      borrowed: number;
      interest: number;
      netAsset: number;
      unitPrice: number;
      usdVal: number;
    }> = [];

    if (cmResp && (cmResp as any).ok) {
      try {
        const cmData = await (cmResp as any).json();
        if (cmData && (Array.isArray(cmData.userAssets) || cmData.totalNetAssetOfBtc !== undefined)) {
          crossMarginSuccess = true;
          crossMarginNetBtc = parseFloat(cmData.totalNetAssetOfBtc || '0');
          crossMarginLiabilityBtc = parseFloat(cmData.totalLiabilityOfBtc || '0');
          crossMarginLevel = parseFloat(cmData.marginLevel || '0');

          if (Array.isArray(cmData.userAssets)) {
            cmData.userAssets.forEach((ua: any) => {
              const free = parseFloat(ua.free || '0');
              const locked = parseFloat(ua.locked || '0');
              const borrowed = parseFloat(ua.borrowed || '0');
              const interest = parseFloat(ua.interest || '0');
              let net = parseFloat(ua.netAsset || '0');
              if (net === 0 && (free > 0 || locked > 0)) {
                net = Math.max(0, free + locked - borrowed - interest);
              }

              if (free > 0 || locked > 0 || borrowed > 0 || net > 0) {
                const asset = ua.asset;
                const unitPrice = getAssetPrice(asset);
                const usdVal = net > 0 && unitPrice > 0 ? net * unitPrice : 0;
                crossMarginTotalNetUsd += usdVal;

                crossMarginAssets.push({
                  asset,
                  free,
                  locked,
                  borrowed,
                  interest,
                  netAsset: net,
                  unitPrice,
                  usdVal,
                });
              }
            });
          }

          if (crossMarginTotalNetUsd <= 0 && crossMarginNetBtc > 0 && Number.isFinite(btcPrice) && btcPrice > 0) {
            crossMarginTotalNetUsd = crossMarginNetBtc * btcPrice;
          }
        }
      } catch {}
    }

    // Parse Isolated Margin Account
    let isoMarginSuccess = false;
    let isoMarginNetBtc = 0;
    let isoMarginTotalNetUsd = 0;
    const isoMarginAssets: Array<{
      asset: string;
      netAsset: number;
      symbol: string;
      unitPrice: number;
      usdVal: number;
    }> = [];

    if (isoResp && (isoResp as any).ok) {
      try {
        const isoData = await (isoResp as any).json();
        if (isoData && (Array.isArray(isoData.assets) || isoData.totalNetAssetOfBtc !== undefined)) {
          isoMarginSuccess = true;
          isoMarginNetBtc = parseFloat(isoData.totalNetAssetOfBtc || '0');
          if (Array.isArray(isoData.assets)) {
            isoData.assets.forEach((pair: any) => {
              const baseNet = parseFloat(pair.baseAsset?.netAsset || '0');
              const quoteNet = parseFloat(pair.quoteAsset?.netAsset || '0');
              const symbol = pair.symbol || '';

              if (baseNet > 0) {
                const a = pair.baseAsset.asset;
                const unitPrice = getAssetPrice(a);
                const usdVal = baseNet * (unitPrice || 1);
                isoMarginTotalNetUsd += usdVal;
                isoMarginAssets.push({ asset: a, netAsset: baseNet, symbol, unitPrice, usdVal });
              }
              if (quoteNet > 0) {
                const a = pair.quoteAsset.asset;
                const unitPrice = getAssetPrice(a);
                const usdVal = quoteNet * (unitPrice || 1);
                isoMarginTotalNetUsd += usdVal;
                isoMarginAssets.push({ asset: a, netAsset: quoteNet, symbol, unitPrice, usdVal });
              }
            });
          }
          if (isoMarginTotalNetUsd <= 0 && isoMarginNetBtc > 0 && Number.isFinite(btcPrice) && btcPrice > 0) {
            isoMarginTotalNetUsd = isoMarginNetBtc * btcPrice;
          }
        }
      } catch {}
    }

    // Parse Portfolio Margin (PM Mode)
    let pmSuccess = false;
    let pmActualEquityUsd = 0;
    let pmTotalNetUsd = 0;
    const pmBalances: Array<{
      asset: string;
      walletBalance: number;
      crossMarginFree: number;
      unitPrice: number;
      usdVal: number;
    }> = [];

    if (pmAccountResp && (pmAccountResp as any).ok) {
      try {
        const pma = await (pmAccountResp as any).json();
        if (pma && (pma.actualEquity !== undefined || pma.totalEquity !== undefined)) {
          pmSuccess = true;
          pmActualEquityUsd = parseFloat(pma.actualEquity || pma.totalEquity || '0');
        }
      } catch {}
    }

    const processPmList = (list: any[]) => {
      if (!Array.isArray(list)) return;
      list.forEach((item: any) => {
        const wb = parseFloat(item.totalWalletBalance || item.crossMarginAsset || '0');
        const free = parseFloat(item.crossMarginFree || '0');
        if (wb > 0 || free > 0) {
          pmSuccess = true;
          const asset = item.asset;
          const unitPrice = getAssetPrice(asset);
          const usdVal = wb * (unitPrice || 1);
          pmTotalNetUsd += usdVal;
          if (!pmBalances.some((b) => b.asset === asset)) {
            pmBalances.push({
              asset,
              walletBalance: wb,
              crossMarginFree: free,
              unitPrice,
              usdVal,
            });
          }
        }
      });
    };

    // Portfolio Margin is a separate account product from the USDⓈ-M
    // Futures Testnet worker. Keep this as an explicitly read-only
    // observation: it can never authorize the worker or be silently treated
    // as Futures collateral.
    let portfolioMarginObservation = unavailablePortfolioMarginObservation();

    if (pmResp && (pmResp as any).ok) {
      try {
        const rawPmData = await (pmResp as any).json();
        portfolioMarginObservation = parsePortfolioMarginResponse(rawPmData);
        processPmList(rawPmData);
      } catch (err: any) {
        portfolioMarginObservation = {
          ...unavailablePortfolioMarginObservation(
            err?.message || 'Portfolio Margin response could not be read.',
          ),
          status: 'INVALID_RESPONSE',
        };
      }
    } else if (pmResp) {
      portfolioMarginObservation.message =
        `Portfolio Margin read-only endpoint unavailable (HTTP ${(pmResp as any).status}).`;
    }

    if (papiResp && (papiResp as any).ok) {
      try {
        const papiList = await (papiResp as any).json();
        processPmList(papiList);
      } catch {}
    }

    // Determine Margin Mode
    let marginMode: 'CROSS_MARGIN' | 'ISOLATED_MARGIN' | 'PORTFOLIO_MARGIN' | 'CLASSIC' | 'NONE' = 'CLASSIC';
    if (pmSuccess && (pmBalances.length > 0 || pmActualEquityUsd > 0)) {
      marginMode = 'PORTFOLIO_MARGIN';
    } else if (crossMarginSuccess && (crossMarginAssets.length > 0 || crossMarginNetBtc > 0)) {
      marginMode = 'CROSS_MARGIN';
    } else if (isoMarginSuccess && (isoMarginAssets.length > 0 || isoMarginNetBtc > 0)) {
      marginMode = 'ISOLATED_MARGIN';
    }

    const totalMarginNetUsd = parseFloat((crossMarginTotalNetUsd + isoMarginTotalNetUsd + (pmActualEquityUsd > 0 ? pmActualEquityUsd : pmTotalNetUsd)).toFixed(2));

    // Ensure sub_wallets include Margin components if non-zero
    if (crossMarginSuccess && (crossMarginTotalNetUsd > 0.001 || crossMarginNetBtc > 0)) {
      const hasCm = sub_wallets.some((w) => w.walletName.toLowerCase().includes('cross margin'));
      if (!hasCm) {
        const usdVal = parseFloat(crossMarginTotalNetUsd.toFixed(2));
        sub_wallets.push({
          walletName: 'Cross Margin Wallet',
          category: 'PORTFOLIO_MARGIN',
          btcVal: crossMarginNetBtc > 0 ? parseFloat(crossMarginNetBtc.toFixed(8)) : (Number.isFinite(btcPrice) && btcPrice > 0 ? parseFloat((crossMarginTotalNetUsd / btcPrice).toFixed(8)) : 0),
          usdVal,
          pctOfTotal: 0,
        });
        totalWalletsUsd += usdVal;
      }
    }

    if (isoMarginSuccess && (isoMarginTotalNetUsd > 0.001 || isoMarginNetBtc > 0)) {
      const hasIso = sub_wallets.some((w) => w.walletName.toLowerCase().includes('isolated margin'));
      if (!hasIso) {
        const usdVal = parseFloat(isoMarginTotalNetUsd.toFixed(2));
        sub_wallets.push({
          walletName: 'Isolated Margin Wallet',
          category: 'PORTFOLIO_MARGIN',
          btcVal: isoMarginNetBtc > 0 ? parseFloat(isoMarginNetBtc.toFixed(8)) : (Number.isFinite(btcPrice) && btcPrice > 0 ? parseFloat((isoMarginTotalNetUsd / btcPrice).toFixed(8)) : 0),
          usdVal,
          pctOfTotal: 0,
        });
        totalWalletsUsd += usdVal;
      }
    }

    if (pmSuccess && (pmTotalNetUsd > 0.001 || pmActualEquityUsd > 0.001)) {
      const pmVal = pmActualEquityUsd > 0 ? pmActualEquityUsd : pmTotalNetUsd;
      const hasPm = sub_wallets.some((w) => w.walletName.toLowerCase().includes('portfolio') || w.walletName.toLowerCase().includes('pm'));
      if (!hasPm) {
        const usdVal = parseFloat(pmVal.toFixed(2));
        sub_wallets.push({
          walletName: 'Portfolio Margin (Unified)',
          category: 'PORTFOLIO_MARGIN',
          btcVal: Number.isFinite(btcPrice) && btcPrice > 0 ? parseFloat((pmVal / btcPrice).toFixed(8)) : 0,
          usdVal,
          pctOfTotal: 0,
        });
        totalWalletsUsd += usdVal;
      }
    }

    sub_wallets.forEach((sw) => {
      sw.pctOfTotal = totalWalletsUsd > 0 ? parseFloat(((sw.usdVal / totalWalletsUsd) * 100).toFixed(1)) : 0;
    });
    sub_wallets.sort((a, b) => b.usdVal - a.usdVal);

    // Merge Margin assets into holdings if holdings empty or supplementary
    if (crossMarginAssets.length > 0) {
      crossMarginAssets.forEach((ca) => {
        if (ca.netAsset > 0) {
          addOrMergeHolding(ca.asset, ca.netAsset, ca.unitPrice, ca.usdVal);
        }
      });
    }

    if (isoMarginAssets.length > 0) {
      isoMarginAssets.forEach((ia) => {
        if (ia.netAsset > 0) {
          addOrMergeHolding(ia.asset, ia.netAsset, ia.unitPrice, ia.usdVal);
        }
      });
    }

    if (pmBalances.length > 0) {
      pmBalances.forEach((pb) => {
        if (pb.walletBalance > 0) {
          addOrMergeHolding(pb.asset, pb.walletBalance, pb.unitPrice, pb.usdVal);
        }
      });
    }

    // Parse Flexible Earn
    let earnFlexData: any = { rows: [] };
    if (earnFlexResp && (earnFlexResp as any).ok) {
      try {
        earnFlexData = await (earnFlexResp as any).json();
      } catch {}
    }

    // Parse Locked Earn
    let earnLockedData: any = { rows: [] };
    if (earnLockedResp && (earnLockedResp as any).ok) {
      try {
        earnLockedData = await (earnLockedResp as any).json();
      } catch {}
    }

    // Parse Futures
    let futuresWalletBalance = 0;
    let futuresMarginBalance = 0;
    let futuresUnrealizedPnl = 0;
    let futuresAvailableMargin = 0;
    let futuresUsedMargin = 0;
    let futuresSuccess = false;
    let futuresError = '';
    let totalPositionNotional = 0;
    let fData: any = null;

    if (fResp && (fResp as any).ok) {
      try {
        fData = await (fResp as any).json();
        const requiredAccountFields = [
          'totalWalletBalance',
          'totalMarginBalance',
          'totalUnrealizedProfit',
          'availableBalance',
          'totalPositionInitialMargin',
        ];
        const missingAccountField = requiredAccountFields.find((field) => {
          const value = Number(fData[field]);
          return fData[field] === undefined || fData[field] === null || !Number.isFinite(value);
        });

        if (missingAccountField || !Array.isArray(fData.positions)) {
          futuresError = `Futures account snapshot missing required field: ${missingAccountField || 'positions'}`;
        } else {
          futuresSuccess = true;
          futuresWalletBalance = Number(fData.totalWalletBalance);
          futuresMarginBalance = Number(fData.totalMarginBalance);
          futuresUnrealizedPnl = Number(fData.totalUnrealizedProfit);
          futuresAvailableMargin = Number(fData.availableBalance);
          futuresUsedMargin = Number(fData.totalPositionInitialMargin);

          fData.positions.forEach((pos: any) => {
            const positionAmount = Number(pos.positionAmt);
            const notional = Number(pos.notional);
            if (!Number.isFinite(positionAmount) || !Number.isFinite(notional)) {
              if (Number.isFinite(positionAmount) && Math.abs(positionAmount) > 0) {
                futuresSuccess = false;
                futuresError = 'Futures position snapshot contains an unusable position/notional value';
              }
              return;
            }
            totalPositionNotional += Math.abs(notional);
          });
        }
      } catch (e: any) {
        futuresError = e.message;
      }
    }

    // If Futures succeeded, ensure Futures wallet is represented in sub_wallets
    if (futuresSuccess && futuresWalletBalance > 0) {
      const existingFuturesWallet = sub_wallets.find(
        (w) => w.category === 'PORTFOLIO_MARGIN' || w.walletName.toLowerCase().includes('futures')
      );
      if (!existingFuturesWallet) {
        const usdVal = parseFloat(futuresWalletBalance.toFixed(2));
        sub_wallets.push({
          walletName: 'USDⓈ-M Futures',
          category: 'PORTFOLIO_MARGIN',
          btcVal: Number.isFinite(btcPrice) && btcPrice > 0 ? parseFloat((futuresWalletBalance / btcPrice).toFixed(8)) : 0,
          usdVal,
          pctOfTotal: 0,
        });
        totalWalletsUsd += usdVal;
        sub_wallets.forEach((sw) => {
          sw.pctOfTotal = totalWalletsUsd > 0 ? parseFloat(((sw.usdVal / totalWalletsUsd) * 100).toFixed(1)) : 0;
        });
      }
    }

    // If spot and margin had no holdings, but Futures has balances, populate holdings from Futures
    if (futuresSuccess && holdings.length === 0) {
      if (Array.isArray(fData?.assets)) {
        fData.assets.forEach((fa: any) => {
          const wb = parseFloat(fa.walletBalance || '0');
          if (wb > 0.0001) {
            const cleanAsset = fa.asset;
            const unitPrice = getAssetPrice(cleanAsset);
            holdings.push({
              asset: cleanAsset,
              qty: parseFloat(wb.toFixed(8)),
              unitPrice: parseFloat(unitPrice.toFixed(4)),
              usdVal: parseFloat((wb * (unitPrice || 1)).toFixed(4)),
            });
          }
        });
      } else if (futuresWalletBalance > 0) {
        holdings.push({
          asset: 'USDT',
          qty: parseFloat(futuresWalletBalance.toFixed(8)),
          unitPrice: 1.0,
          usdVal: parseFloat(futuresWalletBalance.toFixed(4)),
        });
      }
    }

    holdings.sort((a, b) => b.usdVal - a.usdVal);

    // ==========================================
    // BUILD 2-LAYER ASSET ALLOCATION STRUCTURE
    // Layer 1: Asset / Coin
    // Layer 2: Allocations (Trading Bot, Portfolio Margin, Earn, Spot)
    // ==========================================
    const botWallet = sub_wallets.find((w) => w.category === 'TRADING_BOT');
    const botUsd = botWallet ? botWallet.usdVal : 0;

    const twoLayerMap: Record<
      string,
      {
        asset: string;
        allocations: Array<{
          location: string;
          category: 'TRADING_BOT' | 'PORTFOLIO_MARGIN' | 'EARN' | 'SPOT' | 'FUNDING';
          qty: number;
          usdVal: number;
          pctOfAsset: number;
          detail?: string;
        }>;
      }
    > = {};

    const getLayerAsset = (name: string) => {
      if (!twoLayerMap[name]) {
        twoLayerMap[name] = { asset: name, allocations: [] };
      }
      return twoLayerMap[name];
    };

    // 1. Trading Bot
    if (botUsd > 0) {
      getLayerAsset('USDC').allocations.push({
        location: 'Trading Bot',
        category: 'TRADING_BOT',
        qty: parseFloat(botUsd.toFixed(2)),
        usdVal: parseFloat(botUsd.toFixed(2)),
        pctOfAsset: 0,
        detail: 'Active Grid / Strategy Bot',
      });
    }

    // 2. Cross Margin Allocations
    if (crossMarginAssets.length > 0) {
      crossMarginAssets.forEach((ca) => {
        if (ca.netAsset > 0) {
          getLayerAsset(ca.asset).allocations.push({
            location: 'Cross Margin',
            category: 'PORTFOLIO_MARGIN',
            qty: parseFloat(ca.netAsset.toFixed(6)),
            usdVal: 0,
            pctOfAsset: 0,
            detail: crossMarginLevel > 0 ? `Cross Margin (${crossMarginLevel.toFixed(2)}x)` : 'Cross Margin Collateral',
          });
        }
      });
    }

    // 3. Isolated Margin Allocations
    if (isoMarginAssets.length > 0) {
      isoMarginAssets.forEach((ia) => {
        if (ia.netAsset > 0) {
          getLayerAsset(ia.asset).allocations.push({
            location: `Isolated Margin (${ia.symbol})`,
            category: 'PORTFOLIO_MARGIN',
            qty: parseFloat(ia.netAsset.toFixed(6)),
            usdVal: 0,
            pctOfAsset: 0,
            detail: `Isolated Margin: ${ia.symbol}`,
          });
        }
      });
    }

    // 4. Portfolio Margin (PM)
    if (portfolioMarginObservation.status === 'OBSERVED_READ_ONLY') {
      portfolioMarginObservation.balances.forEach((item) => {
        const qty = item.crossMarginAsset;
        if (qty > 0) {
          const existing = getLayerAsset(item.asset).allocations.find((a) => a.location === 'Portfolio Margin');
          if (!existing) {
            getLayerAsset(item.asset).allocations.push({
              location: 'Portfolio Margin',
              category: 'PORTFOLIO_MARGIN',
              qty: parseFloat(qty.toFixed(6)),
              usdVal: 0,
              pctOfAsset: 0,
              detail: 'Cross Margin (PM)',
            });
          }
        }
      });
    }

    if (pmBalances.length > 0) {
      pmBalances.forEach((pb) => {
        if (pb.walletBalance > 0) {
          const existing = getLayerAsset(pb.asset).allocations.find((a) => a.location.includes('Portfolio Margin'));
          if (!existing) {
            getLayerAsset(pb.asset).allocations.push({
              location: 'Portfolio Margin (Unified)',
              category: 'PORTFOLIO_MARGIN',
              qty: parseFloat(pb.walletBalance.toFixed(6)),
              usdVal: 0,
              pctOfAsset: 0,
              detail: 'Unified Portfolio Margin',
            });
          }
        }
      });
    }

    // 5. Simple Earn Flexible
    if (Array.isArray(earnFlexData?.rows)) {
      earnFlexData.rows.forEach((row: any) => {
        const qty = parseFloat(row.totalAmount || '0');
        if (qty > 0) {
          const apr = parseFloat(row.latestAnnualPercentageRate || '0') * 100;
          getLayerAsset(row.asset).allocations.push({
            location: 'Simple Earn (Flexible)',
            category: 'EARN',
            qty: parseFloat(qty.toFixed(6)),
            usdVal: 0,
            pctOfAsset: 0,
            detail: apr > 0 ? `APR ${apr.toFixed(2)}%` : 'Flexible Earn',
          });
        }
      });
    }

    // 6. Simple Earn Locked
    if (Array.isArray(earnLockedData?.rows)) {
      earnLockedData.rows.forEach((row: any) => {
        const qty = parseFloat(row.amount || '0');
        if (qty > 0) {
          getLayerAsset(row.asset).allocations.push({
            location: `Simple Earn (Locked ${row.duration}D)`,
            category: 'EARN',
            qty: parseFloat(qty.toFixed(6)),
            usdVal: 0,
            pctOfAsset: 0,
            detail: `Locked ${row.duration} Days`,
          });
        }
      });
    }

    // 7. Spot Balances (excluding LD... tokens which are already captured in Simple Earn)
    if (Array.isArray(spotData?.balances)) {
      spotData.balances.forEach((b: any) => {
        const qty = parseFloat(b.free || '0') + parseFloat(b.locked || '0');
        if (qty <= 0) return;

        if (b.asset.startsWith('LD')) {
          const cleanAsset = b.asset.slice(2);
          const existingEarn = getLayerAsset(cleanAsset).allocations.find((a) => a.category === 'EARN');
          if (!existingEarn) {
            getLayerAsset(cleanAsset).allocations.push({
              location: 'Simple Earn (Flexible)',
              category: 'EARN',
              qty: parseFloat(qty.toFixed(6)),
              usdVal: 0,
              pctOfAsset: 0,
              detail: 'Flexible Earn (LD)',
            });
          }
        } else {
          getLayerAsset(b.asset).allocations.push({
            location: 'Spot Wallet',
            category: 'SPOT',
            qty: parseFloat(qty.toFixed(6)),
            usdVal: 0,
            pctOfAsset: 0,
            detail: 'Available in Spot',
          });
        }
      });
    }

    // 8. Futures Margin Allocations
    if (futuresSuccess) {
      if (Array.isArray(fData?.assets)) {
        fData.assets.forEach((fa: any) => {
          const wb = parseFloat(fa.walletBalance || '0');
          if (wb > 0) {
            getLayerAsset(fa.asset).allocations.push({
              location: 'USDⓈ-M Futures',
              category: 'PORTFOLIO_MARGIN',
              qty: parseFloat(wb.toFixed(6)),
              usdVal: 0,
              pctOfAsset: 0,
              detail: 'Futures Margin Wallet',
            });
          }
        });
      } else if (futuresWalletBalance > 0) {
        getLayerAsset('USDT').allocations.push({
          location: 'USDⓈ-M Futures',
          category: 'PORTFOLIO_MARGIN',
          qty: parseFloat(futuresWalletBalance.toFixed(6)),
          usdVal: 0,
          pctOfAsset: 0,
          detail: 'Futures Margin Wallet',
        });
      }
    }

    // Calculate Prices, Values, and Percentages for each 2-Layer Asset
    let totalPortfolioVal = 0;

    const two_layer_assets = Object.values(twoLayerMap)
      .map((entry) => {
        const asset = entry.asset;
        const unitPrice = getAssetPrice(asset);

        let totalQty = 0;
        entry.allocations.forEach((al) => {
          totalQty += al.qty;
          al.usdVal = parseFloat((al.qty * unitPrice).toFixed(4));
        });

        const totalUsdVal = parseFloat((totalQty * unitPrice).toFixed(2));
        totalPortfolioVal += totalUsdVal;

        entry.allocations.forEach((al) => {
          al.pctOfAsset = totalQty > 0 ? parseFloat(((al.qty / totalQty) * 100).toFixed(1)) : 0;
        });

        entry.allocations.sort((x, y) => y.usdVal - x.usdVal);

        return {
          asset,
          totalQty: parseFloat(totalQty.toFixed(6)),
          unitPrice: parseFloat(unitPrice.toFixed(4)),
          totalUsdVal,
          pctOfPortfolio: 0,
          allocations: entry.allocations,
        };
      })
      .filter((a) => a.totalUsdVal > 0.001 || a.totalQty > 0.001);

    two_layer_assets.forEach((a) => {
      a.pctOfPortfolio =
        totalPortfolioVal > 0 ? parseFloat(((a.totalUsdVal / totalPortfolioVal) * 100).toFixed(1)) : 0;
    });

    two_layer_assets.sort((a, b) => b.totalUsdVal - a.totalUsdVal);

    // Final Equity and Balance calculation
    const finalEquity = totalPortfolioVal > 0 
      ? totalPortfolioVal 
      : parseFloat((spotTotalUsd + futuresMarginBalance + totalMarginNetUsd).toFixed(2));
    const finalBalance = finalEquity;

    let finalFreeMargin = futuresAvailableMargin;
    let finalUsedMargin = futuresUsedMargin;

    if (finalUsedMargin === 0 && crossMarginLiabilityBtc > 0 && Number.isFinite(btcPrice) && btcPrice > 0) {
      finalUsedMargin = parseFloat((crossMarginLiabilityBtc * btcPrice).toFixed(2));
    }
    if (finalFreeMargin === 0) {
      finalFreeMargin = Math.max(0, finalEquity - finalUsedMargin);
    }

    const marginUtilization = finalEquity > 0 ? (finalUsedMargin / finalEquity) * 100 : 0;
    const effectiveLeverage = finalEquity > 0 ? totalPositionNotional / finalEquity : 0;

    const hasAnySuccess = spotSuccess || futuresSuccess || crossMarginSuccess || isoMarginSuccess || pmSuccess;
    const snapshotValid = hasAnySuccess && valuationComplete;

    return {
      success: snapshotValid,
      configured: true,
      spotSuccess,
      futuresSuccess,
      marginSuccess: crossMarginSuccess || isoMarginSuccess || pmSuccess,
      margin_mode: marginMode,
      margin_level: crossMarginLevel > 0 ? crossMarginLevel : undefined,
      margin_balance: totalMarginNetUsd,
      spot_balance: spotTotalUsd,
      futures_wallet_balance: futuresWalletBalance,
      futures_margin_balance: futuresMarginBalance,
      futures_unrealized_pnl: futuresUnrealizedPnl,
      free_margin: finalFreeMargin,
      used_margin: finalUsedMargin,
      equity: finalEquity,
      balance: finalBalance,
      margin_utilization_pct: marginUtilization,
      effective_leverage: effectiveLeverage,
      daily_pnl: futuresUnrealizedPnl,
      daily_pnl_pct: finalBalance > 0 ? (futuresUnrealizedPnl / finalBalance) * 100 : 0,
      holdings,
      two_layer_assets,
      sub_wallets,
      portfolio_margin_observation: portfolioMarginObservation,
      source: isTestnet ? 'BINANCE_TESTNET' : 'BINANCE_MAINNET',
      environment,
      last_sync_time: new Date().toISOString(),
      error: snapshotValid
        ? undefined
        : [
            spotError,
            futuresError,
            !valuationComplete ? 'One or more non-zero assets could not be valued reliably' : '',
          ].filter(Boolean).join('; ') || 'Account snapshot is incomplete or invalid.',
    };
  } catch (err: any) {
    return {
      success: false,
      configured: true,
      error: err.message,
      message: `Binance balance fetch exception: ${err.message}`,
    };
  }
}
