# Blessing AI ETHUSDC Mainnet Release Runbook

This runbook is the operational boundary for the first Mainnet launch. The
repository changes and CI checks do not send an order. Every step is fail-closed
and must leave the Worker `DISARMED` when evidence is missing or stale.

## Fixed release policy

- Project: `gen-lang-client-0730128480`
- Region: `asia-southeast1`
- Symbol: `ETHUSDC` USDⓈ-M perpetual
- Cloud SQL (Cloud Run only): `blessing-sql-primary` / `blessing_trading`
- Initial release policy: `STAGED_FIRST_ORDER`
- Continuation policy: `AUTONOMOUS_AFTER_REVIEW`
- Collateral: at most 250 USDC
- Gross exposure: at most 1,000 USDC
- First order: at most 50 USDC notional
- Daily loss: at most 5 USDC
- Basket budget: 250 USDC; basket drawdown stop: 125 USDC
- Risk:reward: at least 1:2 net of fees, funding, and slippage
- Configured and effective leverage: at most 10x
- Active exposure chains: exactly one
- The exchange leverage setting is observed, never changed automatically
- `VITE_DATA_CONNECT_CUTOVER=false`; Firestore remains the authority
- Carry remains fail-closed until live economics are evidenced

Never put an API key, API secret, password, DSN, Firebase token, OIDC token, or
private stream listen key in a prompt, command argument, log, evidence file, or
this repository. Secret Manager values are injected only into the Worker.

## Separate runtime targets

Cloud Run and Local are independent release targets. Cloud candidates use the
Cloud release store, immutable Worker digest/revision, and Cloud SQL. Local
candidates use the separate `local_release_candidates` collection, a fresh
Local run ID, source/dependency/migration fingerprints, pinned Secret Manager
versions, and the canonical `config/risk/mainnet_local_policy.json` hash.
Cloud endpoints reject Local candidate/approval IDs; Local endpoints never
read or consume Cloud release candidates.

The Local launcher binds the UI to `127.0.0.1:3001`, Worker to
`127.0.0.1:8000`, and PostgreSQL to `127.0.0.1:5433`. The Worker starts in
`PAPER`/`DISARMED` with no Mainnet key. Only a Firebase
`trading_admin` approval of a current, unexpired Local candidate permits the
server to retrieve the exact numeric Secret Manager versions and pass them to
the supervised Worker. That transition starts `LIVE`/`DISARMED`, performs a
read-only preflight, and does not arm or submit an order. Any failed preflight
returns the Worker to `PAPER`; restart/reboot never restores approval or
credentials. Local persistence must identify itself as loopback PostgreSQL,
not Cloud SQL.

The first risk-increasing order is capped at 50 USDC notional. With a fresh
5 USDC daily-loss headroom, planned loss (R) is at most 5 USDC and net target
reward must be at least 10 USDC after fees, funding, and slippage. The 125 USDC
basket drawdown cap is separate and the tighter remaining limit wins. Orders
are blocked unless durable basket headroom and the exact Binance stop/target
orders can both be verified. The Local adapter now has code paths for durable
risk context, exchange-backed cost evidence, fill-sized protection read-back,
and pilot accounting. These code paths are not proof of operational readiness:
the risk context requires a fresh VERIFIED accounting snapshot after the first
order; the lifecycle monitor re-reads the live bracket and position; and the
emergency flatten path requires a second signed position read proving flat.
Regression tests cover these fail-closed conditions, but no approved live
preflight or live-order test has been run.

Current Mainnet reconciliation audits `allOrders` and `userTrades` with
bounded pagination and fails closed on missing identities, duplicate client
IDs, invalid cursors, or a page limit. Durable launch-scoped history cursors,
accounting event anchors, and protection ownership are implemented in the
current source tree, but still require an isolated PostgreSQL 17 integration
run against this exact reviewed commit and an authorized Testnet lifecycle
artifact. A Python-level reconnect test is not equivalent to killing and
restarting the Worker or PostgreSQL service.

