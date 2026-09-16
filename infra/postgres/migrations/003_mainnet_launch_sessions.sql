-- Durable first-launch state machine.  The Worker creates one session only
-- after the release approval has been consumed and the LIVE revision is still
-- DISARMED.  All writes are idempotent or conditional so a restart cannot
-- reset the one-order limit.
CREATE TABLE IF NOT EXISTS mainnet_launch_sessions (
    launch_id VARCHAR(128) PRIMARY KEY,
    approval_id VARCHAR(128) NOT NULL UNIQUE,
    image_digest VARCHAR(256) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    policy VARCHAR(32) NOT NULL,
    max_risk_increasing_orders INTEGER NOT NULL DEFAULT 1,
    reserved_orders INTEGER NOT NULL DEFAULT 0,
    submitted_orders INTEGER NOT NULL DEFAULT 0,
    state VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT mainnet_launch_policy_check
        CHECK (policy = 'STAGED_FIRST_ORDER'),
    CONSTRAINT mainnet_launch_limit_check
        CHECK (max_risk_increasing_orders = 1),
    CONSTRAINT mainnet_launch_reserved_check
        CHECK (reserved_orders >= 0 AND reserved_orders <= max_risk_increasing_orders),
    CONSTRAINT mainnet_launch_submitted_check
        CHECK (submitted_orders >= 0 AND submitted_orders <= max_risk_increasing_orders),
    CONSTRAINT mainnet_launch_state_check
        CHECK (state IN ('ACTIVE', 'PAUSED_NEW_RISK', 'RECONCILIATION_REQUIRED', 'CLOSED'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mainnet_launch_one_active
    ON mainnet_launch_sessions(symbol)
    WHERE state IN ('ACTIVE', 'PAUSED_NEW_RISK', 'RECONCILIATION_REQUIRED');

CREATE INDEX IF NOT EXISTS idx_mainnet_launch_updated
    ON mainnet_launch_sessions(updated_at);
