-- Durable, bounded Local LIVE research campaigns and fenced history scans.
-- This migration is additive to existing launch/order evidence.

ALTER TABLE mainnet_launch_sessions
    ADD COLUMN IF NOT EXISTS pilot_campaign_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS pilot_strategy_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS pilot_risk_policy_hash VARCHAR(64),
    ADD COLUMN IF NOT EXISTS pilot_max_position_notional_usdc NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS pilot_per_position_risk_usdc NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS pilot_max_drawdown_usdc NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS pilot_peak_pnl_usdc NUMERIC(20, 8) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS pilot_drawdown_triggered BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS pilot_quick_target_net_usdc NUMERIC(20, 8),
    ADD COLUMN IF NOT EXISTS pilot_quick_max_hold_seconds INTEGER,
    ADD COLUMN IF NOT EXISTS pilot_max_leverage NUMERIC(8, 4);

ALTER TABLE mainnet_launch_sessions
    DROP CONSTRAINT IF EXISTS mainnet_launch_policy_check,
    DROP CONSTRAINT IF EXISTS mainnet_launch_limit_check;

ALTER TABLE mainnet_launch_sessions
    ADD CONSTRAINT mainnet_launch_policy_check
        CHECK (policy IN ('STAGED_FIRST_ORDER', 'AUTONOMOUS_AFTER_REVIEW', 'LIVE_RESEARCH_PILOT')),
    ADD CONSTRAINT mainnet_launch_limit_check
        CHECK (
            (policy = 'STAGED_FIRST_ORDER' AND max_risk_increasing_orders = 1)
            OR (policy = 'AUTONOMOUS_AFTER_REVIEW' AND max_risk_increasing_orders IS NULL)
            OR (policy = 'LIVE_RESEARCH_PILOT' AND max_risk_increasing_orders IS NULL)
        ),
    ADD CONSTRAINT mainnet_launch_pilot_policy_check
        CHECK (
            (policy <> 'LIVE_RESEARCH_PILOT'
             AND pilot_campaign_expires_at IS NULL
             AND pilot_strategy_hash IS NULL
             AND pilot_risk_policy_hash IS NULL
             AND pilot_max_position_notional_usdc IS NULL
             AND pilot_per_position_risk_usdc IS NULL
             AND pilot_max_drawdown_usdc IS NULL
             AND pilot_quick_target_net_usdc IS NULL
             AND pilot_quick_max_hold_seconds IS NULL
             AND pilot_max_leverage IS NULL)
            OR
            (policy = 'LIVE_RESEARCH_PILOT'
             AND runtime_target = 'LOCAL'
             AND symbol = 'ETHUSDC'
             AND max_risk_increasing_orders IS NULL
             AND pilot_campaign_expires_at > created_at
             AND pilot_campaign_expires_at <= created_at + INTERVAL '7 days'
             AND pilot_strategy_hash IS NOT NULL
             AND pilot_strategy_hash ~ '^[0-9a-f]{64}$'
             AND pilot_risk_policy_hash IS NOT NULL
             AND pilot_risk_policy_hash ~ '^[0-9a-f]{64}$'
             AND pilot_max_position_notional_usdc IS NOT NULL
             AND pilot_max_position_notional_usdc = 50
             AND pilot_per_position_risk_usdc IS NOT NULL
             AND pilot_per_position_risk_usdc = 2
             AND pilot_max_drawdown_usdc IS NOT NULL
             AND pilot_max_drawdown_usdc = 5
             AND pilot_quick_target_net_usdc IS NOT NULL
             AND pilot_quick_target_net_usdc = 0.25
             AND pilot_quick_max_hold_seconds IS NOT NULL
             AND pilot_quick_max_hold_seconds = 86400
             AND pilot_max_leverage IS NOT NULL
             AND pilot_max_leverage = 10)
        );

ALTER TABLE binance_algo_protections
    ADD COLUMN IF NOT EXISTS management_mode VARCHAR(8);
ALTER TABLE binance_algo_protections
    ADD CONSTRAINT binance_algo_protection_management_mode_check
        CHECK (management_mode IS NULL OR management_mode IN ('QUICK', 'HOLD'));

-- Older in-flight scans have no fencing identity. Invalidate them so no
-- pre-migration worker can advance a checkpoint under the new semantics.
ALTER TABLE binance_history_checkpoints
    ADD COLUMN IF NOT EXISTS scan_id UUID;
UPDATE binance_history_checkpoints
SET coverage_status = 'UNKNOWN',
    failure_code = 'MIGRATION_FENCED_INFLIGHT_SCAN',
    scan_started_at = NULL,
    scan_from_at = NULL,
    scan_to_at = NULL,
    updated_at = CURRENT_TIMESTAMP
WHERE coverage_status = 'SCANNING';
ALTER TABLE binance_history_checkpoints
    ADD CONSTRAINT binance_history_checkpoint_scan_fence_check
        CHECK ((coverage_status = 'SCANNING' AND scan_id IS NOT NULL)
            OR (coverage_status <> 'SCANNING' AND scan_id IS NULL));

-- Append every observed version for normal order and trade history. The
-- first-seen identity row remains immutable; changing status/fill payloads are
-- represented here instead of being misclassified as duplicate corruption.
CREATE TABLE IF NOT EXISTS binance_history_item_observations (
    observation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    runtime_target VARCHAR(16) NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    history_kind VARCHAR(24) NOT NULL,
    item_id BIGINT NOT NULL,
    client_id VARCHAR(128),
    event_at TIMESTAMPTZ NOT NULL,
    payload_sha256 VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT binance_history_item_observation_scope_fk
        FOREIGN KEY (runtime_target, run_id, symbol, history_kind)
        REFERENCES binance_history_checkpoints(runtime_target, run_id, symbol, history_kind),
    CONSTRAINT binance_history_item_observation_identity_unique
        UNIQUE (runtime_target, run_id, symbol, history_kind, item_id, payload_sha256),
    CONSTRAINT binance_history_item_observation_shape_check
        CHECK (history_kind IN ('ALL_ORDERS', 'USER_TRADES')
            AND item_id > 0
            AND payload_sha256 ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(payload) = 'object')
);
CREATE INDEX IF NOT EXISTS idx_binance_history_item_observation_latest
    ON binance_history_item_observations(runtime_target, run_id, symbol,
                                         history_kind, item_id,
                                         observed_at DESC, observation_id DESC);
CREATE TRIGGER binance_history_item_observation_immutable
    BEFORE UPDATE OR DELETE ON binance_history_item_observations
    FOR EACH ROW EXECUTE FUNCTION reject_binance_history_immutable_mutation();