The Local supervisor passes a minimal Worker environment allowlist rather than
inheriting the Control Plane's full environment. Firebase/Google ADC and
unrelated credentials are excluded; PostgreSQL connection values and explicit
runtime settings are retained. This isolation has unit coverage but has not
yet been read back from a running Worker process.

The capped seven-day Local Live Research Pilot is a separate, fixed-risk
research cohort. OOS/Shadow evidence is not a prerequisite for that pilot, but
pilot results must not be counted as OOS/Shadow evidence or authorize larger
size. Any later promotion or increase in risk still requires authentic OOS and
Shadow evidence: at least 50 closed baskets combined, with each cohort
independently meeting MaxDD (le 3.5%), Sharpe (ge 1.0), Win Rate (ge 50%), and
zero Rule #0 violations. Evidence must be hash-bound to its dataset,
configuration, replay artifact, and reviewed source SHA. Missing or
illustrative evidence keeps scale-up blocked.

Local Pilot capability checks use a separate audit export at
`artifacts/local-pilot-capability.json` (ignored by Git). The collector
`npx tsx scripts/collect_local_pilot_capability.ts` runs the TypeScript and
Python suites, lint, build, isolated PostgreSQL 17 migration/restart test,
lease tests, and protection/monitor tests against an exact clean commit. It
requires `BLESSING_MIGRATION_TEST_DSN` to name a fresh dedicated test database
and the pinned secret resource identifiers in the operator environment; it
does not read secret values or call the exchange. A failed or interrupted
collection invalidates an earlier receipt. The receipt is accepted only for
the current commit, source/dependency/migration/policy fingerprints and recent
results. This JSON and its hashes prove integrity only, not who ran the checks.
The capability receipt is therefore an audit export only and never opens the
gate. Readiness clears only from the Track C GitHub-signed attestations
described in the next section; plain JSON reports under
`artifacts/local-pilot-reviews/` and distinct reviewer-name strings do not
authenticate reviewer identity or independence and cannot open the gate.

The Worker runtime is selected by `LOCAL_WORKER_RUNTIME`.
`scripts/start-local.ps1` always sets it to `DOCKER`, so the default launcher
never resolves a host Python for the Worker: the Worker runs in the pinned
`blessing-worker:local-runtime` image and the supervisor instead requires a
non-elevated shell, a trusted Docker CLI at
`C:\Program Files\Docker\Docker\resources\bin\docker.exe` (admin-owned,
Authenticode `CN=Docker Inc,`), the `docker-desktop` Linux engine, and a
matching `LOCAL_WORKER_IMAGE_ID`.

Only when the Worker is started in `HOST_PYTHON` mode (not the default) does
the Local supervisor require, before any Mainnet secret can be passed to the
Worker, a non-elevated Windows host with a Python Software Foundation signed
interpreter, a protected installation tree, and the pinned Worker
dependencies available in isolated mode. It ignores
`LOCAL_PYTHON_EXECUTABLE`; the process starts with Python isolated mode so
user-site startup hooks do not run. The same trusted interpreter is also
required by `scripts/collect_local_pilot_capability.ts` and by the advisory
CI-attestation check. On the current host, Python 3.14 is missing Worker
dependencies and the Python 3.13 installation with dependencies is under the
interactive user's profile; additionally the protected-tree check rejects the
inherited `Authenticated Users:(OI)(CI)(IO)(M)` entry on every NTFS drive
root, so no eligible runtime is verified. For `HOST_PYTHON` and the collector
this is a blocking host prerequisite, not a test result that can be
overridden.

Open item (launcher hygiene): `scripts/start-local.ps1` runs
`scripts/apply_local_postgres_migrations.py` with whichever `python.exe`
`Get-Command` finds first on `PATH`, without the trust checks above and
without Python isolated mode, while `POSTGRES_PASSWORD` is set in the
environment. User-writable `PATH` entries or user-site packages could
therefore execute code with the local database password. This does not touch
Mainnet keys, but the migration step should use a verified interpreter in
isolated mode (or run inside the pinned container) before the local database
is relied on for Pilot evidence.

