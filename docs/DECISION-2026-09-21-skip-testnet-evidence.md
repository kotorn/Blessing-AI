# Decision record — Skip Testnet evidence for the first Mainnet launch

- Date: 2026-09-21
- Decision owner: operator (KOTORN), recorded via ZCode session
- Status: ACCEPTED — waives the Testnet contract/soak **evidence chain** only

## Decision

The Testnet contract evidence chain (readonly / mutating / soak runs against
Binance Testnet) is waived for the first ETHUSDC Mainnet launch. Paper-trading
coverage is accepted as the pre-mainnet behavioral evidence: the Worker in
`PAPER` mode exercises the full decision path and records simulated fills
without sending orders.

## Compensating evidence channels (in place of Testnet)

1. **Mainnet read-only preflight (runbook Gate 3)** — the first real contact
   with Binance Mainnet endpoints happens as a signed, order-free preflight on
   the LIVE-disarmed revision: `canTrade`, symbol filters from live
   `exchangeInfo`, private stream, reconciliation `IN_SYNC`, zero
   order-endpoint attempts required.
2. **Official Binance CLI (2.1.1, SHA-256 pinned in `Dockerfile.worker`)** —
   signed read-only command testing against real endpoints through
   `apps/trading_worker/research/binance_cli.py` before any staged order.
3. **Staged first order (runbook Gate 5)** — first real order is capped at
   50 USDC notional, atomically reserved, followed immediately by
   `pause_new_risk`; daily loss cap 5 USDC, leverage ≤ 10x, exactly one
   exposure chain.

## What is NOT waived

Every Mainnet release gate in `docs/MAINNET-RELEASE-RUNBOOK.md` remains
mandatory and fail-closed: Gate 1..6 in order, one-time `trading_admin`
approvals, LIVE-disarmed deploy, reconciliation `IN_SYNC` + durable outbox
evidence before continuation, rollback via kill switch.

## Accepted risks

1. Integration defects in authentication/signing/symbol filters/rate
   limits/reconciliation surface on Mainnet (preflight or staged first order)
   instead of Testnet. The read-only preflight and the 50 USDC cap bound the
   blast radius; rollback is the kill switch.
2. `testnet_soak_verified` / `manual_trial_verified` in
   `artifacts/build-evidence.json` stay `false`. These flags gate only the
   TESTNET-autonomous path (`main.py` `testnet_autonomous_ready`); the
   Mainnet preflight/continuation chain does not read them (verified by code
   review at `main.py:1833-1854`), so no gate code change is required.
3. Medium findings from `evidence/trading-readiness-report.md` remain
   accepted as-is pending operator review: margin-utilization env knob
   (`gates.py:159`), unbounded staleness env knobs (`gates.py:320`),
   documented schema-apply gap (closed by runbook Gate 1 step 4 on
   2026-09-21). They are operator knobs / process items, not code defects.

## Conditions that still gate real trading

- Mainnet HMAC API keys created on Binance with USDⓈ-M futures trading
  permission, stored only as Secret Manager versions (never in the repo);
- Cloud SQL schema applied (runbook Gate 1 step 4, added 2026-09-21);
- GCP resources confirmed (Artifact Registry `blessing-repo`, service
  accounts, Secret Manager versions, Cloud SQL `blessing-sql-primary`,
  budget/alert policies);
- runbook Gate 1..6 executed in order with evidence at every gate.
