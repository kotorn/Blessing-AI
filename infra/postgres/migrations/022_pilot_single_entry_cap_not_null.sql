-- 021 used "max_risk_increasing_orders = 1" in the LIVE_RESEARCH_PILOT branches.
-- A CHECK constraint passes when its expression is NULL, so a pilot row with a
-- NULL cap (unlimited entries) was still accepted. Require IS NOT NULL
-- explicitly, as the other pilot columns already do.
ALTER TABLE mainnet_launch_sessions
    DROP CONSTRAINT IF EXISTS mainnet_launch_limit_check,
    DROP CONSTRAINT IF EXISTS mainnet_launch_pilot_policy_check,
    DROP CONSTRAINT IF EXISTS mainnet_launch_pilot_binding_check;

ALTER TABLE mainnet_launch_sessions
    ADD CONSTRAINT mainnet_launch_limit_check
        CHECK (
            (policy = 'STAGED_FIRST_ORDER' AND max_risk_increasing_orders = 1)
            OR (policy = 'AUTONOMOUS_AFTER_REVIEW' AND max_risk_increasing_orders IS NULL)
            OR (policy = 'LIVE_RESEARCH_PILOT' AND max_risk_increasing_orders IS NOT NULL AND max_risk_increasing_orders = 1)
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
             AND max_risk_increasing_orders IS NOT NULL
             AND max_risk_increasing_orders = 1
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
        ),
    ADD CONSTRAINT mainnet_launch_pilot_binding_check
        CHECK (
            (policy <> 'LIVE_RESEARCH_PILOT'
             AND pilot_campaign_id IS NULL
             AND pilot_git_sha IS NULL
             AND pilot_source_hash IS NULL
             AND pilot_dependency_hash IS NULL
             AND pilot_migration_hash IS NULL
             AND pilot_strategy_hash IS NULL
             AND pilot_secret_project_id IS NULL
             AND pilot_api_key_version IS NULL
             AND pilot_api_secret_version IS NULL
             AND pilot_management_mode IS NULL
             AND pilot_campaign_expires_at IS NULL
             AND pilot_status IS NULL)
            OR
            (policy = 'LIVE_RESEARCH_PILOT'
             AND runtime_target = 'LOCAL'
             AND symbol = 'ETHUSDC'
             AND max_risk_increasing_orders IS NOT NULL
             AND max_risk_increasing_orders = 1
             AND pilot_campaign_id IS NOT NULL
             AND pilot_campaign_id ~ '^pilot-[A-Za-z0-9][A-Za-z0-9_-]{7,126}$'
             AND pilot_git_sha IS NOT NULL
             AND pilot_git_sha ~ '^([0-9a-f]{40}|[0-9a-f]{64})$'
             AND pilot_source_hash IS NOT NULL
             AND pilot_source_hash ~ '^[0-9a-f]{64}$'
             AND pilot_dependency_hash IS NOT NULL
             AND pilot_dependency_hash ~ '^[0-9a-f]{64}$'
             AND pilot_migration_hash IS NOT NULL
             AND pilot_migration_hash ~ '^[0-9a-f]{64}$'
             AND pilot_strategy_hash IS NOT NULL
             AND pilot_strategy_hash ~ '^[0-9a-f]{64}$'
             AND pilot_risk_policy_hash IS NOT NULL
             AND pilot_risk_policy_hash ~ '^[0-9a-f]{64}$'
             AND pilot_secret_project_id IS NOT NULL
             AND pilot_secret_project_id ~ '^[a-z][a-z0-9-]{4,28}[a-z0-9]$'
             AND pilot_api_key_version IS NOT NULL
             AND pilot_api_key_version ~ '^[1-9][0-9]*$'
             AND pilot_api_secret_version IS NOT NULL
             AND pilot_api_secret_version ~ '^[1-9][0-9]*$'
             AND pilot_management_mode IS NOT NULL
             AND pilot_management_mode = 'QUICK'
             AND pilot_campaign_expires_at IS NOT NULL
             AND pilot_campaign_expires_at > created_at
             AND pilot_status IS NOT NULL
             AND pilot_status IN ('APPROVED', 'ACTIVE', 'CLOSE_ONLY', 'EXPIRED', 'REVOKED', 'COMPLETED')
             AND pilot_max_position_notional_usdc IS NOT NULL
             AND pilot_max_position_notional_usdc = 50
             AND pilot_per_position_risk_usdc IS NOT NULL
             AND pilot_per_position_risk_usdc = 2
             AND pilot_max_drawdown_usdc IS NOT NULL
             AND pilot_max_drawdown_usdc = 5
             AND pilot_max_leverage IS NOT NULL
             AND pilot_max_leverage = 10)
        );