The collector also requires a recent sanitized `testnet-trial-*.json` from a
separately authorized `ETHUSDC` Testnet lifecycle with `IN_SYNC` reconciliation
and no remaining position. That artifact is an audit claim, not a trusted
exchange observation; without a trusted observer/attestation path it cannot
open the gate. The existing manual Testnet workflow currently
targets `BTCUSDT`; its artifact cannot pass the Pilot receipt. No Testnet
order is submitted by the collector. Missing, stale, incomplete, or
cross-symbol evidence keeps Pilot approval blocked.

## Local Pilot readiness: Track C evidence and the Docker Worker

Status: written from the code at base commit `52015b2`. No real signed Track C
bundle exists yet, so every statement about live GitHub/`gh` output is
**UNVERIFIED**. Readiness stays `BLOCKED` without real signed evidence.

**What opens the gate.** Five distinct GitHub-signed subjects, all bound to the
same clean reviewed commit and younger than 24 hours: `CHECKS` (from the `main`
push of `ci.yml`), `REVIEW_AUTH_RELEASE`, `REVIEW_ORDER_RISK`,
`REVIEW_PERSISTENCE` (three separate `workflow_dispatch` runs of
`local-pilot-track-c.yml`, each approved in the `pilot-review` Environment by a
different non-author user) and `TESTNET_ETHUSDC` (a dispatch approved in the
`testnet` Environment). Full rules and limits are in
`docs/LOCAL-PILOT-TRACK-C.md`. These attestations do not prove Mainnet
behavior, profitability, campaign approval, or permission to ARM. Environments
and branch protection are human configuration; this repository does not change
them.

**Operator sequence (humans do the GitHub steps).**

1. Merge the reviewed commit to `main` and let CI produce `CHECKS`.
2. Dispatch the review and Testnet runs and approve them as the required
   reviewers. An agent must not self-approve or impersonate a reviewer.
3. On the Local host, check out exactly that `main` SHA with a clean tree and
   LF line endings (`python scripts/check_tracked_eol.py`). "Clean" means clean
   to the Python verifier, which ignores your global Git config: an untracked
   file hidden only by a global ignore (e.g. `.claude/settings.local.json`)
   still blocks Track C. See `docs/LOCAL-PILOT-DAY-OF-CHECKLIST.md` B10 for the
   exact check. Whether to ignore `.claude/` in the repository `.gitignore` is a
   human decision and was not changed.
4. `python scripts/download_local_pilot_track_c.py --sha <that SHA>` copies the
   subject/bundle pairs into `artifacts/local-pilot-attestations/`. It is
   convenience only (and has only been tested against a fake `gh`); it never
   overwrites files and verifies nothing.
5. The Control Plane verifies them when `/api/local/pilot/readiness` is read,
   by running `scripts/verify_local_pilot_track_c.py` with a trusted host
   Python and `gh attestation verify` with the signed GitHub CLI at
   `C:\Program Files\GitHub CLI\gh.exe`. The `--binding` it passes comes from
   `computeLocalReleaseFingerprint`, which derives its git SHA and
   source/dependency/migration hashes from `computeTrackCBinding`, plus the
   pilot policy hash. These are byte-identical to the Python values
   (`tests/local-pilot-binding-parity.test.ts`). Changing the hashed file lists
   changed every hash, so any previously created local candidates, approvals
   or capability receipts no longer match and must be recreated (fail-closed).

