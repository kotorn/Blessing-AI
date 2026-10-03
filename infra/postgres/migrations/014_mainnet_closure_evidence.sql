-- Preserve proof for every newly verified Mainnet Algo closure. Existing
-- CLOSED owners cannot be retroactively treated as proven and receive an
-- explicit legacy marker that keeps readiness fail-closed.
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
