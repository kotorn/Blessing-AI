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
    processed_at TIMESTAMPTZ,
    last_error TEXT,
    CONSTRAINT persistence_outbox_status_check
        CHECK (status IN ('PENDING', 'PROCESSING', 'PROCESSED'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_persistence_outbox_idempotency
    ON persistence_outbox(event_type, idempotency_key);
CREATE INDEX IF NOT EXISTS idx_persistence_outbox_pending
    ON persistence_outbox(status, next_attempt_at, created_at);
