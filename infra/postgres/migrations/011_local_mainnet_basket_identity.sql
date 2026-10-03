-- Bind a staged Local Mainnet launch and its Algo protection owner to one
-- globally unique basket identity. Historical rows remain nullable so this
-- additive migration never invents ownership for prior execution records.
ALTER TABLE mainnet_launch_sessions
    ADD COLUMN IF NOT EXISTS basket_id VARCHAR(128);

ALTER TABLE binance_algo_protections
    ADD COLUMN IF NOT EXISTS basket_id VARCHAR(128);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mainnet_launch_basket_unique
    ON mainnet_launch_sessions(basket_id)
    WHERE basket_id IS NOT NULL;
