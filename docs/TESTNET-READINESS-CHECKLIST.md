# Binance USDⓈ-M Testnet readiness

This checklist is evidence-based. `LOCAL_VERIFIED` means the command was run
in the current local working tree; it does not mean GitHub CI or Binance
Testnet has been verified.

## Status

| Gate | Status | Evidence boundary |
|---|---|---|
| Reproducible `npm ci`, TypeScript lint/tests/build | `LOCAL_VERIFIED; CI_VERIFIED` | Current candidate `bc45677`; GitHub Actions run `34754008070` passed Node install/lint/tests/build. |
| Worker import smoke and non-secret Python tests | `LOCAL_VERIFIED; CI_VERIFIED` | Current candidate `bc45677`; GitHub Actions run `34754008070` passed Python install/tests and hygiene. |
| Net economics and WFO/OOS research evaluator | `LOCAL_VERIFIED; RESEARCH_ONLY` | Explicit fees/funding/spread/slippage inputs, purged/embargoed OOS folds, regime coverage, and parameter plateau checks; never launch evidence |
| Worker/adapter state contract | `UNIT_TESTED` | Adapter state is canonical; worker mirrors it |
| Truthful Testnet readiness | `UNIT_TESTED` | Requires credentials, signed account, rules, stream, fresh account/market data, and `IN_SYNC` |
| Binance CLI research cross-check boundary | `UNIT_TESTED; LOCAL_VERIFIED` | The isolated wrapper allowlists read-only USDⓈ-M checks and blocks Mainnet/mutations; no CLI binary or credentials were present |
| Account snapshot and liquidation math | `UNIT_TESTED` | Uses Binance account/position-risk fields; liquidation is `UNKNOWN` when unusable |
| Reconciliation and canonical fill recovery | `UNIT_TESTED` | Missing fill recovery cannot produce `IN_SYNC` |
| Read-only Binance Testnet contract | `NOT_RUN` | No local Testnet credentials were configured in this session |
| Controlled manual Testnet mutation | `NOT_RUN` | Requires explicit `TESTNET_MANUAL_TRIAL_APPROVED=true` and read-only evidence |
| Credentialed contract evidence handoff | `CODE_PRESENT` | The workflow runs non-secret tests first, records current-SHA evidence after read-only success in either mode, and promotes manual evidence only after the mutating trial passes |
| GitHub CI for current candidate `bc45677` | `CI_VERIFIED` | Run `34754008070` passed Node install/lint/tests/build, Python install/tests, and hygiene |
| Autonomous Testnet execution | `LOCKED` | Requires current-SHA evidence, approval, and runtime readiness; default is false |
| Mainnet execution | `DISABLED` | LIVE ARM and mutable Mainnet adapter construction are rejected |

## Required runtime formula

`testnetExecutionReady` is true only when all of these are true:

```text
Testnet configured
AND signed account authentication succeeded
AND signed account permission reports `canTrade == true`
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
The local non-secret gates were rerun for the current working tree. GitHub CI
verified the current candidate `bc45677` in run `34754008070`.
