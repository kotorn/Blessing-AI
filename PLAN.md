# PLAN.md — Blessing-AI v0.2 Completion Plan
## Executor: ZCode Agent + GLM-5.3

> **Architecture Reference**: For the comprehensive Google SaaS-First Architecture Specification, refer to [`Blessing-AI-v0.2-Plan.md`](./Blessing-AI-v0.2-Plan.md).
>
> **Current implementation boundary (2026-09-22):** The repository uses
> Firestore as the UI authority and Cloud SQL as the Worker operational
> authority for v0.2. `VITE_DATA_CONNECT_CUTOVER=false` is enforced; Data
> Connect cutover is deferred to v0.3 by
> [`docs/ADR-2026-09-22-dataconnect-deferral-v0.3.md`](docs/ADR-2026-09-22-dataconnect-deferral-v0.3.md).
> PAPER/default and fail-closed execution remain mandatory.


**Repository:** `kotorn/Blessing-AI`
**Primary agent:** ZCode Agent
**Primary model:** GLM-5.3
**Goal:** Finish Blessing-AI v0.2, close remaining functional gaps, reduce avoidable cloud cost, validate staging, and stop at `READY_FOR_OPERATOR_APPROVAL` without autonomously enabling real Mainnet trading.

---

# 0. Mission

This file is an **execution specification for ZCode**, not a design brainstorm.

ZCode must take the current repository from its actual present state to a clearly verified **v0.2 COMPLETE** state by:

1. auditing code, tests, Git state, deployment config, and live cloud configuration;
2. creating an explicit P0/P1/P2/P3 backlog from real evidence;
3. completing frontend, Control Plane, Trading Worker, research/backtest, persistence, observability, cost controls, and release readiness;
4. removing avoidable operating cost without weakening trading safety;
5. running CI-equivalent verification and staging UAT;
6. synchronizing documentation with the code that actually ships;
7. ending at `READY_FOR_OPERATOR_APPROVAL — NOT ARMED`.

> **Important:** GLM-5.3 is the coding model used by ZCode to finish the project. Do **not** add GLM-5.3 as a runtime dependency of Blessing-AI unless a separate requirement explicitly requests it.

---

# 1. Non-Negotiable Safety Rules

## 1.1 Trading authority

The authority path must remain deterministic:

```text
Strategies
   ↓
Meta Allocator
   ↓
Portfolio Risk Governor
   ↓
Execution / Reconciliation
   ↓
Binance Adapter
```

No coding agent, runtime LLM, UI component, AGY queue, research script, or Copilot may bypass this path.

## 1.2 Mainnet boundary

ZCode may make the system **ready** for Mainnet but must not:

- arm real Mainnet trading;
- submit a real order;
- consume a continuation approval;
- increase risk limits;
- bypass persistence or reconciliation;
- disable a kill switch;
- expose Binance secrets;
- rotate production secrets without explicit operator approval.

Final automated release state:

```text
READY_FOR_OPERATOR_APPROVAL
```

Never:

```text
AUTONOMOUS_LIVE
```

## 1.3 Production mutation policy

Repository-local edits and local tests may proceed after the plan is accepted.

Before any mutation involving production Cloud Run, Cloud SQL, IAM, billing, Artifact Registry deletion, Firebase production rules, Secret Manager, Mainnet credentials, or production migrations, switch ZCode to **Ask before changes** and require explicit operator approval.

Read-only inspection is allowed.

---

# 2. ZCode Operating Rules

Use ZCode's long-horizon workflow deliberately.

## 2.1 Required setup

```text
ZCode >= 3.14.1
Model = GLM-5.3
Workspace = Blessing-AI repository root
```

Prefer the user's existing **Z.ai Coding Plan** connection rather than silently falling back to general usage-billed API access.

Do not purchase or upgrade a Coding Plan automatically.

## 2.2 Execution modes

| Activity | ZCode mode |
|---|---|
| Initial audit / architecture analysis | Plan |
| Normal repo edits | Edit automatically |
| Local test/build/Docker | Edit automatically |
| Critical trading changes | Ask before changes when uncertain |
| GCP/IAM/DB/billing/secret mutation | Ask before changes |
| Destructive production action | Never autonomous |

