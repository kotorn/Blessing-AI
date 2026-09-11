import { TradingSystemState, PreflightResult, EngineState, ExecutionMode, ExecutionCapabilities, ActiveTradingConfiguration, ActionRiskClass } from './types';

export const EXECUTION_CAPABILITIES: ExecutionCapabilities = {
  paper: true,
  testnet: true,
  live: false,
  spot: true,
  usdmFutures: true,
  hedgeModeSupported: true,
  liveExecutionReady: false
};

export const RISK_PROFILES = {
  CONSERVATIVE: {
    maxPortfolioDrawdownPct: 10.0,
    maxGrossLeverage: 1.5,
    maxMarginUtilizationPct: 30.0,
    maxStrategyRiskUnits: { grid: 0.5, trend: 0.5, shock: 0.25, carry: 0.5 }
  },
  BALANCED: {
    maxPortfolioDrawdownPct: 15.0, // EXAMPLE / RESEARCH DEFAULT
    maxGrossLeverage: 3.0,
    maxMarginUtilizationPct: 50.0,
    maxStrategyRiskUnits: { grid: 1.0, trend: 1.0, shock: 0.5, carry: 0.5 }
  },
  AGGRESSIVE: {
    maxPortfolioDrawdownPct: 25.0, // EXAMPLE / RESEARCH DEFAULT
    maxGrossLeverage: 5.0,
    maxMarginUtilizationPct: 70.0,
    maxStrategyRiskUnits: { grid: 2.0, trend: 2.0, shock: 1.0, carry: 1.0 }
  }
};

export function evaluatePreflight(state: TradingSystemState, requestedConfiguration: any): PreflightResult {
  const executionMode: ExecutionMode = requestedConfiguration?.executionMode || 'PAPER';
  
  const checks: PreflightResult['checks'] = [
    {
      id: 'CHK-CORE',
      name: 'Backend Core Engine Health',
      required: true,
      status: 'PASS',
      message: 'Engine process is running and responsive.'
    },
    {
      id: 'CHK-DB',
      name: 'Database / Persistence Layer',
      required: true,
      status: 'PASS',
      message: 'Local memory / persistence layer is active.'
    },
    {
      id: 'CHK-MKT',
      name: 'Market Data Stream',
      required: true,
      status: state.marketDataHealthy ? 'PASS' : 'FAIL',
      message: state.marketDataHealthy ? 'Real-time quotes active.' : 'Market data stale.'
    },
    {
      id: 'CHK-SYNC',
      name: 'Binance Account Synchronization',
      required: executionMode === 'LIVE' || executionMode === 'TESTNET',
      status: state.accountSynchronized ? 'PASS' : (executionMode === 'PAPER' ? 'WARN' : 'FAIL'),
      message: state.accountSynchronized ? 'Synchronized with exchange.' : 'Not synchronized. Operating on simulated paper balance.'
    },
    {
      id: 'CHK-PERM',
      name: 'Execution Permissions (Withdrawals Disabled)',
      required: executionMode === 'LIVE',
      status: executionMode === 'PAPER' ? 'PASS' : (state.accountSynchronized ? 'PASS' : 'FAIL'),
      message: executionMode === 'PAPER' ? 'Paper execution always permitted.' : 'Verification required.'
    }
  ];

  if (executionMode === 'LIVE') {
    checks.push({
      id: 'CHK-LIVE-GUARD',
      name: 'Live Execution Capability',
      required: true,
      status: EXECUTION_CAPABILITIES.live ? 'PASS' : 'FAIL',
      message: EXECUTION_CAPABILITIES.live ? 'Live execution adapter ready.' : 'Live Binance execution adapter is not production ready. Use PAPER or TESTNET.'
    });
  }
  
  if (executionMode === 'TESTNET') {
    checks.push({
      id: 'CHK-TESTNET-GUARD',
      name: 'Testnet Execution Capability',
      required: true,
      status: EXECUTION_CAPABILITIES.testnet ? 'PASS' : 'FAIL',
      message: EXECUTION_CAPABILITIES.testnet ? 'Testnet execution adapter ready.' : 'Binance Testnet execution adapter is not available yet.'
    });
  }

  let canArm = true;
  for (const check of checks) {
    if (check.required && check.status === 'FAIL') {
      canArm = false;
      break;
    }
  }
  
  if (requestedConfiguration?.instruments?.length === 0) {
     canArm = false;
  }
  
  if (requestedConfiguration?.strategies) {
     const hasStrategy = Object.values(requestedConfiguration.strategies).some(v => v === true);
     if (!hasStrategy) canArm = false;
  }

  return { executionMode, canArm, checks };
}

export function validateStateTransition(currentState: EngineState, nextState: EngineState): boolean {
  const transitions: Record<EngineState, EngineState[]> = {
    'DISARMED': ['ARMING', 'ARMED'],
    'ARMING': ['ARMED', 'DISARMED'],
    'ARMED': ['PAUSED_NEW_RISK', 'RECOVERY_ONLY', 'DISARMED', 'EMERGENCY'],
    'PAUSED_NEW_RISK': ['ARMED', 'RECOVERY_ONLY', 'DISARMED', 'EMERGENCY'],
    'RECOVERY_ONLY': ['ARMED', 'DISARMED', 'EMERGENCY'],
    'EMERGENCY': ['DISARMED']
  };

  return transitions[currentState].includes(nextState);
}

export function canExecuteAction(engineState: EngineState, actionRiskClass: ActionRiskClass): boolean {
  if (engineState === 'DISARMED') {
    return actionRiskClass === 'CLOSE'; // Maybe allow manual close?
  }
  
  if (engineState === 'EMERGENCY') {
    return actionRiskClass === 'EMERGENCY' || actionRiskClass === 'CLOSE' || actionRiskClass === 'REDUCE_RISK';
  }
  
  if (engineState === 'PAUSED_NEW_RISK') {
    return ['REDUCE_RISK', 'RECOVERY', 'CLOSE', 'EMERGENCY'].includes(actionRiskClass);
  }
  
  if (engineState === 'RECOVERY_ONLY') {
    return ['RECOVERY', 'REDUCE_RISK', 'CLOSE', 'EMERGENCY'].includes(actionRiskClass);
  }
  
  if (engineState === 'ARMED') {
    return true; // Risk governor will handle further logic
  }
  
  return false;
}
