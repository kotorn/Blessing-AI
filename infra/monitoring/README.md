# Blessing AI Cloud Monitoring Contract

This directory describes the monitoring resources that must be applied after
Cloud Run, Cloud SQL, and Secret Manager have been provisioned. The files are
declarative/runbook inputs only; CI and local tests must not create remote
resources.

## Required signals

The worker exposes `/health`, `/ready`, and `/readiness`. Cloud Logging should
also retain these stable event names from the worker log stream:

- `persistence_outbox_failure`
- `persistence_outbox_queue_full`
- `private_stream_disconnected`
- `reconciliation_drift`
- `daily_loss_cap_breached`
- `kill_switch_active`
- `readiness_degraded`
- `control_plane_auth_failure`
- `control_plane_oidc_failure`
- `agy_queue_depth_high`
- `agy_lease_expired`
- `agy_protocol_failure`
- `agy_timeout`
- `ambiguous_order`
- `staged_session_violation`

Each signal must include only non-secret metadata such as environment,
symbol, persistence mode, and a sanitized error class. API keys, passwords,
DSNs, listen keys, full account payloads, and user OAuth tokens must never be
logged.

## Alert policy requirements

Create log-based metrics and alert policies in the target project for:

| Signal | Required action |
| --- | --- |
| `readiness_degraded` | Page the operator when the required worker is not ready. |
| `persistence_outbox_failure` or queue full | Page immediately in LIVE; investigate in non-live modes. |
| `private_stream_disconnected` | Page immediately; block risk-increasing decisions until recovery. |
| `reconciliation_drift` | Page immediately; require operator reconciliation. |
| `daily_loss_cap_breached` | Page immediately and keep the kill switch active. |
| `kill_switch_active` | Page and retain the event for the incident trail. |
| `control_plane_auth_failure` or OIDC failure | Page on repeated failures; keep release actions blocked. |
| AGY queue pressure/protocol failure | Investigate queue state independently; never use AGY to authorize execution. |
| `ambiguous_order` or `staged_session_violation` | Page immediately; pause new risk and reconcile before any retry. |

The deployment owner must confirm the alert notification channel, metric
names, retention, and a test notification during the Cloud Run acceptance
runbook. An alert is not a spending cap and cannot authorize Mainnet.

The declarative budget in `budget.json` is a monthly alert amount in the
billing account's currency (the current target account reports THB). It is
scoped to the Blessing project by `apply.ps1` and is an alert only; Google
Cloud Billing Budgets does not stop spend or change trading authorization.

## Read-back acceptance

Acceptance requires a live read-back of the Cloud Run revision, readiness
status, log-based metrics, and alert policies. The UI must continue to show
`CONFIGURATION_DECLARED_NOT_VERIFIED` until that evidence is available.