Do not use unrestricted Full Access for production mutations.

## 2.3 Long-task controls

At task start:

1. use `/goal` to set the project completion goal;
2. keep implementation in one long-running task when practical;
3. use `/compact` after large phases to reduce token waste;
4. use ZCode Review before each phase commit;
5. use `/workflow` for parallel lanes when dynamic workflows are available;
6. use idle-time tasks for non-urgent documentation/static-review work when available.

## 2.4 Model policy

Primary implementation and review model:

```text
GLM-5.3
```

Do not silently downgrade critical implementation/review work to another model.

If quota pressure occurs:

1. compact context;
2. use targeted search instead of rereading whole files;
3. store findings in concise repo Markdown;
4. avoid overlapping agents reading the same modules;
5. queue non-urgent work as idle-time tasks if supported;
6. report quota pressure.

GLM-5.3-Flash may be used only for low-risk mechanical work if explicitly allowed by the operator.

---

# 3. Definition of Done — v0.2 COMPLETE

## 3.1 Repository

- [ ] No undocumented P0/P1 issue remains.
- [ ] Every deferred item has an explicit decision/target version.
- [ ] No credentials or secrets are committed.
- [ ] No temporary patch/fix files remain tracked.
- [ ] Architecture documents describe the implementation that actually ships.
- [ ] Local and production architectures are clearly separated.

## 3.2 TypeScript / Frontend / Control Plane

- [ ] `npm ci` passes.
- [ ] `npm run dataconnect:generate` produces no unexpected generated diff.
- [ ] `npm run lint` passes.
- [ ] `npm run test` passes.
- [ ] `npm run build` passes.
- [ ] All primary routes render.
- [ ] No primary control is unreachable.
- [ ] Loading/error/empty/stale states are handled.
- [ ] LIVE / PAPER / SIMULATED / STALE / UNAVAILABLE evidence is visually distinct.
- [ ] Start Trading Wizard shows readiness blockers before action.
- [ ] Alerts are reachable and status/count is visible.
- [ ] Dashboard works on desktop/tablet/mobile.

## 3.3 Trading Worker

- [ ] Persistence is durable.
- [ ] Transactional outbox works.
- [ ] Execution lease prevents duplicate executors.
- [ ] Reconciliation is deterministic.
- [ ] Private stream reconnect/recovery is tested.
- [ ] Stale market/account state fails closed.
- [ ] Restart does not silently resume unsafe risk.
- [ ] Mainnet order paths remain gated.
- [ ] Kill-switch behavior remains deterministic and independent of AI queues.

## 3.4 Research / Backtest

- [ ] Backtest/replay is reproducible.
- [ ] Fees, spread, slippage, and funding assumptions are explicit.
- [ ] Evidence records data source, date range, commit, and config fingerprint.
- [ ] Simulated results are never presented as live evidence.
- [ ] Research code has no execution authority.
- [ ] Lack of profitable evidence is accepted as a valid result.

## 3.5 Infrastructure / Release

- [ ] Staging deploy is reproducible from immutable digests.
- [ ] IAM is least privilege.
- [ ] Secret Manager is used for secrets.
- [ ] Monitoring and alerts function.
- [ ] Cost controls are active.
- [ ] Artifact retention is controlled.
- [ ] Database schema state is reproducible.
- [ ] Rollback is verified.
- [ ] GitHub Actions CI is green.
- [ ] Staging UAT is green.
- [ ] Final release state is `READY_FOR_OPERATOR_APPROVAL — NOT ARMED`.

---

# 4. Baseline to Re-Verify

Do not assume these facts are still current. ZCode must re-read the repo and live cloud state before making changes.

Known architecture:

```text
React/Vite Dashboard
        │
        ▼
Control Plane / Express
        │
        ▼
Python Trading Worker
        │
        ├─ Risk Governor
        ├─ Persistence / Outbox
        ├─ Execution Lease
        ├─ Reconciliation
        └─ Binance Adapter
```

