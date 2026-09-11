-- ============================================================================
-- Blessing AI v0.1 Production Database Schema (PostgreSQL 17 / TimescaleDB)
-- Designed for High-Throughput Event-Driven Quant Execution & Basket Auditing
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Instruments Table
CREATE TABLE IF NOT EXISTS instruments (
    symbol VARCHAR(32) PRIMARY KEY,
    venue VARCHAR(32) NOT NULL,              -- e.g. binance_global, bybit, okx
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

-- 2. Strategies
CREATE TABLE IF NOT EXISTS strategies (
    strategy_id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(64) NOT NULL,
    version VARCHAR(16) NOT NULL,
    config JSONB NOT NULL,
    status VARCHAR(16) DEFAULT 'ACTIVE',    -- ACTIVE, PAUSED, DRAIN_ONLY, RETIRED
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

-- 3. Baskets (First-Class Entity for Adaptive Basket Recovery)
CREATE TABLE IF NOT EXISTS baskets (
    basket_id VARCHAR(64) PRIMARY KEY,
    strategy_id VARCHAR(64) REFERENCES strategies(strategy_id),
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

CREATE INDEX idx_baskets_state ON baskets(state);
CREATE INDEX idx_baskets_instrument ON baskets(instrument);
CREATE INDEX idx_baskets_created_at ON baskets(created_at);

-- 4. Orders
CREATE TABLE IF NOT EXISTS orders (
    client_order_id VARCHAR(64) PRIMARY KEY,
    exchange_order_id VARCHAR(64),
    basket_id VARCHAR(64) REFERENCES baskets(basket_id),
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    venue VARCHAR(32) NOT NULL,
    side VARCHAR(8) NOT NULL,                -- BUY, SELL
    order_type VARCHAR(16) NOT NULL,         -- LIMIT, MARKET, STOP_MARKET
    order_role VARCHAR(24) NOT NULL,         -- GRID_ENTRY, GRID_STEP, BASKET_TP, RECOVERY_EXIT, EMERGENCY_SL
    grid_level INT,
    price NUMERIC(28, 10),
    quantity NUMERIC(28, 10) NOT NULL,
    status VARCHAR(24) NOT NULL,             -- PENDING, SUBMITTED, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED
    time_in_force VARCHAR(8) DEFAULT 'GTC',
    filled_quantity NUMERIC(28, 10) DEFAULT 0.0,
    avg_fill_price NUMERIC(28, 10) DEFAULT 0.0,
    cumulative_fee NUMERIC(28, 10) DEFAULT 0.0,
    fee_asset VARCHAR(16),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_orders_basket_id ON orders(basket_id);
CREATE INDEX idx_orders_exchange_id ON orders(exchange_order_id);

-- 5. Fills / Executions
CREATE TABLE IF NOT EXISTS fills (
    fill_id VARCHAR(64) PRIMARY KEY,
    client_order_id VARCHAR(64) REFERENCES orders(client_order_id),
    exchange_trade_id VARCHAR(64) NOT NULL,
    basket_id VARCHAR(64) REFERENCES baskets(basket_id),
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    side VARCHAR(8) NOT NULL,
    price NUMERIC(28, 10) NOT NULL,
    quantity NUMERIC(28, 10) NOT NULL,
    fee NUMERIC(28, 10) NOT NULL,
    fee_asset VARCHAR(16) NOT NULL,
    is_maker BOOLEAN DEFAULT FALSE,
    executed_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_fills_basket ON fills(basket_id);
CREATE INDEX idx_fills_executed_at ON fills(executed_at);

-- 6. Positions
CREATE TABLE IF NOT EXISTS positions (
    id SERIAL PRIMARY KEY,
    venue VARCHAR(32) NOT NULL,
    symbol VARCHAR(32) REFERENCES instruments(symbol),
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
    UNIQUE(venue, symbol)
);

-- 7. Funding Events
CREATE TABLE IF NOT EXISTS funding_events (
    id SERIAL PRIMARY KEY,
    venue VARCHAR(32) NOT NULL,
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    basket_id VARCHAR(64) REFERENCES baskets(basket_id),
    funding_rate NUMERIC(16, 8) NOT NULL,
    payment NUMERIC(28, 10) NOT NULL,        -- Positive = received, Negative = paid
    position_size NUMERIC(28, 10) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_funding_symbol_time ON funding_events(symbol, timestamp);

-- 8. Regime Predictions (Regime Engine Outputs)
CREATE TABLE IF NOT EXISTS regime_predictions (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    timestamp TIMESTAMPTZ NOT NULL,
    model_version VARCHAR(32) NOT NULL,
    predicted_regime VARCHAR(32) NOT NULL,   -- R0..R6
    prob_r0_strong_mean_reversion NUMERIC(6, 4) NOT NULL,
    prob_r1_range NUMERIC(6, 4) NOT NULL,
    prob_r2_weak_trend NUMERIC(6, 4) NOT NULL,
    prob_r3_strong_trend NUMERIC(6, 4) NOT NULL,
    prob_r4_breakout NUMERIC(6, 4) NOT NULL,
    prob_r5_volatility_shock NUMERIC(6, 4) NOT NULL,
    prob_r6_crisis NUMERIC(6, 4) NOT NULL,
    entropy NUMERIC(8, 4)
);

CREATE INDEX idx_regime_symbol_time ON regime_predictions(symbol, timestamp);

-- 9. Grid Safety Predictions (Grid Safety AI Outputs)
CREATE TABLE IF NOT EXISTS grid_safety_predictions (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(32) REFERENCES instruments(symbol),
    timestamp TIMESTAMPTZ NOT NULL,
    model_version VARCHAR(32) NOT NULL,
    grid_safety_score NUMERIC(6, 2) NOT NULL, -- 0 to 100
    prob_basket_profit NUMERIC(6, 4) NOT NULL,
    prob_reach_level_2 NUMERIC(6, 4) NOT NULL,
    prob_reach_level_3 NUMERIC(6, 4) NOT NULL,
    prob_reach_level_5 NUMERIC(6, 4) NOT NULL,
    expected_mae_pct NUMERIC(8, 4) NOT NULL,
    expected_max_equity_dd_pct NUMERIC(8, 4) NOT NULL,
    expected_grid_depth NUMERIC(6, 2) NOT NULL,
    expected_recovery_time_hrs NUMERIC(8, 2) NOT NULL,
    expected_net_basket_return NUMERIC(10, 4) NOT NULL,
    features_snapshot JSONB
);

CREATE INDEX idx_grid_safety_symbol_time ON grid_safety_predictions(symbol, timestamp);

-- 10. Portfolio Risk Snapshots
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

CREATE INDEX idx_portfolio_time ON portfolio_snapshots(timestamp);

-- 11. Model Versions (ML Governance & Experiment Audit)
CREATE TABLE IF NOT EXISTS model_versions (
    model_id VARCHAR(64) PRIMARY KEY,
    model_family VARCHAR(32) NOT NULL,       -- REGIME_CLASSIFIER, GRID_SAFETY_SCORE
    framework VARCHAR(32) NOT NULL,          -- CATBOOST, XGBOOST, LIGHTGBM, HMM
    artifact_uri TEXT NOT NULL,
    train_start_date TIMESTAMPTZ NOT NULL,
    train_end_date TIMESTAMPTZ NOT NULL,
    val_metrics JSONB NOT NULL,
    is_active BOOLEAN DEFAULT FALSE,
    deployed_at TIMESTAMPTZ
);
