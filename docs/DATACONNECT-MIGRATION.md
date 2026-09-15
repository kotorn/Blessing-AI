# Firebase SQL Connect guarded cutover

SQL Connect is staged behind two client flags:

- `VITE_DATA_CONNECT_CUTOVER=false` keeps Firestore authoritative.
- `VITE_DATA_CONNECT_ROLLBACK=true` forces the Firestore path even if a cutover flag was left enabled.

The client wrapper in `src/dataconnect/client.ts` dynamically loads the generated SDK only when cutover is enabled. It invokes SQL Connect exclusively for basket and risk-setting writes; it never dual-writes Firestore and SQL Connect. The worker remains the sole execution authority, so these records do not arm or submit orders directly.

## Idempotent Firestore-to-SQL backfill

The repository contains the contract and verification query for the backfill,
but does not run a remote migration implicitly. The operator runbook must use
these rules:

1. Export the Firestore source with the Firebase UID, source document ID,
   source `updated_at`, and a deterministic destination ID. Run a dry-run
   report first; do not enable client cutover during export.
2. Insert or update only the destination row owned by that UID. The operation
   must be idempotent, must preserve the source timestamp, and must never
   delete a destination row during the backfill. A later source version wins;
   equal versions are a no-op.
3. Verify row counts, ownership, timestamps, duplicate destination IDs, and
   orphaned owners with `dataconnect/migration-verification.sql`. Save the
   report outside the repository and require a clean two-user authorization
   test before cutover.
4. Enable one authoritative writer only after read-back succeeds. The
   `VITE_DATA_CONNECT_ROLLBACK=true` flag is the rollback control and routes
   clients back to Firestore; it is not a dual-write mode.

The backfill is therefore a separately approved data operation. No service
account key, database password, or unreviewed bulk-write script belongs in the
repository.

Before enabling cutover:

1. Generate the SDK with `firebase dataconnect:sdk:generate` and inspect the generated operation names.
2. Run `dataconnect/migration-verification.sql` against the destination. Record row counts, zero orphaned owners, valid timestamps, and zero duplicate `(owner_uid, id)` keys.
3. Run the two-user authorization test: User A can list/update/delete only User A baskets and settings; User B receives no User A rows and cannot update or delete them. Verify that a client cannot set `UserProfile.role` or `UserProfile.isActive`.
4. Enable the flag in a non-production preview, read back the destination rows, and compare ownership and timestamps to the migration report.
5. Promote only after the destination read-back is complete. If any check fails, set `VITE_DATA_CONNECT_ROLLBACK=true`; do not run a dual-write bridge.

`SetUserProfileAccess` is `NO_ACCESS` and must be called only by a trusted Admin SDK/server path. The `USER` operations omit role, activation, owner, and server timestamps from client-controlled inputs. Basket updates and deletes include an owner filter in the mutation itself.
