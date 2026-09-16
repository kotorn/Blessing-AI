# Small-Live Mainnet checklist

Status for this sprint: `DISABLED_BY_DEFAULT` / `NOT_OPERATIONAL`. The
Mainnet path is implemented only behind dedicated credentials,
`MAINNET_LIVE_APPROVED=true`, and the complete Worker preflight; the checked-in
default remains disarmed and no Mainnet order has been authorized.

Evidence status is intentionally separate from implementation status:
`CODE_PRESENT` does not mean `LOCAL_VERIFIED`, and local verification does not
mean `CI_VERIFIED` or operational approval.

The following is an evidence checklist. Each item must become
`LOCAL_VERIFIED`, then `CI_VERIFIED`, and finally receive independent
operational approval before Mainnet can be enabled.

## Required future evidence

- [x] Current clean candidate Testnet
      read-only contract: `LOCAL_VERIFIED; CONTRACT_TESTED` (1 passed,
      2 deselected; no mutation).
- [ ] Current-SHA controlled Testnet mutation and sanitized artifact:
      `MANUAL_TESTNET_VERIFIED` (not run: manual mutation approval and trial
      evidence are still pending; the 50 USDT exchange minimum is within the
      100 USDT first-launch cap).
- [x] Deterministic strategy replay and event-time research windows:
      `UNIT_TESTED; LOCAL_VERIFIED; RESEARCH_ONLY`; this is not a live-readiness
      or positive-edge claim.
- [ ] Long-duration Testnet soak with zero unhandled execution exceptions.
- [ ] Independent risk and operations sign-off.
- [ ] Durable operational persistence on **Cloud SQL PostgreSQL** for orders,
      fills, positions, risk state, and reconciliation evidence.
- [ ] Crash/restart and ambiguous-response recovery proven against durable
      state.
- [x] Mainnet-specific adapter and endpoint audit with explicit approval and
      execution-lease gates; operational enablement remains pending.

Current sprint evidence: local non-secret gates are `LOCAL_VERIFIED` and
verified safety/research implementation checkpoint recorded in the generated
`build-evidence.json` is `CI_VERIFIED` by the exact-SHA GitHub Actions check.
Testnet read-only is `LOCAL_VERIFIED; CONTRACT_TESTED`;
manual mutation is `NOT_RUN` because approval and trial evidence are pending;
the 50 USDT exchange minimum is within the 100 USDT cap; the supervised soak runner is not approved or run; autonomous
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

## Unified release gate

Release readiness is evaluated using the unified release gate entrypoints:
- `python -m apps.release_gate.repo_gate`: runs offline, CI-safe verification checks.
- `infra/release_gate/cloud_gate.ps1`: runs live-cloud verification checks (requires gcloud auth).
- `python -m apps.release_gate.gate`: the combined pass/fail entrypoint that evaluates release readiness across repo and cloud tiers, failing closed on missing or stale cloud evidence.
- Soak contract path: opt-in via the `allow_soak` and `confirm_soak` workflow_dispatch inputs.
