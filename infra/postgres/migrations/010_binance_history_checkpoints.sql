-- Launch/run-scoped Binance history anchors, durable page checkpoints, and
-- explicit read-only baselines for terminal pre-existing Testnet Algo orders.

CREATE TABLE IF NOT EXISTS binance_history_anchors (
    runtime_target VARCHAR(16) NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    anchor_at TIMESTAMPTZ NOT NULL,
    anchor_source VARCHAR(32) NOT NULL,
    mainnet_launch_id VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (runtime_target, run_id, symbol),
    CONSTRAINT binance_history_anchor_identity_check
        CHECK (
            symbol ~ '^[A-Z0-9]{2,32}$'
            AND run_id ~ '^[A-Za-z0-9_.:-]{1,128}$'
            AND (
                (runtime_target IN ('LOCAL', 'CLOUD_RUN')
                 AND anchor_source = 'MAINNET_LAUNCH_SESSION'
                 AND mainnet_launch_id = run_id)
                OR
                (runtime_target = 'TESTNET'
                 AND anchor_source = 'TESTNET_READONLY_START'
                 AND mainnet_launch_id IS NULL)
            )
        ),
    CONSTRAINT binance_history_anchor_launch_fk
        FOREIGN KEY (mainnet_launch_id)
        REFERENCES mainnet_launch_sessions(launch_id)
);

CREATE TABLE IF NOT EXISTS binance_history_checkpoints (
    runtime_target VARCHAR(16) NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    history_kind VARCHAR(24) NOT NULL,
    cursor_id BIGINT NOT NULL DEFAULT 0,
    coverage_status VARCHAR(16) NOT NULL DEFAULT 'NOT_STARTED',
    covered_through TIMESTAMPTZ,
    scan_started_at TIMESTAMPTZ,
    scan_from_at TIMESTAMPTZ,
    scan_to_at TIMESTAMPTZ,
    last_page_at TIMESTAMPTZ,
    failure_code VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (runtime_target, run_id, symbol, history_kind),
    CONSTRAINT binance_history_checkpoint_anchor_fk
        FOREIGN KEY (runtime_target, run_id, symbol)
        REFERENCES binance_history_anchors(runtime_target, run_id, symbol),
    CONSTRAINT binance_history_checkpoint_shape_check
        CHECK (
            history_kind IN ('ALL_ORDERS', 'USER_TRADES', 'ALL_ALGO_ORDERS')
            AND cursor_id >= 0
            AND coverage_status IN ('NOT_STARTED', 'SCANNING', 'COVERED', 'GAP', 'UNKNOWN')
            AND (coverage_status <> 'COVERED' OR (covered_through IS NOT NULL AND last_page_at IS NOT NULL))
            AND (coverage_status <> 'SCANNING' OR (scan_started_at IS NOT NULL AND scan_from_at IS NOT NULL AND scan_to_at IS NOT NULL AND scan_to_at > scan_from_at))
            AND (coverage_status NOT IN ('GAP', 'UNKNOWN') OR failure_code IS NOT NULL)
        )
);

CREATE TABLE IF NOT EXISTS binance_history_items (
    runtime_target VARCHAR(16) NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    history_kind VARCHAR(24) NOT NULL,
    item_id BIGINT NOT NULL,
    client_id VARCHAR(128),
    event_at TIMESTAMPTZ NOT NULL,
    payload_sha256 VARCHAR(64) NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (runtime_target, run_id, symbol, history_kind, item_id),
    CONSTRAINT binance_history_item_checkpoint_fk
        FOREIGN KEY (runtime_target, run_id, symbol, history_kind)
        REFERENCES binance_history_checkpoints(runtime_target, run_id, symbol, history_kind),
    CONSTRAINT binance_history_item_shape_check
        CHECK (
            history_kind IN ('ALL_ORDERS', 'USER_TRADES', 'ALL_ALGO_ORDERS')
            AND item_id > 0
            AND payload_sha256 ~ '^[0-9a-f]{64}$'
        )
);

CREATE INDEX IF NOT EXISTS idx_binance_history_items_client
    ON binance_history_items(runtime_target, run_id, symbol, history_kind, client_id)
    WHERE client_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS binance_preexisting_algo_baselines (
    runtime_target VARCHAR(16) NOT NULL DEFAULT 'TESTNET',
    run_id VARCHAR(128) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    algo_id BIGINT NOT NULL,
    client_algo_id VARCHAR(128) NOT NULL,
    algo_created_at TIMESTAMPTZ NOT NULL,
    anchor_at TIMESTAMPTZ NOT NULL,
    terminal_status VARCHAR(24) NOT NULL,
    snapshot_observed_at TIMESTAMPTZ NOT NULL,
    position_snapshot JSONB NOT NULL,
    open_orders_snapshot JSONB NOT NULL,
    open_algo_orders_snapshot JSONB NOT NULL,
    proof_sha256 VARCHAR(64) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (runtime_target, run_id, symbol, algo_id),
    CONSTRAINT binance_preexisting_algo_anchor_fk
        FOREIGN KEY (runtime_target, run_id, symbol)
        REFERENCES binance_history_anchors(runtime_target, run_id, symbol),
    CONSTRAINT binance_preexisting_algo_proof_check
        CHECK (
            runtime_target = 'TESTNET'
            AND symbol ~ '^[A-Z0-9]{2,32}$'
            AND run_id ~ '^[A-Za-z0-9_.:-]{1,128}$'
            AND algo_id > 0
            AND client_algo_id ~ '^[A-Za-z0-9_-]{1,128}$'
            AND algo_created_at < anchor_at
            AND snapshot_observed_at >= anchor_at
            AND terminal_status IN ('CANCELED', 'CANCELLED', 'EXPIRED', 'REJECTED', 'FINISHED')
            AND jsonb_typeof(position_snapshot) = 'array'
            AND jsonb_typeof(open_orders_snapshot) = 'array'
            AND jsonb_typeof(open_algo_orders_snapshot) = 'array'
            AND proof_sha256 ~ '^[0-9a-f]{64}$'
        )
);

CREATE OR REPLACE FUNCTION reject_binance_history_immutable_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Binance history evidence is immutable';
END;
$$;

CREATE TRIGGER binance_history_anchor_immutable
    BEFORE UPDATE OR DELETE ON binance_history_anchors
    FOR EACH ROW EXECUTE FUNCTION reject_binance_history_immutable_mutation();

CREATE TRIGGER binance_history_item_immutable
    BEFORE UPDATE OR DELETE ON binance_history_items
    FOR EACH ROW EXECUTE FUNCTION reject_binance_history_immutable_mutation();

CREATE TRIGGER binance_preexisting_algo_baseline_immutable
    BEFORE UPDATE OR DELETE ON binance_preexisting_algo_baselines
    FOR EACH ROW EXECUTE FUNCTION reject_binance_history_immutable_mutation();
