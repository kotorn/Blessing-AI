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

## 2. Binance Testnet Execution Adapter
- [x] Single execution authority established (Python Trading Worker)
- [x] Signed Testnet order submission via `/fapi/` scaffolding implemented
- [x] Deterministic client order IDs mapping implemented
- [x] Handle partial fills gracefully (ExchangeFill vs ExchangeOrder split designed)
- [x] Handle cancel / replace (Audited)
- [ ] Implement user/private WebSocket stream for updates
- [ ] REST reconciliation fallback on stream disconnect (Required invariant)
- [x] Handle timeout ambiguity (STATE_UNKNOWN logic designed)
- [ ] Real position reconciliation logic
- [ ] CI Contract tests implemented and passing

## Status
**IMPLEMENTATION PRESENT — CONTRACT VALIDATION REQUIRED**. The adapter exists in Python but lacks final CI contract test validation, WebSocket stream implementations, and robust reconciliation before it can be considered production ready for Testnet.
