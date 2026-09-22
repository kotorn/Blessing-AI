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
  correlationBtcEth: number | null;
  cryptoBetaExposurePct: number | null;
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
  equity: 0,
  balance: 0,
  margin_utilization_pct: 0,
  effective_leverage: 0,
  free_margin: 0,
  used_margin: 0,
  daily_pnl: 0,
  daily_pnl_pct: 0,
  portfolio_drawdown_pct: 0,
  kill_switch_active: false,
  risk_state: 'UNKNOWN',
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
  const [correlationBtcEth, setCorrelationBtcEth] = useState<number | null>(null);
  const [cryptoBetaExposurePct, setCryptoBetaExposurePct] = useState<number | null>(null);
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
      setAccount(data?.account || DEFAULT_ACCOUNT);
      setBaskets(Array.isArray(data?.baskets) ? data.baskets : []);
      setInstruments(data?.instruments || {});
      setOrders(Array.isArray(data?.orders) ? data.orders : []);
      setAlerts(Array.isArray(data?.alerts) ? data.alerts : []);
      setRiskRules(Array.isArray(data?.risk_rules) ? data.risk_rules : []);
      setStrategyIntents(Array.isArray(data?.strategy_intents) ? data.strategy_intents : []);
      setMetaAllocations(data?.meta_allocations || null);
      setExposureRecovery(data?.exposure_recovery || null);
      setCorrelationBtcEth(
        typeof data?.correlation_btc_eth === 'number' ? data.correlation_btc_eth : null,
      );
      setCryptoBetaExposurePct(
        typeof data?.crypto_beta_exposure_pct === 'number'
          ? data.crypto_beta_exposure_pct
          : null,
      );
      if (data?.liquidation_distance_pct === null) {
        setLiquidationDistancePct(null);
      } else if (typeof data?.liquidation_distance_pct === 'number') {
        setLiquidationDistancePct(data.liquidation_distance_pct);
      }

      const serverUpdatedAt = Date.parse(sysState.updatedAt);
      setLastUpdated(Number.isFinite(serverUpdatedAt) ? new Date(serverUpdatedAt) : null);
      setError(null);
    } catch (err: any) {
      if (!isMountedRef.current) return;
      // Never retain an old exchange snapshot or healthy execution state when
      // either the UI gateway or the Python worker is unavailable.
      setSystemState(null);
      setAccount({ ...DEFAULT_ACCOUNT, kill_switch_active: true });
      setBaskets([]);
      setInstruments({});
      setOrders([]);
      setAlerts([]);
      setRiskRules([]);
      setStrategyIntents([]);
      setMetaAllocations(null);
      setExposureRecovery(null);
      setCorrelationBtcEth(null);
      setCryptoBetaExposurePct(null);
      setLiquidationDistancePct(null);
      setLastUpdated(null);
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
    try {
      await quantApi.arm(params);
      if (auditLogger) {
        auditLogger('ENGINE_ARMED', 'SYSTEM', 'Armed in ' + params.executionMode + ' mode');
      }
      await fetchState();
    } finally {
      setIsActionLoading(false);
    }
  }, [auditLogger, fetchState]);

  const disarmEngine = useCallback(async () => {
    setIsActionLoading(true);
    try {
      await quantApi.disarm();
      if (auditLogger) {
        auditLogger('ENGINE_DISARMED', 'SYSTEM', 'Engine successfully disarmed');
      }
      await fetchState();
    } finally {
      setIsActionLoading(false);
    }
  }, [auditLogger, fetchState]);

  const togglePauseNewRisk = useCallback(async (active: boolean) => {
    setIsActionLoading(true);
    try {
      await quantApi.pauseNewRisk(active);
      if (auditLogger) {
        auditLogger('PAUSE_NEW_RISK_CHANGED', 'SYSTEM', 'Pause new risk state: ' + active);
      }
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
    try {
      const resp = await quantApi.toggleKillSwitch(nextState);
      if (typeof resp?.kill_switch_active === 'boolean') {
        setAccount((prev) => ({
          ...prev,
          kill_switch_active: resp.kill_switch_active,
          risk_state: resp.kill_switch_active ? 'EMERGENCY' : 'UNKNOWN',
        }));
      }
      if (auditLogger) {
        auditLogger(
          'KILL_SWITCH_TRIGGERED',
          'SYSTEM',
          `Kill switch state changed to: ${nextState ? 'ENGAGED' : 'DISARMED'}`
        );
      }
      await fetchState();
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
