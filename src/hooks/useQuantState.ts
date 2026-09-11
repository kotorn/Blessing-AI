import { useState, useEffect, useCallback, useRef } from 'react';
import { AccountData, BasketItem, InstrumentData, RiskRuleItem } from '../types';
import { quantApi } from '../api/quant';

export interface UseQuantStateReturn {
  account: AccountData;
  baskets: BasketItem[];
  instruments: Record<string, InstrumentData>;
  riskRules: RiskRuleItem[];
  correlationBtcEth: number;
  cryptoBetaExposurePct: number;
  liquidationDistancePct: number;
  isLoading: boolean;
  isActionLoading: boolean;
  lastUpdated: Date | null;
  error: string | null;
  refresh: () => Promise<void>;
  updateAccount: (account: AccountData) => void;
  toggleKillSwitch: () => Promise<void>;
  expandGrid: (basketId: string) => Promise<void>;
  enterRecovery: (basketId: string) => Promise<void>;
  closeBasket: (basketId: string) => Promise<void>;
}

const DEFAULT_ACCOUNT: AccountData = {
  equity: 50720.5,
  balance: 50000.0,
  margin_utilization_pct: 14.2,
  effective_leverage: 0.85,
  free_margin: 43518.19,
  used_margin: 7202.31,
  daily_pnl: 720.5,
  daily_pnl_pct: 1.44,
  portfolio_drawdown_pct: 1.15,
  kill_switch_active: false,
  risk_state: 'NORMAL',
  source: 'SIMULATED',
};

export function useQuantState(auditLogger?: (action: string, entityId: string, details: string) => void): UseQuantStateReturn {
  const [account, setAccount] = useState<AccountData>(DEFAULT_ACCOUNT);
  const [baskets, setBaskets] = useState<BasketItem[]>([]);
  const [instruments, setInstruments] = useState<Record<string, InstrumentData>>({});
  const [riskRules, setRiskRules] = useState<RiskRuleItem[]>([]);
  const [correlationBtcEth, setCorrelationBtcEth] = useState<number>(0.74);
  const [cryptoBetaExposurePct, setCryptoBetaExposurePct] = useState<number>(42.8);
  const [liquidationDistancePct, setLiquidationDistancePct] = useState<number>(38.5);

  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isActionLoading, setIsActionLoading] = useState<boolean>(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);

  const isMountedRef = useRef<boolean>(true);

  const fetchState = useCallback(async () => {
    try {
      const data = await quantApi.getState();
      if (!isMountedRef.current) return;

      if (data?.account) {
        setAccount((prev) => ({
          ...prev,
          ...data.account,
        }));
      }
      if (Array.isArray(data?.baskets)) {
        setBaskets(data.baskets);
      }
      if (data?.instruments) {
        setInstruments(data.instruments);
      }
      if (Array.isArray(data?.risk_rules)) {
        setRiskRules(data.risk_rules);
      }
      if (typeof data?.correlation_btc_eth === 'number') {
        setCorrelationBtcEth(data.correlation_btc_eth);
      }
      if (typeof data?.crypto_beta_exposure_pct === 'number') {
        setCryptoBetaExposurePct(data.crypto_beta_exposure_pct);
      }
      if (typeof data?.liquidation_distance_pct === 'number') {
        setLiquidationDistancePct(data.liquidation_distance_pct);
      }

      setLastUpdated(new Date());
      setError(null);
    } catch (err: any) {
      if (!isMountedRef.current) return;
      // Stale or initializing backend is expected occasionally during dev reload
      setError(err?.message || 'Failed to fetch quant state');
    } finally {
      if (isMountedRef.current) {
        setIsLoading(false);
      }
    }
  }, []);

  // Single centralized 3-second polling loop
  useEffect(() => {
    isMountedRef.current = true;
    fetchState();

    const interval = setInterval(() => {
      fetchState();
    }, 3000);

    return () => {
      isMountedRef.current = false;
      clearInterval(interval);
    };
  }, [fetchState]);

  const updateAccount = useCallback((newAccount: AccountData) => {
    setAccount(newAccount);
  }, []);

  const toggleKillSwitch = useCallback(async () => {
    const nextState = !account.kill_switch_active;
    setIsActionLoading(true);
    if (auditLogger) {
      auditLogger(
        'KILL_SWITCH_TRIGGERED',
        'SYSTEM',
        `Kill switch state changed to: ${nextState ? 'ENGAGED' : 'DISARMED'}`
      );
    }
    try {
      const resp = await quantApi.toggleKillSwitch(nextState);
      if (resp?.success) {
        setAccount((prev) => ({
          ...prev,
          kill_switch_active: resp.kill_switch_active,
          risk_state: resp.kill_switch_active ? 'EMERGENCY' : 'NORMAL',
        }));
      }
      await fetchState();
    } catch (e) {
      console.warn('Kill switch action failed or deferred:', e);
    } finally {
      setIsActionLoading(false);
    }
  }, [account.kill_switch_active, auditLogger, fetchState]);

  const expandGrid = useCallback(async (basketId: string) => {
    setIsActionLoading(true);
    if (auditLogger) {
      auditLogger('EXPAND_GRID', basketId, 'Manual grid expansion requested by operator');
    }
    try {
      await quantApi.expandGrid(basketId);
      await fetchState();
    } catch (e) {
      console.warn('Expand grid action failed:', e);
    } finally {
      setIsActionLoading(false);
    }
  }, [auditLogger, fetchState]);

  const enterRecovery = useCallback(async (basketId: string) => {
    setIsActionLoading(true);
    if (auditLogger) {
      auditLogger('ENTER_RECOVERY', basketId, 'Initiated dynamic counter-trend exposure recovery');
    }
    try {
      await quantApi.enterRecovery(basketId);
      await fetchState();
    } catch (e) {
      console.warn('Recovery action failed:', e);
    } finally {
      setIsActionLoading(false);
    }
  }, [auditLogger, fetchState]);

  const closeBasket = useCallback(async (basketId: string) => {
    setIsActionLoading(true);
    if (auditLogger) {
      auditLogger('CLOSE_BASKET', basketId, 'Market flatten and close basket requested');
    }
    try {
      await quantApi.closeBasket(basketId);
      await fetchState();
    } catch (e) {
      console.warn('Close basket action failed:', e);
    } finally {
      setIsActionLoading(false);
    }
  }, [auditLogger, fetchState]);

  return {
    account,
    baskets,
    instruments,
    riskRules,
    correlationBtcEth,
    cryptoBetaExposurePct,
    liquidationDistancePct,
    isLoading,
    isActionLoading,
    lastUpdated,
    error,
    refresh: fetchState,
    updateAccount,
    toggleKillSwitch,
    expandGrid,
    enterRecovery,
    closeBasket,
  };
}
