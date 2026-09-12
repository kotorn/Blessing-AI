import { apiClient } from './client';
import { AccountData, BasketItem, InstrumentData, RiskRuleItem, SystemAlert, TradingSystemState, PreflightResult } from '../types';
import { ExecutionOrder } from '../types/orders';

export interface QuantStateResponse {
  account: AccountData;
  baskets: BasketItem[];
  instruments: Record<string, InstrumentData>;
  orders?: ExecutionOrder[];
  alerts?: SystemAlert[];
  strategy_intents?: any[];
  meta_allocations?: any[];
  exposure_recovery?: any;
  risk_rules?: RiskRuleItem[];
  correlation_btc_eth?: number;
  crypto_beta_exposure_pct?: number;
  liquidation_distance_pct?: number | null;
}

export const quantApi = {

  getSystemState: async (): Promise<TradingSystemState> => {
    return apiClient.get<TradingSystemState>('/api/system/state');
  },
  preflight: async (executionMode: string): Promise<PreflightResult> => {
    return apiClient.get<PreflightResult>('/api/system/preflight?executionMode=' + executionMode);
  },
  arm: async (params: any): Promise<TradingSystemState> => {
    return apiClient.post<TradingSystemState>('/api/system/arm', params);
  },
  disarm: async (): Promise<TradingSystemState> => {
    return apiClient.post<TradingSystemState>('/api/system/disarm', {});
  },
  pauseNewRisk: async (active: boolean): Promise<TradingSystemState> => {
    return apiClient.post<TradingSystemState>('/api/system/pause-new-risk', { active });
  },
  recoveryOnly: async (active: boolean): Promise<TradingSystemState> => {
    return apiClient.post<TradingSystemState>('/api/system/recovery-only', { active });
  },

  getState: async (): Promise<QuantStateResponse> => {
    return apiClient.get<QuantStateResponse>('/api/quant/state');
  },

  getHealth: async (): Promise<{ status: string }> => {
    return apiClient.get<{ status: string }>('/api/health');
  },

  toggleKillSwitch: async (active: boolean): Promise<{ success: boolean; kill_switch_active: boolean }> => {
    return apiClient.post<{ success: boolean; kill_switch_active: boolean }>(
      '/api/quant/risk/kill-switch',
      { active }
    );
  },

  expandGrid: async (basketId: string): Promise<any> => {
    return apiClient.post('/api/quant/basket/expand', { basket_id: basketId });
  },

  enterRecovery: async (basketId: string): Promise<any> => {
    return apiClient.post('/api/quant/basket/recovery', { basket_id: basketId });
  },

  closeBasket: async (basketId: string): Promise<any> => {
    return apiClient.post('/api/quant/basket/close', { basket_id: basketId });
  },
};
