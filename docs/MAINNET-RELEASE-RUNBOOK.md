# Blessing AI ETHUSDC Mainnet Release Runbook

This runbook is the operational boundary for the first Mainnet launch. The
repository changes and CI checks do not send an order. Every step is fail-closed
and must leave the Worker `DISARMED` when evidence is missing or stale.

## Fixed release policy

- Project: `gen-lang-client-0730128480`
- Region: `asia-southeast1`
- Symbol: `ETHUSDC` USDⓈ-M perpetual
- SQL: `blessing-sql-primary` / `blessing_trading`
- Release policy: `STAGED_FIRST_ORDER`
- Collateral: at most 100 USDC
- Gross exposure: at most 1,000 USDC
- First order: at most 50 USDC notional
- Daily loss: at most 5 USDC
- Leverage: at most 10x
- Active exposure chains: exactly one
- `VITE_DATA_CONNECT_CUTOVER=false`; Firestore remains the authority
- Carry remains fail-closed until live economics are evidenced

Never put an API key, API secret, password, DSN, Firebase token, OIDC token, or
private stream listen key in a prompt, command argument, log, evidence file, or
this repository. Secret Manager values are injected only into the Worker.

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

## Gate 2 — Control Plane authentication

Deploy the Control Plane with an immutable image digest and no Binance or SQL
secret injection. It may have public transport for the SPA, but protected
routes must require server-verified Firebase claims. Run:

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
approval. A kill switch always uses the direct Control Plane route and does not
wait for AGY. Rollback is: kill switch, reconcile, pause/disarm, route to a
known disarmed digest, set approval false, and read back the final state.

## Stop conditions

Stop and leave the Worker `DISARMED` for missing identity, secret version
mismatch, `canTrade=false`, stale stream or market data, SQL failure, any
reconciliation drift, leverage/cap breach, image mismatch, failed monitoring
read-back, approval expiry/replay, or any non-zero order attempt during a
read-only step. No fallback to simulated state, auto-transfer, auto-leverage,
position-mode mutation, or arbitrary Binance endpoint is permitted.
