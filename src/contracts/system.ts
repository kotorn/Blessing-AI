export type SystemMode = 'RESEARCH' | 'BACKTEST' | 'PAPER' | 'TESTNET' | 'LIVE';

export type HealthStatus = 'HEALTHY' | 'DEGRADED' | 'DISCONNECTED' | 'UNKNOWN';

export interface ServiceHealth {
  service: string;
  status: HealthStatus;
  lastHeartbeat?: string;
  latencyMs?: number;
  message?: string;
}

export type AppRoute =
  | '/command'
  | '/markets'
  | '/strategies'
  | '/orders'
  | '/positions'
  | '/risk'
  | '/portfolio'
  | '/research/replay'
  | '/analytics'
  | '/system/connections'
  | '/system/audit'
  | '/settings';
