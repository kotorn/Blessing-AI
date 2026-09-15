-- Read-only SQL Connect cutover verification.
-- Run this against the target Cloud SQL database after the generated schema
-- migration and before setting VITE_DATA_CONNECT_CUTOVER=true. These table and
-- column names follow the schema in dataconnect/schema/schema.gql.

-- 1. Row counts are captured for reconciliation with the legacy store.
SELECT 'user_profile' AS table_name, COUNT(*) AS row_count FROM user_profile
UNION ALL
SELECT 'basket', COUNT(*) FROM basket
UNION ALL
SELECT 'risk_settings', COUNT(*) FROM risk_settings;

-- 2. Ownership must be present and must not be duplicated for a user's risk row.
SELECT COUNT(*) AS baskets_without_owner
FROM basket
WHERE owner_uid IS NULL OR btrim(owner_uid) = '';

SELECT owner_uid, COUNT(*) AS basket_count
FROM basket
GROUP BY owner_uid
HAVING COUNT(*) > 0;

SELECT owner_uid, COUNT(*) AS risk_rows
FROM risk_settings
GROUP BY owner_uid
HAVING COUNT(*) > 1;

-- 3. Timestamps must exist and cannot be materially in the future.
SELECT 'basket' AS table_name, COUNT(*) AS invalid_timestamps
FROM basket
WHERE created_at IS NULL OR updated_at IS NULL
   OR created_at > CURRENT_TIMESTAMP + INTERVAL '5 minutes'
   OR updated_at > CURRENT_TIMESTAMP + INTERVAL '5 minutes'
UNION ALL
SELECT 'risk_settings', COUNT(*)
FROM risk_settings
WHERE updated_at IS NULL
   OR updated_at > CURRENT_TIMESTAMP + INTERVAL '5 minutes';

-- 4. Duplicate logical ownership keys must be zero before cutover.
SELECT owner_uid, id, COUNT(*) AS duplicate_rows
FROM basket
GROUP BY owner_uid, id
HAVING COUNT(*) > 1;

-- 5. Profiles and settings/baskets must not be orphaned.
SELECT COUNT(*) AS orphaned_baskets
FROM basket b
LEFT JOIN user_profile p ON p.uid = b.owner_uid
WHERE p.uid IS NULL;

SELECT COUNT(*) AS orphaned_risk_settings
FROM risk_settings r
LEFT JOIN user_profile p ON p.uid = r.owner_uid
WHERE p.uid IS NULL;
