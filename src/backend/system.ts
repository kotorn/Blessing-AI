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

/**
 * Project the worker's canonical connection-health verdict without inferring
 * readiness from a transport/state label.  A READY connection can still be
 * unsafe for execution when authentication, the private stream, or
 * reconciliation is unhealthy, so a missing or conflicting health field must
 * fail closed.
 */
export function isWorkerTradingConnectionHealthy(workerState: unknown): boolean {
  if (!workerState || typeof workerState !== 'object') return false;
  const state = workerState as { trading_connection_healthy?: unknown };
  return state.trading_connection_healthy === true;
}

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
  const requestedMode = requestedConfiguration?.executionMode;
  const executionMode: ExecutionMode = ['PAPER', 'TESTNET', 'LIVE'].includes(requestedMode)
    ? requestedMode
    : 'PAPER';
  const checks: PreflightResult['checks'] = [];
  const add = (
    id: string,
    name: string,
    passed: boolean,
    message: string,
    status: 'FAIL' | 'PASS' = passed ? 'PASS' : 'FAIL',
  ) => checks.push({ id, name, required: true, status, message });

  const instruments = Array.isArray(requestedConfiguration?.instruments)
    ? requestedConfiguration.instruments
      .map((symbol: unknown) => String(symbol).trim().toUpperCase())
      .filter(Boolean)
    : [];
  const strategies = requestedConfiguration?.strategies;
  const strategyEnabled = strategies && typeof strategies === 'object'
    ? Object.values(strategies).some((enabled) => enabled === true)
    : false;
  const supportedSymbols = executionMode === 'TESTNET'
    ? new Set(['BTCUSDT'])
    : executionMode === 'LIVE'
      ? new Set(['ETHUSDC'])
      : new Set(['BTCUSDT', 'ETHUSDT', 'ETHUSDC']);
  const unsupported = instruments.filter((symbol: string) => !supportedSymbols.has(symbol));
  const riskProfile = requestedConfiguration?.riskProfile;

  add('CHK-MODE', 'Execution Mode', requestedMode === executionMode, requestedMode === executionMode ? `${executionMode} requested` : 'Unsupported execution mode');
  const expectedEnvironment = executionMode === 'TESTNET'
    ? 'BINANCE_TESTNET'
    : executionMode === 'LIVE'
      ? 'BINANCE_MAINNET'
      : 'NONE';
  add(
    'CHK-ENV',
    'Environment',
    executionMode === 'PAPER' || state.exchangeEnvironment === expectedEnvironment,
    executionMode === 'PAPER'
      ? 'Execution mode is non-live'
      : state.exchangeEnvironment === expectedEnvironment
        ? `${expectedEnvironment} selected`
        : `Requested ${executionMode} but state is ${state.exchangeEnvironment}`,
  );
  add('CHK-INSTRUMENTS', 'Instruments', instruments.length > 0 && unsupported.length === 0, instruments.length === 0 ? 'At least one instrument is required' : unsupported.length ? `Unsupported instruments: ${unsupported.join(', ')}` : 'All requested instruments are supported');
  add('CHK-STRATEGY', 'Enabled Strategy', strategyEnabled, strategyEnabled ? 'At least one strategy enabled' : 'At least one strategy is required');
  add('CHK-RISK', 'Risk Profile', ['CONSERVATIVE', 'BALANCED', 'AGGRESSIVE'].includes(riskProfile), 'Valid risk profile is required');

  if (executionMode === 'TESTNET' || executionMode === 'LIVE') {
    const environmentName = executionMode === 'LIVE' ? 'Mainnet' : 'Testnet';
    const environmentLabel = executionMode === 'LIVE' ? 'BINANCE_MAINNET' : 'BINANCE_TESTNET';
    add('CHK-ENV-MISMATCH', 'Environment Compatibility', state.exchangeEnvironment === environmentLabel, state.exchangeEnvironment === environmentLabel ? `Worker state is Binance ${environmentName}` : `Requested ${environmentName} but state is ${state.exchangeEnvironment}`);
    add('CHK-WORKER', 'Worker Responsive', state.workerResponsive === true, state.workerResponsive === true ? 'Python Worker is responsive' : 'Python Worker is not verified responsive');
    add('CHK-ADAPTER', `${environmentName} Connection`, state.tradingConnectionHealthy === true, state.tradingConnectionHealthy ? `${environmentName} connection is healthy` : `${environmentName} connection is not healthy`);
    add('CHK-STREAM', 'Private Stream', state.privateStreamHealthy === true, state.privateStreamHealthy ? 'Private stream is healthy' : 'Private stream is not healthy');
    add('CHK-ACCOUNT', 'Account Snapshot', state.accountSynchronized === true, state.accountSynchronized ? 'Account snapshot is synchronized' : 'Account snapshot is not synchronized');
    add('CHK-RECONCILIATION', 'Reconciliation', state.reconciliationStatus === 'IN_SYNC', state.reconciliationStatus === 'IN_SYNC' ? 'Reconciliation is IN_SYNC' : `Reconciliation is ${state.reconciliationStatus}`);
    add('CHK-MARKET', 'Market Data', state.marketDataHealthy === true, state.marketDataHealthy ? 'Market data is healthy' : 'Market data is not healthy');
    add('CHK-KILL', 'Kill Switch', state.killSwitchActive === false, state.killSwitchActive ? 'Kill switch is active' : 'Kill switch is inactive');
    if (executionMode === 'LIVE') {
      const approved = state.mainnetLiveApproved === true;
      add('CHK-MAINNET-APPROVAL', 'Mainnet Release Approval', approved, approved ? 'MAINNET_LIVE_APPROVED is active' : 'MAINNET_LIVE_APPROVED is required before autonomous Mainnet execution');
      const credentialsVerified = state.mainnetCredentialsVerified === true;
      add('CHK-MAINNET-CREDENTIALS', 'Mainnet Credentials', credentialsVerified, credentialsVerified ? 'Mainnet credentials are verified' : 'Mainnet credentials are not verified');
      add('CHK-MAINNET-PREFLIGHT', 'Mainnet Worker Preflight', state.mainnetPreflightReady === true, state.mainnetPreflightReady === true ? 'Mainnet worker preflight is ready' : 'Mainnet worker preflight is not ready');
    }
  } else {
    add('CHK-KILL', 'Kill Switch', state.killSwitchActive === false, state.killSwitchActive ? 'Kill switch is active' : 'Kill switch is inactive');
  }

  return { executionMode, canArm: checks.every((check) => check.status === 'PASS'), checks };
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
