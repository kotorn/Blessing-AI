# Blessing AI — measured live infrastructure baseline

> **Superseding metadata read-back — 2026-09-23:** Control Plane `blessing-control-plane-00031-sl6` runs immutable digest `sha256:f9942921b138a807d8fdd59544a5abb1294b839276b869c0748a485386c2c79d`, min/max 1/1 and request-based CPU. Worker `blessing-trading-worker-00039-xkd` runs immutable digest `sha256:eb081c73da3c3a88541118ee13b72bf276a7739ddd009ff04a6c924ba60ec3fb`, min/max 1/1 and continuous CPU. Both had 100% traffic; protected private Worker invocation was read back. Artifact Registry inventory was 37 entries after the release builds; no cleanup was applied. Actual invoice and credit totals remain unavailable; cost projections below are **not measured spend**. Older entries are historical snapshots.

## Post-rollout update — 2026-09-22

The approved disarmed Control Plane rollout was completed and read back from
Cloud Run. This update supersedes the older pre-rollout observations below;
the historical drift record is retained for auditability.

| Resource | Post-rollout observation |
|---|---|
| Control Plane | Cloud Run `blessing-control-plane-00029-c4m`, 100% traffic to the latest Ready revision, exact image `control-plane@sha256:24a93d0e0e6041f3211c71d402eb50f3be2f948c9a06f9f22b1fc81274474209` |
| Control Plane profile | `MAINNET_OPERATOR_UI`; service-level min/max scale `1/1`; revision request-based CPU throttling `true`; revision-level max annotation remains `20` and is a different Cloud Run scope |
| Health | `/` and `/api/health` returned HTTP 200; health identified `Blessing AI v0.2` with `status: ok` and `mode: deterministic_engine` |
| Worker | Unchanged at `blessing-trading-worker-00038-nk7`, exact image `trading-worker@sha256:fe19d33714e8b869ae13a7593fd1c6354eb89e4051b1b8a43252e48064fc474e`; read-only disarmed verification passed with zero order submissions |
| Rollback references | Previous v0.2 Control Plane `blessing-control-plane-00028-jvw`, digest `sha256:9fb1a350647cd32e759cee737325d9ef4080014f3eb043a73b7e23bca31ee52a`; prior v0.1 revision `blessing-control-plane-00027-h5d` retained |
| Artifact Registry | Read-only audit observed 32 image entries; protected digest verification passed for the current Control Plane, Worker, and previous Control Plane digests; no cleanup was applied |

The rollout did not read or rotate secrets, change billing, mutate IAM or the
database, or arm Mainnet. Billing invoice/credit totals remain unavailable.

## Pre-rollout snapshot (historical)

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
