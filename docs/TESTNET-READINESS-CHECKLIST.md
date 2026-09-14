# Binance USDⓈ-M Testnet readiness

This checklist is evidence-based. `LOCAL_VERIFIED` means the command was run
in the current local working tree; it does not mean GitHub CI or Binance
Testnet has been verified.

## Status

| Gate | Status | Evidence boundary |
|---|---|---|
| Reproducible `npm ci`, TypeScript lint/tests/build | `LOCAL_VERIFIED; CI_VERIFIED` | The exact clean commit SHA is recorded in the generated `build-evidence.json`; the exact-SHA GitHub Actions check must pass Node install/lint/tests/build. |
| Worker import smoke and non-secret Python tests | `LOCAL_VERIFIED; CI_VERIFIED` | The exact clean commit SHA is recorded in the generated `build-evidence.json`; the exact-SHA GitHub Actions check must pass Python install/tests and hygiene. |
| Net economics and WFO/OOS research evaluator | `LOCAL_VERIFIED; RESEARCH_ONLY` | Explicit fees/funding/spread/slippage inputs, contiguous complete-horizon research candles, purged/embargoed OOS folds, regime coverage, parameter plateau, common OOS-fold coverage, and train-only selection binding; never launch evidence |
| Deterministic strategy replay and event-time WFO windows | `UNIT_TESTED; LOCAL_VERIFIED; RESEARCH_ONLY` | Existing strategy engines run through allocator, recovery, RiskGovernor, per-order research gate, recorded bid/ask/depth fills, explicit funding settlement, event-level equity, and canonical fill lineage; train-only variant selection is replayed on untouched OOS windows; never launch evidence |
| Causal Binance public event-dataset assembler | `UNIT_TESTED; LOCAL_VERIFIED; RESEARCH_ONLY` | Requires independent 1m klines, mark-price klines, and historical bookTicker observations; rejects gaps, stale/future quotes, conflicts, mixed symbols, and missing fields; supports source-archive SHA-256 verification and deterministic JSONL hashes; bounded real-data run at `e7da4f7` verified 15 events but produced negative net PnL, so it is not edge or launch evidence |
| Worker/adapter state contract | `UNIT_TESTED` | Adapter state is canonical; worker mirrors it |
| Truthful Testnet readiness | `UNIT_TESTED` | Requires credentials, signed account, rules, stream, fresh account/market data, and `IN_SYNC` |
| Binance CLI research cross-check boundary | `UNIT_TESTED; LOCAL_VERIFIED` | The isolated wrapper allowlists read-only USDⓈ-M checks and blocks Mainnet/mutations; no CLI binary is configured |
| Public Testnet BTCUSDT rule spot-check | `LOCAL_VERIFIED` | Public GET-only `exchangeInfo` spot-check recorded on 2026-09-14 reports `TRADING`, `MIN_NOTIONAL=50 USDT`, `tickSize=0.10`, and `stepSize=0.0001`; this does not authenticate or authorize execution |
| Account snapshot, liquidation math, and derived risk state | `UNIT_TESTED; LOCAL_VERIFIED` | Uses Binance account/position-risk fields; liquidation is `UNKNOWN` when unusable and unsafe account metrics set `NO_NEW_RISK` |
| Spot/Portfolio Margin wallet observation | `CODE_PRESENT` (unverified) | Separate from USDⓈ-M Futures Testnet collateral; a Spot-to-Portfolio-Margin transfer cannot authorize the Worker or satisfy Testnet readiness |
| Reconciliation and canonical fill recovery | `UNIT_TESTED` | Missing fill recovery cannot produce `IN_SYNC` |
| Read-only Binance Testnet contract | `LOCAL_VERIFIED; CONTRACT_TESTED` | Worker-level contract on 2026-09-14 passed (`1 passed, 2 deselected`) using signed read-only Testnet calls; no mutation |
| Controlled manual Testnet mutation | `NOT_RUN` | Live BTCUSDT rules report `MIN_NOTIONAL=50 USDT`, within the 100 USDT first-launch cap; manual approval remains disabled |
| Credentialed contract evidence handoff | `CODE_PRESENT` | The workflow runs non-secret tests first, records current-SHA evidence after read-only success in either mode, and promotes manual evidence only after the mutating trial passes |
| Supervised bounded Testnet soak runner | `UNIT_TESTED` | Worker-owned market-event path, explicit caps, fill bounds, cleanup, and reconciliation are implemented; no soak approval or mutation was run |
| GitHub CI for verified safety/research implementation candidate | `CI_VERIFIED` | The PR check and generated evidence artifact must report success for the same clean commit SHA; the workflow is the authoritative run record. |
| Autonomous Testnet execution | `LOCKED` | Requires current-SHA evidence, successful manual trial, both explicit autonomous flags, approval, and runtime readiness; defaults are false |
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

