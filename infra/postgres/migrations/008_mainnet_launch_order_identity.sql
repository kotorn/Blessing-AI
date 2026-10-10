-- Preserve the exchange client order identity across ambiguous responses and
-- bind staged continuation review to the single confirmed first order.
ALTER TABLE mainnet_launch_sessions
    ADD COLUMN IF NOT EXISTS pending_order_client_order_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS first_order_client_order_id VARCHAR(64);

ALTER TABLE mainnet_launch_sessions
    DROP CONSTRAINT IF EXISTS mainnet_launch_order_identity_check;

ALTER TABLE mainnet_launch_sessions
    ADD CONSTRAINT mainnet_launch_order_identity_check
        CHECK (
            (pending_order_client_order_id IS NULL OR
                pending_order_client_order_id ~ '^[A-Za-z0-9_-]{1,64}$')
            AND
            (first_order_client_order_id IS NULL OR
                first_order_client_order_id ~ '^[A-Za-z0-9_-]{1,64}$')
        );
