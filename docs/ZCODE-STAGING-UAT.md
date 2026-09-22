# Blessing AI v0.2 — named staging UAT

- **Named target:** `https://blessing-control-plane-hrybwxl4ra-as.a.run.app`
- **Paired Worker URL:** `https://blessing-trading-worker-hrybwxl4ra-as.a.run.app`
- **Observed:** 2026-09-22, read-only browser and HTTP session
- **Scope:** verify rendered truth and fail-closed presentation; do not ARM,
  DISARM, engage the kill switch, enter credentials, submit orders, or mutate
  cloud state.

## Remote read-only smoke

The named Control Plane URL returned HTTP 200 for `/` and `/api/health` on
2026-09-22. The root response contained the application marker, but the health
body reported `system: Blessing AI v0.1`. The repository now reports v0.2; this
is therefore evidence that the deployed revision is older than the current
branch, not evidence that this branch has been deployed. No authenticated
session, credential, order, or mutating endpoint was used.

The live read-back also shows Control Plane revision
`blessing-control-plane-00027-h5d` with `minScale=0`, `maxScale=20`, and CPU throttling off,
while the repository runtime profile requires a separately approved bounded
profile. The Worker read-back remains min/max 1/1 with continuous CPU.

The read-only `verify-live-disarmed.ps1` contract passed for Worker revision
`blessing-trading-worker-00038-nk7`: immutable digest, IAM, `LIVE` execution
mode with `MAINNET_LIVE_APPROVED=false`, `DISARMED`, durable `REQUIRED`
persistence, and zero order-submission attempts. The read-only Control Plane
profile verifier correctly failed with `min=0,max=20` versus the required
`MAINNET_OPERATOR_UI` `min=1,max=1`; this is an open rollout gate.

## Baseline observation

The page loaded with a nonblank dashboard and a visible `v0.2` application
marker, but the document title still reported `Blessing AI v0.1` before this
PR. The unauthenticated state showed `UNKNOWN`, `DEGRADED`, `Offline`, no
verified Binance snapshot, and an auth-required warning. These are recorded as
remote baseline facts, not as evidence that this branch is deployed.

## UAT matrix

| Scenario | Expected result | Evidence status |
|---|---|---|
| Page identity and nonblank DOM | Dashboard renders without framework overlay | PASS — baseline browser inspection; remote deployment is v0.1 |
| HTTP smoke | `/` and `/api/health` return expected transport status | PASS transport; FAIL release identity because remote health reports v0.1 |
| Console health | No uncaught error; auth-required warning is explainable | PASS with expected auth warning |
| Unauthenticated viewer | Sign-in affordance visible; state is UNKNOWN/UNAVAILABLE and destructive execution remains blocked | PASS — baseline |
| Authenticated viewer role | Read-only views work; ARM remains blocked | NOT_RUN — no authorized session was entered |
| Authenticated operator role | PAPER/TESTNET preflight shows role and persistence gates | NOT_RUN — no authorized session was entered |
| Authenticated trading_admin role | LIVE remains release-gated by server approval, credentials and preflight | NOT_RUN — no authorized session was entered |
| Stale worker state | Explicit STALE label and no unsafe ARM | PASS locally via evidence helper tests; staging UAT open |
| Worker unavailable | Explicit UNAVAILABLE label, kill/fail-closed state | PASS locally via rendered QA; staging UAT open |
| Restart/reconciliation fault injection | Deterministic fail-closed response | NOT_RUN — no remote fault injection permitted |
| Responsive desktop/mobile rendering | Labels remain readable and controls do not overlap | Local rendered QA required |

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
named staging UAT: no staging credentials were entered, no remote fault was
injected, and no remote deployment was performed. The v0.2 branch must be
deployed and read back before authenticated role or fault-injection rows can be
closed.
