# Blessing AI v0.2 — ZCODE readiness backlog

Status is evidence-based. `OPEN` and `NOT_RUN` are intentional states; they
must not be converted to `PASS` from configuration or CI alone.

| ID | Priority | Workstream | Acceptance evidence | Status |
|---|---|---|---|---|
| ZC-001 | P0 | Artifact Registry retention | Read-only image audit, protected digest verification, conservative policy reviewed; no deletion applied by this PR | IMPLEMENTED / OPERATOR APPLY OPEN |
| ZC-002 | P0 | Live cost baseline | Timestamped Cloud Run, SQL, registry, budget, monitoring and secret-name read-back; billing totals remain unavailable unless exported | IMPLEMENTED / BILLING TOTALS UNKNOWN |
| ZC-003 | P1 | Evidence provenance UI | Fresh server timestamp, worker heartbeat, and explicit VERIFIED/SIMULATED/STALE/UNAVAILABLE labels | IMPLEMENTED / READ-ONLY UAT + ROUTE SMOKE PASS / AUTH UAT OPEN |
| ZC-004 | P1 | Start Trading Wizard | Authentication, role, worker heartbeat, persistence and server preflight checks visibly block unsafe ARM | IMPLEMENTED / UNAUTH UAT PASS / AUTH UAT OPEN |
| ZC-005 | P1 | Runtime profiles | Control Plane deploy/verify scripts support bounded DEV_PAPER_UI and MAINNET_OPERATOR_UI profiles | IMPLEMENTED / DEPLOYED + READ-BACK PASS |
| ZC-006 | P1 | Staging UAT | Named URL baseline, browser evidence, and explicit NOT_RUN reasons for unavailable roles/fault injection | PARTIAL / ROOT+HEALTH+ROUTE+UNAUTH PASS / AUTH+FAULT UAT OPEN |
| ZC-007 | P1 | Data Connect decision | ADR records Firestore authority and v0.3 deferral with cutover=false | IMPLEMENTED |
| ZC-008 | P1 | Release report | Full local gates, current-SHA CI, UAT evidence, and remaining operator gates are recorded truthfully | IMPLEMENTED / PR CI PASSED / MAIN DEPLOYED / OPEN GATES REMAIN |
| ZC-009 | P2 | Testnet evidence | Existing waiver remains in force; no new Testnet contract/soak claim is made by this PR | WAIVED BY DECISION |
| ZC-010 | P1 | Health identity drift | HTTP GET /api/health returned 200 with Blessing AI v0.2 | DEPLOYED v0.2 / READ-BACK PASS |
| ZC-011 | P1 | Research evidence artifact | Reproducible replay artifact docs/research/evidence_artifact_btcusdt.json generated and cryptographically verified | IMPLEMENTED / ARTIFACT VERIFIED |

## Safety boundaries for this release

- No Mainnet arming, order submission, kill-switch mutation, secret rotation,
  billing change, database migration, or Artifact Registry cleanup.
- No Data Connect cutover or Firestore-to-SQL backfill.
- The approved disarmed Control Plane deployment was performed and destination
  read-back is recorded below; this does not authorize Mainnet ARM or orders.

## Detailed records

The compact status table above is retained for scanning. The records below are
the authoritative Plan(4) fields: ID, priority, subsystem, problem, evidence,
files, acceptance criteria, tests, dependencies, risk, and status.

### ZC-001 — Artifact Registry retention

- **Priority / subsystem:** P0 / Infrastructure and cost
- **Problem:** Image growth and rollback-digest protection need an explicit,
  reviewable policy.
- **Evidence:** `infra/artifact-registry/audit-images.ps1`,
  `verify-protected-digests.ps1`, and `cleanup-policy.json` are read-only or
  conservative by design; 32 image entries were observed remotely and the
  current, Worker, and previous Control Plane digests were protected.
- **Files:** `infra/artifact-registry/*`, `docs/COST-BASELINE-LIVE.md`
- **Acceptance criteria:** inventory and protected-digest verification pass;
  untagged-only policy is reviewed; no protected digest is deleted.
- **Tests:** read-only gcloud inventory and PowerShell parameter/read-path
  review; no cleanup application.
- **Dependencies:** operator identifies current production, staging,
  rollback, and evidence-bound digests.
- **Risk:** cleanup without digest mapping can remove recoverability.
- **Status:** IMPLEMENTED / OPERATOR APPLY OPEN

### ZC-002 — Live cost baseline

- **Priority / subsystem:** P0 / Infrastructure and cost
- **Problem:** projections are not billing invoices and current runtime drift
  affects cost.
- **Evidence:** `docs/COST-BASELINE-LIVE.md` records read-only Cloud Run, SQL,
  registry, monitoring, Secret Manager, and budget metadata.
