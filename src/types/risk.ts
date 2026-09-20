import { RiskState,  } from '../types';

export interface FailClosedSafeguard {
  id: string;
  name: string;
  status: 'HEALTHY' | 'WARNING' | 'BREACHED' | 'UNKNOWN';
  latencyOrLag: string;
  failClosedAction: string;
  lastChecked: string;
}

export interface ExposureRecoveryAssessment {
  basketId: string;
  symbol: string;
  direction: 'LONG' | 'SHORT';
  gridDepth: number;
  netExposureUsd: number;
  grossExposureUsd: number;
  currentDrawdownPct: number;
  volatilityRegime: string;
  trendContinuationProb: number;
  recommendedAction: 'REDUCE_INVENTORY' | 'OPEN_COUNTER_HEDGE' | 'HOLD_RECOVERY' | 'BLOCK_GRID_EXPANSION';
  actionComparison: {
    reduceExposureScore: number;
    reduceExposureGrossImpact: string;
    openHedgeScore: number;
    openHedgeGrossImpact: string;
    decisionRationale: string;
  };
  toxicLevelsToHarvest: number[];
}

export interface HardRiskBounds {
  maxLeverage: number;
  currentLeverage: number;
  maxMarginUtilizationPct: number;
  currentMarginUtilizationPct: number;
  maxPortfolioDrawdownPct: number;
  currentPortfolioDrawdownPct: number;
  maxSingleAssetBetaPct: number;
  currentSingleAssetBetaPct: number;
  governorState: RiskState;
  killSwitchActive: boolean;
}
