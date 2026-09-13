# Binance USDⓈ-M Testnet readiness

This checklist is evidence-based. `LOCAL_VERIFIED` means the command was run
in the current local working tree; it does not mean GitHub CI or Binance
Testnet has been verified.

## Status

| Gate | Status | Evidence boundary |
|---|---|---|
| Reproducible `npm ci`, TypeScript lint/tests/build | `LOCAL_VERIFIED; CI_VERIFIED` | Local checkout plus GitHub Actions run `34746139860` for candidate `42e082418ef0e5360e74d586942a4fa176113288` |
| Worker import smoke and non-secret Python tests | `LOCAL_VERIFIED; CI_VERIFIED` | Local checkout plus GitHub Actions run `34746139860` for candidate `42e082418ef0e5360e74d586942a4fa176113288` |
| Worker/adapter state contract | `UNIT_TESTED` | Adapter state is canonical; worker mirrors it |
| Truthful Testnet readiness | `UNIT_TESTED` | Requires credentials, signed account, rules, stream, fresh account/market data, and `IN_SYNC` |
| Account snapshot and liquidation math | `UNIT_TESTED` | Uses Binance account/position-risk fields; liquidation is `UNKNOWN` when unusable |
| Reconciliation and canonical fill recovery | `UNIT_TESTED` | Missing fill recovery cannot produce `IN_SYNC` |
| Read-only Binance Testnet contract | `NOT_RUN` | No local Testnet credentials were configured in this session |
| Controlled manual Testnet mutation | `NOT_RUN` | Requires explicit `TESTNET_MANUAL_TRIAL_APPROVED=true` and read-only evidence |
| GitHub CI for candidate `42e0824` | `CI_VERIFIED` | Run `34746139860` passed Node install/lint/tests/build, Python install/tests, and hygiene |
| Autonomous Testnet execution | `LOCKED` | Requires current-SHA evidence, approval, and runtime readiness; default is false |
| Mainnet execution | `DISABLED` | LIVE ARM and mutable Mainnet adapter construction are rejected |

## Required runtime formula

`testnetExecutionReady` is true only when all of these are true:

```text
Testnet configured
AND signed account authentication succeeded
AND adapter READY
AND active symbol rules are complete and TRADING
AND private user stream is connected
AND reconciliation is IN_SYNC
AND the account snapshot is valid, Testnet-owned, complete, and fresh
AND market data is fresh for every active symbol
AND the local kill switch is inactive
```

Infrastructure readiness does not depend on `engine_state == ARMED`.

## First-launch Testnet limits

The safe defaults are BTCUSDT only, 25 USDT maximum single-order notional,
50 USDT maximum total open notional, one open order, and one active exposure
chain. Positive environment overrides are supported; empty, malformed, or
non-positive values fall back to these defaults.

All orders pass both the worker decision gate and an individual order gate.
Market orders require a fresh Testnet market price. No synthetic price is
used. Positive limit overrides require `TESTNET_LIMITS_OVERRIDE_APPROVED=true`;
without that acknowledgement, malformed or larger values remain bounded by
the first-launch defaults.

## Evidence boundary for this session

No Binance Testnet credentials were present locally, so the signed read-only
contract, private-stream contract, and controlled mutation trial are
`NOT_RUN`. No Testnet order was submitted and no Testnet trial artifact exists.
The local non-secret gates and GitHub CI were verified for candidate
`42e082418ef0e5360e74d586942a4fa176113288` by run `34746139860`. Any later
source change requires a new current-SHA workflow result.
