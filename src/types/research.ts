export interface OverfittingMetrics {
  complexityBudget: {
    marketStatesUsed: number;
    marketStatesMax: number;
    alphaEnginesUsed: number;
    alphaEnginesMax: number;
    gridLevelsUsed: number;
    gridLevelsMax: number;
    featuresPerEngineAvg: number;
    featuresBudgetMax: number;
    budgetStatus: 'COMPLIANT' | 'OVER_BUDGET';
  };
  deflatedSharpeRatio: number; // e.g. 1.92 (> 1.50 is statistically significant under multiple testing)
  probabilityOfBacktestOverfittingPct: number; // e.g. 6.4% (< 15% is passing)
  parameterPlateauStabilityPct: number; // e.g. 88.5% (wide plateau vs isolated spike)
  totalParameterCombinationsTested: number; // e.g. 1,420 combinations recorded in MLflow
  walkForwardEfficiencyRatio: number; // OOS Sharpe / IS Sharpe e.g. 0.82
}

export interface PurgedCVFold {
  foldId: number;
  trainWindow: string;
  purgeWindowHours: number;
  testWindow: string;
  embargoWindowHours: number;
  isSharpe: number;
  oosSharpe: number;
  oosReturnPct: number;
  status: 'STABLE' | 'DEGRADED';
}

export interface ExecutionFrictionAnalysis {
  naiveCandleCloseReturnPct: number;
  realisticMicrostructureReturnPct: number;
  unmodeledFrictionGapPct: number;
  frictionBreakdown: {
    bidAskSpreadDragPct: number;
    makerTakerFeeDragPct: number;
    fundingSettlementDragPct: number;
    latencySlippageDragPct: number;
    partialFillDecayPct: number;
  };
}

export interface StressScenarioInfo {
  id: string;
  name: string;
  period: string;
  assetDisplacement: string;
  marketConditions: string;
  severity: 'EXTREME' | 'HIGH' | 'ELEVATED' | 'BENCHMARK';
  keyRiskTested: string;
}
