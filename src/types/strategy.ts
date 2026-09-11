export type AlphaEngineId =
  | 'structural_grid'
  | 'trend_breakout'
  | 'shock_momentum'
  | 'funding_carry'
  | 'relative_value';

export interface StrategyIntentItem {
  id: string;
  engineId: AlphaEngineId;
  strategyName: string;
  symbol: string;
  direction: 'LONG' | 'SHORT' | 'NEUTRAL';
  rawTargetDelta: number; // e.g. +1.0 BTC
  opportunityScore: number; // 0-100 continuous score
  confidence: number; // 0.0 - 1.0
  urgency: 'LOW' | 'MEDIUM' | 'HIGH' | 'IMMEDIATE';
  timeHorizon: string; // e.g. "4h - 24h"
  hypothesis: string;
  regimeFit: string;
  proposedMaxNotionalUsd: number;
}

export interface MetaAllocationWeight {
  engineId: AlphaEngineId;
  strategyName: string;
  baseWeightPct: number;
  expectedEdgeBps: number;
  confidence: number;
  regimeFitFactor: number;
  executionQualityFactor: number;
  cryptoBetaDiscount: number;
  finalBudgetFactor: number; // continuous factor e.g. 1.25x
  virtualNotionalCapUsd: number;
  status: 'ACTIVE' | 'SCALED_DOWN' | 'PAUSED';
}

export interface StrategyConflictResolution {
  symbol: string;
  strategyIntents: {
    engineId: AlphaEngineId;
    name: string;
    targetDelta: number;
    valueUsd: number;
  }[];
  grossDisputedDelta: number; // sum of absolute deltas
  netPhysicalTargetDelta: number; // netted delta sent to exchange
  nettingFeeSavingsUsd: number;
  slippageMitigationBps: number;
  virtualPnLAttributionPreserved: boolean;
}

export interface ConservatismMetrics {
  zeroExposurePct: number; // percent of time with zero exposure
  rejectedOpportunityCount24h: number;
  riskBudgetUtilizationPct: number;
  capitalUtilizationPct: number;
  estimatedReturnLostToVetoPct: number;
  tailRiskDrawdownAvoidedPct: number;
  overConservatismFlag: boolean;
}
