-- Add an explicit launch runtime identity without changing existing Cloud Run
-- sessions. Local launches are identified by a source/runtime fingerprint and
-- deliberately have no image digest to avoid inventing container provenance.
ALTER TABLE mainnet_launch_sessions
    ALTER COLUMN image_digest DROP NOT NULL;

ALTER TABLE mainnet_launch_sessions
    ADD COLUMN IF NOT EXISTS runtime_target VARCHAR(32) NOT NULL DEFAULT 'CLOUD_RUN',
    ADD COLUMN IF NOT EXISTS runtime_fingerprint VARCHAR(64);

ALTER TABLE mainnet_launch_sessions
    DROP CONSTRAINT IF EXISTS mainnet_launch_runtime_identity_check;

ALTER TABLE mainnet_launch_sessions
    ADD CONSTRAINT mainnet_launch_runtime_identity_check
        CHECK (
            (
                runtime_target = 'LOCAL'
                AND image_digest IS NULL
                AND runtime_fingerprint IS NOT NULL
                AND runtime_fingerprint ~* '^[0-9a-f]{64}$'
            )
            OR
            (
                runtime_target = 'CLOUD_RUN'
                AND image_digest IS NOT NULL
                AND image_digest ~* '^.+@sha256:[0-9a-f]{64}$'
            )
        );

