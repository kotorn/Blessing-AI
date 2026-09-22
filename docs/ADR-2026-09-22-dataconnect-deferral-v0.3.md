# ADR: Defer Data Connect cutover to v0.3

- **Status:** Accepted for v0.2
- **Date:** 2026-09-22
- **Decision owners:** Blessing AI maintainers and release operator

## Decision

Keep `VITE_DATA_CONNECT_CUTOVER=false` and retain Firestore as the UI's
authoritative persistence path for v0.2. The Python Worker and its required
Cloud SQL persistence remain the execution and operational-state authority.
Data Connect cutover and any Firestore-to-SQL backfill are deferred to v0.3.

## Rationale

The generated Data Connect client and migration verification query exist, but
there is no completed two-user authorization read-back, timestamp/ownership
comparison, or approved staging cutover evidence in this release. Enabling the
flag would change the authoritative writer without that evidence.

## v0.3 entry criteria

1. Generate and review the SDK from the checked-in schema.
2. Produce a dry-run backfill report with owner, source document ID, timestamp,
   destination ID, row counts, duplicate checks, and orphan checks.
3. Complete the two-user authorization test, including role-claim protection.
4. Verify destination rows in a non-production preview before enabling one
   writer. If any check fails, set `VITE_DATA_CONNECT_ROLLBACK=true`.

No dual-write bridge, implicit migration, credential, or production cutover is
introduced by this decision.
