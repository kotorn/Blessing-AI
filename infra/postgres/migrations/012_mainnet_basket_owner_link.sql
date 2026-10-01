-- Tie a Local Mainnet protection owner to the exact launch and basket that
-- authorized it. Legacy/Testnet ownership rows remain nullable and are not
-- retroactively adopted.
DO $$ BEGIN
    ALTER TABLE mainnet_launch_sessions
        ADD CONSTRAINT mainnet_launch_basket_identity_unique
        UNIQUE (launch_id, basket_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE binance_algo_protections
    ADD COLUMN IF NOT EXISTS mainnet_launch_id VARCHAR(128);

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
        CHECK (
            mainnet_launch_id IS NULL
            OR (environment = 'MAINNET' AND basket_id IS NOT NULL)
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
