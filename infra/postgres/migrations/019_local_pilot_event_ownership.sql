-- Bind each append-only pilot event to the launch that owns its campaign.
-- Existing mismatched rows cause migration failure; no evidence is rewritten.
ALTER TABLE mainnet_launch_sessions
    ADD CONSTRAINT mainnet_launch_pilot_pair_unique
        UNIQUE (launch_id, pilot_campaign_id);

ALTER TABLE local_live_pilot_events
    ADD CONSTRAINT local_live_pilot_events_owner_fk
        FOREIGN KEY (launch_id, campaign_id)
        REFERENCES mainnet_launch_sessions (launch_id, pilot_campaign_id);