**Docker path.** `scripts/start-local.ps1` always builds the Worker image from
`Dockerfile.worker` and runs it as a container; the Control Plane and the Track
C verification stay on the host. The image now copies the `scripts/` modules
the Worker imports (`tests/python/test_dockerfile_worker_imports.py` keeps this
complete), but the Worker's own Python gate cannot reach `READY` inside the
container: there is no `git`, no `gh` and no `artifacts/` or source tree in the
image. Treat the host TypeScript gate as the readiness authority for this path;
how the container should consume host-verified evidence is an open decision
(see `docs/LOCAL-PILOT-TRACK-C.md`). The default Docker Worker does not need a
host Python, but the **Track C verification does** (trusted, PSF-signed,
protected-tree interpreter). The paragraph above recording that no eligible
interpreter existed on the original host may still apply; its current state is
**UNVERIFIED**.

**Manual recovery and day-of checks.** `docs/LOCAL-PILOT-BREAK-GLASS.md` and
`docs/LOCAL-PILOT-DAY-OF-CHECKLIST.md` replace the older
`docs/LIVE-READINESS-CHECKLIST.md` and `docs/SMALL-LIVE-CHECKLIST.md`, which
are marked superseded.

## Gate 1 — Repository and identities

1. Confirm the release branch is based on the reviewed `origin/main` SHA and
   that CI is green, including Data Connect generation and Python 3.13 tests.
2. Run the read-only identity check:

   ```powershell
   .\infra\cloudrun\verify-release-identities.ps1 `
     -ProjectId gen-lang-client-0730128480 `
     -Region asia-southeast1
   ```

   The Worker invoker must contain only the dedicated Control Plane service
   account. `allUsers` and `allAuthenticatedUsers` are a hard failure. The
   Release Controller must not invoke the Worker and must not access secrets.
3. Run the monitoring and budget read-backs. Supply the billing account only
   in the local operator environment, never in a repository artifact:

   ```powershell
   .\infra\monitoring\verify-release-monitoring.ps1 `
     -ProjectId gen-lang-client-0730128480
   .\infra\monitoring\verify-budget.ps1 `
     -ProjectId gen-lang-client-0730128480 `
     -BillingAccount '<BILLING_ACCOUNT_ID>'
   ```
4. For Cloud Run only, apply the durable database schema to Cloud SQL before any Worker
   deployment that requires persistence. Use the current full schema in `infra/postgres/init_schema.sql`
   for a fresh install, or `infra/postgres/migrations/001..020` in filename
   order for an upgrade (migration 020 is deliberately not in
   `SAFE_POPULATED_UPGRADES`; do not apply it to a populated database without
   separate review), executed through the Cloud SQL Auth Proxy against
   `blessing-sql-primary` / database `blessing_trading`. Record the applied
   file list and timestamp as evidence. There is no automated applier in this
   repository on purpose: this step is manual and audited. The Worker
   fail-closes at `/ready` when the schema is absent, and the Release
   Controller rejects a candidate whose persistence is not durable, so a
   missed schema surfaces as a stop condition — never as a degraded trade.

## Gate 2 — Control Plane authentication

Deploy the Control Plane with an immutable image digest and no Binance or SQL
secret injection. It may have public transport for the SPA, but protected
routes must require server-verified Firebase claims. Run:

If browser access to the SPA is required, configure public transport as a
separate administrator IAM operation after deployment. This does not grant any
public access to the Trading Worker:

```powershell
.\infra\cloudrun\configure-control-plane-transport.ps1 `
  -ProjectId gen-lang-client-0730128480 `
  -Region asia-southeast1 `
  -Apply
```

```powershell
.\infra\cloudrun\verify-control-plane-auth.ps1 `
  -ControlPlaneUrl 'https://<control-plane-host>'
