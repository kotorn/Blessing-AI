import { apiClient } from './client';
import { AccountData, BasketItem, InstrumentData, RiskRuleItem } from '../types';

export interface QuantStateResponse {
  account: AccountData;
  baskets: BasketItem[];
  instruments: Record<string, InstrumentData>;
  risk_rules?: RiskRuleItem[];
  correlation_btc_eth?: number;
  crypto_beta_exposure_pct?: number;
  liquidation_distance_pct?: number;
}

export const quantApi = {
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