At plan creation time, deployment scripts show both Control Plane and Trading Worker configured approximately as:

```text
min=1
max=1
concurrency=1
cpu=1
memory=1Gi
no-cpu-throttling
```

Continuous CPU is defensible for the autonomous Worker but likely unnecessary for the Control Plane.

`PLAN.md` currently acts mainly as an architecture pointer. `docs/COST_MODEL.md` contains historical estimates and must not be treated as the billing source of truth.

---

# 5. Cost Objective

Cost optimization is required, but **zero cost is not the primary goal**.

Priority:

```text
Safety
> Correctness
> Recoverability
> Observability
> Cost
> Convenience
```

Do not save a small monthly amount by making the trading path materially less safe.

Engineering targets, not guaranteed prices:

| Stage | Target |
|---|---:|
| Local development | near $0 cloud spend |
| Staging / intermittent testing | $0–$15/month where practical |
| 24/7 PAPER / small-live-ready infrastructure | target <= $65/month after verified credits |
| Higher scale | new cost review required |

If safe measured cost is above target, document why instead of weakening the system.

The operator has Google AI Pro and may have an associated Google Cloud credit. Do not subtract it until billing shows it is active and applicable to this project/account.

---

# 6. Cost Workstream

## 6.1 Measure first

Create:

```text
docs/COST-BASELINE-LIVE.md
```

Collect read-only evidence for:

- Cloud Run min/max/CPU/RAM/billing mode;
- Cloud SQL tier/storage/backups;
- Artifact Registry storage and image count;
- BigQuery storage and recent query usage;
- GCS storage;
- Logging ingestion;
- Secret Manager versions;
- billing budget configuration;
- 7-day and 30-day cost by service where permissions allow;
- active credits if visible.

Measured billing beats estimates whenever available.

## 6.2 Control Plane — P0 safe cost reduction

Refactor deployment into explicit runtime profiles.

### DEV / PAPER UI

```text
min instances = 0
request-based billing
CPU throttling enabled
```

### MAINNET operator UI

Prefer:

```text
min instances = 1
request-based billing
CPU throttling while idle
```

This keeps the operator/emergency path warm while avoiding continuous CPU allocation.

Acceptance:

- auth still works;
- OIDC worker calls still work;
- kill-switch/operator route remains reachable;
- cold/warm startup is measured;
- no Binance/SQL secrets are added to Control Plane;
- cost docs are updated.

## 6.3 Trading Worker — preserve continuous semantics

For continuous PAPER soak or Mainnet:

```text
min instances = 1
max instances = 1
concurrency = 1
continuous CPU available
```

Do **not** scale the existing autonomous worker to zero simply to reduce cost.

A separate on-demand research/test worker may be added if useful, but it must not replace the live daemon.

## 6.4 Cloud SQL

Do not delete production Cloud SQL to chase a zero-dollar architecture.

For v0.2:

1. keep Cloud SQL as default production durable store;
2. right-size only from measured usage;
3. use local PostgreSQL for normal development;
4. external free PostgreSQL may be tested only as a separate staging experiment;
5. any future production migration requires schema, SSL, pool, latency, disconnect, outbox, lease, reconciliation, backup, and restore tests.

No production DB deletion is part of this plan.

## 6.5 Artifact Registry — P0

Create safe cleanup tooling:

```text
infra/artifact-registry/
  audit-images.ps1
  cleanup-policy.json
  verify-protected-digests.ps1
```

Protect at minimum:

- current production digest;
- current staging digest;
- known-good rollback digest;
- release-candidate/evidence-bound digest;
- explicitly protected tags.

Delete only old/unprotected images after dry-run verification.

## 6.6 BigQuery

Verify or implement:

- mandatory partition filters where appropriate;
- clustering for high-volume analytical tables;
- maximum bytes billed for automated research;
- dry-run for expensive automated queries;
- no uncontrolled `SELECT *` scans;
- retention for disposable intermediate tables.

## 6.7 Logging / Monitoring

Never log every raw tick.

Log decisions, state transitions, errors, reconnections, order/fill/reconciliation events, risk vetoes, and release actions.

