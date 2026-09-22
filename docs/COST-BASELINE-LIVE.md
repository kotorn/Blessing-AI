# Blessing AI — measured live infrastructure baseline

- **Observed:** 2026-09-22 (Asia/Bangkok)
- **Method:** read-only `gcloud` metadata and resource-list read-back
- **Project:** `gen-lang-client-0730128480`
- **Region:** `asia-southeast1`
- **Status:** measured metadata only; this is not a billing invoice or a cost authorization

The same read-only session returned HTTP 200 for the Control Plane root and
`/api/health`. The deployed health body still identified `Blessing AI v0.1`,
so this baseline is also a deployment-drift record: repository v0.2 changes
must not be treated as live until an approved rollout and destination read-back
confirm them.

## Resource read-back

| Resource | Observed state |
|---|---|
| Control Plane | Cloud Run `blessing-control-plane`, ready revision `blessing-control-plane-00027-h5d`, 100% traffic, concurrency 1, 1 vCPU, 1 GiB; service-level min/max scale is 1/1, revision-level max scale is 20, and CPU throttling is currently false |
| Worker | Cloud Run `blessing-trading-worker`, ready revision `blessing-trading-worker-00038-nk7`, min/max scale 1/1, concurrency 1, 1 vCPU, 1 GiB, CPU throttling false, Cloud SQL attached |
| Cloud SQL | `blessing-sql-primary`, PostgreSQL 17, `db-f1-micro`, 10 GB, backups enabled, RUNNABLE |
| Artifact Registry | `blessing-repo`, Docker Standard; 29 tagged image entries observed: 7 Control Plane and 22 Worker entries |
| Logging/Monitoring | 18 log-based metrics and 14 alert policies observed, including readiness, persistence, reconciliation, private-stream, kill-switch and staged-launch alerts |
| Secret Manager | 3 secret names observed; values were not read: two Binance Mainnet names and one Cloud SQL password name |
| Billing | Billing enabled; account metadata read-only. No invoice total or credit balance was available in this read-back |

## Repository-to-live drift

| Check | Repository expectation | Live observation | Status |
|---|---|---|---|
| Release identity | `Blessing AI v0.2` | `/api/health` reports `Blessing AI v0.1` | OPEN — approved rollout/read-back required |
| Control Plane profile | Service-level min/max 1/1 plus revision request-based CPU throttling | service min/max `1/1`; revision max `20`; CPU throttling `false` | OPEN — CPU profile and v0.2 image still require approved rollout/read-back |
| Worker continuity | min/max 1/1, continuous CPU | min/max 1/1, CPU throttling `false` | MATCHED |

## Budget observations

- Blessing AI monthly alert budget: THB 10, scoped to project number
  `201945750223`, thresholds 50/75/90/100%.
- Existing THB 5 budget alert: unscoped in the returned metadata and requires
  operator review; it was not changed.
- No budget, billing account, quota, or spending-limit mutation was performed.

## Interpretation

Repository projections remain in `docs/COST_MODEL.md` and must not be
presented as actual spend. Artifact cleanup is intentionally not applied by
this PR. A future operator action must first run the image audit and protected
digest verification, then review a dry-run policy result.
