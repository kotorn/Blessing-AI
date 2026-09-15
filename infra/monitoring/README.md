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

The deployment owner must confirm the alert notification channel, metric
names, retention, and a test notification during the Cloud Run acceptance
runbook. An alert is not a spending cap and cannot authorize Mainnet.

## Read-back acceptance

Acceptance requires a live read-back of the Cloud Run revision, readiness
status, log-based metrics, and alert policies. The UI must continue to show
`CONFIGURATION_DECLARED_NOT_VERIFIED` until that evidence is available.