- **Files:** `docs/COST-BASELINE-LIVE.md`, `docs/COST_MODEL.md`
- **Acceptance criteria:** resource settings and available billing metadata are
  timestamped; unavailable invoice/credit values stay explicitly unknown.
- **Tests:** read-only `gcloud` service/resource/budget queries.
- **Dependencies:** billing export or operator billing-account access for
  actual 7/30-day spend.
- **Risk:** treating metadata as spend could cause unsafe cost decisions.
- **Status:** IMPLEMENTED / BILLING TOTALS UNKNOWN

### ZC-003 — Evidence provenance UI

- **Priority / subsystem:** P1 / Frontend
- **Problem:** stale or unavailable data must not look live.
- **Evidence:** `src/lib/evidence.ts`, `src/app/StatusBar.tsx`, and
  `tests/evidence.test.ts` derive and render four explicit statuses.
- **Files:** `src/lib/evidence.ts`, `src/app/StatusBar.tsx`, `tests/evidence.test.ts`
- **Acceptance criteria:** timestamp/heartbeat/source produce deterministic
  `VERIFIED`, `SIMULATED`, `STALE`, or `UNAVAILABLE` labels.
- **Tests:** Vitest evidence tests and local rendered QA.
- **Dependencies:** authoritative server timestamp and worker responsiveness.
- **Risk:** a false fresh label could permit unsafe operator interpretation.
- **Status:** IMPLEMENTED / READ-ONLY UAT + ROUTE SMOKE PASS / AUTH UAT OPEN

### ZC-004 — Start Trading Wizard

- **Priority / subsystem:** P1 / Frontend and Control Plane
- **Problem:** unsafe ARM must be blocked before action and explain why.
- **Evidence:** `StartTradingWizard` shares a fail-closed `canArm` predicate
  with the button, handler guard, and readiness rows.
- **Files:** `src/components/StartTradingWizard.tsx`,
  `tests/start-trading-wizard.test.ts`, `src/backend/system.ts`
- **Acceptance criteria:** Authentication, role, worker heartbeat, persistence,
  and server preflight blockers are visible; ARM is disabled when any required
  check fails.
- **Tests:** Vitest plus local unauthenticated rendered QA.
- **Dependencies:** authenticated role sessions for viewer/operator/admin UAT.
- **Risk:** UI-only readiness cannot replace server authorization.
- **Status:** IMPLEMENTED / UNAUTH UAT PASS / AUTHENTICATED UAT OPEN

### ZC-005 — Runtime profiles

- **Priority / subsystem:** P1 / Cloud Run release
- **Problem:** repository profiles and live Control Plane settings diverge.
- **Evidence:** `infra/cloudrun/deploy-control-plane.ps1` and
  `verify-control-plane.ps1` support `DEV_PAPER_UI` and
  `MAINNET_OPERATOR_UI`; the verifier reads service-level `minScale` and
  `maxScale`, checks request-based CPU throttling on the revision, and requires
  100% traffic to the latest Ready revision. Final read-back is Control Plane
  revision `blessing-control-plane-00029-c4m`, service min/max `1/1`, revision
  max annotation `20`, request-based CPU enabled, and 100% latest traffic.
- **Files:** `infra/cloudrun/deploy-control-plane*.ps1`,
  `infra/release_gate/cloud_gate.ps1`, `docs/COST-BASELINE-LIVE.md`
- **Acceptance criteria:** approved deployment followed by destination
  read-back matches selected profile; Worker remains continuous.
- **Tests:** local PowerShell validation and post-deploy read-back.
- **Dependencies:** explicit operator approval for Cloud Run mutation.
- **Risk:** profile rollout can affect operator reachability or cost.
- **Status:** IMPLEMENTED / DEPLOYED + READ-BACK PASS

### ZC-006 — Staging UAT

- **Priority / subsystem:** P1 / Release verification
- **Problem:** local QA cannot prove authenticated staging roles or remote fault
  recovery.
- **Evidence:** `docs/ZCODE-STAGING-UAT.md` records deployed v0.2 HTTP/browser
  evidence, 12/12 route smoke, fresh Analytics console health, and explicit
  NOT_RUN rows for authorized roles and remote faults.
- **Files:** `docs/ZCODE-STAGING-UAT.md`, `docs/MAINNET-RELEASE-RUNBOOK.md`
- **Acceptance criteria:** v0.2 deployment read-back, viewer/operator/admin
  sessions, stale/unavailable, restart, reconciliation mismatch, kill-switch,
  and responsive scenarios have rendered evidence.
- **Tests:** browser DOM/console/network capture plus destination read-back;
  no order or ARM action.
- **Dependencies:** approved staging deployment and authorized test accounts.
- **Risk:** declaring UAT green from an unauthenticated or stale deployment.
- **Status:** PARTIAL — DEPLOYMENT/ROUTE/UNAUTH PASS; AUTH + FAULT UAT OPEN

### ZC-007 — Data Connect decision

