-- The engine now tags orders/fills/positions with environment-scoped venue
-- labels (BINANCE_MAINNET / BINANCE_TESTNET, via environment_label() in
-- apps/trading_worker/venues/binance/config.py) instead of the original
-- untagged default 'binance_global'. PostgresExecutionLedger.load() queries
-- WHERE UPPER(venue) = $2 against the new labels, so any row still tagged
-- 'binance_global' would otherwise be permanently invisible to the durable
-- ledger on restart -- a real open position could be silently treated as
-- nonexistent, and a later upsert would create a disjoint duplicate row
-- instead of updating it.
--
-- Real Mainnet order submission has never been enabled at the time of this
-- migration (order_submission_attempts is enforced to 0 everywhere and
-- MAINNET_LIVE_APPROVED has always been false), so every row still bearing
-- the legacy default is necessarily Testnet data. Backfill it explicitly
-- rather than leaving it unmatchable.
UPDATE orders SET venue = 'BINANCE_TESTNET' WHERE venue = 'binance_global';
UPDATE fills SET venue = 'BINANCE_TESTNET' WHERE venue = 'binance_global';
UPDATE positions SET venue = 'BINANCE_TESTNET' WHERE venue = 'binance_global';
