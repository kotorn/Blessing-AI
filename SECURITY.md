# Security Policy

## Supported scope

Blessing AI is a live-trading system connected to Binance and Google Cloud.
Report anything that could lead to:

- unauthorized order submission, arming, or kill-switch release;
- credential, secret, token, or DSN exposure (repository, logs, evidence
  files, Cloud Run services, Firestore, BigQuery);
- bypass of the Control Plane RBAC (`viewer < operator < trading_admin`),
  the `/internal/release` service boundary, or the Worker OIDC boundary;
- a release-gate bypass (approval replay, evidence forgery, stale or
  fabricated gate output accepted);
- reconciliation drift between the exchange and the durable ledger.

## How to report

Do **not** open a public GitHub issue for a vulnerability.

Email the maintainer directly (see the GitHub profile of `kotorn`) or use
GitHub's private security advisory for this repository. Include a minimal
reproduction and, if possible, evidence hashes/IDs affected.

You will receive an acknowledgement within 7 days. Please keep details
private until a fix is deployed and the Worker revision is rotated.

## Handling rules for reporters

- Never submit live API keys, secrets, or tokens in a report.
- Do not execute orders or mutate Testnet/Mainnet state while
  demonstrating a finding.
- The staging/sandbox (Testnet) environment is the intended proving ground.

## Operational notes

- Secrets are stored in Google Secret Manager and injected only into the
  Worker; the Control Plane is deployed without Binance/SQL secrets.
- Every release requires fresh, fail-closed gate evidence (see
  `docs/MAINNET-RELEASE-RUNBOOK.md`).
- Dependabot monitors pip, npm, and GitHub Actions dependencies weekly.
