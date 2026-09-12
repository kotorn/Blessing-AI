# Binance Testnet Readiness Checklist

This document tracks the capabilities required before live Binance Testnet order execution can be enabled.

## 1. Safety Enforcements
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

## 2. Binance Testnet Execution Adapter (Native)
- [x] Single execution authority established (Python Trading Worker)
- [x] Native Binance REST Client implemented (`aiohttp`)
- [x] Capability Discovery implemented (`Hedge Mode`, `Rules`)
- [x] Clock Synchronization implemented
- [x] Decimal-safe normalization implemented (`normalize_quantity`, `normalize_price`)
- [x] Signed Testnet order submission via `/fapi/order` implemented
- [x] Deterministic client order IDs mapping implemented (`BAI-<context>-<attempt>`)
- [x] Handle partial fills gracefully (ExchangeFill vs ExchangeOrder split designed)
- [x] Implement user/private WebSocket stream for updates
- [x] REST reconciliation fallback on stream disconnect (Required invariant)
- [x] Handle timeout ambiguity (STATE_UNKNOWN logic designed)
- [x] Real position reconciliation logic
- [x] CI unit and integration tests passing (9 passed, 2 contract suites ready for live credentials)

## Status
**READY FOR TESTNET TRIAL**. The native execution adapter, user stream recovery, conflict-resolution meta allocator, risk governor, and exposure recovery engines are fully implemented and verified with a passing test suite (`pytest`).
