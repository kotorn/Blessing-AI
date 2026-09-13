# Binance USDⓈ-M Testnet readiness

This checklist is evidence-based. `LOCAL_VERIFIED` means the command was run
in the current local working tree; it does not mean GitHub CI or Binance
Testnet has been verified.

## Status

| Gate | Status | Evidence boundary |
|---|---|---|
| Reproducible `npm ci`, TypeScript lint/tests/build | `LOCAL_VERIFIED; CI_VERIFIED` | Candidate `d109f3ab46dca5ba7c966cb34ff6502e994f9943`; GitHub Actions run `34767576411` passed Node install/lint/tests/build. |
| Worker import smoke and non-secret Python tests | `LOCAL_VERIFIED; CI_VERIFIED` | Candidate `d109f3ab46dca5ba7c966cb34ff6502e994f9943`; GitHub Actions run `34767576411` passed Python install/tests and hygiene. |
| Net economics and WFO/OOS research evaluator | `LOCAL_VERIFIED; RESEARCH_ONLY` | Explicit fees/funding/spread/slippage inputs, contiguous complete-horizon research candles, purged/embargoed OOS folds, regime coverage, parameter plateau, common OOS-fold coverage, and train-only selection binding; never launch evidence |
| Worker/adapter state contract | `UNIT_TESTED` | Adapter state is canonical; worker mirrors it |
| Truthful Testnet readiness | `UNIT_TESTED` | Requires credentials, signed account, rules, stream, fresh account/market data, and `IN_SYNC` |
| Binance CLI research cross-check boundary | `UNIT_TESTED; LOCAL_VERIFIED` | The isolated wrapper allowlists read-only USDⓈ-M checks and blocks Mainnet/mutations; no CLI binary or credentials were present |
| Public Testnet BTCUSDT rule spot-check | `LOCAL_VERIFIED` | Public GET-only `exchangeInfo` spot-check recorded on 2026-09-13 reports `TRADING`, `MIN_NOTIONAL=50 USDT`, `tickSize=0.10`, and `stepSize=0.0001`; this does not authenticate or authorize execution |
| Account snapshot, liquidation math, and derived risk state | `UNIT_TESTED; LOCAL_VERIFIED` | Uses Binance account/position-risk fields; liquidation is `UNKNOWN` when unusable and unsafe account metrics set `NO_NEW_RISK` |
| Spot/Portfolio Margin wallet observation | `CODE_PRESENT` (unverified) | Separate from USDⓈ-M Futures Testnet collateral; a Spot-to-Portfolio-Margin transfer cannot authorize the Worker or satisfy Testnet readiness |
| Reconciliation and canonical fill recovery | `UNIT_TESTED` | Missing fill recovery cannot produce `IN_SYNC` |
| Read-only Binance Testnet contract | `NOT_RUN` | No local Testnet credentials were configured in this session |
| Controlled manual Testnet mutation | `NOT_RUN` | Requires explicit `TESTNET_MANUAL_TRIAL_APPROVED=true` and read-only evidence |
| Credentialed contract evidence handoff | `CODE_PRESENT` | The workflow runs non-secret tests first, records current-SHA evidence after read-only success in either mode, and promotes manual evidence only after the mutating trial passes |
| GitHub CI for verified safety/research implementation candidate `d109f3a` | `CI_VERIFIED` | Run `34767576411` passed Node install/lint/tests/build, Python install/tests, and hygiene |
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

## Wallet-product boundary

The Worker reads only the Binance USDⓈ-M Futures Testnet account and position
endpoints. Spot and Portfolio Margin balances belong to a separate account
product. The TypeScript control-plane can expose a strictly parsed, signed
Portfolio Margin observation for display, but it is always read-only and
unverified relative to the Worker; missing or malformed Portfolio Margin
fields are not converted to zero. No Portfolio Margin transfer is performed
by this repository.

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
The recorded public Testnet BTCUSDT spot-check from 2026-09-13 reported a 50 USDT minimum notional,
which exceeds the configured 25 USDT first-launch cap; the manual trial must
abort until the exchange rule changes or an explicitly approved cap change is
made. The cap was not raised automatically.
The local non-secret gates were rerun for the safety/research implementation
candidate `d109f3ab46dca5ba7c966cb34ff6502e994f9943`. GitHub CI verified that
same candidate in run `34767576411`.