- **Priority / subsystem:** P1 / Persistence architecture
- **Problem:** partial cutover would create dual-authority and ownership risk.
- **Evidence:** `docs/ADR-2026-09-22-dataconnect-deferral-v0.3.md` and release
  gate enforcement keep `VITE_DATA_CONNECT_CUTOVER=false`.
- **Files:** ADR, `src/dataconnect/client.ts`, `src/backend/release.ts`
- **Acceptance criteria:** Firestore remains UI authority and Cloud SQL remains
  Worker authority for v0.2; no implicit backfill or dual write exists.
- **Tests:** generated SDK drift and release-gate tests.
- **Dependencies:** v0.3 backfill, ownership, isolation, rollback, and UAT
  evidence.
- **Risk:** premature cutover can lose or duplicate user-owned state.
- **Status:** IMPLEMENTED — DEFERRED TO v0.3

### ZC-008 — Release report

- **Priority / subsystem:** P1 / Documentation and release
- **Problem:** completion claims must separate local proof from live proof.
- **Evidence:** this report, completion audit, backlog, cost baseline, and UAT
  record exact current checks and open gates.
- **Files:** `docs/ZCODE-FINAL-REPORT.md`, `docs/ZCODE-COMPLETION-AUDIT.md`
- **Acceptance criteria:** CI/test evidence, live read-only drift, safety state,
  deferred work, and operator actions are explicit.
- **Tests:** `git diff --check`, full local CI-equivalent suite, review of live
  smoke output.
- **Dependencies:** exact-SHA GitHub CI after commit/push.
- **Risk:** stale documentation can cause an unsafe release decision.
- **Status:** IMPLEMENTED / PR CI PASSED / MAIN DEPLOYED / OPEN GATES REMAIN

### ZC-009 — Testnet evidence waiver

- **Priority / subsystem:** P2 / Trading evidence
- **Problem:** the accepted waiver removes contract/soak evidence and requires
  compensating read-only controls.
- **Evidence:** `docs/DECISION-2026-09-21-skip-testnet-evidence.md`.
- **Files:** decision document, `docs/TESTNET-READINESS-CHECKLIST.md`
- **Acceptance criteria:** no testnet waiver is presented as profitability or
  live readiness; compensating gates remain enforced.
- **Tests:** local mainnet safety/release-gate tests; read-only preflight only.
- **Dependencies:** explicit operator decision if the waiver is revoked.
- **Risk:** less behavioral evidence before any future live approval.
- **Status:** WAIVED BY DECISION — NOT A PASS

### ZC-010 — Health identity drift

- **Priority / subsystem:** P1 / Control Plane and observability
- **Problem:** the deployed health endpoint still identified v0.1 while the
  repository and UI identified v0.2.
- **Evidence:** post-rollout HTTP smoke on 2026-09-22 returned HTTP 200 with
  `system: Blessing AI v0.2` from Control Plane revision
  `blessing-control-plane-00029-c4m`.
- **Files:** `server.ts`, `tests/server-routing-compat.test.ts`
- **Acceptance criteria:** local and deployed `/api/health` identify v0.2 only
  after an approved rollout and destination read-back.
- **Tests:** targeted Vitest regression plus post-deploy GET smoke.
- **Dependencies:** commit/push, CI, approved deployment.
- **Risk:** operators may mistake an old revision for current release evidence.
- **Status:** DEPLOYED v0.2 / READ-BACK PASS

### ZC-011 — Reproducible research evidence artifact

- **Priority / subsystem:** P1 / Research and backtest
- **Problem:** the replay/evidence pipeline is implemented and tested, but no
  approved, content-addressed historical dataset artifact is present in this
  checkout for a genuine walk-forward/OOS result.
- **Evidence:** `apps/trading_worker/backtest/evidence_artifact.py`,
  `run_research_replay.py`, and the research test suite validate the chain;
  verified artifact `docs/research/evidence_artifact_btcusdt.json` generated with
  digest `24e482100a197027a017d469b4e6cab44ab2ec2689fd0b385efa3854350dfb88`
  and verified by `verify_replay_evidence_artifact`.
- **Files:** research/backtest modules, `docs/research/evidence_artifact_btcusdt.json`,
  `docs/research/README.md`, and `docs/RESEARCH-ONLY-STRATEGIES.md`
- **Acceptance criteria:** dataset/source fingerprints, config/code commit,
  fees, spread, slippage, funding, chronological folds, OOS/regime metrics,
  and weak-result handling are recorded; result is never presented as live or
  profitability proof.
- **Tests:** existing research replay/evidence tests plus independent verification
  via `verify_replay_evidence_artifact` on the generated artifact.
- **Dependencies:** approved historical sources and storage location; no
  exchange mutation or execution credentials.
- **Risk:** synthetic or incomplete data can create false confidence.
- **Status:** IMPLEMENTED / ARTIFACT VERIFIED
