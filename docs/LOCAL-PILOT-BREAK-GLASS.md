# Local Pilot break-glass procedure (ETHUSDC, Local Mainnet)

Status: written from a static reading of the code at base commit `52015b2`.
Nothing here has been exercised against a running Worker or against Binance.
Anything marked **UNVERIFIED** is an assumption the operator must confirm
before relying on it. Binance screen names change; treat menu names as
approximate.

Priority order, always: (1) stop the software from acting, (2) make the
exchange flat with no open orders, (3) preserve evidence, (4) only then touch
the database.

## When to use this

Use it when any of the following is true and the normal UI flow
(`Close only` / `Revoke` / kill switch) is unavailable, unconfirmed, or you do
not trust it:

- the UI or Worker is unreachable, hung, or returns `UNKNOWN`/`PARTIAL` for a
  kill switch or close-only request;
- Binance shows a position or order you did not expect, or a position without
  both protective orders;
- reconciliation will not return to `IN_SYNC` and a position exists;
- the host is going down (Windows Update, power, network) while a position is
  open and nobody can attend;
- you suspect the API key is exposed.

If the UI works, prefer the application paths first
(`POST /api/local/pilot/close-only`, `POST /api/system/kill-switch`,
`POST /api/system/disarm`, all from the signed-in UI). They are fail-closed
and audited. This document is for when they do not work.

## Step 1 - Stop the software from placing or amending orders

Do this first so the Worker does not fight your manual actions (for example by
re-placing protections or sending its own emergency close).

1. If the UI responds: engage the **kill switch**, then **Disarm**. By code
   reading, the kill switch sets a local block, pauses new risk, cancels the
   *regular* open orders returned by the exchange `openOrders` endpoint, then
   reconciles (`apps/trading_worker/main.py` `set_kill_switch`,
   `execution.py` `cancel_all_open_orders`). It does **not** close a position
   and, by static reading, does **not** cancel conditional (Algo) stop/target
   orders, which live on a separate `openAlgoOrders` endpoint. Runtime
   behavior is **UNVERIFIED**. Treat the kill switch as necessary but not
   sufficient.
2. If the UI does not respond or you cannot confirm the state: stop the Worker
   container. In PowerShell:

   ```powershell
   docker ps --filter "name=blessing-local-worker"
   docker stop <container-name-from-the-list>
   ```

   The supervisor starts it with `--rm --restart=no`
   (`src/backend/local-worker-supervisor.ts`), so a stopped Worker does not
   come back on its own. If `docker` is not on `PATH`, use the Docker Desktop
   window to stop the container whose name starts with
   `blessing-local-worker-`.
3. Stop the Control Plane too: click the PowerShell window running
   `scripts/start-local.ps1` (it runs `npm run dev` in the foreground) and
   press `Ctrl+C`. Closing that window has the same intent but is less clean.
4. Do **not** stop or remove the `blessing-postgres-local` container, and do
   not run `docker compose down -v` or delete the Postgres volume. The
   database is the evidence of what the Worker believed it owned.

## Step 2 - Make Binance flat (manual, Binance web or app)

Use the account that holds the pilot (USD-M Futures, symbol `ETHUSDC`). The
Local Mainnet protection code only handles **one-way** position mode
(`positionSide=BOTH`, `_submit_local_mainnet_protection_algo` returns nothing
otherwise), so expect a single net `ETHUSDC` position. If you see separate
LONG and SHORT positions the account is in hedge mode: close both.

1. Open **Futures -> Positions**. If an `ETHUSDC` position exists, close the
   full quantity with a **Market** close. Use the close-position / reduce-only
   control rather than opening an opposite order. Confirm the position shows
   zero afterwards.
2. Open **Futures -> Open Orders** and cancel every open `ETHUSDC` order.
3. Open the **conditional / TP-SL orders** view (separate tab from Open Orders
   in the Binance UI; exact name **UNVERIFIED**) and cancel every conditional
   stop-market and take-profit-market order for `ETHUSDC`. Repeat until the
   list is empty. These are the Algo protections the Worker placed.
4. Re-open Positions, Open Orders and the conditional view. All three must be
   empty. Take screenshots that do not show API keys or balances beyond what
   you are comfortable storing.

