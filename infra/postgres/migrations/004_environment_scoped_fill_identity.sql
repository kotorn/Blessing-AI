-- Existing installations may already have migration 001 recorded. Keep the
-- environment discriminator in a separate idempotent migration so those
-- databases receive the same Hedge Mode-safe fill identity as fresh installs.

ALTER TABLE IF EXISTS fills
  ADD COLUMN IF NOT EXISTS venue VARCHAR(32) NOT NULL DEFAULT 'binance_global';

CREATE INDEX IF NOT EXISTS idx_fills_venue_symbol_time
  ON fills (venue, symbol, executed_at);

CREATE UNIQUE INDEX IF NOT EXISTS idx_fills_venue_trade
  ON fills (venue, exchange_trade_id);
