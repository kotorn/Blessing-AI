# Agent goal: make the local ETHUSDC QUICK pilot ready for a human to start

This file is the standing goal for any coding agent working in this repo,
including Antigravity. Work through the milestones below in order and keep
going until the Definition of Done is met or you reach a **STOP** checkpoint.
At a STOP, report and wait for the human. Do not skip a STOP because the next
step looks easy.

## Definition of Done

The goal is done when all of these are true on one clean, committed SHA:

1. `GET /api/local/runtime` and `GET /api/local/pilot/readiness` report
   `READY`, and the Python Worker gate
   (`apps/trading_worker/venues/binance/local_pilot_readiness.py`) agrees.
   Both gates reach READY only from **externally attested** evidence, never
   because an operator is logged in.
2. CI is green on that SHA, and the SHA is merged to `main` by the human.
3. A protected ETHUSDC Testnet lifecycle artifact for that SHA reads PASS,
   `PROTECTED_VERIFIED`, `IN_SYNC`, `diff_count` 0, with nothing left open.
4. The pilot is approved and prepared to `LIVE/DISARMED` with zero order
   attempts, and the signed read-only preflight passes.
5. A short report lists the evidence for 1–4 and the exact button the human
   presses next.

**Pressing Start (ARM) and every Mainnet order belong to the human.** The
agent's job ends at item 5.

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
- Never `git push --force`, merge a PR, change GitHub settings or
  Environments, or delete branches. Normal `git push` of a feature branch is
  allowed only at the STOP that asks for it.
- Keep the working tree clean between steps: the pilot refuses to prepare on
  a dirty tree (`LOCAL_PILOT_REQUIRES_REVIEWED_CLEAN_COMMIT`). Commit finished
  work with a clear message; never leave stray logs or temp folders tracked.
- Write a failing test before every behaviour change, then make it pass.
  Python and TypeScript gates must change together and agree (shared
  fixtures in `tests/fixtures/`).
- If a tool, hook or permission check refuses an action, do not look for a
  way around it. Report it at the next STOP.

## Current state (2026-10-03)

- Branch `codex/local-ethusdc-quick-pilot`; PR #51 to `main` is open with
  green CI up to `464415e`. Five later local commits are **not pushed and not
  reviewed**: `e185809`, `381bf37`, `c679d29`, `41d3acc`, `d3bdef7`.
- `381bf37` makes the TypeScript gate return `READY`/`canApprove: true`
  whenever a non-empty `adminUid` is passed
  (`src/backend/local-live-pilot-readiness.ts` ~338-344). That removes the
  three `*_PROVENANCE_UNVERIFIED` blockers with no external attestation. The
  Python Worker gate was not changed and still returns `BLOCKED`, so the two
  gates disagree. ARM and orders are still refused by the Worker.
- Campaign `pilot-267acb37-1fb9-4d31-931c-48ea11f5cf6f` is `APPROVED`;
  Prepare failed with `LOCAL_PILOT_PREPARE_FAILED`. The real `reason` is in the
  HTTP response body and is not shown in the UI or logged.
- Track C (attested provenance) is partly built and uncommitted in worktree
  `.claude/worktrees/wf_c86e89dc-1c6-1` on branch
  `codex/local-pilot-attestation-channel`: Python gate and verifier done (306
  tests), certificate run-id/event binding added; TypeScript wiring, CI
  producer jobs and docs not done. Review classes stay BLOCKED by design
  because a GitHub certificate cannot prove who approved an Environment.
- GitHub Environments: `testnet` exists with no required reviewers;
  `pilot-review` does not exist.
- Plan with full background: `C:\Users\Kan\.claude\plans\scalable-bubbling-dongarra.md`.

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

**STOP 1** — Report M0/M1. Ask the human to press Prepare once and paste the
`reason`. Do not press Prepare yourself (it reads Mainnet secrets).

### M2 — Fix the cause of the Prepare failure

- Fix only what the `reason` points to. If it is an environment problem
  (ADC, Docker not running, Postgres unreachable, Binance account not flat,
  elevated shell), write the exact steps for the human instead of changing
  code.
- Accept: the human confirms Prepare reaches `LIVE/DISARMED` with zero
  attempts, or the remaining cause is outside the code and documented.

### M3 — Realign the readiness gates

**STOP 2 before writing code.** Ask the human which way to go:

- **A (recommended):** revert the `adminUid`-only READY path from `381bf37`
  and let provenance clear only through Track C attestations.
- **B:** keep server-authority READY for the local pilot only, with
  `canStart` pinned false in TypeScript, the Python Worker as the final gate,
  a visible UI warning, and tests that prove READY never enables ARM.

Then implement the chosen option with tests: empty, whitespace or forged
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

**STOP 3** — Ask the human to (a) add required reviewers and a `main`-only
branch policy to the `testnet` Environment, (b) decide how reviewer identity
will be proven, and (c) approve pushing the branch and opening a PR.

### M5 — Testnet lifecycle on the reviewed SHA

**STOP 4 before running.** The human must approve the Testnet trial and set
Testnet ETHUSDC leverage to 1–10 themselves. Then run
`apps/trading_worker/venues/binance/protected_ethusdc_testnet_runner.py` with
the environment it requires (no Mainnet keys present) and keep the artifact.

- Accept: artifact PASS, `PROTECTED_VERIFIED`, `IN_SYNC`, `diff_count` 0, and
  it confirms that a reduce-only close is accepted while reduce-only stop and
  target algos are open. If that close is rejected, stop and report: the
  emergency-close path would not work.

### M6 — Hand-off

- After the human merges and the gates read READY from attested evidence,
  write the Definition of Done report.

**STOP 5 (final)** — Hand over. Do not ARM.

## How to report at a STOP

Keep it short:

- What was done, with commit SHAs.
- Test counts and anything that failed.
- What you need from the human, as a numbered list of concrete actions.
- Anything a tool refused, word for word.
