import { useState, useEffect, useCallback, useRef } from 'react';
import { AccountData, BasketItem, InstrumentData, RiskRuleItem, SystemAlert, TradingSystemState } from '../types';
import { ExecutionOrder } from '../types/orders';
import { quantApi } from '../api/quant';

export interface UseQuantStateReturn {
  systemState: TradingSystemState | null;
  account: AccountData;
  baskets: BasketItem[];
  instruments: Record<string, InstrumentData>;
  orders: ExecutionOrder[];
  alerts: SystemAlert[];
  riskRules: RiskRuleItem[];
  strategyIntents: any[];
  metaAllocations: Record<string, any> | null;
  exposureRecovery: any | null;
  correlationBtcEth: number;
  cryptoBetaExposurePct: number;
  liquidationDistancePct: number | null;
  isLoading: boolean;
  isActionLoading: boolean;
  lastUpdated: Date | null;
  error: string | null;
  refresh: () => Promise<void>;
  updateAccount: (account: AccountData) => void;
  toggleKillSwitch: () => Promise<void>;
  armEngine: (params: any) => Promise<void>;
  disarmEngine: () => Promise<void>;
  togglePauseNewRisk: (active: boolean) => Promise<void>;
  toggleRecoveryOnly: (active: boolean) => Promise<void>;
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
  const [systemState, setSystemState] = useState<TradingSystemState | null>(null);
  const [account, setAccount] = useState<AccountData>(DEFAULT_ACCOUNT);
  const [baskets, setBaskets] = useState<BasketItem[]>([]);
  const [instruments, setInstruments] = useState<Record<string, InstrumentData>>({});
  const [orders, setOrders] = useState<ExecutionOrder[]>([]);
  const [alerts, setAlerts] = useState<SystemAlert[]>([]);
  const [riskRules, setRiskRules] = useState<RiskRuleItem[]>([]);
  const [strategyIntents, setStrategyIntents] = useState<any[]>([]);
  const [metaAllocations, setMetaAllocations] = useState<Record<string, any> | null>(null);
  const [exposureRecovery, setExposureRecovery] = useState<any | null>(null);
  const [correlationBtcEth, setCorrelationBtcEth] = useState<number>(0.74);
  const [cryptoBetaExposurePct, setCryptoBetaExposurePct] = useState<number>(42.8);
  const [liquidationDistancePct, setLiquidationDistancePct] = useState<number | null>(null);

  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isActionLoading, setIsActionLoading] = useState<boolean>(false);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [error, setError] = useState<string | null>(null);

  const isMountedRef = useRef<boolean>(true);

  const fetchState = useCallback(async () => {
    try {

      if (!isMountedRef.current) return;
      const [data, sysState] = await Promise.all([
        quantApi.getState(),
        quantApi.getSystemState()
      ]);
      setSystemState(sysState);
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
      if (Array.isArray(data?.orders)) {
        setOrders(data.orders);
      }
      if (Array.isArray(data?.alerts)) {
        setAlerts(data.alerts);
      }
      if (Array.isArray(data?.risk_rules)) {
        setRiskRules(data.risk_rules);
      }
      if (Array.isArray(data?.strategy_intents)) {
        setStrategyIntents(data.strategy_intents);
      }
      if (data?.meta_allocations) {
        setMetaAllocations(data.meta_allocations);
      }
      if (data?.exposure_recovery) {
        setExposureRecovery(data.exposure_recovery);
      }
      if (typeof data?.correlation_btc_eth === 'number') {
        setCorrelationBtcEth(data.correlation_btc_eth);
      }
      if (typeof data?.crypto_beta_exposure_pct === 'number') {
        setCryptoBetaExposurePct(data.crypto_beta_exposure_pct);
      }
      if (data?.liquidation_distance_pct === null) {
        setLiquidationDistancePct(null);
      } else if (typeof data?.liquidation_distance_pct === 'number') {
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


  const armEngine = useCallback(async (params: any) => {
    setIsActionLoading(true);
    if (auditLogger) {
      auditLogger('ENGINE_ARMED', 'SYSTEM', 'Armed in ' + params.executionMode + ' mode');
    }
    try {
      await quantApi.arm(params);
      await fetchState();
    } finally {
      setIsActionLoading(false);
    }
  }, [auditLogger, fetchState]);

  const disarmEngine = useCallback(async () => {
    setIsActionLoading(true);
    if (auditLogger) {
      auditLogger('ENGINE_DISARMED', 'SYSTEM', 'Engine successfully disarmed');
    }
    try {
      await quantApi.disarm();
      await fetchState();
    } finally {
      setIsActionLoading(false);
    }
  }, [auditLogger, fetchState]);

  const togglePauseNewRisk = useCallback(async (active: boolean) => {
    setIsActionLoading(true);
    if (auditLogger) {
      auditLogger('PAUSE_NEW_RISK_CHANGED', 'SYSTEM', 'Pause new risk state: ' + active);
    }
    try {
      await quantApi.pauseNewRisk(active);
      await fetchState();
    } finally {
      setIsActionLoading(false);
    }
  }, [auditLogger, fetchState]);

  const toggleRecoveryOnly = useCallback(async (active: boolean) => {
    setIsActionLoading(true);
    if (auditLogger) {
      auditLogger('RECOVERY_ONLY_CHANGED', 'SYSTEM', 'Recovery only state: ' + active);
    }
    try {
      await quantApi.recoveryOnly(active);
      await fetchState();
    } finally {
      setIsActionLoading(false);
    }
  }, [auditLogger, fetchState]);

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
    systemState,
    account,
    baskets,
    instruments,
    orders,
    alerts,
    riskRules,
    strategyIntents,
    metaAllocations,
    exposureRecovery,
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
    armEngine,
    disarmEngine,
    togglePauseNewRisk,
    toggleRecoveryOnly,
    expandGrid,
    enterRecovery,
    closeBasket,
  };
}
