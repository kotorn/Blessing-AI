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

## Execution gates

The decision gate checks worker mode/state, authentication, stream health,
reconciliation, account freshness for risk-increasing decisions, market
freshness, kill switch, and the economic risk class. For risk reduction or
emergency handling, a stale account snapshot does not by itself block the
safer fallback; the order gate still requires reduce-only semantics and
known/authoritative exposure where applicable. The order gate independently checks every `OrderIntent`
for symbol status, USDⓈ-M market type, supported order type, quantity/price
normalization, exchange filters, Testnet caps, and reduce-only semantics.

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
