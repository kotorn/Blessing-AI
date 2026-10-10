# Local Pilot day-of checklist (ETHUSDC, Local Mainnet)

> **Release-specific update (2026-10-10):** For the one-entry, two-hour plumbing trial, [`LOCAL-PILOT-TWO-HOUR-SESSION.md`](LOCAL-PILOT-TWO-HOUR-SESSION.md) supersedes any conflicting size, database, Testnet, evidence, and session-duration instruction below. In particular, the session is 120 minutes (not a 24-hour attended trial), the entry target is 40 USDC with a 0.20 USDC risk reserve, the old database volume must be preserved, and a real protected Testnet lifecycle trial is required. The readiness state for this implementation is **NOT ARMABLE** until the new release SHA passes every gate.

Status: derived from a static reading of the code (originally at `52015b2`, revised for Plan v4 and re-checked at `985d162`).
This checklist does not approve anything, does not replace the readiness
gates, and is not evidence that the pilot is ready. Items marked
**UNVERIFIED** are assumptions or things the code does not check; confirm them
yourself. Pressing Start (ARM) is human-only and is never done by an agent.

Fixed pilot limits (from `config/risk/live_research_pilot.json` and
`config/risk/mainnet_local_policy.json`): symbol `ETHUSDC` USD-M perpetual,
50 USDC position/order notional, 2 USDC planned stop risk, 5 USDC campaign
drawdown, leverage at most 10x, wallet/collateral at most 250 USDC, QUICK
management with a 24 hour (86,400 s) maximum hold, campaign length exactly
seven days from approval.

## A. Binance account (do these on Binance, not in the app)

| # | Check | How the code treats it |
|---|-------|------------------------|
| A1 | Account type matches `BINANCE_PORTFOLIO_MARGIN`. A classic USD-M Futures account needs `false`; a Portfolio Margin account needs `true`. | `scripts/start-local.ps1` strictly validates that `BINANCE_PORTFOLIO_MARGIN` is explicitly set to `'true'` or `'false'`. When `true` the adapter uses Binance PAPI endpoints (`/papi/v1/um/*`). An operator may run `scripts/probe_papi_readonly.py` with explicit human authorization for GET-only PAPI diagnostics. Its passing report is diagnostic only; it does not establish readiness, protection acceptance, or permission to ARM. |
| A2 | Position mode is **one-way** (not hedge). | The Local Mainnet protection path only handles `positionSide=BOTH`. Hedge mode is expected to block (**UNVERIFIED** exact gate). |
| A3 | **Multi-Assets mode is off.** | The gate accepts only `CROSS`, `ISOLATED` or `SINGLE_ASSET_CROSS`; `MULTI_ASSET_CROSS` is derived from `multiAssetsMargin` and is not accepted (`reconciliation.py`, `gates.py`). |
| A4 | Collateral is **USDC** and wallet balance is **at most 250 USDC**. | `mainnet_risk.py` rejects other collateral assets and any wallet or collateral above 250 USDC. |
| A5 | Configured leverage for `ETHUSDC` is **at most 10x**. | Checked for configured and effective leverage. The Worker observes leverage and never changes it. |
| A6 | Account is **flat**: no `ETHUSDC` position, no open orders, no conditional (TP/SL) orders. | A leftover position or order blocks readiness/preflight; unowned exchange positions are rejected. Whether other symbols matter is **UNVERIFIED**. |
| A7 | API key has **Reading + Futures** permissions only. No withdrawals, no transfers, no spot/margin trading. | **Not checked by the code** (no key-restriction lookup exists). Manual control only. |
| A8 | API key **IP allow-list** contains only this host's public IP. | **Not checked by the code.** If your ISP changes the IP, the key stops working: verify just before starting. |
| A9 | The pinned Secret Manager versions in local configuration are the ones holding this key. | Names/versions only; never view or paste secret values. |

## B. Host (the Windows machine running the launcher)

