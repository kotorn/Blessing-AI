# Execution Adapter Contract

To ensure safety and decouple strategy logic from specific exchanges, all execution must be routed through an `ExecutionAdapter`.

## Adapter Interface

```typescript
export interface ExecutionCapabilities {
  paper: boolean;
  testnet: boolean;
  live: boolean;
  spot: boolean;
  usdmFutures: boolean;
  hedgeModeSupported: boolean;
  liveExecutionReady: boolean;
}

export interface ExecutionAdapter {
  getCapabilities(): Promise<ExecutionCapabilities>;
  
  reconcile(): Promise<ReconciliationResult>;
  
  getOpenOrders(): Promise<ExchangeOrder[]>;
  
  getPositions(): Promise<ExchangePosition[]>;
  
  placeOrder(request: OrderPlacementRequest): Promise<OrderPlacementResult>;
  
  cancelOrder(request: OrderCancellationRequest): Promise<OrderCancellationResult>;
}
```

## Supported Implementations

1. **`PaperExecutionAdapter` (Current)**:
   - Owns simulated orders and mock fills.
   - Used for research, historical replay, and UI testing.
   - Appends `source: 'SIMULATED'` to all executions.

2. **`BinanceTestnetExecutionAdapter` (Next Sprint)**:
   - Interfaces directly with Binance Testnet API.
   - Enforces deterministic client IDs and handles WebSocket user streams.
   - Appends `source: 'BINANCE_TESTNET'` to all executions.

3. **`BinanceLiveExecutionAdapter` (Future)**:
   - Interfaces with Binance Mainnet.
   - Requires full compliance with production live-readiness constraints.
   - Appends `source: 'BINANCE_LIVE'` to all executions.

Strategies must **never** call Binance HTTP endpoints or CCXT functions directly.
