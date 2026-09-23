# Blessing AI v0.2 — named staging UAT

> **Superseding correction — 2026-09-23:** This document's named Cloud Run target is **production, not staging**. The new disarmed release uses Control Plane `blessing-control-plane-00031-sl6` and Worker `blessing-trading-worker-00039-xkd` from source SHA `4f43ff4f522cc3df9874afa065f24eb60fd9bd60`. Production root/health returned 200; anonymous system-state, wealth and PDCA GETs returned 401; the unsigned browser displayed `WEALTH_TELEMETRY_UNAVAILABLE` and no inferred safety/promotion status. Authenticated viewer/operator/trading-admin, responsive role UAT, remote fault injection, and rollback remain **NOT_RUN**. The historical observations below must not be read as a current staging-UAT pass. No ARM, order, or mutating auth POST probe was performed.

## Post-rollout verification — 2026-09-22

The approved disarmed Control Plane rollout is now deployed and read back:

- Revision: `blessing-control-plane-00029-c4m`
- Image: `control-plane@sha256:24a93d0e0e6041f3211c71d402eb50f3be2f948c9a06f9f22b1fc81274474209`
- Profile: `MAINNET_OPERATOR_UI`; service min/max `1/1`; request-based CPU
  throttling enabled; 100% traffic to the latest Ready revision
- HTTP: `/` and `/api/health` both returned 200; health reported
  `Blessing AI v0.2`, `status: ok`, and `mode: deterministic_engine`
- Browser route smoke: 12/12 primary routes rendered after the final deploy,
  including Analytics, Connections, Audit, and Settings
- Analytics: a fresh direct-load check rendered both `Analytics & Performance
  Attribution` and `Google BigQuery Quant Lakehouse` with no console errors;
  the earlier analytics crash was fixed in PR #41
- Unauthenticated Start Trading Wizard: Authentication, Role, Worker
  heartbeat, and Persistence blockers were visible and `ARM ENGINE (PAPER)`
  remained disabled

Authenticated role sessions and remote fault injection were intentionally not
run because no authorized test credentials were entered. This is a read-only
release verification, not a full authenticated staging-UAT pass.

- **Named target:** `https://blessing-control-plane-hrybwxl4ra-as.a.run.app`
- **Paired Worker URL:** `https://blessing-trading-worker-hrybwxl4ra-as.a.run.app`
- **Observed:** 2026-09-22, read-only browser and HTTP session
- **Scope:** verify rendered truth and fail-closed presentation; do not ARM,
  DISARM, engage the kill switch, enter credentials, submit orders, or mutate
  cloud state.

## Remote read-only smoke

The named Control Plane URL returned HTTP 200 for `/` and `/api/health` on
2026-09-22, and the post-rollout body identified `Blessing AI v0.2`. No
authenticated session, credential, order, or mutating endpoint was used.

The final live read-back shows Control Plane service
`blessing-control-plane-00029-c4m` with service-level min/max `1/1`, a
revision-level max annotation of `20`, request-based CPU throttling enabled,
and 100% traffic to the latest Ready revision. The Worker read-back remains
min/max 1/1 with continuous CPU.

The read-only `verify-live-disarmed.ps1` contract passed for Worker revision
`blessing-trading-worker-00038-nk7`: immutable digest, IAM, `LIVE` execution
mode with `MAINNET_LIVE_APPROVED=false`, `DISARMED`, durable `REQUIRED`
persistence, and zero order-submission attempts. The read-only Control Plane
profile verifier passed for the final service-level scale, request-based CPU,
immutable image, and 100% traffic checks.

## Pre-rollout baseline observation

Before the approved rollout, the page loaded with a nonblank dashboard and a
visible `v0.2` application marker, but the document title and health endpoint
still reported `Blessing AI v0.1`. The unauthenticated state showed `UNKNOWN`,
`DEGRADED`, `Offline`, no verified Binance snapshot, and an auth-required
warning. These are retained as historical baseline facts.

## UAT matrix

| Scenario | Expected result | Evidence status |
|---|---|---|
| Page identity and nonblank DOM | Dashboard renders without framework overlay | PASS — deployed v0.2 browser inspection |
| HTTP smoke | `/` and `/api/health` return expected transport status and identity | PASS — HTTP 200 and v0.2 health identity |
| Console health | No uncaught error; auth-required warning is explainable | PASS with expected auth warning |
| Unauthenticated viewer | Sign-in affordance visible; state is UNKNOWN/UNAVAILABLE and destructive execution remains blocked | PASS — baseline |
| Authenticated viewer role | Read-only views work; ARM remains blocked | NOT_RUN — no authorized session was entered |
| Authenticated operator role | PAPER/TESTNET preflight shows role and persistence gates | NOT_RUN — no authorized session was entered |
| Authenticated trading_admin role | LIVE remains release-gated by server approval, credentials and preflight | NOT_RUN — no authorized session was entered |
| Stale worker state | Explicit STALE label and no unsafe ARM | PASS locally via evidence helper tests; staging UAT open |
| Worker unavailable | Explicit UNAVAILABLE label, kill/fail-closed state | PASS locally via rendered QA; staging UAT open |
| Restart/reconciliation fault injection | Deterministic fail-closed response | NOT_RUN — no remote fault injection permitted |
| Responsive desktop/mobile rendering | Labels remain readable and controls do not overlap | NOT_RUN — no authenticated responsive UAT session |

No unauthenticated browser observation is sufficient to claim authenticated
role coverage or release readiness.

## Local implementation QA — 2026-09-22

The implementation branch was started locally after a clean dependency install.
The dashboard rendered with the `Blessing AI v0.2` title and visible
`UNAVAILABLE`/`SIMULATED` provenance labels. Opening the Start Trading Wizard
and advancing to Preflight showed Authentication, Role, Worker heartbeat and
Persistence checks. With no signed-in user and no healthy worker snapshot,
`ARM ENGINE (PAPER)` remained disabled. The browser emitted only the expected
`CONTROL_PLANE_AUTH_REQUIRED` warning while the unsigned local session was
polled.

This validates the local fail-closed presentation and does not replace the
authenticated staging UAT: no staging credentials were entered and no remote
fault was injected. The deployment/read-back gate is now PASS; authenticated
role, responsive, and fault-injection rows remain open.
