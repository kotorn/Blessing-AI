# Execution Adapter Contract

To ensure safety and decouple strategy logic from specific exchanges, all execution is routed through the Python `BinanceExecutionAdapter`. The TypeScript backend only acts as a gateway and does not have an execution adapter of its own.

## Adapter Responsibilities

1. **Symbol Normalization:** Fetches exchange symbol rules (stepSize, minQty, tickSize) and rigorously formats target quantities/prices using native Python `Decimal`.
2. **Order Placement:** Sends `ExecutionDecision`s to the exchange.
3. **Deterministic Identity:** Generates deterministic `clientOrderId`s to prevent duplicate execution during timeout ambiguity (e.g., `BAI-<hash>-<attempt>`).
4. **Reconciliation:** Maintains synchronization between exchange positions, open orders, and internal `ExecutionLedger`.
5. **Clock Synchronization:** Maintains `server_time_offset_ms` to avoid timestamp errors during signature generation.

## Fill Ledger Contract

An Order is not a Fill. The system maintains separate records:
- `ExecutionOrder`: The intent placed on the exchange (e.g., LIMIT 1.0 BTC @ 64,000).
- `ExchangeFill`: An actual trade execution record. Can be partial. Contains commission, realized PnL, maker/taker flag.

## Timeout Ambiguity Invariant

If an order placement request times out (`SUBMITTING` -> `RESPONSE_UNKNOWN`), the system transitions to `RECONCILING` and blocks new risk until a query by `origClientOrderId` determines if the order was placed. 

## Supported Implementations

1. **`PAPER`**:
   - Owns simulated orders and mock fills locally.
   - Used for research, historical replay, and UI testing.
   - Logs `[PAPER][SIMULATED] EXECUTION DECISION`.

2. **`TESTNET`**:
   - Interfaces directly with Binance Testnet API via native `aiohttp`.
   - Enforces deterministic client IDs and handles WebSocket user streams.
   - Requires explicit opt-in environment validation.

3. **`LIVE`**:
   - Currently hard-blocked.
   - Cannot be accidentally triggered.
