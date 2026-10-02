-- Preserve each observed lifecycle revision of an Algo order. The canonical
-- binance_history_items row remains first-seen identity; this append-only log
-- records later NEW/TRIGGERED/terminal payloads without mutating history.
CREATE TABLE IF NOT EXISTS binance_algo_history_observations (
    observation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    runtime_target VARCHAR(16) NOT NULL,
    run_id VARCHAR(128) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    history_kind VARCHAR(24) NOT NULL DEFAULT 'ALL_ALGO_ORDERS',
    item_id BIGINT NOT NULL,
    client_id VARCHAR(128),
    event_at TIMESTAMPTZ NOT NULL,
    payload_sha256 VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL,
    CONSTRAINT binance_algo_history_observation_scope_fk
        FOREIGN KEY (runtime_target, run_id, symbol, history_kind)
        REFERENCES binance_history_checkpoints(runtime_target, run_id, symbol, history_kind),
    CONSTRAINT binance_algo_history_observation_identity_unique
        UNIQUE (runtime_target, run_id, symbol, item_id, payload_sha256),
    CONSTRAINT binance_algo_history_observation_shape_check
        CHECK (
            item_id > 0
            AND history_kind = 'ALL_ALGO_ORDERS'
            AND payload_sha256 ~ '^[0-9a-f]{64}$'
            AND jsonb_typeof(payload) = 'object'
        )
);

CREATE INDEX IF NOT EXISTS idx_binance_algo_history_observation_latest
    ON binance_algo_history_observations(runtime_target, run_id, symbol, item_id,
                                         observed_at DESC, observation_id DESC);

DO $$ BEGIN
    CREATE TRIGGER binance_algo_history_observation_immutable
        BEFORE UPDATE OR DELETE ON binance_algo_history_observations
        FOR EACH ROW EXECUTE FUNCTION reject_binance_history_immutable_mutation();
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
