# Changelog

All notable changes to this project are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/). Release candidates are tagged in
git as `rc/<candidate-id>` so gate evidence IDs map to immutable refs.

## [Unreleased]

### Added
- Durable operator audit trail: arm, disarm, kill-switch, and autonomous
  continuation events persist to the `operator_audit_events` Firestore
  collection and are awaited before the route responds. A failed durable
  write fails open into a bounded in-memory buffer, marks the event
  `persisted=false`, and logs `monitor_event=audit_persistence_failed`.
  `AUDIT_BACKEND=memory` forces the in-process repository.
- CI quality gates: Python coverage floor at 65% with per-module reporting,
  error-level ruff gate (`E9,F63,F7,F82`), ESLint 9 flat config wired into
  `npm run lint` (unused-vars at warn until the existing UI backlog is
  ratcheted down), Dependabot for pip/npm/GitHub Actions.
- Repository hygiene: SECURITY.md (private disclosure policy), PR template
  with risk classes, bug/feature issue templates, CONTRIBUTING.md,
  pre-commit config, and a CI badge.

### Fixed
- `/api/google/*` routes now require operator RBAC; they previously served
  project, region, and user identifiers to anonymous callers.
- Local Docker Compose stack repaired: shared dev image
  (`infra/docker/Dockerfile.python`), the phantom `blessing-api` service
  (nonexistent `apps.control_api`) removed with the Worker control API
  exposed on port 8000, and the cockpit built from the real
  `Dockerfile.control-plane`.

### Changed
- `docs/MAINNET-RELEASE-RUNBOOK.md` collateral ceiling aligned with the
  calibrated `MAINNET_MAX_COLLATERAL` default of 250 USDC (runbook previously
  said 100 USDC).

## [0.1.0]

Initial tagged baseline.

- Adaptive basket grid trading system (anti-martingale, max 5 levels) with a
  7-state market regime engine, research-only AI grid safety score, and a
  fail-closed portfolio risk governor for Binance Spot & USDⓈ-M Futures.
- Python 3.13 trading worker: NATS/Postgres/Redis stack, durable outbox
  persistence, reconciliation, execution leases, deterministic client order
  IDs, daily-loss and exposure caps, restart-fenced launch sessions.
- Three-tier fail-closed release gate (repo-deterministic, live-cloud,
  combiner) with hashed evidence and dual one-time `trading_admin` approvals
  for staged first order and autonomous continuation.
- Native Binance Portfolio Margin (PAPI) support and portfolio-margin
  preflight checks.
- AGY event queue worker, Testnet contract/soak suites (manual dispatch only),
  and the React/Express control-plane cockpit with Firebase RBAC.
