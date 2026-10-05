# Agent goal: make the local ETHUSDC QUICK pilot ready for a human to start

This file is the standing goal for any coding agent working in this repo,
including Antigravity. Work through the milestones below in order and keep
going until the Definition of Done is met or a genuinely human-only authority
or evidence dependency remains. The user has authorized the agent to continue
through routine checkpoints, choose the recommended safe option, run eligible
Testnet acceptance, and perform normal Git delivery actions when repository
protections allow them. A checkpoint is a progress report, not a reason to
pause, when the next action is already authorized and safe. Do not repeatedly
ask for information obtainable from code, tests, logs, or the UI.

## Definition of Done

The goal is done when all of these are true on one clean, committed SHA:

1. `GET /api/local/runtime` and `GET /api/local/pilot/readiness` report
   `READY`, and the Python Worker gate
   (`apps/trading_worker/venues/binance/local_pilot_readiness.py`) agrees.
   Both gates reach READY only from **externally attested** evidence, never
   because an operator is logged in.
2. CI is green on that SHA, and the SHA is merged to `main` through normal
   review and protection rules. The agent may push, create/update the PR, and
   complete the merge only when all required checks and human approvals are
   already satisfied. Never bypass protections or fabricate approval.
3. A protected ETHUSDC Testnet lifecycle artifact for that SHA reads PASS,
   `PROTECTED_VERIFIED`, `IN_SYNC`, `diff_count` 0, with nothing left open.
4. The pilot is approved and prepared to `LIVE/DISARMED` with zero order
   attempts, and the signed read-only preflight passes.
5. A short report lists the evidence for 1–4 and the exact button the human
   presses next.

**Pressing Start (ARM) and every Mainnet order belong to the human.** The agent
may run the approved read-only Prepare flow only after a real `trading_admin`
has approved the campaign, all readiness gates pass, the reviewed source is
clean and fingerprint-bound, and order attempts are zero. The application may
retrieve pinned secrets for its Worker; the agent must never access or expose
their values. The agent's job ends at item 5.

## Standing authorization from the user (2026-10-03)

- Continue through former STOP 1–5 checkpoints without waiting where the next
  step is covered by this document and its safety conditions. Continue across
  turns as needed and report substantive progress/blockers.
- At M3 choose **Option A**: remove any `adminUid`-only readiness shortcut and
  require Track C attestations. Do not weaken either readiness gate.
- The user authorizes the documented protected ETHUSDC **Testnet** lifecycle
  runner once its reviewed-SHA, Testnet-only environment, account, and safety
  prerequisites are verified. Use Testnet credentials only; never change
  leverage or other account settings. If prerequisites are missing or the
  account is out of policy, continue independent work and report the exact
  external action needed.
- The user authorizes ordinary feature-branch push, PR creation/update, and
  merge only when existing required checks and human reviews are satisfied.
  No force push, protection change, review impersonation, or bypass is allowed.
- This does **not** constitute campaign approval, authorization for the agent
  to access Mainnet secret values, permission to ARM, or permission to submit a
  Mainnet order. Campaign approval must be an independently recorded action by
  a real `trading_admin`; Start/ARM and Mainnet orders remain human-only.
- If a prerequisite is blocked, keep advancing independent local work. Pause
  only for an actual human-only action or when the safe next step is
  unspecified. User patience or this authorization never makes a gate pass.

## Hard rules (never break these)

- Never weaken, bypass, stub or delete a safety check, gate, blocker or
  assertion to make something pass. If a gate blocks you, the gate is right
  until a human says otherwise. This includes the readiness gates, risk caps
  (50 USDC order/exposure, 2 USDC stop risk, 5 USDC campaign drawdown
  including costs, ≤10x, 7 days), kill switch, recovery-only and the Python
  Worker `can_start` check.
- Never call `/api/local/pilot/start`, `/api/system/arm`, or anything that
  ARMs the Worker or sends a Mainnet order.
- Never read, print, log or copy secret values: Binance keys, Secret Manager
  payloads, Firebase tokens, `.env*`, `.local-secrets/`, tokens embedded in
  MCP configs. Report key names and presence only.
