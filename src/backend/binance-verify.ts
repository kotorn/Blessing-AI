/**
 * Binance verification service — extracted from server.ts.
 *
 * Contains the internals of `verifyBinanceCredentials` and
 * `fetchBinanceLiveBalances`: HMAC request signing, the Binance REST probes,
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
      signal: AbortSignal.timeout(15_000),
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
      signal: AbortSignal.timeout(15_000),
    });
    const fData: any = await fResp.json();
    if (fResp.ok) {
      results.futures.authenticated = true;
      results.futures.hedgeMode = fData.dualSidePosition;
      results.futures.message = fData.dualSidePosition ? 'Hedge Mode Active' : 'One-Way Mode (Hedge Mode required)';
    } else {
      results.futures.message = fData.msg || `HTTP ${fResp.status}`;
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

  const sign = (secret: string, queryStr: string) => {
    return crypto.createHmac('sha256', secret).update(queryStr).digest('hex');
  };

  try {
    const ts = Date.now();
    const query = `timestamp=${ts}`;
    const sig = sign(apiSecret, query);

    // Fetch in parallel: Spot Account, Prices, Wallet Balances, Portfolio Margin, Simple Earn Flexible, Simple Earn Locked, and Futures
    let spotTotalUsd = 0;
    let spotSuccess = false;
    let spotError = '';
    const holdings: Array<{ asset: string; qty: number; unitPrice: number; usdVal: number }> = [];

    const [
      spotResp,
      tickerResp,
      walletsResp,
      pmResp,
      earnFlexResp,
      earnLockedResp,
      fResp,
    ] = await Promise.all([
      fetch(`${spotBase}/api/v3/account?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
        signal: AbortSignal.timeout(15_000),
      }).catch((e) => ({ ok: false, status: 500, json: async () => ({ msg: e.message }) })),
      fetch(`${spotBase}/api/v3/ticker/price`, { signal: AbortSignal.timeout(15_000) }).catch(() => null),
      fetch(`${spotBase}/sapi/v1/asset/wallet/balance?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
        signal: AbortSignal.timeout(15_000),
      }).catch(() => null),
      fetch(`${spotBase}/sapi/v1/portfolio/balance?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
        signal: AbortSignal.timeout(15_000),
      }).catch(() => null),
      fetch(`${spotBase}/sapi/v1/simple-earn/flexible/position?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
        signal: AbortSignal.timeout(15_000),
      }).catch(() => null),
      fetch(`${spotBase}/sapi/v1/simple-earn/locked/position?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
        signal: AbortSignal.timeout(15_000),
      }).catch(() => null),
      fetch(`${futuresBase}/fapi/v2/account?${query}&signature=${sig}`, {
        headers: { 'X-MBX-APIKEY': apiKey },
        signal: AbortSignal.timeout(15_000),
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

    // Parse Spot
    const spotData = await (spotResp as any).json();
    let valuationComplete = true;
    if ((spotResp as any).ok && Array.isArray(spotData.balances)) {
      spotSuccess = true;
      spotData.balances.forEach((b: any) => {
        const qty = parseFloat(b.free || '0') + parseFloat(b.locked || '0');
        if (qty <= 0) return;

        const asset = b.asset;
        const cleanAsset = asset.startsWith('LD') ? asset.slice(2) : asset;
        let unitPrice = 0;

        if (stableCoins.has(asset) || stableCoins.has(cleanAsset)) {
          unitPrice = 1.0;
        } else if (priceMap[`${cleanAsset}USDT`]) {
          unitPrice = priceMap[`${cleanAsset}USDT`];
        } else if (priceMap[`${asset}USDT`]) {
          unitPrice = priceMap[`${asset}USDT`];
        }

        if (qty > 0 && (!Number.isFinite(unitPrice) || unitPrice <= 0)) {
          valuationComplete = false;
          return;
        }

        const usdVal = qty * unitPrice;
        if (usdVal > 0.0001 || qty > 0.0001) {
          holdings.push({
            asset,
            qty: parseFloat(qty.toFixed(8)),
            unitPrice: parseFloat(unitPrice.toFixed(4)),
            usdVal: parseFloat(usdVal.toFixed(4)),
          });
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
          else if (w.walletName.includes('Cross Margin') || w.walletName.includes('Portfolio') || w.walletName.includes('PM')) category = 'PORTFOLIO_MARGIN';
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

    sub_wallets.forEach((sw) => {
      sw.pctOfTotal = totalWalletsUsd > 0 ? parseFloat(((sw.usdVal / totalWalletsUsd) * 100).toFixed(1)) : 0;
    });
    sub_wallets.sort((a, b) => b.usdVal - a.usdVal);

    // Portfolio Margin is a separate account product from the USDⓈ-M
    // Futures Testnet worker.  Keep this as an explicitly read-only
    // observation: it can never authorize the worker or be silently treated
    // as Futures collateral.
    let portfolioMarginObservation = unavailablePortfolioMarginObservation();

    if (pmResp && (pmResp as any).ok) {
      try {
        const rawPmData = await (pmResp as any).json();
        portfolioMarginObservation = parsePortfolioMarginResponse(rawPmData);
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

    if (fResp && (fResp as any).ok) {
      try {
        const fData = await (fResp as any).json();
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

    // 1. Trading Bot: include only a wallet balance returned by Binance.
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

    // 2. Portfolio Margin (PM)
    if (portfolioMarginObservation.status === 'OBSERVED_READ_ONLY') {
      portfolioMarginObservation.balances.forEach((item) => {
        // This allocation is specifically the documented cross-margin asset
        // balance.  Do not substitute totalWalletBalance: that field can
        // include other Portfolio Margin components and would risk double
        // counting against the separate Futures account below.
        const qty = item.crossMarginAsset;
        if (qty > 0) {
          getLayerAsset(item.asset).allocations.push({
            location: 'Portfolio Margin',
            category: 'PORTFOLIO_MARGIN',
            qty: parseFloat(qty.toFixed(6)),
            usdVal: 0,
            pctOfAsset: 0,
            detail: 'Cross Margin (PM)',
          });
        }
      });
    }

    // 3. Simple Earn Flexible
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

    // 4. Simple Earn Locked
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

    // 5. Spot Balances (excluding LD... tokens which are already captured in Simple Earn)
    if (Array.isArray(spotData?.balances)) {
      spotData.balances.forEach((b: any) => {
        const qty = parseFloat(b.free || '0') + parseFloat(b.locked || '0');
        if (qty <= 0) return;

        if (b.asset.startsWith('LD')) {
          // Token is Simple Earn receipt token (e.g. LDUSDT, LDETH, LDUSDC)
          const cleanAsset = b.asset.slice(2);
          // Check if already present in Earn allocations
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
          // Pure Spot
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

    // Calculate Prices, Values, and Percentages for each 2-Layer Asset
    let totalPortfolioVal = 0;

    const two_layer_assets = Object.values(twoLayerMap)
      .map((entry) => {
        const asset = entry.asset;
        let unitPrice = 0;
        if (stableCoins.has(asset)) {
          unitPrice = 1.0;
        } else if (priceMap[`${asset}USDT`]) {
          unitPrice = priceMap[`${asset}USDT`];
        }

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

    // Final Equity and Balance
    const finalEquity = totalPortfolioVal > 0 ? totalPortfolioVal : spotTotalUsd + futuresMarginBalance;
    const finalBalance = finalEquity;
    const marginUtilization = finalEquity > 0 ? (futuresUsedMargin / finalEquity) * 100 : 0;
    const effectiveLeverage = finalEquity > 0 ? totalPositionNotional / finalEquity : 0;

    const snapshotValid = spotSuccess && futuresSuccess && valuationComplete;

    return {
      success: snapshotValid,
      configured: true,
      spotSuccess,
      futuresSuccess,
      spot_balance: spotTotalUsd,
      futures_wallet_balance: futuresWalletBalance,
      futures_margin_balance: futuresMarginBalance,
      futures_unrealized_pnl: futuresUnrealizedPnl,
      free_margin: futuresAvailableMargin,
      used_margin: futuresUsedMargin,
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
