# Binance Testnet Readiness Checklist

This document tracks the capabilities required before live Binance Testnet order execution can be enabled.

## 1. Safety Enforcements (This Sprint)
- [x] Backend-authoritative `TradingSystemState`
- [x] Preflight checks re-run server-side on ARM
- [x] Testnet capability explicitly declared as `false` until adapter exists
- [x] Successful sync correctly sets `dataSource = BINANCE` and `accountSynchronized = true`
- [x] `executionMode` does not silently become `LIVE` after account sync
- [x] State-transition rules enforced (`EMERGENCY` -> `ARMED` blocked, etc)
- [x] Pause New Risk / Recovery Only correctly block backend actions
- [x] Mock execution orders are truthfully labeled with `source = SIMULATED`
- [x] Backend audit events repository implemented
- [x] Automated state-machine tests added

## 2. Binance Testnet Execution Adapter (Next Sprint)
- [x] Implement `BinanceTestnetExecutionAdapter` abstraction
- [x] Signed Testnet order submission via `/fapi/`
- [x] Deterministic client order IDs mapping
- [x] Handle partial fills gracefully
- [x] Handle cancel / replace
- [x] Implement user/private WebSocket stream for updates
- [x] REST reconciliation fallback on stream disconnect
- [x] Handle timeout ambiguity (idempotency guarantees)
- [x] Real position reconciliation logic

## Status
**UNBLOCKED**. Binance Testnet Execution Ledger & Reconciliation implemented.
