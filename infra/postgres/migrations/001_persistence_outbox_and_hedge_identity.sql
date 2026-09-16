-- Apply after the original v0.2 schema when upgrading an existing database.
-- The statements are idempotent so a deployment retry is safe.

ALTER TABLE IF EXISTS orders
    ADD COLUMN IF NOT EXISTS position_side VARCHAR(8) NOT NULL DEFAULT 'BOTH';

ALTER TABLE IF EXISTS fills
    ADD COLUMN IF NOT EXISTS position_side VARCHAR(8) NOT NULL DEFAULT 'BOTH';

ALTER TABLE IF EXISTS fills
    ADD COLUMN IF NOT EXISTS exchange_order_id VARCHAR(64);

-- Fills need the same fixed environment identity as orders and positions so
-- a Testnet observation can never be used as Mainnet reconciliation evidence.
ALTER TABLE IF EXISTS fills
    ADD COLUMN IF NOT EXISTS venue VARCHAR(32) NOT NULL DEFAULT 'binance_global';

CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_venue_trade
    ON fills(venue, exchange_trade_id);

ALTER TABLE IF EXISTS positions
    ADD COLUMN IF NOT EXISTS position_side VARCHAR(8) NOT NULL DEFAULT 'BOTH';

UPDATE positions
SET position_side = 'BOTH'
WHERE position_side IS NULL OR position_side = '';

DO $$
DECLARE
    existing_constraint TEXT;
BEGIN
    SELECT conname
    INTO existing_constraint
    FROM pg_constraint
    WHERE conrelid = 'positions'::regclass
      AND contype = 'u'
      AND pg_get_constraintdef(oid) LIKE 'UNIQUE (venue, symbol)%'
    LIMIT 1;

    IF existing_constraint IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE positions DROP CONSTRAINT %I',
            existing_constraint
        );
    END IF;
END $$;

-- Older installations may have created a standalone unique index rather than
-- a table constraint. Remove only an exact (venue, symbol) unique index so the
-- hedge-mode identity below can be the sole uniqueness boundary.
DO $$
DECLARE
    existing_index TEXT;
BEGIN
    FOR existing_index IN
        SELECT indexrelid::regclass::TEXT
        FROM pg_index
        WHERE indrelid = 'positions'::regclass
          AND indisunique
          AND NOT EXISTS (
              SELECT 1
              FROM pg_constraint
              WHERE conindid = pg_index.indexrelid
          )
          AND pg_get_indexdef(indexrelid) ~* '\(venue, symbol\)'
    LOOP
        EXECUTE format('DROP INDEX IF EXISTS %s', existing_index);
    END LOOP;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_venue_symbol_side
    ON positions(venue, symbol, position_side);

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

-- A worker crash can leave an outbox event in PROCESSING after its claim
-- transaction has committed.  The dispatcher reclaims only stale claims;
-- active claims remain fenced by SKIP LOCKED in the next transaction.
ALTER TABLE IF EXISTS persistence_outbox
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_persistence_outbox_pending
    ON persistence_outbox(status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_persistence_outbox_claimed
    ON persistence_outbox(status, claimed_at);