Order of 1 and 3: close the position first. Cancelling the stop while a
position is still open leaves it unprotected.

## Step 3 - If you suspect key exposure

In Binance API Management, delete or disable the key used by the pilot. Do not
paste the key, secret or any token into chat, tickets or this repository.
Re-issue a new key later with **Reading + Futures** permissions only (no
withdrawals, no transfers) and the IP allow-list set. The code does not check
key permissions or IP restrictions (no `apiRestrictions` lookup exists), so
these are purely manual controls.

## Step 4 - Stale database rows (only after Steps 1 and 2)

Two tables describe what the Worker thinks it owns:

- `mainnet_launch_sessions` - one row per launch/campaign. A partial unique
  index (`idx_mainnet_launch_one_active`) allows only one session per symbol in
  `ACTIVE`, `PAUSED_NEW_RISK`, `RECONCILIATION_REQUIRED`, `AUTONOMOUS_ACTIVE`
  or `REAUTH_REQUIRED`. A stale row therefore blocks the next launch on
  purpose.
- `binance_algo_protections` - one row per entry, with the stop/target Algo
  identifiers and a `state` of `PENDING`, `PROTECTED`, `CLOSE_PENDING`,
  `CLOSED`, `DEGRADED` or `UNKNOWN`. A Mainnet row may only become `CLOSED`
  with recognized, hash-bound `closure_evidence`
  (`BINANCE_ALGO_CLOSE_VERIFIED`, `LOCAL_EMERGENCY_CLOSE_VERIFIED`,
  `UNFILLED_ENTRY_TERMINAL`, or the explicit `LEGACY_UNVERIFIED` marker that
  keeps readiness fail-closed).

Safe handling:

1. Confirm in Step 2 that Binance is flat with no open or conditional orders.
2. Take a backup of both tables before anything else. Use the database and
   user names from your local configuration (do not print the password; the
   launcher supplies it via the environment):

   ```powershell
   docker exec blessing-postgres-local pg_dump -U <POSTGRES_USER> -d <POSTGRES_DB> `
     -t mainnet_launch_sessions -t binance_algo_protections > break-glass-backup.sql
   ```

   Keep the file outside the repository.
3. Inspect read-only:

   ```sql
   SELECT launch_id, symbol, policy, state, updated_at FROM mainnet_launch_sessions ORDER BY updated_at DESC LIMIT 10;
   SELECT entry_client_order_id, symbol, state, state_reason, closed_at FROM binance_algo_protections ORDER BY updated_at DESC LIMIT 10;
   ```

4. Prefer to let the Worker resolve its own rows: restart through the normal
   launcher (`PAPER`/`DISARMED`), and let startup reconciliation run. Whether
   it fully resolves a stale `PROTECTED` or `CLOSE_PENDING` row is
   **UNVERIFIED**.
5. If a row still blocks you after that, stop and ask the code owner. This
   repository provides no supported manual `UPDATE` procedure, and none is
   given here on purpose: the closure evidence is a safety proof, and the
   readiness gate treats hand-edited rows as untrusted. A stale row that keeps
   the pilot blocked is the correct, fail-closed outcome.

## What NOT to do

- Do not run `POST /api/local/pilot/start`, `POST /api/system/arm` or
  `POST /api/system/continue` to "fix" anything.
- Do not `DELETE` or `TRUNCATE` rows in either table, drop the schema, or
  delete the Postgres volume.
- Do not set a Mainnet algo row to `CLOSED` or write `closure_evidence` by
  hand.
- Do not change leverage, margin mode, position mode or Multi-Assets mode on
  the exchange to "make the Worker happy".
- Do not paste API keys, secrets, Firebase tokens or `.env*` content anywhere.
- Do not restart the Worker repeatedly hoping it recovers while an unprotected
  position exists. Close the position manually first.
- Do not disable the kill switch until Binance is flat, no orders or conditional
  orders remain, and the Worker reports a verified reconciliation.

## After the incident

Record: time, what you saw, which steps you took, Binance screenshots of the
final flat state, and the database backup file name. Do not resume the pilot
until a maintainer has reviewed the evidence; a new attempt needs fresh
readiness (Track C evidence is bound to a reviewed clean commit and expires
within 24 hours).
