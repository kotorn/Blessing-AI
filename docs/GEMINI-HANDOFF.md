# Gemini 3.8 Handoff: First Real ETHUSDC QUICK Trade (Portfolio Margin)

> **Context**: Branch `codex/local-ethusdc-quick-pilot` (PR #51).  
> **Status**: Approved handoff plan (Plan v4 / `gemini-peaceful-pumpkin.md`).  
> **Prior Plan Notice**: Plan v3 (`scalable-bubbling-dongarra.md`) and the legacy staging terms in `LOCAL-PILOT-SUCCESS-CRITERIA.md` (`STAGED_FIRST_ORDER`, "exactly one order", and net reward floor "0.20") are **SUPERSEDED** by this document.

---

## 1. Hard Rules (Verbatim)

- **Never** loosen, bypass, mock or force any gate, except the changes the user has already signed off:
  - **WP1**: SOLO operator approval on Testnet
  - **WP2**: Order gate readiness alignment (`LIVE_RESEARCH_PILOT`)
  - **WP8**: ACL trusted host Python check
  *Anything else that widens permissions &rarr; stop and ask.*
- **Never do any of these**:
  - `ARM`
  - Send an order
  - Merge PRs or branches
  - Change GitHub settings
  - Use the mint script or `emergency_kill_switch.mjs`
  - Read, print, or log secrets (`.env` values, API keys, tokens)
  - Force-push
- **Binance Exchange Calls**:
  - Only public unsigned GET requests in WP3.1, for `ETHUSDC` only. Approving this handoff counts as permission for those.
  - All signed calls must be run by the human operator.
- **TDD Requirement**: Write the failing test first, then fix. When logic lives on both TypeScript and Python sides, change Py and TS in the same commit.
- **Git Restrictions**: May commit and push to `codex/local-ethusdc-quick-pilot` only (no merge, no force push).
- **Python Environment**: Use `py -3.13 -m pytest` or a separate development virtual environment. **Never touch `.venv`** (it lacks pytest and could be the pinned runtime).
- **Hashed Path Tracking**: Before editing any file path, check whether it is in `SOURCE_PATHS` / `DEPENDENCY_PATHS` (`scripts/local_pilot_track_c_source.py:9-18`) and record `hashed: yes/no` in the progress log (`artifacts/gemini-progress/progress.md`).
- **Timing**: Every work package must finish **before merging PR #51**.
- **Acceptance Criteria for Every Work Package**:
  - `npm run lint && npx vitest run`
  - `PYTHONPATH=. py -3.13 -m pytest tests/python -q -m "not contract_readonly and not contract_mutating and not contract_soak"`
  - PR CI remains green.

---

## 2. Order of Execution & Dependencies

Execution must strictly follow this dependency order:
```
WP0 (Setup)
  └─► WP1 (Solo Testnet approval)
        └─► WP4.1 (PAPI route table)
              └─► WP4.8 (PAPI probe script)
                    └─► WP3.1 (Public market data)
                          └─► [Human runs H6 Probe]
                                └─► WP3.2 - WP3.5 (Bracket gate cost planning)
                                      └─► WP2 (Arm-to-order execution chain)
                                            └─► WP4.2 - WP4.7 (Full PAPI support)
                                                  └─► WP5 (Testnet evidence parity)
                                                        └─► WP6 (Sibling algo cancellation)
                                                              └─► WP7 (PilotReadinessVerdict TTL/refresh)
                                                                    └─► WP8 (Trusted Python ACL)
                                                                          └─► WP9 (CI hardening)
                                                                                └─► WP10 (Pilot scope: max_entries=1, grid, fence)
                                                                                      └─► WP11 (Ops hardening)
                                                                                            └─► WP12 (Documentation update)
                                                                                                  └─► WP13 (Close-out & final suite)
```
> **Parametrization Rule**: Every order-path test (WP2.5, WP6, kill switch, WP3 gate) must be parametrized on `portfolio_margin` (`true` / `false`) using the route mappings from `papi-map.md`. A classic-only green run proves a path we will not trade on.

---

## 3. Work Packages

### WP0: Setup
- Add `.claude/**` and `dist/**` to the Vitest exclude list in `vite.config.ts`.
- Initialize `artifacts/gemini-progress/progress.md` (gitignored).

### WP1: Solo Testnet Approval (`TC-01`)
- In `scripts/local_pilot_track_c.py:133-152`, update the Testnet branch to be review-policy aware:
  - `SOLO`: `user.id == operator_github_id` and user is in `reviewer_ids`.
  - `INDEPENDENT`: retain requirement `actor_id != user.id`.
- Pass `review_policy = read_review_policy(root)` at `produce_local_pilot_track_c.py:221`.
- Tests: solo accept, solo wrong approver rejected, operator not in reviewer list rejected, INDEPENDENT self-approval rejected, `verify_all` end-to-end, matching producer test.

### WP4.1: Portfolio Margin Route Table
- Research official Binance PAPI documentation.
- Map every signed `/fapi` route used in the Local Mainnet path to its `/papi` equivalent:
  - Order submission, `openOrders`, `positionRisk`, account/balance, `userTrades`, `allOrders`, `commissionRate`, `leverageBracket`, `positionSide/dual`, and conditional/algo SL/TP routes (`/papi/v1/um/conditional/*`).
- Save table with documentation links to `artifacts/gemini-progress/papi-map.md`.
- *If any required route is missing or behaves differently &rarr; STOP, ask operator.*

### WP4.8: Read-Only PAPI Probe Script
- Write a read-only Python probe script for the human operator to run in **H6**.
- Performs signed GETs only (no orders, outputs redacted) to confirm route shape, taker commission rate, tier-1 MMR, and account state before ARM.

### WP3.1: Public Market Data Fetch
- Perform public unsigned GET requests for `ETHUSDC` only:
  - `/fapi/v1/fundingInfo`
  - `/fapi/v1/exchangeInfo`
  - `/fapi/v1/premiumIndex`
  - `/fapi/v1/depth?limit=20`
- Save results to `artifacts/gemini-progress/ethusdc-public.json`.

--- *Operator Gate: Wait for Human Operator to execute H6 and provide signed rates* ---

### WP3.2 - WP3.5: Bracket Cost & Gate Acceptance (`OP-3`, `OP-5`)
- Evaluate: `50 * cap * events + 100 * taker + slippage + (ask - last) * qty <= ~0.70 USDC`.
  - **Path A** (row exists, borderline/failing): Implement cost-aware async pre-planning before the gate: plan with defaults &rarr; provisional intent &rarr; `adapter.get_local_mainnet_cost_evidence` &rarr; re-plan. Use side price from `get_fresh_market_price`, maintain buffer, require quantity parity, fail closed with clear reason code.
  - **Path B** (no row, fallback): *STOP, ask operator for policy decision.*
- Size the plan at ask/bid instead of `last_price` (`OP-5`).
- Replace circular test in `test_pilot_bracket.py:205-213`.
- Enforce net reward floor of **0.25 USDC** (superseding 0.20).

### WP2: Enable Pilot Order Placement (`OP-1`, `OP-2`, Gap A)
1. Write red test first: `tests/python/test_local_pilot_arm_to_order.py`.
   - ARM a `LOCAL` `LIVE_RESEARCH_PILOT` worker with mocked adapter/persistence &rarr; success.
   - MarketEvent &rarr; `_clamp_order_notional_if_needed` &rarr; gate pass &rarr; `execute_manual_decision` with QUICK bracket.
   - Negative cases stay `MONITOR_ONLY` (pause risk, non-ACTIVE state, expired, drawdown, `MAINNET_LIVE_APPROVED=false`, preflight failure, kill switch).
2. Modify `main.py:3303-3320` and `main.py:3163-3169`:
   - Lifecycle check at ARM/readiness verifies: method presence, `LOCAL`/`LOCAL_ONLY`, monitor health, market freshness, `IN_SYNC`, stream health, `recovery_only=false`, kill switch off.
   - Drop the 5-second per-order evidence requirement from ARM/readiness (per-order correlation remains enforced at execution time in `gates.py` and `execution.py`).
3. Add `local_pilot_execution_ready` near `main.py:327` and explicit `LIVE_RESEARCH_PILOT` branch at `main.py:5507-5516`.
4. In `server.ts:2905-2968`, ensure rejected ARM does not burn campaign into `CLOSE_ONLY`.
5. Full hermetic integration test with fakes across the entire chain.

### WP4.2 - WP4.7: Portfolio Margin Support (`OP-7`, `OPS-02`)
- Cost provider & gate: branch `execution.py:977-987` and `gates.py:271, 282-297` on `portfolio_margin`. Signed routes use `/papi/v1/um/commissionRate` and `/papi/v1/um/leverageBracket`. Public market data stays on `/fapi`.
- Allowlist `/papi/v1/um/commissionRate` in `rest_client.py:89-106`.
- Protection algo submit, read-back, and cancel via PAPI routes using fill-sized reduce-only shape.
- `capabilities.py:45-56`: verify real trade permission in PM mode (remove hardcoded `True`).
- Account checks: collateral &le; 250 USDC, one-way mode, margin-mode derivation.
- Kill switch release (`main.py:3600-3602`): use `adapter._open_orders_path` and inspect open conditional orders.
- Guards: reject `portfolio_margin=True` on TESTNET (`rest_client.py:257`). In `scripts/start-local.ps1:376`, require explicit `true|false`.
- Note: Testnet does not cover `/papi`; the first PAPI order occurs on Mainnet.

### WP5: Testnet Evidence Matches Mainnet Shape (`OPS-03`, `OP-6`)
- `_protect_testnet_entry` (`execution.py:5572-5669`) submits fill-sized `quantity`, `reduceOnly=true`, `closePosition=false`, `positionSide=BOTH` via shared helper.
- Add quantity to close-barrier proof (`execution.py:6520-6534`).
- Update Py `valid_algo` (`scripts/local_pilot_track_c.py:80-91`) **and** TS `validAlgo` (`src/backend/local-live-pilot-readiness.ts:195-204`) simultaneously to require new shape and `quantity == filled`.

### WP6: Sibling Algo Cancellation After Exit (Gap B)
- After verified trigger fill and position flat, call `_cancel_local_mainnet_owned_algos` (fail closed) so reconciliation reaches `IN_SYNC` instead of `MISMATCH` (`reconciliation.py:1921-1949`).
- Tests for PM and classic.

### WP7: PilotReadinessVerdict Refresh & TTL (`D3-01/02/06/07/09`)
- Server side: re-issue verdict on timer (~5 min) while campaign is `ACTIVE`; set null when BLOCKED. `expiresAt = min(now + TTL, oldest_attestation + 24h, campaign_expiry)`.
- Worker side: max TTL 3600, `expiresAt > issuedAt`, binding env checks mandatory with campaign id, retain newest valid verdict, expose `pilot_verdict_status` in `/state`.
- Shared golden fixture and contract tests.

### WP8: Trusted Host Python Verification (`OPS-01`)
- In `src/backend/local-python-runtime.ts:62-85`:
  - Skip parent directory ACEs with `InheritOnly` in PropagationFlags.
  - Handle `Translate()` failures for `S-1-15-2-1`/`S-1-15-2-2` by raw SID.
  - Distinguish "dependencies missing" from "ACL untrusted".
- Windows tests with SDDL fixtures.
- Produce reviewed Windows lock: `uv pip compile --python-version 3.13 --python-platform windows --generate-hashes`.

### WP9: CI Hardening
- Add `actions/setup-python@v7` (3.13) to `local_pilot_ci_attestation` job in `ci.yml`.
- Add `docker build -f Dockerfile.worker` step and import smoke test.
- Hard assert symbol status, trading permission, dual side, open orders, algos, and sizing in `test_testnet_readonly.py`.
- Expose stderr reason from `gh attestation verify` in verifier output (`TC-08c`).

### WP10: Pilot Scope Limits
- Set `max_entries = 1` in `config/risk/live_research_pilot.json` and enforce in persistence (`repositories.py:3288-3312, 3619-3622`).
- Update policy mirrors in `mainnet_risk.py`, `src/backend/local-release-runtime.ts`, and migrations.
- Set strategy to **grid**.
- Fence strategy REDUCE/CLOSE while QUICK bracket is open (only SL/TP, 24h expiry, drawdown, or operator close allowed).

### WP11: Operational Hardening
- Skip `dotenv.config()` in `LOCAL_ONLY` mode (or use sentinel).
- Configure file logging with rotation and secret redaction to gitignored path.
- Update break-glass database dump to use `docker exec ... -f /tmp` + `docker cp`, with backup script and restore test.

### WP12: Documentation Synchronization
- Update `SUCCESS-CRITERIA`, `DAY-OF`, `TRACK-C`, `BREAK-GLASS`, `MAINNET-RELEASE-RUNBOOK`, and `AGENT-GOAL` to match final code:
  - `LIVE_RESEARCH_PILOT` mode
  - 0.25 net reward floor
  - 5-second protection window
  - Verdict TTL/refresh
  - `max_entries = 1`
  - Grid strategy
  - Portfolio Margin routes
  - SOLO Testnet approval rule
  - Ban on admin bypass
- Add Docker clock check, manual leftover algo cancellation, manual close on restart with open position, and 24h window limit.
- Run `scripts/check_tracked_eol.py`.

### WP13: Close-out & Final Verification
- Run complete test suite (Vitest + Pytest) & ensure green status.
- Confirm CI on PR #51 is green.
- Produce final report in `artifacts/gemini-progress/REPORT.md`.

---

## 4. Human Track (Parallel to Development)

- **H1 Testnet Credentials**: Create Futures Testnet key, set GitHub environment secrets `BINANCE_TESTNET_API_KEY` / `SECRET`. Ensure account is ONE_WAY, ETHUSDC leverage 1-10, flat.
- **H2 Mainnet Portfolio Margin Setup**:
  - One-way, ETHUSDC leverage &le; 10, wallet &le; 250 USDC, BNB fee discount OFF, flat.
  - New API key (no withdraw/transfer, IP allowlisted).
  - Store in Secret Manager (`blessing-binance-mainnet-api-key/secret`) and update `.env`.
  - Set `BINANCE_PORTFOLIO_MARGIN=true`.
  - Ensure account holds exclusively USDC &le; 250 (unified equity counts everything).
- **H3 Credential Hygiene**:
  - Rotate any compromised keys/tokens.
  - Test Firebase `trading_admin` login (not mint script).
- **H4 Host Python 3.13 Setup**:
  - As Administrator, install PSF Python 3.13 for all users at `C:\Python313`.
  - Install dependencies: `pip install --require-hashes -r <lockfile from WP8>`.
  - Non-elevated shell must verify resolver returns `TRUSTED`.
- **H5 Host Machine Hardening**:
  - Pause Windows Update, sleep = Never, Docker auto-start enabled.
  - Run `start-local` to build updated image.
  - Test database backup and restore.
  - Enable Binance push notifications.
  - Rehearse manual close + algo cancellation on Testnet.
- **H6 Signed Read-Only Data**:
  - Execute Gemini's probe script (from WP4.8) to collect taker rate, tier-1 MMR, and PAPI account status.
  - Provide output to Gemini for WP3.2 calculations.

---

## 5. Merge & 24-Hour Window Protocol

> [!WARNING]
> The trade window is **under 24 hours from merge** (governed by attestation validity). If entry occurs near the 24h deadline, maximum hold is up to 24h. Total attended commitment may span up to **48 hours**. Merge only when prepared.
> **No-Signal Rule**: If no entry fires by the agreed deadline, stop the campaign and start a fresh window. Do not stretch it.
> **First PAPI Order**: Occurs on Mainnet &mdash; operator must remain attended at the screen until both protection orders confirm on Binance.

1. **Merge PR #51**: Human operator merges PR #51 (commit SHA S = T0). Freeze `main`.
2. **CI Check on Main**: Wait for `ci.yml` push run on S. Verify green and artifact `local-pilot-track-c-checks-S` exists.
3. **Verify Attestation Artifact**: Download and verify with `gh attestation verify` into scratch directory.
4. **Testnet Read-Only Contract**: Dispatch `testnet-contract.yml` with `allow_mutation=false` and verify `TESTNET_READ_ONLY_ALGO_AUDIT`.
5. **Dispatch Track C Evidence**: Dispatch `local-pilot-track-c.yml` 4 times (`REVIEW_AUTH_RELEASE`, `REVIEW_ORDER_RISK`, `REVIEW_PERSISTENCE`, `TESTNET_ETHUSDC`). Approve each via "Review deployments" (never admin bypass).
6. **Local Verification**: Run `download_local_pilot_track_c.py --sha S` and `verify_local_pilot_track_c.py --root .`. Confirm all 5 PASS and readiness reports READY.
7. **Rehearsal**: Request (grid) &rarr; approve &rarr; prepare. Confirm state reaches `LIVE/DISARMED` with 0 attempts and preflight PASS.
8. **Operator ARM**: Human operator clicks ARM. Watch entry fill and verify both SL and TP confirm on Binance within 5 seconds. Monitor until flat and reconciled.
