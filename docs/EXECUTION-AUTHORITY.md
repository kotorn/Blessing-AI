# Execution authority

The Python Trading Worker is the only component authorized to submit mutable
orders. The TypeScript/Express process is a control-plane and UI gateway: it
may display state and forward typed controls, but it does not make risk
decisions or submit orders.

```text
UI / TypeScript control plane
          |
          v
Python Trading Worker
  decision gate -> order gate -> Testnet adapter
          |
          v
Binance USDⓈ-M Testnet
```

No ML, RL, Transformer, Gemini, Claude, Codex, or other LLM is part of the
live execution authority. Research or strategy output is not an execution
authorization.

The TypeScript control plane may perform explicitly read-only observation for
the UI, but those responses are labelled `UNVERIFIED` until the Python worker
reports signed authentication, a healthy private stream, a fresh account
snapshot, and `IN_SYNC` reconciliation. The control plane cannot promote its
own REST observation to execution readiness.

## Environment invariant

The mutable native Binance adapter, REST execution client, and private user
stream are Testnet-only. `LIVE` ARM is rejected, mutable Mainnet adapter
construction is rejected, and `liveExecutionReady` / `small_live_ready` stay
false.

The worker requires `BINANCE_TESTNET=true` plus dedicated Testnet credentials.
Authentication is authoritative only after capability discovery has succeeded
and a signed `/fapi/v2/account` request has returned a valid account payload.
Execution authorization additionally requires that payload's `canTrade`
permission to be explicitly true. A connected user stream must also pass a
recent WebSocket ping/pong or private-event freshness check; socket
establishment alone is not a readiness signal.

## Spot and Portfolio Margin boundary

Spot and Portfolio Margin are separate Binance account products from the
USDⓈ-M Futures Testnet account owned by the Worker. A Spot-to-Portfolio-Margin
transfer is therefore not Testnet Futures collateral, does not populate the
Worker's `ExchangeAccountSnapshot`, and cannot make `testnetExecutionReady`
true. The TypeScript control-plane may show a signed Portfolio Margin balance
as `OBSERVED_READ_ONLY`, but it marks the observation unverified and
`includedInWorkerCollateral=false`. It never performs a Portfolio Margin
transfer and never promotes that observation into execution authority.

## Readiness and reconciliation

An adapter is not ready merely because it exists. Before `READY`, the worker
requires symbol rules, a connected private stream, a complete fresh Testnet
account snapshot, fresh market data for each active symbol, and authoritative
reconciliation. Bootstrap fetches positions, open orders, and account data;
an empty ledger may adopt that authoritative snapshot, while existing local
orders/positions are compared before any refresh. It recovers fills, compares
the ledger with the exchange, and only then reports `IN_SYNC`.

Liquidation distance is calculated from position-side-aware mark and
liquidation prices. Missing or unusable liquidation information is
`UNKNOWN`, not an invented percentage; risk-increasing decisions fail closed.
Risk-increasing decisions also require positive available Testnet balance and
margin utilization below the 70% safety limit.

## Execution gates

The decision gate checks worker mode/state, authentication, stream health,
reconciliation, account freshness for risk-increasing decisions, market
freshness, kill switch, and the economic risk class. For risk reduction or
emergency handling, a stale account snapshot does not by itself block the
safer fallback; the order gate still requires reduce-only semantics and
known/authoritative exposure where applicable. The order gate independently checks every `OrderIntent`
for symbol status, USDⓈ-M market type, supported order type, quantity/price
normalization, exchange filters, Testnet caps, and reduce-only semantics.

The structural grid is an intent-only component. It brakes in trend, breakout,
transition, and shock regimes, refuses depths at or above its five-level cap,
and decreases the next delta as depth grows; it never uses a martingale size
progression.

Economic classes are explicit and separate from command strings:
`NEW_RISK`, `INCREASE_RISK`, `REDUCE_RISK`, `RECOVERY`, `CLOSE`, and
`EMERGENCY`. Pause-new-risk and recovery-only modes block only the first two
classes; the kill switch remains locally active until exchange cancellation
and reconciliation are verified.

## Ambiguity and fills

An ambiguous mutable response is never blindly resubmitted. The worker queries
the client order ID, runs authoritative reconciliation, requires a healthy
private stream, and returns to `READY` only after all evidence is consistent.
Recovered fills use the canonical `domain.models.ExchangeFill` model and are
deduplicated by symbol plus exchange trade ID.

An `ORDER_TRADE_UPDATE` without a local Worker order record is quarantined and
sets reconciliation to `UNKNOWN`; it is never adopted as a local strategy
order. This preserves the required StrategyIntent -> Risk Decision -> Order
lineage, including when an exchange event races a REST acknowledgement.

## Optional Binance CLI research cross-check

The official Binance CLI may be used through the isolated
`apps/trading_worker/research/binance_cli.py` wrapper for read-only USDⓈ-M
Testnet observations. It is not an execution authority, does not replace the
Worker's private stream or reconciliation, and cannot promote readiness. The
wrapper rejects Mainnet/demo routes, profiles, custom requests, and all
mutable CLI commands; details and commands are in
`docs/BINANCE-CLI-RESEARCH.md`.

## Current evidence status

The current verified safety/research implementation checkpoint
`3501414ac33685f60fc1bf2dc7536d14b1e277a0` passed the non-secret unit and
frontend gates in GitHub Actions run `34779085395`. The credentialed contract
workflow records current-SHA sanitized evidence after read-only success and
only marks the manual trial verified after the mutating test passes. The
read-only contract passed locally on 2026-09-14; the controlled mutation trial
remains `NOT_RUN` because the live BTCUSDT minimum notional observed on Testnet
was 50 USDT while the hard first-launch cap is 25 USDT. The supervised soak
runner is implemented and unit-tested but was not approved or run. Autonomous
Testnet execution remains locked, and Mainnet execution remains disabled.
