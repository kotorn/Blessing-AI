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

## Current evidence status

For runtime candidate `cd4e2f91f60527e52d738c0f6ae63e53da7253ac`, non-secret
unit and frontend gates are `LOCAL_VERIFIED` and `CI_VERIFIED` by GitHub
Actions run `34747554913`. The credentialed contract workflow now records
current-SHA sanitized evidence after read-only success and only marks the
manual trial verified after the mutating test passes; that workflow was not
run here. Credentialed Binance Testnet read-only and mutation trials are
`NOT_RUN` because no Testnet credentials were configured. Autonomous Testnet
execution remains locked, and Mainnet execution remains disabled.
