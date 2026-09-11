import { apiClient } from './client';
import { AccountData } from '../types';

export interface BinanceKeyStatus {
  configured: boolean;
  maskedKey?: string;
  source?: string;
  keyStatus?: string;
  hedgeMode?: boolean;
  spot?: {
    authenticated: boolean;
    canTrade: boolean;
    accountType?: string;
    error?: string | null;
  };
  futures?: {
    authenticated: boolean;
    canTrade: boolean;
    dualSidePosition?: boolean;
    error?: string | null;
  };
}

export interface ApiProfile {
  id: string;
  name: string;
  environment: 'MAINNET' | 'TESTNET';
  isLiveRealMoney: boolean;
  maskedApiKey: string;
  hasSecret: boolean;
  isActive: boolean;
}

export const binanceApi = {
  verifyKey: async (): Promise<BinanceKeyStatus> => {
    return apiClient.get<BinanceKeyStatus>('/api/binance/verify-key');
  },

  getProfiles: async (): Promise<{ profiles: ApiProfile[]; activeProfileId: string }> => {
    return apiClient.get<{ profiles: ApiProfile[]; activeProfileId: string }>('/api/binance/profiles');
  },

  switchProfile: async (profileId: string): Promise<any> => {
    return apiClient.post('/api/binance/profiles/switch', { profileId });
  },

  saveProfile: async (payload: {
    id?: string;
    name: string;
    environment: 'MAINNET' | 'TESTNET';
    apiKey: string;
    apiSecret: string;
    isLiveRealMoney: boolean;
  }): Promise<any> => {
    return apiClient.post('/api/binance/profiles/save', payload);
  },

  deleteProfile: async (profileId: string): Promise<any> => {
    return apiClient.post('/api/binance/profiles/delete', { profileId });
  },

  syncAccount: async (): Promise<{ success: boolean; account?: AccountData; message?: string }> => {
    return apiClient.post<{ success: boolean; account?: AccountData; message?: string }>(
      '/api/binance/sync-account',
      {}
    );
  },
};
