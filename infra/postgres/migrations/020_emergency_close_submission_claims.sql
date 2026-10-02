-- A reservation can transfer only before a submission is durably attempted.
-- An ambiguous exchange result never clears the attempt or grants a second POST.
CREATE TABLE IF NOT EXISTS binance_emergency_close_claims (
    venue VARCHAR(32) NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    entry_client_order_id VARCHAR(64) NOT NULL,
    close_client_order_id VARCHAR(64) NOT NULL,
    claimant_id VARCHAR(128) NOT NULL,
    fencing_token BIGINT NOT NULL,
    lease_until TIMESTAMPTZ NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempted_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (venue, symbol, entry_client_order_id),
    CONSTRAINT binance_emergency_close_claim_identity_unique UNIQUE (venue, close_client_order_id),
    CONSTRAINT binance_emergency_close_claim_owner_fk
        FOREIGN KEY (venue, symbol, entry_client_order_id)
        REFERENCES binance_algo_protections(venue, symbol, entry_client_order_id),
    CONSTRAINT binance_emergency_close_claim_shape_check CHECK (
        venue = 'binance_mainnet'
        AND symbol ~ '^[A-Z0-9]{2,32}$'
        AND close_client_order_id ~ '^[A-Za-z0-9_-]{1,64}$'
        AND claimant_id ~ '^[A-Za-z0-9_.:-]{1,128}$'
        AND fencing_token > 0
        AND ((status = 'RESERVED' AND attempted_at IS NULL)
             OR (status = 'ATTEMPTED' AND attempted_at IS NOT NULL))
    )
);

CREATE OR REPLACE FUNCTION fence_binance_emergency_close_claim()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Emergency close claims cannot be deleted';
    END IF;
    IF OLD.status = 'ATTEMPTED'
       OR ROW(NEW.venue, NEW.symbol, NEW.entry_client_order_id, NEW.close_client_order_id, NEW.created_at)
          IS DISTINCT FROM ROW(OLD.venue, OLD.symbol, OLD.entry_client_order_id, OLD.close_client_order_id, OLD.created_at)
    THEN
        RAISE EXCEPTION 'Emergency close identity/attempt is immutable';
    END IF;
    IF NEW.status = 'RESERVED' THEN
        IF OLD.lease_until > clock_timestamp() OR NEW.fencing_token <> OLD.fencing_token + 1 THEN
            RAISE EXCEPTION 'Emergency close reservation transfer is not fenced';
        END IF;
    ELSIF NEW.status = 'ATTEMPTED' THEN
        IF OLD.lease_until <= clock_timestamp()
           OR NEW.fencing_token <> OLD.fencing_token OR NEW.claimant_id <> OLD.claimant_id
           OR NEW.lease_until <> OLD.lease_until THEN
            RAISE EXCEPTION 'Emergency close submission has a stale fence';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER binance_emergency_close_claim_fence
    BEFORE UPDATE OR DELETE ON binance_emergency_close_claims
    FOR EACH ROW EXECUTE FUNCTION fence_binance_emergency_close_claim();
