-- Persist the starting-capital baseline used by LIVE drawdown governance.
-- Existing launch rows are intentionally left NULL; only newly armed sessions
-- must populate both fields. Runtime code fails closed if an armed LIVE session
-- lacks a trusted baseline.

ALTER TABLE mainnet_launch_sessions
  ADD COLUMN IF NOT EXISTS baseline_capital NUMERIC(38, 18),
  ADD COLUMN IF NOT EXISTS baseline_set_at TIMESTAMPTZ;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'mainnet_launch_sessions_baseline_positive'
  ) THEN
    ALTER TABLE mainnet_launch_sessions
      ADD CONSTRAINT mainnet_launch_sessions_baseline_positive
      CHECK (baseline_capital IS NULL OR baseline_capital > 0);
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'mainnet_launch_sessions_baseline_pair'
  ) THEN
    ALTER TABLE mainnet_launch_sessions
      ADD CONSTRAINT mainnet_launch_sessions_baseline_pair
      CHECK ((baseline_capital IS NULL) = (baseline_set_at IS NULL));
  END IF;
END $$;

COMMENT ON COLUMN mainnet_launch_sessions.baseline_capital IS
  'Starting-capital baseline for drawdown governance; immutable for a launch session.';
COMMENT ON COLUMN mainnet_launch_sessions.baseline_set_at IS
  'UTC timestamp when baseline_capital was captured at operator arm.';