- Never run `scripts/mint_trading_admin_token.mjs` (it writes claims to the
  production Firebase project).
- Never `git push --force`, change GitHub settings or Environments, delete
  branches, bypass repository protections, or manufacture review/attestation
  evidence. Normal push, PR creation/update, and protected merge are permitted
  under the standing authorization only when repository-required human
  approvals and checks are already satisfied.
- Keep the working tree clean between steps: the pilot refuses to prepare on
  a dirty tree (`LOCAL_PILOT_REQUIRES_REVIEWED_CLEAN_COMMIT`). Commit finished
  work with a clear message; never leave stray logs or temp folders tracked.
- Write a failing test before every behaviour change, then make it pass.
  Python and TypeScript gates must change together and agree (shared
  fixtures in `tests/fixtures/`).
- If a tool, hook or permission check refuses an action, do not look for a
  workaround. Record the exact refusal, continue independent work, and report
  the human-only resolution needed.

## Current state (refresh before acting)

Snapshot refreshed against base commit `52015b2` ("Bind Testnet close proof to
final mutation barrier") on `codex/local-ethusdc-quick-pilot`. It will go
stale again; always re-run `git log -1`, `git status` and the three test
suites before relying on it.

- Track C is merged into `codex/local-ethusdc-quick-pilot`
  (`origin/codex/pilot-track-c-provenance` is an ancestor of `52015b2`). It is
  **not** on `main` in the local view (`52015b2` is not an ancestor of the
  locally known `origin/main`; no fetch was done), so signed `CHECKS` evidence
  for this code does not exist on `main` yet.
- The `adminUid`-only readiness shortcut introduced by `381bf37`
  ("authenticated server-owned pilot readiness channel") is removed from the
  gate: `fd297ad` ("require provenance attestations for readiness") deleted
  the logic. `381bf37` itself is still an ancestor in git history (commits are
  not erased), and `LocalPilotReadinessOptions.authenticatedServerAuthority`
  still exists as an optional field that the readiness code no longer reads.
  Tests keep empty/forged `adminUid` cases BLOCKED.
- Readiness stays `BLOCKED` without real signed evidence. No real Track C
  bundle (CHECKS, three REVIEW_* classes, TESTNET_ETHUSDC) has ever been
  produced or verified, so the `gh attestation verify` JSON shape is still
  unconfirmed. The TypeScript binding now equals the Python binding
  (`tests/local-pilot-binding-parity.test.ts`), the Worker image now contains
  the Track C modules, and `scripts/download_local_pilot_track_c.py` exists
  (fake-`gh` tested only). See `docs/LOCAL-PILOT-TRACK-C.md`.
- Earlier unresolved P1 recovery/reconciliation findings (adoption of unowned
  excess positions, zero-fill UNKNOWN entry recovery, a concurrent
  cancellation-claim race) were recorded at an older snapshot
  (`6db1f75`, 2026-10-03). Later commits such as `25b894b`, `57f55a6`,
  `4c28c9a` and `a8c1ee1` address related paths, but whether each finding is
  closed has not been re-reviewed (UNVERIFIED).
- No Mainnet secret was read, no ARM was performed, and no order was sent by
  the agent.

## Milestones

Do them in order. Each has an acceptance check; do not start the next one
until the check passes.

### M0 — Baseline

- `git status` clean; `npm run lint`, `npx vitest run`,
  `PYTHONPATH=. python -m pytest tests/python -q -m "not contract_readonly and not contract_mutating and not contract_soak"`
  all green. Record the counts.
- Accept: all three green, counts recorded.

### M1 — Show the real Prepare failure reason

- In `src/components/LocalLivePilotPanel.tsx`, show the `reason` field of a
  failed `/api/local/pilot/prepare` response next to the error code. In
  `server.ts`, keep returning only fixed codes or sanitized messages in
  `reason` (no secret material, no raw library text that could carry
  credentials); map raw errors to a code where needed.
- Accept: a test shows the reason rendered; a test shows raw error text is not
  echoed.

**Checkpoint 1 (non-blocking)** — Record M0/M1 results and inspect current
readiness/UI evidence. If Prepare is unavailable because gates are blocked,
proceed to M3/M4 and fix code-owned blockers; do not repeatedly ask the human
to press Prepare. If it becomes available, invoke it only under the conditions
in the standing authorization. Never inspect secret payloads.

### M2 — Fix the cause of the Prepare failure

- Fix code-owned causes. For environment problems (ADC, Docker, Postgres,
  account state, elevated shell), perform safe read-only diagnosis and already
  authorized local setup; do not change credentials, account settings, or
  security policy. Record the exact operator action and continue independent
  milestones instead of stopping all work.
- Accept: Prepare reaches `LIVE/DISARMED` with zero attempts, or the remaining
  cause is outside the code and documented with the exact operator action.

### M3 — Realign the readiness gates

Choose **A** under the standing authorization: remove the `adminUid`-only
READY path and let provenance clear only through Track C attestations. Do not
implement Option B. Implement with tests: empty, whitespace or forged
`adminUid` stays BLOCKED; `/start` stays BLOCKED; TypeScript and Python agree
on the shared fixture.

### M4 — Finish Track C in its worktree branch

- Wire `src/backend/local-pilot-attestation.ts` into
  `local-live-pilot-readiness.ts` in lockstep with Python.
- Add CI producer jobs to `.github/workflows/ci.yml` following the existing
  `local_pilot_ci_attestation` job: push to `main` of repo id 1366161771 only,
  `CHECKS` on push, `TESTNET_ETHUSDC` via `workflow_dispatch` gated by the
  `testnet` Environment.
- Confirm the real JSON shape of `gh attestation verify --format json`
  (`verificationResult.signature.certificate.runInvocationURI`,
  `githubWorkflowTrigger`) against a real bundle once one exists; until then
  the verifier must keep failing closed.
- Document what the attestations prove and what they cannot prove.
- Accept: full suites green; negative tests (no attestation, CI-only,
  cross-class replay, wrong run id or event, stale, dirty tree, verifier
  error) all stay BLOCKED.

**Checkpoint 3 (continue independent work)** — Inspect existing Environment
and branch-protection configuration read-only. Do not change GitHub settings.
Continue implementation, tests, CI preparation, and authorized PR delivery.
If required reviewers or reviewer-identity proof are absent, keep that
attestation class BLOCKED and report the exact human configuration needed; do
not invent or self-approve it.

### M5 — Testnet lifecycle on the reviewed SHA

Under the standing authorization, run the Testnet trial only after the reviewed
SHA, Testnet-only credentials, symbol/account identity, known account state,
and policy-compliant leverage are verified. Never set leverage. Then run
`apps/trading_worker/venues/binance/protected_ethusdc_testnet_runner.py` with
the environment it requires (no Mainnet keys present) and keep the artifact.
If credentials, required Environment approval, or leverage evidence are
missing, continue independent work and leave this acceptance `NOT_RUN`.

- Accept: artifact PASS, `PROTECTED_VERIFIED`, `IN_SYNC`, `diff_count` 0, and
  it confirms that a reduce-only market close is accepted while the stop and
  target Algo orders are open. Be precise about which protection shape that
  proves: the *Testnet* runner places the Algo orders with
  `closePosition=true` (no `reduceOnly`), whereas the *Local Mainnet* path
  places them with `reduceOnly=true`, `closePosition=false` and a fill-sized
  quantity (`execution.py` `_submit_local_mainnet_protection_algo`). The
  Testnet artifact therefore does not prove the Mainnet protection shape
  coexists with a reduce-only close; treat that as an open gap and report it.
  If the close is rejected, stop and report: the emergency-close path would
  not work.

### M6 — Hand-off

- After a protected merge and when gates read READY from attested evidence,
  write the Definition of Done report.

**Final hand-off** — After achievable gates pass and the human merges the
protected PR, report evidence and the exact next human action. Do not ARM.

## Progress reporting

Keep it short:

- What was done, with commit SHAs.
- Test counts and anything that failed.
- What remains, as numbered human-only actions; do not ask for items already
  authorized or answerable from repository/runtime evidence.
- Anything a tool refused, word for word.