Avoid raw market feed spam, repeated health spam, and secret-bearing bodies.

## 6.8 GCS

Use compressed Parquet, lifecycle old raw data, and separate reproducibility-critical evidence from disposable intermediates.

## 6.9 Billing guardrails

Audit current budget settings and use realistic thresholds in the real billing-account currency:

```text
50% current spend
75% current spend
90% current spend
100% forecast
```

Produce a monthly cost report by service.

Budget alerts are not hard spend caps.

---

# 7. ZCode / GLM-5.3 Development Cost Control

Coding-agent cost is separate from infrastructure cost.

Preferred path:

```text
ZCode
  → Z.ai Coding Plan
  → GLM-5.3
```

Do not silently fall back to general usage-billed API balance.

At phase checkpoints record:

```text
GLM-5.3 usage
5-hour quota status
weekly quota status
tool-call usage
```

Reduce token waste by:

- targeted search;
- avoiding repeated full-file reads of very large files such as `server.ts`;
- using `/compact`;
- persisting concise audit/backlog state in repo Markdown;
- avoiding overlapping subagents on the same code;
- using idle-time tasks for non-urgent work when available.

---

# 8. Branch Strategy

Create:

```text
zcode/finish-blessing-v0.2
```

Do not develop directly on `main`.

Use logical phase commits such as:

```text
audit: record v0.2 completion gaps
cost: make control plane runtime profile cost-aware
ui: complete operator readiness and alerts flows
control-plane: harden auth and worker transport
worker: harden restart and reconciliation invariants
research: finalize reproducible evidence workflow
infra: add artifact retention and billing checks
test: close integration coverage gaps
docs: align architecture, runbook and cost model
```

---

# 9. Dynamic Workflow Lanes

If `/workflow` is available, use one orchestrator with bounded ownership.

## Lane A — Audit / Architecture

Own primarily:

```text
PLAN.md
Blessing-AI-v0.2-Plan.md
README.md
docs/**
```

Outputs:

```text
docs/ZCODE-COMPLETION-AUDIT.md
docs/ZCODE-BACKLOG.md
```

## Lane B — Frontend / Operator UX

Own primarily:

```text
src/app/**
src/pages/**
src/components/**
src/hooks/**
src/api/**
```

Focus on route completeness, dead controls, alerts, readiness UX, evidence labeling, error/loading states, and responsive layout.

## Lane C — Control Plane / Security

Own primarily:

```text
server.ts
src/backend/**
tests/*.test.ts
```

Focus on RBAC, OIDC, release boundaries, validation, audit trail, timeout/cancellation, safe errors, and secret isolation.

## Lane D — Trading Worker

Own primarily:

```text
apps/trading_worker/**
domain/**
venues/**
tests/python/**
```

Focus on state-machine correctness, stale-data handling, persistence, leases, reconciliation, WebSocket recovery, release gates, and kill-switch behavior.

## Lane E — Research / Backtest

Own primarily:

```text
apps/trading_worker/backtest/**
apps/trading_worker/research/**
apps/trading_worker/strategies/**
ai/**
data/**
```

Focus on reproducibility, realistic costs, OOS/walk-forward evidence, strategy attribution, and honest evidence labels.

## Lane F — Infrastructure / Cost

Own primarily:

```text
infra/**
cloudbuild*.yaml
Dockerfile*
docker-compose.yml
docs/COST_MODEL.md
```

Focus on real cost baseline, Control Plane billing profile, Artifact Registry retention, monitoring, budgets, BigQuery guardrails, GCS lifecycle, and reproducible deployment.

## Lane G — Independent QA

Read across all lanes. Do not implement first.

Check authorization boundaries, live/simulated truthfulness, cost regressions, docs-vs-code drift, full test matrix, and proof that no Mainnet execution occurred.

Shared files such as `server.ts`, `.env.example`, `PLAN.md`, lockfiles, and release docs require orchestrator ownership to avoid merge conflicts.

---

# 10. Phase 0 — Baseline and Safeguards