```

Anonymous and invalid Firebase requests must return 401/403. A verified
`trading_admin` acceptance must be performed through the application identity
flow; do not paste a Firebase token into a shell command or chat.

The authenticated Control Plane readiness read-back must also pass. It checks
the Firebase Admin verifier, server-side Firestore release store, and Worker
readiness through the Worker OIDC boundary:

```text
POST /internal/release/readiness
status=ready, evidence_status=VERIFIED
```

## Gate 3 — LIVE-disarmed revision and Mainnet read-only preflight

First deploy the exact immutable Worker image as a LIVE revision that is still
disarmed. This is provisioning only: it must not consume a release approval,
ARM the engine, or call a Binance order endpoint. Supply Secret Manager
numeric versions locally to the deployment helper; never put their values in a
command or repository:

```powershell
.\infra\cloudrun\deploy-live-disarmed.ps1 `
  -ImageUri '<worker-image@sha256:...>' `
  -CloudSqlPasswordVersion '1' `
  -BinanceMainnetApiKeyVersion '1' `
  -BinanceMainnetApiSecretVersion '1'
```

Read back the latest Ready revision with `verify-live-disarmed.ps1`. Record
its `latestReadyRevisionName`; that exact revision is the `WorkerRevision`
supplied when the candidate is created. The Worker state uses Cloud Run's
immutable `K_REVISION` until promotion pins the preflighted source revision
explicitly.

Run the Worker read-only preflight through the Control Plane OIDC boundary.
The preflight must be completed on this LIVE-disarmed revision before a
candidate can be created.

The preflight must prove all of the following without changing state:

- fixed Binance Mainnet endpoint and signed account request
- `canTrade=true`, USDC availability, current position mode, and leverage at
  most 10x
- ETHUSDC is `TRADING`, `PERPETUAL`, quote/margin asset is USDC, and all symbol
  filters came from live `exchangeInfo`
- fresh market data and private-stream heartbeat
- Cloud SQL persistence is `REQUIRED` and durable
- exchange and durable ledger reconciliation is `IN_SYNC`
- `order_submission_attempts=0` and `order_endpoint_attempts=0`
- private stream is closed after the disposable preflight

Any failed, stale, or ambiguous check leaves the Worker `DISARMED`.

After the evidence files have been independently reviewed, create the
candidate with the attached Release Controller identity. The helper accepts
only hashes, immutable identifiers, and numeric Secret Manager versions; it
does not accept credential values or tokens on the command line:

```powershell
.\infra\cloudrun\create-release-candidate.ps1 `
  -ControlPlaneUrl 'https://<control-plane-host>' `
  -RepoSha '<reviewed-repo-sha>' `
  -ImageUri '<worker-image@sha256:...>' `
  -WorkerRevision '<disarmed-worker-revision>' `
  -CloudSqlPasswordVersion '1' `
  -BinanceMainnetApiKeyVersion '1' `
  -BinanceMainnetApiSecretVersion '1' `
  -PreflightEvidenceHash '<sha256>' `
  -RepoGateEvidenceHash '<sha256>' `
  -CloudGateEvidenceHash '<sha256>' `
  -ExpiresAt '<utc-timestamp-within-24-hours>' `
  -Nonce '<unique-random-nonce>'
