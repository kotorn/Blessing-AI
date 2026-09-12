import { TradingSystemState, PreflightResult, EngineState, ExecutionMode, ExecutionCapabilities, ActiveTradingConfiguration, ActionRiskClass } from './types';

// The Control Plane acts as a UI gateway for the Python Worker's state.
export const EXECUTION_CAPABILITIES: ExecutionCapabilities = {
  paper: true,
  testnetConfigured: false,
  testnetAuthenticated: false,
  testnetExecutionReady: false,
  liveConfigured: false,
  liveExecutionReady: false,
  spotSupported: false,
  usdmFuturesSupported: true,
  hedgeModeSupported: false
};

export const RISK_PROFILES = {
  CONSERVATIVE: {
    maxPortfolioDrawdownPct: 10.0,
    maxGrossLeverage: 1.5,
    maxMarginUtilizationPct: 30.0,
    maxStrategyRiskUnits: { grid: 0.5, trend: 0.5, shock: 0.25, carry: 0.5 }
  },
  BALANCED: {
    maxPortfolioDrawdownPct: 15.0,
    maxGrossLeverage: 3.0,
    maxMarginUtilizationPct: 50.0,
    maxStrategyRiskUnits: { grid: 1.0, trend: 1.0, shock: 0.5, carry: 0.5 }
  },
  AGGRESSIVE: {
    maxPortfolioDrawdownPct: 25.0,
    maxGrossLeverage: 5.0,
    maxMarginUtilizationPct: 70.0,
    maxStrategyRiskUnits: { grid: 2.0, trend: 2.0, shock: 1.0, carry: 1.0 }
  }
};

export function evaluatePreflight(state: TradingSystemState, requestedConfiguration: any): PreflightResult {
  const executionMode: ExecutionMode = requestedConfiguration?.executionMode || 'PAPER';

  if (executionMode === 'TESTNET' && state.exchangeEnvironment === 'BINANCE_MAINNET') {
    return { executionMode, canArm: false, checks: [{ id: 'CHK-ENV-MISMATCH', name: 'Environment Compatibility', required: true, status: 'FAIL', message: 'Mismatch' }] };
  }
  if (executionMode === 'LIVE') {
    return { executionMode, canArm: false, checks: [{ id: 'CHK-ENV', name: 'Environment', required: true, status: 'FAIL', message: 'LIVE is blocked' }] };
  }

  let canArm = true;
  if (!state.tradingConnectionHealthy && executionMode === 'TESTNET') canArm = false;
  
  return { executionMode, canArm, checks: [] };
}

export function validateStateTransition(currentState: EngineState, nextState: EngineState): boolean {
  return true; // Validated via worker
}

export function canExecuteAction(engineState: EngineState, actionRiskClass: ActionRiskClass): boolean {
  const reducingRisk = ['REDUCE_RISK', 'RECOVERY', 'CLOSE', 'EMERGENCY'].includes(actionRiskClass);
  if (engineState === 'EMERGENCY') {
    return ['REDUCE_RISK', 'CLOSE', 'EMERGENCY'].includes(actionRiskClass);
  }
  if (engineState === 'PAUSED_NEW_RISK' || engineState === 'RECOVERY_ONLY') {
    return reducingRisk;
  }
  if (engineState !== 'ARMED') return false;
  return ['NEW_RISK', 'INCREASE_RISK', 'REDUCE_RISK', 'RECOVERY', 'CLOSE', 'EMERGENCY'].includes(actionRiskClass);
}