1. inspect `git status`, current branch, HEAD, and working tree;
2. create `zcode/finish-blessing-v0.2`;
3. inspect recent commits/PRs/issues;
4. read release/runbook/decision docs;
5. run baseline CI-equivalent checks;
6. perform read-only GCP inspection if credentials are available;
7. write `docs/ZCODE-COMPLETION-AUDIT.md`.

Classify each subsystem:

```text
COMPLETE
PARTIAL
MISSING
BROKEN
DEFERRED
UNKNOWN
```

Do not start broad implementation until baseline failures are recorded.

## CI-equivalent Node checks

```bash
npm ci
npm run dataconnect:generate
git diff --exit-code -- src/dataconnect-generated
npm run lint
npm run test
npm run build
```

## CI-equivalent Python checks

```bash
python -m pip install -e ".[dev]"
ruff check --select E9,F63,F7,F82 .

pytest tests/python/ \
  -m "not contract_readonly and not contract_mutating and not contract_soak" \
  --cov=apps --cov=domain --cov=venues --cov=data --cov=ai --cov=infrastructure \
  --cov-report=term-missing:skip-covered \
  --cov-fail-under=65
```

---

# 11. Phase 1 — Authoritative Backlog

Create:

```text
docs/ZCODE-BACKLOG.md
```

Every item requires:

```text
ID
priority
subsystem
problem
evidence
files
acceptance criteria
tests
dependencies
risk
status
```

Priority:

```text
P0 = safety/security/data-loss/release blocker
P1 = required for v0.2 completion
P2 = quality/observability/cost/UX
P3 = future enhancement
```

No gap may disappear merely because GitHub currently has no open issue for it.

---

# 12. Phase 2 — Safe Cost Wins

Complete before broad feature expansion where practical:

- measure live cost baseline;
- refactor Control Plane runtime profile;
- preserve continuous Worker behavior;
- add safe Artifact Registry cleanup policy/tooling;
- audit logging volume;
- audit BigQuery query guardrails;
- correct budget configuration;
- update `docs/COST_MODEL.md` from current architecture and measured assumptions.

Do not migrate production DB in this phase.

---

# 13. Phase 3 — Finish Dashboard

Audit all major routes, including:

```text
/command
/markets
/strategies
/orders
/positions
/risk
/portfolio
/research/replay
/analytics
/system/connections
/system/audit
/settings
```

For each page verify:

```text
data source
loading
error
empty
stale state
authorization
responsive layout
primary actions
evidence labeling
```

## Alerts

Verify that Alerts Drawer is reachable from the main shell/status UI. If still broken, wire active count, open callback, and dismissed-alert filtering.

## Start Trading Wizard

Make it readiness-first and show at minimum:

```text
Authentication
Role
Worker status
Execution mode
Persistence
Reconciliation
Market freshness
Private stream
Binance credential state
Release state
Mainnet approval state
```

Explain exactly why progression is blocked.

## Evidence truthfulness

Never present simulated fixtures as current live state.

---

# 14. Phase 4 — Finish Control Plane

Complete and verify:

- Firebase RBAC;
- OIDC Worker transport;
- release API boundaries;
- request validation;
- durable operator audit trail;
- timeout/cancellation;
- safe error mapping;
- system-state contract;
- release-readiness endpoint;
- cloud connection status.

Control Plane must remain isolated from Binance/SQL secrets unless a separately reviewed architecture change requires them.

---

# 15. Phase 5 — Finish Trading Worker

Prioritize correctness over new strategies.

Verify:

### State
- PAPER default;
- live approval false by default;
- restart/revision behavior is safe;
- unknown state fails closed.

### Persistence
- schema present;
- outbox durable;
- bounded queue;
- visible failure state;
- REQUIRED persistence truly fails closed;
- lease prevents duplicate active executor.

### Freshness
- stale market data blocks unsafe action;
- stale account snapshot blocks unsafe action;
- user-stream recovery is deterministic.

### Execution
- client order ID idempotency;
- ambiguous response reconciliation;
- no blind retries;
- symbol filters/precision;
- leverage/risk bounds.

