
export type DataSource = 'SIMULATED' | 'BINANCE';
export type ExchangeEnvironment = 'NONE' | 'BINANCE_TESTNET' | 'BINANCE_MAINNET';
export type ExecutionMode = 'PAPER' | 'TESTNET' | 'LIVE';
export type EngineState = 'DISARMED' | 'ARMING' | 'ARMED' | 'PAUSED_NEW_RISK' | 'RECOVERY_ONLY' | 'DEGRADED' | 'EMERGENCY';

export interface ExecutionCapabilities {
  paper: boolean;
  testnet: boolean;
  live: boolean;
  spot: boolean;
  usdmFutures: boolean;
  hedgeMode: boolean;
}

export interface TradingSystemState {
  dataSource: DataSource;
  exchangeEnvironment: ExchangeEnvironment;
  executionMode: ExecutionMode;
  engineState: EngineState;

  accountSynchronized: boolean;
  marketDataHealthy: boolean;
  privateStreamHealthy: boolean;
  tradingConnectionHealthy: boolean;

  reconciliationStatus: 'UNKNOWN' | 'IN_SYNC' | 'RECONCILING' | 'MISMATCH';

  killSwitchActive: boolean;
  pauseNewRisk: boolean;
  recoveryOnly: boolean;

  configVersion: string;
  updatedAt: string;
  workerResponsive?: boolean;
}

export interface PreflightCheck {
  id: string;
  name: string;
  required: boolean;
  status: 'PASS' | 'WARN' | 'FAIL' | 'UNKNOWN';
  message: string;
}

export interface PreflightResult {
  executionMode: ExecutionMode;
  canArm: boolean;
  checks: PreflightCheck[];
}

export interface RiskConfiguration {
  maxPortfolioDrawdownPct: number;
  maxGrossLeverage: number;
  maxMarginUtilizationPct: number;
  maxStrategyRiskUnits: Record<string, number>;
}

export type BasketState =
  | 'NEW'
  | 'ACTIVE'
  | 'GRID_EXPANDING'
  | 'PROFITABLE'
  | 'RECOVERY'
  | 'NO_NEW_GRID'
  | 'DELEVERAGING'
  | 'CLOSING'
  | 'CLOSED'
  | 'EMERGENCY_EXIT';

export type RiskState =
  | 'NORMAL'
  | 'CAUTION'
  | 'NO_NEW_GRID'
  | 'RECOVERY_ONLY'
  | 'DELEVERAGE'
  | 'EMERGENCY'
  | 'UNKNOWN';

export type MarketRegimeType =
  | 'R0_STRONG_MEAN_REVERSION'
  | 'R1_RANGE'
  | 'R2_WEAK_TREND'
  | 'R3_STRONG_TREND'
  | 'R4_BREAKOUT'
  | 'R5_VOLATILITY_SHOCK'
  | 'R6_CRISIS';

export interface GridLevelItem {
  level: number;
  price: number;
  size: number;
  status: 'FILLED' | 'PENDING' | 'CANCELLED';
  filled_at?: string;
}

export interface BasketItem {
  basket_id: string;
  venue: string;
  instrument: string;
  direction: 'LONG' | 'SHORT';
  state: BasketState;
  grid_depth: number;
  max_grid_levels: number;
  total_size: number;
  average_entry: number;
  current_mark_price: number;
  unrealized_pnl: number;
  trading_fees: number;
  funding_pnl: number;
  slippage_cost: number;
  net_pnl: number;
  created_at: string;
  last_updated: string;
  grid_levels: GridLevelItem[];
  data_source?: 'BINANCE_TESTNET' | 'SIMULATED';
  verified?: boolean;
}

