-- Add the account/environment-scoped distributed execution lease used by
-- Cloud Run workers.  This migration is idempotent and contains no exchange
-- or cloud-resource mutation.
CREATE TABLE IF NOT EXISTS execution_leases (
    scope_key VARCHAR(256) PRIMARY KEY,
    owner_id VARCHAR(128) NOT NULL,
    fencing_token BIGINT NOT NULL,
    lease_until TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_execution_leases_expiry
    ON execution_leases(lease_until);