The safe defaults are BTCUSDT only, 100 USDT maximum single-order notional,
100 USDT maximum total open notional, one open order, and one active exposure
chain. Positive environment overrides are supported; empty, malformed, or
non-positive values fall back to these defaults.

All orders pass both the worker decision gate and an individual order gate.
Market orders require a fresh Testnet market price. No synthetic price is
used. Positive limit overrides require `TESTNET_LIMITS_OVERRIDE_APPROVED=true`;
without that acknowledgement, malformed or larger values remain bounded by
the first-launch defaults.

## Evidence boundary for this session

Dedicated Binance Testnet credentials were present in the ignored local
`.env`; their values are intentionally not recorded. The Worker-level signed
read-only contract passed on 2026-09-14 (`1 passed, 2 deselected`) and covered
server time, exchangeInfo/rules, authentication, account snapshot, position
mode, positions, open orders, private stream health/keepalive, bootstrap, and
reconciliation. No Testnet order was submitted and no Testnet trial artifact
exists.
The recorded public Testnet BTCUSDT spot-check from 2026-09-14 reported a 50 USDT minimum notional,
which is within the configured 100 USDT first-launch cap. The manual trial
remains `NOT_RUN` because `TESTNET_MANUAL_TRIAL_APPROVED` is false; no Testnet
order was submitted and no trial artifact exists.
The local non-secret gates were rerun for the current clean checkout. The
generated `artifacts/build-evidence.json` records the exact commit SHA and the
exact-SHA GitHub CI result; the workflow artifact is the authoritative handoff
when this document is viewed from a clean checkout.

The deterministic replay is now available for validated public read-only event
datasets. It is intentionally conservative: unknown historical liquidation
safety blocks further risk increase after entry, missing funding settlements
fail closed, insufficient recorded depth rejects a fill, and an explicit
end-of-sample close is required for a complete trade result. No replay result
is Testnet evidence or proof of positive expected return; real dataset hashes,
walk-forward train-only selection artifacts, and statistical robustness remain
required before any capital decision. The replay runner can produce
train-only selection hashes and untouched OOS results, but this repository has
not yet verified a qualifying real-data edge or approved a capital increase.

The research-only event assembler joins the independent Binance Vision/public
observations causally at closed 1-minute events. A kline close is never used as
a substitute for a quote: missing mark-price or top-of-book data rejects the
dataset, and an archive must be verified against its published SHA-256 before
its digest is recorded in a research manifest. The repository contains parser
and join tests only; it does not claim that a qualifying multi-source archive,
positive net expectancy, or walk-forward result has been produced here.

The first bounded real-data replay was run locally from the 2024-01-15
BTCUSDT public USDⓈ-M archives. Its self-contained artifact was verified at
build SHA `e7da4f7805cf3ce3f9f3c710d2cb6bc5b17732cd`, with 15 closed 1-minute
events, 10 accepted simulated fills, 5 completed trades, and net PnL
`-22.44961953372701969589640263` after the explicit cost model. This is a
negative research observation and a pipeline-validation artifact only; it does
not satisfy multi-fold OOS evidence, positive expected return, Testnet
mutation, or any capital-increase gate.