| # | Check | Notes |
|---|-------|-------|
| B1 | Docker Desktop is set to start with Windows and is running (`docker-desktop` Linux engine). | The launcher tries to start it but needs service permission; it refuses remote Docker contexts. The Worker image is `blessing-worker:local-runtime`, built from `Dockerfile.worker`. |
| B2 | The shell is **not elevated** and Docker CLI is at `C:\Program Files\Docker\Docker\resources\bin\docker.exe`. | The supervisor requires a non-elevated shell and a trusted Docker CLI. |
| B3 | **No Windows Update window** during the session: set active hours or pause updates, and confirm no reboot is pending. | A reboot or sleep kills the launcher and Worker. A restart never restores approval or credentials; a position left open would have no running Worker. |
| B4 | Power/sleep: disable sleep and hibernate for the session. Remote Desktop session stays connected. | The UI is on `127.0.0.1:3001`, reached via the remote session. |
| B5 | One launcher window: `scripts/start-local.ps1` is running (it runs `npm run dev` in the foreground). Do not close it; closing it stops the Control Plane. | Leave it visible. |
| B6 | No stale containers: `docker ps -a --filter "name=blessing-"`. Only `blessing-postgres-local` should exist as a long-lived container; stop and remove any old `blessing-local-worker-*`. | Do not remove the Postgres volume. Take safe backup via `scripts/backup_local_postgres.py` or `scripts/backup-local-postgres.ps1` if needed. |
| B7 | Ports `3001` and `8000` are free; `5433` is held only by the managed `blessing-postgres-local`. | The launcher refuses to start otherwise and says what holds the port. |
| B8 | Windows clock is correct (`w32tm /resync`). | Attestation freshness tolerates about 2 seconds of clock skew into the future; larger skew can invalidate evidence. |
| B9 | Trusted host tools for Track C verification exist: GitHub CLI at `C:\Program Files\GitHub CLI\gh.exe` with a valid "GitHub, Inc." signature, and a Python Software Foundation signed interpreter with Worker dependencies matching `requirements-worker-windows.lock` in a protected install tree. | The Control Plane verifies Track C on the host even though the Worker runs in Docker. Host Python ACL verification ignores non-inheriting parent directory permissions (`InheritOnly`) and translates AppContainer capability package SIDs safely. |
| B10 | Checkout is a **clean** tree on the reviewed `main` commit, judged **the way the verifier judges it**, with LF line endings (`python scripts/check_tracked_eol.py` exits 0). Run `python -I -c "import sys; sys.path.insert(0,'scripts'); import local_pilot_track_c_source as s; from pathlib import Path; print(repr(s.git(Path('.'),'status','--porcelain','--untracked-files=all')))"` and require `''`. | The Python verifier and downloader scrub Git's user/global config, so a file hidden only by your *global* ignore file (for example an untracked `.claude/settings.local.json`) makes a plain `git status` look clean while Track C refuses with `LOCAL_PILOT_REVIEWED_COMMIT_NOT_CLEAN`. The repository `.gitignore` does not ignore `.claude/` (a human decision; not changed here). The TypeScript `committedClean` uses your normal Git config, so it can say clean when Python says dirty; that disagreement is fail-closed. Dirty trees and CRLF working copies change the source binding and block readiness. |

## C. Evidence and readiness (all must be real, none may be forced)

1. Track C CHECKS, three distinct REVIEW_* classes (satisfying
   `config/risk/track_c_review_policy.json`: either `SOLO_OPERATOR` matching
   `operator_github_id` across 3 distinct runs, or `INDEPENDENT` with 3 distinct
   non-author approvers) and TESTNET_ETHUSDC exist as GitHub-signed subjects
   for this exact commit, and are younger than 24 hours.
2. `python scripts/download_local_pilot_track_c.py --sha <HEAD>` placed the
   five pairs in `artifacts/local-pilot-attestations/` (tool only exercised
   against a fake `gh`; **UNVERIFIED** against real artifacts).