export interface InstrumentData {
  symbol: string;
  spot_price: number;
  perp_price: number;
  basis: number;
  basis_pct: number;
  basis_zscore: number;
  funding_rate: number;
  funding_annualized_pct: number;
  atr_1h: number;
  realized_vol_24h_pct: number;
  open_interest_usd: number;
  open_interest_delta_24h_pct: number;
  regime: MarketRegimeType;
  regime_probabilities: Record<MarketRegimeType, number>;
  grid_safety_score: number;
  grid_status: string;
  expected_recovery_time_hrs: number;
  expected_mae_pct: number;
  prob_basket_profit: number;
  data_source?: 'BINANCE_TESTNET' | 'BINANCE_PUBLIC_MAINNET' | 'SIMULATED';
  verified?: boolean;
}

export interface AccountHolding {
  asset: string;
  qty: number;
  unitPrice: number;
  usdVal: number;
}

export interface AssetAllocationItem {
  location: string;
  category: 'TRADING_BOT' | 'PORTFOLIO_MARGIN' | 'EARN' | 'SPOT' | 'FUNDING';
  qty: number;
  usdVal: number;
  pctOfAsset: number;
  detail?: string;
}

export interface TwoLayerAsset {
  asset: string;
  totalQty: number;
  totalUsdVal: number;
  unitPrice: number;
  pctOfPortfolio: number;
  allocations: AssetAllocationItem[];
}

export interface SubWalletSummary {
  walletName: string;
  category: 'TRADING_BOT' | 'PORTFOLIO_MARGIN' | 'EARN' | 'SPOT' | 'FUNDING';
  btcVal: number;
  usdVal: number;
  pctOfTotal: number;
}

export interface AccountData {
  equity: number;
  balance: number;
  margin_utilization_pct: number;
  effective_leverage: number;
  free_margin: number;
  used_margin: number;
  daily_pnl: number;
  daily_pnl_pct: number;
  portfolio_drawdown_pct: number;
  kill_switch_active: boolean;
  risk_state: RiskState;
  realized_daily_pnl?: number;
  source?: 'BINANCE_LIVE' | 'BINANCE_TESTNET' | 'SIMULATED';
  evidence_status?: 'ILLUSTRATIVE_ONLY' | 'UNVERIFIED' | 'VERIFIED';
  verified?: boolean;
  spot_balance?: number;
  futures_wallet_balance?: number;
  futures_unrealized_pnl?: number;
  last_sync_time?: string;
  account_alias?: string;
  holdings?: AccountHolding[];
  two_layer_assets?: TwoLayerAsset[];
  sub_wallets?: SubWalletSummary[];
}

export interface RiskRuleItem {
  rule: string;
  current: string;
  status: 'PASS' | 'WARN' | 'FAIL' | 'UNKNOWN';
}

export interface BacktestMetrics {
  name: string;
  total_bars: number;
  total_baskets: number;
  win_baskets: number;
  failed_baskets: number;
  net_profit: number;
  roi_pct: number;
  max_equity_drawdown_pct: number;
  max_balance_drawdown_pct: number;
  equity_balance_divergence_pct: number;
  ulcer_index: number;
  expected_shortfall_99_pct: number;
  time_under_water_hrs: number;
  worst_basket_pnl: number;
  longest_recovery_hrs: number;
  max_grid_depth_reached: number;
  grid_depth_p95: number;
  emergency_exits: number;
  total_funding_cost: number;
  total_trading_fees: number;
  slippage_cost: number;
  profit_to_floating_dd_ratio: number;
  data_source?: 'SIMULATED' | 'HISTORICAL_DATASET';
  evidence_status?: 'ILLUSTRATIVE_ONLY' | 'UNVERIFIED' | 'VERIFIED';
  verified?: boolean;
  net_economic_pnl_verified?: boolean;
  execution_cost_model_status?: 'NOT_VERIFIED' | 'MODELED' | 'VERIFIED';
  launch_eligible?: boolean;
}

export interface SystemAlert {
  id: string;
  type: 'SHOCK' | 'FUNDING' | 'MARGIN' | 'SYSTEM' | 'RISK';
  severity: 'INFO' | 'WARNING' | 'CRITICAL';
  title: string;
  message: string;
  timestamp: string;
  symbol?: string;
}
