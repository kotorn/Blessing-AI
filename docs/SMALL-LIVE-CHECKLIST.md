# Small-Live Mainnet checklist

Status for this sprint: `DISABLED` / `NOT_IMPLEMENTED`. This repository does
not authorize Mainnet execution, and no shortcut or approval flag can enable
it. `LIVE` ARM and mutable Mainnet adapter construction are rejected.

Evidence status is intentionally separate from implementation status:
`CODE_PRESENT` does not mean `LOCAL_VERIFIED`, and local verification does not
mean `CI_VERIFIED` or operational approval.

The following is a future-only checklist. Each item must become
`LOCAL_VERIFIED`, then `CI_VERIFIED`, and finally receive independent
operational approval before any Mainnet implementation is considered.

## Required future evidence

- [x] Current clean candidate Testnet
      read-only contract: `LOCAL_VERIFIED; CONTRACT_TESTED` (1 passed,
      2 deselected; no mutation).
- [ ] Current-SHA controlled Testnet mutation and sanitized artifact:
      `MANUAL_TESTNET_VERIFIED` (blocked: 50 USDT exchange minimum exceeds
      the hard 25 USDT first-launch cap).
- [ ] Long-duration Testnet soak with zero unhandled execution exceptions.
- [ ] Independent risk and operations sign-off.
- [ ] Durable operational persistence on **Cloud SQL PostgreSQL** for orders,
      fills, positions, risk state, and reconciliation evidence.
- [ ] Crash/restart and ambiguous-response recovery proven against durable
      state.
- [ ] Mainnet-specific adapter and endpoint audit, implemented in a separate
      approved change.

Current sprint evidence: local non-secret gates are `LOCAL_VERIFIED` and
verified safety/research implementation checkpoint recorded in the generated
`build-evidence.json` is `CI_VERIFIED` by the exact-SHA GitHub Actions check.
Testnet read-only is `LOCAL_VERIFIED; CONTRACT_TESTED`;
manual mutation is `NOT_RUN` because the 50 USDT exchange minimum exceeds the
hard 25 USDT cap; the supervised soak runner is not approved or run; autonomous
Testnet is locked. No Mainnet order has been created or tested.

## MVP infrastructure boundary

NATS is not mandatory for the MVP execution gate. It may be added for event
distribution after the Python worker’s durable state and reconciliation
contracts are independently correct.

## Non-negotiable future controls

- [ ] Per-order decision and order gates remain fail-closed.
- [ ] Account and position-risk math uses authoritative exchange fields.
- [ ] No synthetic market price or inferred liquidation distance.
- [ ] Kill switch cancellation is verified before reporting success.
- [ ] Secrets remain outside logs, artifacts, screenshots, and Git.