```

The returned candidate remains `PENDING_APPROVAL` and `DISARMED` until a
verified Firebase `trading_admin` performs the separate approval step.

## Gate 4 — Candidate verification and approval

The candidate endpoint itself reads back the Worker and rejects creation if
the image digest, latest revision, LIVE mode, disarmed state, or zero-order
counter does not match. Re-run the authenticated candidate read-back and
preflight verification immediately before approval. The engine must still
report `DISARMED` and submit zero orders; do not call ARM as part of this
gate.

## Gate 5 — Approval and first order

A verified Firebase `trading_admin` approves the unexpired candidate. Approval
creates a one-time record; it does not directly set `MAINNET_LIVE_APPROVED` or
ARM the engine. The Release Controller consumes that record and deploys the
same digest as a LIVE-approved but still-disarmed revision.

The approval-consuming deployment runs from
`cloudbuild-release-controller.yaml` with the dedicated Release Controller
identity. The Cloud Build submission must provide an immutable Cloud SDK image
digest and the SHA-256 of
`infra/cloudrun/release_controller.py`. It does not use local
service-account impersonation, receive Binance/SQL secret values, or grant the
controller a Worker invoker role. The controller reads back the Cloud Run
revision and the authenticated Control Plane runtime before it reports success.

After the post-deploy read-back, the separate staged ARM request must include
`ETHUSDC`, `LIVE`, `STAGED_FIRST_ORDER`, and `enforcePreflight=true`. Immediately
before the first risk-increasing decision the Worker rechecks approval,
preflight freshness, private stream, account snapshot, reconciliation,
persistence, kill switch, market freshness, and every hard cap.

The first order is reserved atomically in `mainnet_launch_session`, has a
deterministic client order ID, and is no larger than 50 USDC notional. After
the first submission the Worker sets `pause_new_risk=true`. An ambiguous
response is reconciled by client order ID; it is never blindly retried.

## Gate 6 — Post-first-order verification and continuation

Before any additional risk-increasing order, independently verify:

1. order/fill/position records are durable in Cloud SQL through the outbox;
2. Binance order, position, and balance agree with the ledger;
3. reconciliation is `IN_SYNC`;
4. the staged session is paused and its one-order reservation is consumed; and
5. monitoring events and alert delivery are visible.

Continuing beyond the first order requires a second explicit `trading_admin`
approval. The continuation approval is one-time, expires, and is bound to the
candidate, launch session, initial approval, image digest, Worker revision,
secret versions, first-order evidence hash, fresh preflight, reconciliation
status, requester UID, and nonce. It never stores a token or secret.

Create and inspect continuation evidence through the Control Plane. The
Release Controller identity can run the read-only verification helper:

```powershell
.\infra\cloudrun\verify-continuation.ps1 `
  -ControlPlaneUrl 'https://<control-plane-host>' `
  -LaunchId '<durable-launch-id>'
```

The helper must report `VERIFIED`, `PAUSED_NEW_RISK` or `REAUTH_REQUIRED`, a
durable first-order count of at least one, `IN_SYNC` reconciliation, and zero
preflight order endpoint/submission attempts. It never calls `/arm`,
`/continue`, or any Binance mutation endpoint.

Only after that evidence is independently checked may the verified Firebase
`trading_admin` approve `POST /api/release/mainnet/continuation/approve`.
The Control Plane then verifies the one-time approval and forwards
`POST /continue` to the Worker through Google-signed OIDC. The Worker performs
an atomic SQL transition to `AUTONOMOUS_ACTIVE`; the Control Plane verifies the
returned digest, revision, `LIVE`, `ARMED`, and continuation ID before marking
the approval consumed.

An autonomous process restart or Cloud Run revision change always fences the
durable session as `REAUTH_REQUIRED` and starts the Worker `DISARMED`. It must
obtain fresh preflight evidence and a new continuation approval; it never
resumes from process memory or an old conversation automatically.

Once `AUTONOMOUS_ACTIVE`, each risk-increasing decision still checks fresh
private stream, account snapshot, reconciliation, durable persistence, the
execution lease, kill switch, market data, and all hard caps. Carry remains
fail-closed until live fee/funding/spread/slippage/holding-horizon economics
are evidenced.

A kill switch always uses the direct Control Plane route and does not wait for
AGY. Rollback is: kill switch, reconcile, pause/disarm, route to a known
disarmed digest, set approval false, and read back the final state. The first
order and continuation are separate release actions; repository implementation
and read-only preflight never send an order.

## Stop conditions

Stop and leave the Worker `DISARMED` for missing identity, secret version
mismatch, `canTrade=false`, stale stream or market data, SQL failure, any
reconciliation drift, leverage/cap breach, image mismatch, failed monitoring
read-back, approval expiry/replay, or any non-zero order attempt during a
read-only step. No fallback to simulated state, auto-transfer, auto-leverage,
position-mode mutation, or arbitrary Binance endpoint is permitted.
