-- Durable runtime binding and research accounting for the bounded Local pilot.
ALTER TABLE mainnet_launch_sessions
    ADD COLUMN IF NOT EXISTS pilot_campaign_id VARCHAR(128),
    ADD COLUMN IF NOT EXISTS pilot_git_sha VARCHAR(64),
    ADD COLUMN IF NOT EXISTS pilot_source_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS pilot_dependency_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS pilot_migration_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS pilot_strategy_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS pilot_secret_project_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS pilot_api_key_version VARCHAR(32),
    ADD COLUMN IF NOT EXISTS pilot_api_secret_version VARCHAR(32),
    ADD COLUMN IF NOT EXISTS pilot_management_mode VARCHAR(8),
    ADD COLUMN IF NOT EXISTS pilot_net_pnl_usdc NUMERIC(20, 8) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS pilot_status VARCHAR(16),
    ADD COLUMN IF NOT EXISTS pilot_last_account_snapshot_at TIMESTAMPTZ;

ALTER TABLE mainnet_launch_sessions
    DROP CONSTRAINT IF EXISTS mainnet_launch_pilot_binding_check;

ALTER TABLE mainnet_launch_sessions
    ADD CONSTRAINT mainnet_launch_pilot_binding_check
        CHECK (
            (policy <> 'LIVE_RESEARCH_PILOT'
             AND pilot_campaign_id IS NULL
             AND pilot_git_sha IS NULL
             AND pilot_source_hash IS NULL
             AND pilot_dependency_hash IS NULL
             AND pilot_migration_hash IS NULL
             AND pilot_strategy_hash IS NULL
             AND pilot_secret_project_id IS NULL
             AND pilot_api_key_version IS NULL
             AND pilot_api_secret_version IS NULL
             AND pilot_management_mode IS NULL
             AND pilot_campaign_expires_at IS NULL
             AND pilot_status IS NULL)
            OR
            (policy = 'LIVE_RESEARCH_PILOT'
             AND runtime_target = 'LOCAL'
             AND symbol = 'ETHUSDC'
             AND max_risk_increasing_orders IS NULL
             AND pilot_campaign_id ~ '^pilot-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$'
             AND pilot_git_sha ~ '^[0-9a-f]{40,64}$'
             AND pilot_source_hash ~ '^[0-9a-f]{64}$'
             AND pilot_dependency_hash ~ '^[0-9a-f]{64}$'
             AND pilot_migration_hash ~ '^[0-9a-f]{64}$'
             AND pilot_strategy_hash ~ '^[0-9a-f]{64}$'
             AND pilot_secret_project_id IS NOT NULL
             AND pilot_api_key_version ~ '^[1-9][0-9]*$'
             AND pilot_api_secret_version ~ '^[1-9][0-9]*$'
             AND pilot_management_mode = 'QUICK'
             AND pilot_status IN ('APPROVED', 'ACTIVE', 'CLOSE_ONLY', 'EXPIRED', 'REVOKED', 'COMPLETED')
             AND pilot_campaign_expires_at > created_at
             AND pilot_max_position_notional_usdc = 50
             AND pilot_per_position_risk_usdc = 2
             AND pilot_max_drawdown_usdc = 5
             AND pilot_max_leverage = 10)
        );

CREATE UNIQUE INDEX IF NOT EXISTS idx_mainnet_launch_pilot_campaign
    ON mainnet_launch_sessions(pilot_campaign_id)
    WHERE pilot_campaign_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS local_live_pilot_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    campaign_id VARCHAR(128) NOT NULL,
    launch_id VARCHAR(128) NOT NULL REFERENCES mainnet_launch_sessions(launch_id),
    event_key VARCHAR(192) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    source VARCHAR(24) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    net_pnl_delta_usdc NUMERIC(20, 8),
    payload_sha256 VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    CONSTRAINT local_live_pilot_event_unique UNIQUE (campaign_id, event_key),
    CONSTRAINT local_live_pilot_event_type_check
        CHECK (event_type IN ('ORDER_INTENT', 'ORDER', 'FILL', 'FEE', 'FUNDING', 'MARK', 'PROTECTION', 'CLOSE', 'RECONCILIATION', 'STATE')),
    CONSTRAINT local_live_pilot_event_source_check
        CHECK (source IN ('WORKER', 'BINANCE', 'SUPERVISOR')),
    CONSTRAINT local_live_pilot_event_payload_check
        CHECK (payload_sha256 ~ '^[0-9a-f]{64}$' AND jsonb_typeof(payload) = 'object')
);
CREATE INDEX IF NOT EXISTS idx_local_live_pilot_events_campaign_time
    ON local_live_pilot_events(campaign_id, observed_at, event_id);

CREATE OR REPLACE FUNCTION reject_local_live_pilot_event_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'local live pilot events are append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS local_live_pilot_event_immutable ON local_live_pilot_events;
CREATE TRIGGER local_live_pilot_event_immutable
    BEFORE UPDATE OR DELETE ON local_live_pilot_events
    FOR EACH ROW EXECUTE FUNCTION reject_local_live_pilot_event_mutation();
