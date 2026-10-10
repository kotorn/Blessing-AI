-- ============================================================================
-- Blessing AI v0.2 Production Database Schema (Google Cloud SQL PostgreSQL 17)
-- Designed for High-Throughput Event-Driven Quant Execution & Basket Auditing
-- Tri-Tier HOT DATA: Operational State, Active Risk, and Execution Attribution
-- Zero Raw Ticks Rule: High-frequency ticks stream to GCS Parquet & BigQuery.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Users (Application & Cockpit Access via Firebase Auth)
CREATE TABLE IF NOT EXISTS users (
    user_id VARCHAR(64) PRIMARY KEY,        -- Corresponds to Firebase UID
    email VARCHAR(128) NOT NULL UNIQUE,
    role VARCHAR(24) DEFAULT 'TRADER',      -- ADMIN, TRADER, VIEWER
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 2. Exchange Connections Metadata (Credentials kept in Google Secret Manager)
CREATE TABLE IF NOT EXISTS exchange_connections (
    connection_id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) REFERENCES users(user_id),
    venue VARCHAR(32) NOT NULL DEFAULT 'binance_global',
    account_label VARCHAR(64) NOT NULL,
    account_type VARCHAR(16) NOT NULL,      -- SPOT, USDM_FUTURES
    is_testnet BOOLEAN DEFAULT FALSE,
    secret_manager_ref VARCHAR(256) NOT NULL, -- GSM Secret resource URI
    masked_key VARCHAR(16) NOT NULL,
    permissions JSONB DEFAULT '{"trade": true, "withdraw": false}',
    status VARCHAR(24) DEFAULT 'ACTIVE',    -- ACTIVE, DISCONNECTED, ERROR
    last_connected_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_exchange_conn_user ON exchange_connections(user_id);

-- 3. Instruments Table (Symbol Metadata & Exchange Discovery Capabilities)
CREATE TABLE IF NOT EXISTS instruments (
    symbol VARCHAR(32) PRIMARY KEY,
    venue VARCHAR(32) NOT NULL DEFAULT 'binance_global',
    market_type VARCHAR(16) NOT NULL,        -- SPOT, USDM_PERP, COINM_PERP
    base_asset VARCHAR(16) NOT NULL,         -- BTC, ETH, SOL, BNB
    quote_asset VARCHAR(16) NOT NULL,        -- USDT, BUSD
    contract_size NUMERIC(20, 8) DEFAULT 1.0,
    price_precision INT NOT NULL,
    quantity_precision INT NOT NULL,
    tick_size NUMERIC(20, 8) NOT NULL,
    step_size NUMERIC(20, 8) NOT NULL,
    min_notional NUMERIC(20, 8) NOT NULL,
    max_leverage INT DEFAULT 20,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 4. Strategy Configurations
CREATE TABLE IF NOT EXISTS strategy_configs (
    config_id VARCHAR(64) PRIMARY KEY,
    strategy_id VARCHAR(32) NOT NULL,        -- GRID, TREND, SHOCK, CARRY
    version VARCHAR(16) NOT NULL,
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    parameters JSONB NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 5. Risk Configurations (Hard Survival Constraints)
CREATE TABLE IF NOT EXISTS risk_configs (
    config_id VARCHAR(64) PRIMARY KEY,
    max_portfolio_leverage NUMERIC(6, 2) DEFAULT 2.0,
    max_single_position_pct NUMERIC(6, 2) DEFAULT 20.0,
    max_drawdown_limit_pct NUMERIC(6, 2) DEFAULT 10.0,
    daily_var_99_limit_pct NUMERIC(6, 2) DEFAULT 3.0,
    emergency_kill_switch_pct NUMERIC(6, 2) DEFAULT 12.0,
    parameters JSONB,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 6. Baskets (First-Class Entity for Adaptive Basket Execution & Recovery)
CREATE TABLE IF NOT EXISTS baskets (
    basket_id VARCHAR(64) PRIMARY KEY,
    strategy_id VARCHAR(64) NOT NULL,
    venue VARCHAR(32) NOT NULL,
    instrument VARCHAR(32) REFERENCES instruments(symbol),
    direction VARCHAR(8) NOT NULL,           -- LONG, SHORT
    state VARCHAR(32) NOT NULL,              -- NEW, ACTIVE, GRID_EXPANDING, PROFITABLE, RECOVERY, NO_NEW_GRID, DELEVERAGING, CLOSING, CLOSED, EMERGENCY_EXIT
    grid_depth INT DEFAULT 0,
    max_grid_levels INT NOT NULL,
    total_size NUMERIC(28, 10) DEFAULT 0.0,
    average_entry NUMERIC(28, 10) DEFAULT 0.0,
    current_mark_price NUMERIC(28, 10) DEFAULT 0.0,
    target_tp_price NUMERIC(28, 10),
    stop_loss_price NUMERIC(28, 10),
    realized_pnl NUMERIC(28, 10) DEFAULT 0.0,
    unrealized_pnl NUMERIC(28, 10) DEFAULT 0.0,
    trading_fees NUMERIC(28, 10) DEFAULT 0.0,
    funding_accrued NUMERIC(28, 10) DEFAULT 0.0,
    slippage_cost NUMERIC(28, 10) DEFAULT 0.0,
    net_pnl NUMERIC(28, 10) DEFAULT 0.0,
    grid_safety_score_entry NUMERIC(6, 2),
    market_regime_entry VARCHAR(32),
    created_at TIMESTAMPTZ NOT NULL,
    last_updated TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_baskets_state ON baskets(state);
CREATE INDEX IF NOT EXISTS idx_baskets_instrument ON baskets(instrument);
CREATE INDEX IF NOT EXISTS idx_baskets_created_at ON baskets(created_at);

-- 7. Basket Entries (Individual Leg Tracking inside a Basket)
CREATE TABLE IF NOT EXISTS basket_entries (
    entry_id VARCHAR(64) PRIMARY KEY,
    basket_id VARCHAR(64) REFERENCES baskets(basket_id),
    level INT NOT NULL,
    direction VARCHAR(8) NOT NULL,
    price NUMERIC(28, 10) NOT NULL,
    quantity NUMERIC(28, 10) NOT NULL,
    notional NUMERIC(28, 10) NOT NULL,
    fee_paid NUMERIC(28, 10) DEFAULT 0.0,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL,
    closed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_basket_entries_basket ON basket_entries(basket_id);

-- 8. Grid Levels (Dynamic Volatility Levels Prepared for Execution)
CREATE TABLE IF NOT EXISTS grid_levels (
    level_id VARCHAR(64) PRIMARY KEY,
    basket_id VARCHAR(64) REFERENCES baskets(basket_id),
    level_index INT NOT NULL,
    target_price NUMERIC(28, 10) NOT NULL,
    target_quantity NUMERIC(28, 10) NOT NULL,
    spacing_atr_multiple NUMERIC(8, 4) NOT NULL,
    status VARCHAR(24) DEFAULT 'PENDING',    -- PENDING, PLACED, FILLED, SKIPPED, CANCELLED
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_grid_levels_basket ON grid_levels(basket_id);

-- 9. Strategy Intents (Continuous Signals Produced by Alpha Engines)
CREATE TABLE IF NOT EXISTS strategy_intents (
    intent_id VARCHAR(64) PRIMARY KEY,
    strategy_id VARCHAR(32) NOT NULL,        -- GRID, TREND, SHOCK, CARRY
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    desired_action VARCHAR(24) NOT NULL,     -- OPEN_LONG, OPEN_SHORT, CLOSE, PYRAMID, HEDGE, REDUCE
    target_delta NUMERIC(28, 10) NOT NULL,
    urgency VARCHAR(16) NOT NULL,            -- LOW, MEDIUM, HIGH, IMMEDIATE
    confidence NUMERIC(6, 4) NOT NULL,
    expected_edge_bps NUMERIC(8, 2) NOT NULL,
    horizon_seconds INT NOT NULL,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_strategy_intents_symbol_time ON strategy_intents(symbol, created_at);

-- 10. Opportunity Scores (Normalized Cross-Engine Opportunity Matrix)
CREATE TABLE IF NOT EXISTS opportunity_scores (
    score_id VARCHAR(64) PRIMARY KEY,
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    strategy_id VARCHAR(32) NOT NULL,
    raw_score NUMERIC(6, 4) NOT NULL,
    calibrated_score NUMERIC(6, 4) NOT NULL,
    regime_fitness NUMERIC(6, 4) NOT NULL,
    expected_sharpe NUMERIC(6, 2),
    win_probability NUMERIC(6, 4),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_opp_scores_symbol_time ON opportunity_scores(symbol, created_at);

-- 11. Allocation Decisions (Meta Allocator Continuous Risk Budgeting)
CREATE TABLE IF NOT EXISTS allocation_decisions (
    allocation_id VARCHAR(64) PRIMARY KEY,
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    strategy_id VARCHAR(32) NOT NULL,
    budget_allocated_usd NUMERIC(28, 10) NOT NULL,
    leverage_cap NUMERIC(6, 2) NOT NULL,
    opportunity_weight NUMERIC(6, 4) NOT NULL,
    correlation_penalty NUMERIC(6, 4) DEFAULT 0.0,
    tail_risk_penalty NUMERIC(6, 4) DEFAULT 0.0,
    created_at TIMESTAMPTZ NOT NULL
);

-- 12. Target Exposures (Net Target Calculated across All Strategies)
CREATE TABLE IF NOT EXISTS target_exposures (
    target_id VARCHAR(64) PRIMARY KEY,
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    net_target_quantity NUMERIC(28, 10) NOT NULL,
    gross_exposure_limit NUMERIC(28, 10) NOT NULL,
    current_physical_exposure NUMERIC(28, 10) NOT NULL,
    required_delta NUMERIC(28, 10) NOT NULL,
    risk_governor_approved BOOLEAN NOT NULL,
    reason VARCHAR(128),
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_target_exp_symbol_time ON target_exposures(symbol, created_at);

-- 13. Orders (Exchange Orders Placed by Execution Optimizer)
CREATE TABLE IF NOT EXISTS orders (
    client_order_id VARCHAR(64) PRIMARY KEY,
    exchange_order_id VARCHAR(64),
    basket_id VARCHAR(64) REFERENCES baskets(basket_id),
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    venue VARCHAR(32) NOT NULL DEFAULT 'binance_global',
    side VARCHAR(8) NOT NULL,                -- BUY, SELL
    order_type VARCHAR(16) NOT NULL,         -- LIMIT, MARKET, STOP_MARKET
    order_role VARCHAR(24) NOT NULL,         -- GRID_ENTRY, GRID_STEP, BASKET_TP, RECOVERY_EXIT, EMERGENCY_SL
    grid_level INT,
    price NUMERIC(28, 10),
    quantity NUMERIC(28, 10) NOT NULL,
    status VARCHAR(24) NOT NULL,             -- PENDING, SUBMITTED, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED
    time_in_force VARCHAR(8) DEFAULT 'GTC',
    position_side VARCHAR(8) NOT NULL DEFAULT 'BOTH', -- BOTH, LONG, SHORT
    filled_quantity NUMERIC(28, 10) DEFAULT 0.0,
    avg_fill_price NUMERIC(28, 10) DEFAULT 0.0,
    cumulative_fee NUMERIC(28, 10) DEFAULT 0.0,
    fee_asset VARCHAR(16),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_basket_id ON orders(basket_id);
CREATE INDEX IF NOT EXISTS idx_orders_exchange_id ON orders(exchange_order_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

-- 14. Fills / Executions
CREATE TABLE IF NOT EXISTS fills (
    fill_id VARCHAR(64) PRIMARY KEY,
    client_order_id VARCHAR(64) REFERENCES orders(client_order_id),
    exchange_order_id VARCHAR(64),
    exchange_trade_id VARCHAR(64) NOT NULL,
    basket_id VARCHAR(64) REFERENCES baskets(basket_id),
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    venue VARCHAR(32) NOT NULL DEFAULT 'binance_global',
    side VARCHAR(8) NOT NULL,
    position_side VARCHAR(8) NOT NULL DEFAULT 'BOTH', -- BOTH, LONG, SHORT
    price NUMERIC(28, 10) NOT NULL,
    quantity NUMERIC(28, 10) NOT NULL,
    fee NUMERIC(28, 10) NOT NULL,
    fee_asset VARCHAR(16) NOT NULL,
    is_maker BOOLEAN DEFAULT FALSE,
    executed_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fills_basket ON fills(basket_id);
CREATE INDEX IF NOT EXISTS idx_fills_executed_at ON fills(executed_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_venue_trade
    ON fills(venue, exchange_trade_id);

-- 15. Active Positions
CREATE TABLE IF NOT EXISTS positions (
    id SERIAL PRIMARY KEY,
    venue VARCHAR(32) NOT NULL DEFAULT 'binance_global',
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    position_side VARCHAR(8) NOT NULL DEFAULT 'BOTH', -- BOTH, LONG, SHORT
    direction VARCHAR(8) NOT NULL,           -- LONG, SHORT, FLAT
    quantity NUMERIC(28, 10) NOT NULL,
    entry_price NUMERIC(28, 10) NOT NULL,
    mark_price NUMERIC(28, 10) NOT NULL,
    liquidation_price NUMERIC(28, 10),
    unrealized_pnl NUMERIC(28, 10) NOT NULL,
    leverage NUMERIC(6, 2) NOT NULL,
    margin_type VARCHAR(16) DEFAULT 'CROSS',
    initial_margin NUMERIC(28, 10) NOT NULL,
    maintenance_margin NUMERIC(28, 10) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(venue, symbol, position_side)
);

-- 16. Funding Events (USDⓈ-M Futures Funding Cashflows)
CREATE TABLE IF NOT EXISTS funding_events (
    id SERIAL PRIMARY KEY,
    venue VARCHAR(32) NOT NULL DEFAULT 'binance_global',
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    basket_id VARCHAR(64) REFERENCES baskets(basket_id),
    funding_rate NUMERIC(16, 8) NOT NULL,
    payment NUMERIC(28, 10) NOT NULL,        -- Positive = received, Negative = paid
    position_size NUMERIC(28, 10) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_funding_symbol_time ON funding_events(symbol, timestamp);

-- 17. Risk Snapshots (Portfolio Telemetry & Health)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    total_equity NUMERIC(28, 10) NOT NULL,
    total_balance NUMERIC(28, 10) NOT NULL,
    free_margin NUMERIC(28, 10) NOT NULL,
    used_margin NUMERIC(28, 10) NOT NULL,
    margin_utilization_pct NUMERIC(6, 2) NOT NULL,
    effective_leverage NUMERIC(6, 2) NOT NULL,
    current_drawdown_pct NUMERIC(6, 2) NOT NULL,
    risk_state VARCHAR(24) NOT NULL,         -- NORMAL, CAUTION, NO_NEW_GRID, RECOVERY_ONLY, DELEVERAGE, EMERGENCY
    aggregate_long_exposure NUMERIC(28, 10) NOT NULL,
    aggregate_short_exposure NUMERIC(28, 10) NOT NULL,
    crypto_beta_exposure_pct NUMERIC(6, 2) NOT NULL,
    cross_correlation_btc_eth NUMERIC(6, 4),
    active_baskets_count INT NOT NULL,
    kill_switch_active BOOLEAN DEFAULT FALSE,
    reasons JSONB
);

CREATE INDEX IF NOT EXISTS idx_portfolio_time ON portfolio_snapshots(timestamp);

-- 18. Exposure Recovery Actions (Dynamic Hedging & Deleveraging Audit)
CREATE TABLE IF NOT EXISTS exposure_recovery_actions (
    action_id VARCHAR(64) PRIMARY KEY,
    basket_id VARCHAR(64) REFERENCES baskets(basket_id),
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    action_type VARCHAR(32) NOT NULL,        -- GRID_BRAKE, OPEN_COUNTER_HEDGE, TRIM_TOXIC_ENTRY, HARVEST_PROFIT, FULL_UNWIND
    hedge_ratio NUMERIC(6, 4),
    quantity_adjusted NUMERIC(28, 10) NOT NULL,
    gross_exposure_reduction NUMERIC(28, 10),
    pnl_realized_usd NUMERIC(28, 10),
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recovery_basket ON exposure_recovery_actions(basket_id);

-- 19. Execution Decisions (Slippage, Spread, & Maker Optimization Log)
CREATE TABLE IF NOT EXISTS execution_decisions (
    decision_id VARCHAR(64) PRIMARY KEY,
    client_order_id VARCHAR(64) REFERENCES orders(client_order_id),
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    execution_algo VARCHAR(32) NOT NULL,     -- PASSIVE_PEG, TWAP, IMMEDIATE_SWEEP
    spread_at_submission_bps NUMERIC(8, 2),
    effective_slippage_bps NUMERIC(8, 2),
    maker_rebate_captured NUMERIC(28, 10),
    latency_ms INT,
    created_at TIMESTAMPTZ NOT NULL
);

-- 20. Model Versions (Machine Learning Governance & Audit)
CREATE TABLE IF NOT EXISTS model_versions (
    model_id VARCHAR(64) PRIMARY KEY,
    model_family VARCHAR(32) NOT NULL,       -- REGIME_CLASSIFIER, GRID_SAFETY_SCORE
    framework VARCHAR(32) NOT NULL,          -- CATBOOST, XGBOOST, LIGHTGBM, HMM
    artifact_uri TEXT NOT NULL,              -- GCS URI: gs://blessing-ai-data/models/...
    train_start_date TIMESTAMPTZ NOT NULL,
    train_end_date TIMESTAMPTZ NOT NULL,
    val_metrics JSONB NOT NULL,
    is_active BOOLEAN DEFAULT FALSE,
    deployed_at TIMESTAMPTZ
);

-- 21. System Health (Watchdog & Heartbeat Telemetry)
CREATE TABLE IF NOT EXISTS system_health (
    component VARCHAR(64) PRIMARY KEY,
    status VARCHAR(24) NOT NULL,             -- HEALTHY, DEGRADED, DOWN
    last_heartbeat TIMESTAMPTZ NOT NULL,
    latency_ms INT,
    metadata JSONB
);

-- 22. Audit Events (Tamper-evident Event Log)
CREATE TABLE IF NOT EXISTS audit_events (
    event_id VARCHAR(64) PRIMARY KEY,
    event_type VARCHAR(64) NOT NULL,
    source VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL,           -- INFO, WARNING, CRITICAL, EMERGENCY
    details JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(created_at);

-- 23. Transactional Persistence Outbox
CREATE TABLE IF NOT EXISTS persistence_outbox (
    event_id VARCHAR(128) PRIMARY KEY,
    event_type VARCHAR(32) NOT NULL,
    idempotency_key VARCHAR(256) NOT NULL,
    aggregate_type VARCHAR(64) NOT NULL,
    aggregate_id VARCHAR(256) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'PENDING',
    attempt_count INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    claimed_at TIMESTAMPTZ,
    processed_at TIMESTAMPTZ,
    last_error TEXT,
    CONSTRAINT persistence_outbox_status_check
        CHECK (status IN ('PENDING', 'PROCESSING', 'PROCESSED'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_persistence_outbox_idempotency
    ON persistence_outbox(event_type, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_persistence_outbox_pending
    ON persistence_outbox(status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_persistence_outbox_claimed
    ON persistence_outbox(status, claimed_at);

-- 24. Distributed execution lease and fencing token
-- One account/environment scope can have only one unexpired owner.  The
-- monotonically increasing token lets a replacement instance fence an older
-- worker before its next risk-increasing submission.
CREATE TABLE IF NOT EXISTS execution_leases (
    scope_key VARCHAR(256) PRIMARY KEY,
    owner_id VARCHAR(128) NOT NULL,
    fencing_token BIGINT NOT NULL,
    lease_until TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_execution_leases_expiry
    ON execution_leases(lease_until);

-- 25. Durable Mainnet launch session.  This is deliberately separate from the
-- release candidate in Firestore so the Worker can enforce staged and
-- autonomous lifecycle state across restarts.
CREATE TABLE IF NOT EXISTS mainnet_launch_sessions (
    launch_id VARCHAR(128) PRIMARY KEY,
    approval_id VARCHAR(128) NOT NULL UNIQUE,
    image_digest VARCHAR(256),
    symbol VARCHAR(32) NOT NULL,
    policy VARCHAR(32) NOT NULL,
    max_risk_increasing_orders INTEGER DEFAULT 1,
    reserved_orders INTEGER NOT NULL DEFAULT 0,
    submitted_orders INTEGER NOT NULL DEFAULT 0,
    state VARCHAR(32) NOT NULL DEFAULT 'ACTIVE',
    continuation_approval_id VARCHAR(128),
    first_order_verified_at TIMESTAMPTZ,
    autonomous_approved_at TIMESTAMPTZ,
    last_restart_at TIMESTAMPTZ,
    pending_order_client_order_id VARCHAR(64),
    first_order_client_order_id VARCHAR(64),
    basket_id VARCHAR(128),
    runtime_target VARCHAR(32) NOT NULL DEFAULT 'CLOUD_RUN',
    runtime_fingerprint VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT mainnet_launch_policy_check
        CHECK (policy IN ('STAGED_FIRST_ORDER', 'AUTONOMOUS_AFTER_REVIEW')),
    CONSTRAINT mainnet_launch_limit_check
        CHECK (
            (policy = 'STAGED_FIRST_ORDER' AND max_risk_increasing_orders = 1)
            OR (policy = 'AUTONOMOUS_AFTER_REVIEW' AND max_risk_increasing_orders IS NULL)
        ),
    CONSTRAINT mainnet_launch_reserved_check
        CHECK (reserved_orders >= 0 AND (max_risk_increasing_orders IS NULL OR reserved_orders <= max_risk_increasing_orders)),
    CONSTRAINT mainnet_launch_submitted_check
        CHECK (submitted_orders >= 0 AND (max_risk_increasing_orders IS NULL OR submitted_orders <= max_risk_increasing_orders)),
    CONSTRAINT mainnet_launch_state_check
        CHECK (state IN ('ACTIVE', 'PAUSED_NEW_RISK', 'RECONCILIATION_REQUIRED', 'AUTONOMOUS_ACTIVE', 'REAUTH_REQUIRED', 'CLOSED')),
    CONSTRAINT mainnet_launch_runtime_identity_check
        CHECK (
            (
                runtime_target = 'LOCAL'
                AND image_digest IS NULL
                AND runtime_fingerprint IS NOT NULL
                AND runtime_fingerprint ~* '^[0-9a-f]{64}$'
            )
            OR
            (
                runtime_target = 'CLOUD_RUN'
                AND image_digest IS NOT NULL
                AND image_digest ~* '^.+@sha256:[0-9a-f]{64}$'
            )
        ),
    CONSTRAINT mainnet_launch_order_identity_check
        CHECK (
            (pending_order_client_order_id IS NULL OR
                pending_order_client_order_id ~ '^[A-Za-z0-9_-]{1,64}$')
            AND
            (first_order_client_order_id IS NULL OR
                first_order_client_order_id ~ '^[A-Za-z0-9_-]{1,64}$')
        )
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mainnet_launch_one_active
    ON mainnet_launch_sessions(symbol)
    WHERE state IN ('ACTIVE', 'PAUSED_NEW_RISK', 'RECONCILIATION_REQUIRED', 'AUTONOMOUS_ACTIVE', 'REAUTH_REQUIRED');
CREATE UNIQUE INDEX IF NOT EXISTS idx_mainnet_launch_continuation_approval
    ON mainnet_launch_sessions(continuation_approval_id)
    WHERE continuation_approval_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_mainnet_launch_basket_unique
    ON mainnet_launch_sessions(basket_id)
    WHERE basket_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mainnet_launch_updated
    ON mainnet_launch_sessions(updated_at);

-- 26. Durable ownership of Binance USDⓈ-M conditional protection orders.
-- Testnet and Mainnet are distinct venues and are also recorded explicitly so
-- the same symbol/client ID can never alias across environments.
CREATE TABLE IF NOT EXISTS binance_algo_protections (
    environment VARCHAR(8) NOT NULL,
    venue VARCHAR(32) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    entry_client_order_id VARCHAR(64) NOT NULL,
    entry_side VARCHAR(8) NOT NULL,
    position_side VARCHAR(8) NOT NULL,
    requested_quantity NUMERIC(28, 10) NOT NULL,
    filled_quantity NUMERIC(28, 10) NOT NULL DEFAULT 0,
    entry_average_price NUMERIC(28, 10),
    stop_trigger_price NUMERIC(28, 10) NOT NULL,
    take_profit_trigger_price NUMERIC(28, 10) NOT NULL,
    stop_algo_id VARCHAR(64),
    take_profit_algo_id VARCHAR(64),
    stop_client_algo_id VARCHAR(64) NOT NULL,
    take_profit_client_algo_id VARCHAR(64) NOT NULL,
    state VARCHAR(24) NOT NULL DEFAULT 'PENDING',
    state_reason VARCHAR(256),
    first_fill_at TIMESTAMPTZ,
    protection_verified_at TIMESTAMPTZ,
    last_reconciled_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (venue, symbol, entry_client_order_id),
    CONSTRAINT binance_algo_protection_environment_check
        CHECK (
            (venue = 'binance_testnet' AND environment = 'TESTNET')
            OR (venue = 'binance_mainnet' AND environment = 'MAINNET')
        ),
    CONSTRAINT binance_algo_protection_entry_identity_check
        CHECK (
            symbol ~ '^[A-Z0-9]{2,32}$'
            AND entry_client_order_id ~ '^[A-Za-z0-9_-]{1,64}$'
            AND entry_side IN ('BUY', 'SELL')
            AND position_side IN ('BOTH', 'LONG', 'SHORT')
            AND (
                (entry_side = 'BUY' AND position_side IN ('BOTH', 'LONG'))
                OR (entry_side = 'SELL' AND position_side IN ('BOTH', 'SHORT'))
            )
        ),
    CONSTRAINT binance_algo_protection_quantity_check
        CHECK (
            requested_quantity > 0
            AND filled_quantity >= 0
            AND filled_quantity <= requested_quantity
            AND (
                (filled_quantity = 0 AND entry_average_price IS NULL AND first_fill_at IS NULL)
                OR (
                    filled_quantity > 0
                    AND entry_average_price IS NOT NULL
                    AND entry_average_price > 0
                    AND first_fill_at IS NOT NULL
                )
            )
        ),
    CONSTRAINT binance_algo_protection_trigger_check
        CHECK (
            stop_trigger_price > 0
            AND take_profit_trigger_price > 0
            AND stop_trigger_price <> take_profit_trigger_price
            AND (
                entry_average_price IS NULL
                OR (
                    entry_side = 'BUY'
                    AND stop_trigger_price < entry_average_price
                    AND entry_average_price < take_profit_trigger_price
                )
                OR (
                    entry_side = 'SELL'
                    AND take_profit_trigger_price < entry_average_price
                    AND entry_average_price < stop_trigger_price
                )
            )
        ),
    CONSTRAINT binance_algo_protection_algo_identity_check
        CHECK (
            stop_client_algo_id ~ '^[A-Za-z0-9_-]{1,64}$'
            AND take_profit_client_algo_id ~ '^[A-Za-z0-9_-]{1,64}$'
            AND stop_client_algo_id <> take_profit_client_algo_id
            AND (stop_algo_id IS NULL OR stop_algo_id ~ '^[0-9]{1,64}$')
            AND (take_profit_algo_id IS NULL OR take_profit_algo_id ~ '^[0-9]{1,64}$')
            AND (stop_algo_id IS NULL OR take_profit_algo_id IS NULL OR stop_algo_id <> take_profit_algo_id)
        ),
    CONSTRAINT binance_algo_protection_state_check
        CHECK (
            state IN ('PENDING', 'PROTECTED', 'CLOSE_PENDING', 'CLOSED', 'DEGRADED', 'UNKNOWN')
            AND (
                state <> 'PROTECTED'
                OR (
                    filled_quantity > 0
                    AND stop_algo_id IS NOT NULL
                    AND take_profit_algo_id IS NOT NULL
                    AND protection_verified_at IS NOT NULL
                )
            )
            AND (state <> 'CLOSED' OR closed_at IS NOT NULL)
        )
);
CREATE INDEX IF NOT EXISTS idx_binance_algo_protections_nonterminal
    ON binance_algo_protections(venue, symbol, created_at)
    WHERE state <> 'CLOSED';
ALTER TABLE binance_algo_protections
    ADD COLUMN IF NOT EXISTS basket_id VARCHAR(128);
ALTER TABLE binance_algo_protections
    ADD COLUMN IF NOT EXISTS mainnet_launch_id VARCHAR(128);
DO $$ BEGIN
    ALTER TABLE mainnet_launch_sessions
        ADD CONSTRAINT mainnet_launch_basket_identity_unique
        UNIQUE (launch_id, basket_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE binance_algo_protections
        ADD CONSTRAINT binance_algo_protection_launch_basket_fk
        FOREIGN KEY (mainnet_launch_id, basket_id)
        REFERENCES mainnet_launch_sessions(launch_id, basket_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    ALTER TABLE binance_algo_protections
        ADD CONSTRAINT binance_algo_protection_launch_identity_check
        CHECK (mainnet_launch_id IS NULL OR (environment = 'MAINNET' AND basket_id IS NOT NULL));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
ALTER TABLE binance_algo_protections
    ADD COLUMN IF NOT EXISTS closure_evidence JSONB;
UPDATE binance_algo_protections
SET closure_evidence = '{"kind":"LEGACY_UNVERIFIED"}'::jsonb
WHERE environment = 'MAINNET'
  AND state = 'CLOSED'
  AND closure_evidence IS NULL;
DO $$ BEGIN
    ALTER TABLE binance_algo_protections
        ADD CONSTRAINT binance_algo_protection_closure_evidence_check
        CHECK (
            (
                environment = 'MAINNET'
                AND state = 'CLOSED'
                AND jsonb_typeof(closure_evidence) = 'object'
                AND closure_evidence->>'kind' IN (
                    'LEGACY_UNVERIFIED',
                    'BINANCE_ALGO_CLOSE_VERIFIED',
                    'UNFILLED_ENTRY_TERMINAL'
                )
            )
            OR (
                (state <> 'CLOSED' OR environment = 'TESTNET')
                AND closure_evidence IS NULL
            )
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- 27. Durable Binance launch-history coverage and proof-backed Testnet baselines.
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

DO $$ BEGIN
    CREATE TRIGGER binance_algo_history_observation_immutable
        BEFORE UPDATE OR DELETE ON binance_algo_history_observations
        FOR EACH ROW EXECUTE FUNCTION reject_binance_history_immutable_mutation();
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- A fresh volume starts from this complete base schema. Record the migrations it
-- already contains so apply_local_postgres_migrations.py applies only 013+ instead of
-- re-running 001-012 (which fail on objects that already exist, e.g. migration 010's
-- trigger). tests/python/test_init_schema_ledger.py pins these checksums.
CREATE TABLE IF NOT EXISTS public.local_schema_migrations (
    version VARCHAR(128) PRIMARY KEY,
    checksum CHAR(64) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO public.local_schema_migrations (version, checksum) VALUES
    ('001_persistence_outbox_and_hedge_identity.sql', 'c7259ab9fdd4f9f186d2ecab9027a7b7971d85080dce19dab5d5eb81b6ea462f'),
    ('002_execution_leases.sql', 'd99a267a1a5a158366756fb45c62f399a494e56866144ab81eb2ed8b31de3185'),
    ('003_mainnet_launch_sessions.sql', '8e644963e3c5009cfe13d6c809a04076f2e163b1d33e503016a521a569eb5a89'),
    ('004_environment_scoped_fill_identity.sql', '3126168d425e169b059f0a6f1aa064f03598458540591c20f305ea781b01a2b2'),
    ('005_mainnet_autonomous_continuation.sql', '689ac5d9d3a6554251876db02eefa5854e83dfc6a55602dbd60419ccffc3c419'),
    ('006_backfill_environment_scoped_venue.sql', '7c319518050cd5acf899c7879dbb30540b69410ece3a667a910aa998c12efbe3'),
    ('007_mainnet_launch_runtime_identity.sql', '4df64f954486eabf118fb36044dfe875aebf7f467b503eeadf99ffd7d6e9cc13'),
    ('008_mainnet_launch_order_identity.sql', '6a73a7bd9620c3c6a297fcc1173701ed1b8b8ee5785b7f6a04918a2829a84d50'),
    ('009_binance_algo_protection_ownership.sql', 'ae93d7c03c0954f72057a05ee7072bf3c370379ccc8dff40a730e579e28310c8'),
    ('010_binance_history_checkpoints.sql', '7a644ce9a2b32733518b89ccbe42aae1ba480868f30d24b1124a9c79f4a219c3'),
    ('011_local_mainnet_basket_identity.sql', '29eb75acdc445433517be032a7230f0729ea9a01b95d37fefb70069788a4a9a2'),
    ('012_mainnet_basket_owner_link.sql', '0511b2549641736a26ebc815aea5e5a7440e1cb29661c9d5f9a2ac9ba4326f14')
ON CONFLICT (version) DO NOTHING;
