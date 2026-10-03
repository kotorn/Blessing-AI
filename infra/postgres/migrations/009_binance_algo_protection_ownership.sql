-- Persist ownership and lifecycle for conditional Binance stop/target Algo
-- orders. Testnet and Mainnet use distinct venue keys and explicit environment.
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
