-- Convert the durable staged launch session into a restart-safe continuation
-- state machine.  This migration is intentionally additive and preserves all
-- existing counters and first-order evidence.
ALTER TABLE mainnet_launch_sessions
    ALTER COLUMN max_risk_increasing_orders DROP NOT NULL;

ALTER TABLE mainnet_launch_sessions
    ADD COLUMN IF NOT EXISTS continuation_approval_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS first_order_verified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS autonomous_approved_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_restart_at TIMESTAMPTZ;

ALTER TABLE mainnet_launch_sessions
    DROP CONSTRAINT IF EXISTS mainnet_launch_policy_check,
    DROP CONSTRAINT IF EXISTS mainnet_launch_limit_check,
    DROP CONSTRAINT IF EXISTS mainnet_launch_reserved_check,
    DROP CONSTRAINT IF EXISTS mainnet_launch_submitted_check,
    DROP CONSTRAINT IF EXISTS mainnet_launch_state_check;

ALTER TABLE mainnet_launch_sessions
    ADD CONSTRAINT mainnet_launch_policy_check
        CHECK (policy IN ('STAGED_FIRST_ORDER', 'AUTONOMOUS_AFTER_REVIEW')),
    ADD CONSTRAINT mainnet_launch_limit_check
        CHECK (
            (policy = 'STAGED_FIRST_ORDER' AND max_risk_increasing_orders = 1)
            OR (policy = 'AUTONOMOUS_AFTER_REVIEW' AND max_risk_increasing_orders IS NULL)
        ),
    ADD CONSTRAINT mainnet_launch_reserved_check
        CHECK (reserved_orders >= 0 AND (max_risk_increasing_orders IS NULL OR reserved_orders <= max_risk_increasing_orders)),
    ADD CONSTRAINT mainnet_launch_submitted_check
        CHECK (submitted_orders >= 0 AND (max_risk_increasing_orders IS NULL OR submitted_orders <= max_risk_increasing_orders)),
    ADD CONSTRAINT mainnet_launch_state_check
        CHECK (state IN ('ACTIVE', 'PAUSED_NEW_RISK', 'RECONCILIATION_REQUIRED', 'AUTONOMOUS_ACTIVE', 'REAUTH_REQUIRED', 'CLOSED'));

DROP INDEX IF EXISTS idx_mainnet_launch_one_active;
CREATE UNIQUE INDEX IF NOT EXISTS idx_mainnet_launch_one_active
    ON mainnet_launch_sessions(symbol)
    WHERE state IN ('ACTIVE', 'PAUSED_NEW_RISK', 'RECONCILIATION_REQUIRED', 'AUTONOMOUS_ACTIVE', 'REAUTH_REQUIRED');

CREATE UNIQUE INDEX IF NOT EXISTS idx_mainnet_launch_continuation_approval
    ON mainnet_launch_sessions(continuation_approval_id)
    WHERE continuation_approval_id IS NOT NULL;
