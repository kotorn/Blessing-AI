# System State Model (Blessing AI v0.2)

## Authoritative System State
The system state is fundamentally driven by the Python Trading Worker, defining
the boundaries between pure UI simulations, connected Testnet trading, and the
permanently disabled real-capital Live mode.

### Contract Definition
```typescript
interface TradingSystemState {
  dataSource: 'SIMULATED' | 'BINANCE';
  exchangeEnvironment: 'NONE' | 'BINANCE_TESTNET' | 'BINANCE_MAINNET';
  executionMode: 'PAPER' | 'TESTNET' | 'LIVE';
  engineState: 'DISARMED' | 'ARMING' | 'ARMED' | 'PAUSED_NEW_RISK' | 'RECOVERY_ONLY' | 'EMERGENCY';

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
}
```

### Safety Transitions
- \`PAPER\` can be armed when its local simulation controls allow it.
- \`TESTNET\` can be armed only after the Worker completes configuration,
  authentication, stream, account, market-data, and reconciliation preflight.
- \`LIVE\` is permanently rejected in this sprint. No capability flag,
  acknowledgement, or UI state can enable Mainnet mutable execution.
- The **Kill Switch** overrides all local states and enforces \`EMERGENCY\` state universally across all components.

## Provenance
All orders and strategy intents now include a \`source\` field:
\`SIMULATED\` | \`BINANCE_TESTNET\` | \`BINANCE_LIVE\`
This prevents UI spoofing where a simulated paper order is visually mistaken for a real live order.