3. `GET /api/local/pilot/readiness` and `GET /api/local/runtime` report `READY`
   from verified attestations. The Control Plane delivers the signed
   `PilotReadinessVerdict` (TTL 300s, max 3600s, refreshed periodically every ~4m)
   to the containerized Worker, and the Worker reports `can_start=True`.
4. A real `trading_admin` approved this campaign. Prepare reached
   `LIVE/DISARMED` with zero order attempts and the read-only preflight passed.
5. Scope limits verified: single-entry cap `max_entries = 1` in effect, strategy
   defaulted to `'grid'`, and non-emergency exit fencing active while QUICK bracket
   is open.

## D. Attended-operator schedule (recommendation, not enforced by code)

- Start only when you can stay at the screen with the Binance app open on a
  second device.
- Stay present from Start until the first entry fills, both protections are
  visible on Binance, and the app shows them verified.
- Re-check Binance (position, open orders, conditional orders) and the app
  state at least every 30 minutes for the first two hours, then at least every
  2-4 hours while awake.
- Be reachable at the 24 hour maximum-hold boundary of any open position and
  at campaign day boundaries. Do not leave a position open across a planned
  reboot, travel, or network outage.
- Have `docs/LOCAL-PILOT-BREAK-GLASS.md` open and the Binance app logged in
  before you start.

## E. Go / no-go

## While armed: why is nothing happening?

Entry comes only from the grid strategy, so an armed pilot can sit for hours
without trading. The Worker `/state` endpoint (served by the local Worker
container) reports `pilot_attempt_diagnostics`: counts of signals, preplan
failures, blocked and executed attempts, the last stage and reason, the last
signal time and the adapter's last order-gate or final-fence block. A rising
`signals` count with rising `execution_blocked` means signals arrive but a gate
refuses them; read `last_reason`. Zero signals means the strategy has not fired.
Stop the campaign at the deadline you agreed beforehand; do not stretch the
window.

## Extra host checks (Plan v4)

- [ ] The Docker host clock agrees with Binance server time within a second (market-data freshness is 3 s and compares your clock with Binance timestamps; a skewed host reads as permanently stale). Check with `w32tm /query /status` and re-sync before ARM.
- [ ] Python 3.13 at `C:\Python313` is on PATH for the shell that runs `start-local.ps1` (the resolver reads `where.exe python.exe`).
- [ ] Local PostgreSQL volume: migrations 020-022 are refused on a database that already holds trading data (`Existing local trading data found while migrations are pending`). If your `blessing_postgres_local_data` volume has rows from earlier campaigns, back it up with `scripts/backup-local-postgres.ps1`, then recreate the volume. A fresh volume works: `init_schema.sql` now records migrations 001-012 so only 013+ are applied.
- [ ] `git status --porcelain --untracked-files=all` is empty and `start-local.ps1` is run from the reviewed commit (the launcher now refuses a dirty tree and labels the Worker image with the commit).
- [ ] You know how to cancel leftover stop/target Algo orders by hand on Binance and how to close the position by hand (the kill switch does neither).

GO only if every line is YES:

- [ ] A1-A9 confirmed on Binance today (A7/A8/A9 by you, not the code)
- [ ] B1-B10 confirmed on the host
- [ ] Track C evidence for this exact commit is present, verified and under 24 hours old
- [ ] Readiness is `READY` in the UI from attestations (not forced, not mocked)
- [ ] A real `trading_admin` approved; Prepare is `LIVE/DISARMED`, zero attempts, preflight passed
- [ ] Account flat; wallet at most 250 USDC; leverage at most 10x
- [ ] You are attended for the entry and protection verification window
- [ ] Break-glass document and a second Binance session are open

NO-GO (stop, do not Start) if any line above is NO, if anything says `UNKNOWN`
or `BLOCKED`, if the clock, IP, or tree state changed after verification, or if
you feel rushed. A blocked gate is correct until a human with authority decides
otherwise.
