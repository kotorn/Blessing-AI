export type EngineState = 'DISARMED' | 'ARMING' | 'ARMED' | 'PAUSED_NEW_RISK' | 'RECOVERY_ONLY' | 'EMERGENCY';
export type ExecutionMode = 'PAPER' | 'TESTNET' | 'LIVE';
export type ActionRiskClass = 'NEW_RISK' | 'INCREASE_RISK' | 'REDUCE_RISK' | 'RECOVERY' | 'CLOSE' | 'EMERGENCY';

export interface ExecutionCapabilities {
  paper: boolean;
  testnet: boolean;
  live: boolean;
  spot: boolean;
  usdmFutures: boolean;
  hedgeModeSupported: boolean;
  liveExecutionReady: boolean;
}

export interface ActiveTradingConfiguration {
  executionMode: ExecutionMode;
  instruments: string[];
  strategies: {
    grid: boolean;
    trend: boolean;
    shock: boolean;
    carry: boolean;
  };
  riskProfile: 'CONSERVATIVE' | 'BALANCED' | 'AGGRESSIVE';
  riskConfiguration: RiskConfiguration;
  configVersion: string;
  armedAt: string;
}

export interface RiskConfiguration {
  maxPortfolioDrawdownPct: number;
  maxGrossLeverage: number;
  maxMarginUtilizationPct: number;
  maxStrategyRiskUnits: Record<string, number>;
}

export interface TradingSystemState {
  dataSource: 'SIMULATED' | 'BINANCE';
  exchangeEnvironment: 'NONE' | 'BINANCE_TESTNET' | 'BINANCE_MAINNET';
  executionMode: ExecutionMode;
  engineState: EngineState;

  accountSynchronized: boolean;
  marketDataHealthy: boolean;
  privateStreamHealthy: boolean;
  tradingConnectionHealthy: boolean;
  reconciliationStatus: 'UNKNOWN' | 'IN_SYNC' | 'MISMATCH';

  killSwitchActive: boolean;
  pauseNewRisk: boolean;
  recoveryOnly: boolean;

  configVersion: string;
  updatedAt: string;
  activeConfiguration?: ActiveTradingConfiguration;
}

export interface PreflightResult {
  executionMode: ExecutionMode;
  canArm: boolean;
  checks: Array<{
    id: string;
    name: string;
    required: boolean;
    status: 'PASS' | 'FAIL' | 'WARN' | 'UNKNOWN';
    message: string;
  }>;
}

