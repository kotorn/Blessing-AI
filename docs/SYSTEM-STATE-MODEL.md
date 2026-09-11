# System State Model (Blessing AI v0.2)

## Authoritative System State
The system state is fundamentally driven by the backend process, defining the boundaries between pure UI simulations, connected Testnet trading, and real-capital Live trading.

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
- \`PAPER\` / \`TESTNET\` can be armed at any time as long as the simulated dependencies are met.
- \`LIVE\` requires full cryptographic connection verification, \`capabilities.liveExecutionReady = true\`, and manual user risk acknowledgement.
- The **Kill Switch** overrides all local states and enforces \`EMERGENCY\` state universally across all components.

## Provenance
All orders and strategy intents now include a \`source\` field:
\`SIMULATED\` | \`BINANCE_TESTNET\` | \`BINANCE_LIVE\`
This prevents UI spoofing where a simulated paper order is visually mistaken for a real live order.
