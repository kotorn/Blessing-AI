export interface StrategyAttribution {
  strategyId: 'grid' | 'trend' | 'shock' | 'carry';
  name: string;
  tag: string;
  grossPnL: number;
  netPnL: number;
  feesPaid: number;
  fundingSettlement: number;
  slippageCost: number;
  tradesCount: number;
  winRatePct: number;
  profitFactor: number;
  sharpeRatio: number;
  maxDrawdownPct: number;
  capitalAllocPct: number;
  currentVirtualExposure: number; // in USD
  primaryRegime: string;
}

export interface FilterEfficacyItem {
  filterName: string;
  description: string;
  timesTriggered: number;
  returnForfeitedPct: number; // e.g. -1.2%
  tailRiskAvoidedPct: number; // e.g. +7.4%
  efficiencyRatio: number; // tailRiskAvoided / returnForfeited e.g. 6.16x
  status: 'OPTIMAL' | 'CAUTION_TOO_CONSERVATIVE' | 'INSUFFICIENT_PROTECTION';
}

export interface MissedMoveItem {
  id: string;
  timestamp: string;
  symbol: string;
  moveMagnitudePct: number;
  vetoingGate: string;
  gateCategory: 'RISK' | 'REGIME' | 'VOLATILITY' | 'SPREAD';
  postMoveValidation: 'VALID_TAIL_RISK_AVOIDED' | 'EXCESSIVE_CAUTION_FALSE_ALARM';
  detail: string;
}

export interface ConservatismMetrics {
  zeroExposurePct: number; // % of time with zero exposure
  rejectedOpportunityCount: number;
  avgCapitalUtilizationPct: number;
  peakCapitalUtilizationPct: number;
  riskBudgetUtilizationPct: number;
  returnLostDueToFiltersPct: number;
  tailRiskReductionPct: number;
  netFilterAdvantageRatio: number; // Tail risk avoided / return forfeited
  opportunityScoreDistribution: {
    range: string;
    count: number;
    executedCount: number;
    conversionRatePct: number;
  }[];
  filters: FilterEfficacyItem[];
  recentMissedMoves: MissedMoveItem[];
}
