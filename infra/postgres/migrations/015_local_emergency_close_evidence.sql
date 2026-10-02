-- Permit a verified, deterministic Local emergency close to retain its own
-- proof kind without mislabeling it as a triggered Binance Algo order.
ALTER TABLE binance_algo_protections
    DROP CONSTRAINT IF EXISTS binance_algo_protection_closure_evidence_check;

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
                'LOCAL_EMERGENCY_CLOSE_VERIFIED',
                'UNFILLED_ENTRY_TERMINAL'
            )
        )
        OR (
            (state <> 'CLOSED' OR environment = 'TESTNET')
            AND closure_evidence IS NULL
        )
    );