### Recovery
- reconciliation mismatch is surfaced;
- recovery-only mode works;
- kill switch remains independent of AI queues.

Do not add strategies merely to declare the project finished.

---

# 16. Phase 6 — Finish Research / Evidence

A working trading system and a proven profitable strategy are different goals.

Build/verify a reproducible research chain:

```text
historical dataset
→ feature/strategy replay
→ fee/spread/slippage/funding model
→ chronological train/test handling
→ walk-forward / OOS
→ regime attribution
→ strategy metrics
→ portfolio metrics
→ evidence artifact
```

Every evidence artifact should record:

```text
dataset fingerprint
config fingerprint
code commit
date range
symbol
venue
fees
funding
spread
slippage
test folds
seed where relevant
```

Weak results must remain weak results. Never tune acceptance criteria until favorable results appear.

---

# 17. Phase 7 — Data Connect / Persistence Decision

Do not leave v0.2 half-migrated.

Choose exactly one:

## Option A — Explicit deferral

Keep:

```text
VITE_DATA_CONNECT_CUTOVER=false
Firestore = UI authority
Cloud SQL = Worker operational authority
```

Create an ADR moving full Data Connect cutover to v0.3.

This is preferred if cutover does not materially improve v0.2 completion.

## Option B — Complete cutover

Only if backfill, idempotency, ownership/auth tests, verification SQL, rollback, multi-user isolation, and preview UAT all pass.

No partial production cutover.

---

# 18. Phase 8 — Staging End-to-End

After repository CI is green, validate staging scenarios:

1. unauthenticated user;
2. authenticated viewer;
3. trading_admin;
4. Worker unavailable;
5. DB unavailable;
6. stale market data;
7. reconciliation mismatch;
8. PAPER normal operation;
9. restart;
10. kill switch;
11. Control Plane cold/warm behavior after cost optimization.

Verify browser console and network failures.

Record:

```text
docs/ZCODE-STAGING-UAT.md
```

Include evidence, not only PASS/FAIL text.

---

# 19. Phase 9 — Release Readiness

Reconcile actual implementation with:

```text
docs/MAINNET-RELEASE-RUNBOOK.md
docs/LIVE-READINESS-CHECKLIST.md
docs/SMALL-LIVE-CHECKLIST.md
docs/EXECUTION-AUTHORITY.md
```

Perform only approved non-mutating/live-disarmed checks.

Final release state must show:

```text
code complete
CI green
staging green
cost baseline known
rollback available
live-disarmed procedure ready
operator approval required
```

Do not arm Mainnet.

---

# 20. Phase 10 — Documentation Drift

Update:

```text
README.md
PLAN.md
Blessing-AI-v0.2-Plan.md
docs/COST_MODEL.md
docs/MIGRATION-MATRIX.md
```

Explicitly separate:

```text
PRODUCTION
Cloud Run
Cloud SQL
Firestore
BigQuery
GCS
Secret Manager
Cloud Monitoring
```

from:

```text
LOCAL DEVELOPMENT
Docker Compose
Postgres
Redis
NATS
MLflow
Prometheus
Grafana
```

Do not allow future agents to mistake local-compose dependencies for mandatory production infrastructure.

---

# 21. Phase 11 — Final Verification

Run the exact CI-equivalent suite again:

```bash
npm ci
npm run dataconnect:generate
git diff --exit-code -- src/dataconnect-generated
npm run lint
npm run test
npm run build

python -m pip install -e ".[dev]"
ruff check --select E9,F63,F7,F82 .

pytest tests/python/ \
  -m "not contract_readonly and not contract_mutating and not contract_soak" \
  --cov=apps --cov=domain --cov=venues --cov=data --cov=ai --cov=infrastructure \
  --cov-report=term-missing:skip-covered \
  --cov-fail-under=65
```

Also verify:

```text
git status
git diff
tracked hygiene
secret leakage
rollback docs
deployment docs
cost docs
```

Create:

```text
docs/ZCODE-FINAL-REPORT.md
```

---

# 22. Final Report Format

ZCode must not finish with only “done”.

Use:

```markdown
# Blessing-AI v0.2 Final Report

## Completion
- Overall:
- Commit:
- Branch:
- CI:
- Staging:

## Completed
...

## Deferred
...

## Safety status
- Execution mode:
- Mainnet armed:
- Kill switch:
- Reconciliation:
- Persistence:
- Release state:

## Cost
- Previous measured monthly run-rate:
- New measured/projected run-rate:
- Control Plane:
- Worker:
- Database:
- Artifact Registry:
- Logging:
- BigQuery:
- Credits verified:

## Tests
...

## Known risks
...

## Operator actions still required
...
```

Expected final Mainnet line:

```text
READY_FOR_OPERATOR_APPROVAL — NOT ARMED
```

---

# 23. Stop Conditions

Stop and request operator approval if any step requires:

- deleting production Cloud SQL;
- deleting data without a verified backup;
- deleting an image digest required for rollback;
- changing Binance Mainnet secrets;
- widening IAM privilege;
- creating a real Mainnet order;
- arming Mainnet;
- increasing risk limits;
- removing fail-closed behavior;
- changing billing/subscription plans;
- spending money outside the authorized plan.

Do not stop merely because a normal code/test failure is difficult. Diagnose and continue.

---

# 24. First ZCode Prompt

Open the Blessing-AI workspace in ZCode with GLM-5.3 and send:

```text
Read @PLAN.md completely.

You are the primary implementation agent for Blessing-AI v0.2.
Your job is to finish the program according to PLAN.md, not to redesign it from scratch.

Start in Plan mode.

First:
1. inspect the current repository state and git history;
2. run the Phase 0 baseline checks;
3. perform read-only inspection of the current GCP deployment if credentials are available;
4. create docs/ZCODE-COMPLETION-AUDIT.md;
5. create docs/ZCODE-BACKLOG.md with P0/P1/P2/P3 priorities;
6. compare actual code with the authoritative architecture and release runbook;
7. identify cost regressions and actual current monthly cost drivers.

Do not mutate production cloud resources yet.
Do not arm Mainnet.
Do not place real orders.
Do not add GLM-5.3 as a runtime product dependency; GLM-5.3 is the coding model executing this task.

After Phase 0, proceed through the phases in PLAN.md, using GLM-5.3 as the primary model and checkpointing verified changes with tests and commits.
```

Then use `/goal` to set:

```text
Complete Blessing-AI v0.2 according to PLAN.md until repository, staging, safety, documentation, and cost acceptance criteria are satisfied, ending at READY_FOR_OPERATOR_APPROVAL and never autonomously arming or placing a Mainnet order.
```

---

# 25. Recommended Workflow Topology

```text
Orchestrator — GLM-5.3
│
├─ Audit / Architecture
├─ Frontend
├─ Control Plane
├─ Trading Worker
├─ Research
├─ Infra / Cost
└─ Independent QA
```

Rules:

- one lane owns a shared file at a time;
- subagents return evidence and test results, not only summaries;
- orchestrator reviews all diffs before merge;
- independent QA runs after implementation lanes complete;
- no production mutation occurs without the required approval mode.

---

# 26. Success Criterion

The project succeeds when Blessing-AI is a coherent verified product with:

```text
working Dashboard
+ authenticated Control Plane
+ durable Trading Worker
+ reproducible research
+ fail-closed Mainnet safety
+ staging verification
+ rollback
+ observability
+ realistic cost controls
+ synchronized documentation
```

and development/runtime costs are both visible and controlled.

Final project state:

```text
BLESSING-AI v0.2 COMPLETE
READY_FOR_OPERATOR_APPROVAL
NOT ARMED
```

---

# 27. ZCode References

- https://zcode.z.ai/en/docs/welcome
- https://zcode.z.ai/en/docs/agents
- https://zcode.z.ai/en/docs/agent-framework
- https://zcode.z.ai/en/docs/safety-confirm
- https://zcode.z.ai/en/docs/commands
- https://zcode.z.ai/en/docs/usage-stats
- https://zcode.z.ai/en/docs/configuration
- https://zcode.z.ai/en/changelog
