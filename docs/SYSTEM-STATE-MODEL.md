# System State Model (Blessing AI v0.2)

## Authoritative System State
The system state is fundamentally driven by the Python Trading Worker, defining
the boundaries between pure UI simulations, connected Testnet trading, and the
explicitly gated real-capital Live mode.

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
- \`LIVE\` is available only behind dedicated Mainnet credentials,
  \`MAINNET_LIVE_APPROVED=true\`, and the Worker's complete Mainnet account,
  stream, reconciliation, risk, and execution-lease preflight. The default
  approval remains false; no UI acknowledgement or capability flag can bypass
  those gates.
- The **Kill Switch** overrides all local states and enforces \`EMERGENCY\` state universally across all components.

## Provenance
All orders and strategy intents now include a \`source\` field:
\`SIMULATED\` | \`BINANCE_TESTNET\` | \`BINANCE_MAINNET\`
This prevents UI spoofing where a simulated paper order is visually mistaken for a real live order.
