"""Add the owner-scoped, non-secret provider auth operation ledger."""

VERSION = 25
REQUIRED_COLUMNS = {
    "connection_auth_operations": {
        "operation_id", "owner_id", "connection_id", "kind", "state", "phase",
        "idempotency_key_hash", "server_replay_discriminator_hash",
        "intent_fingerprint", "before_state_fingerprint",
        "current_generation_fingerprint", "revoke_requested",
        "revoke_attempt_started_at", "revoke_state", "attempt_count",
        "next_attempt_at", "error_code", "created_at", "updated_at", "expires_at",
        "terminal_at",
    },
}


def upgrade(db):
    db.execute("""CREATE TABLE connection_auth_operations (
        operation_id VARCHAR(64) PRIMARY KEY
            CHECK (length(operation_id) = 64 AND operation_id NOT GLOB '*[^0-9a-f]*'),
        owner_id INTEGER NOT NULL,
        connection_id INTEGER NOT NULL,
        kind VARCHAR(32) NOT NULL CHECK (kind IN
            ('auth','disconnect','grant_replace','same_grant_refresh')),
        state VARCHAR(32) NOT NULL CHECK (state IN
            ('prepared','awaiting_user','polling','exchanging','committing','recovering',
             'succeeded','cancelled','expired','interrupted','failed')),
        phase VARCHAR(40) NOT NULL CHECK (phase IN
            ('none','recovery_preparing','old_retained','old_delete_pending',
             'db_commit_pending','rollback_pending','reconcile_pending',
             'committed_cleanup_pending','terminal')),
        idempotency_key_hash VARCHAR(64)
            CHECK (idempotency_key_hash IS NULL OR
                (length(idempotency_key_hash) = 64 AND
                 idempotency_key_hash NOT GLOB '*[^0-9a-f]*')),
        server_replay_discriminator_hash VARCHAR(64)
            CHECK (server_replay_discriminator_hash IS NULL OR
                (length(server_replay_discriminator_hash) = 64 AND
                 server_replay_discriminator_hash NOT GLOB '*[^0-9a-f]*')),
        intent_fingerprint VARCHAR(64) NOT NULL
            CHECK (length(intent_fingerprint) = 64 AND
                   intent_fingerprint NOT GLOB '*[^0-9a-f]*'),
        before_state_fingerprint VARCHAR(64) NOT NULL
            CHECK (length(before_state_fingerprint) = 64 AND
                   before_state_fingerprint NOT GLOB '*[^0-9a-f]*'),
        current_generation_fingerprint VARCHAR(64) NOT NULL
            CHECK (length(current_generation_fingerprint) = 64 AND
                   current_generation_fingerprint NOT GLOB '*[^0-9a-f]*'),
        revoke_requested BOOLEAN NOT NULL CHECK (revoke_requested IN (0,1)),
        revoke_attempt_started_at DATETIME,
        revoke_state VARCHAR(32) NOT NULL CHECK (revoke_state IN
            ('pending','revoked','skipped','not_supported','already_invalid',
             'remote_revoke_failed')),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        next_attempt_at DATETIME,
        error_code VARCHAR(48) CHECK (error_code IS NULL OR error_code IN
            ('auth_method_unavailable','configuration_changed','idempotency_conflict',
             'auth_operation_busy','owner_epoch_unavailable','owner_epoch_changed',
             'credential_store_unavailable','credential_recovery_required',
             'credential_cleanup_unconfirmed','provider_authorization_denied',
             'provider_temporary_failure','provider_invalid_response',
             'operation_cancelled','operation_expired','operation_interrupted',
             'internal_error')),
        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at DATETIME,
        terminal_at DATETIME,
        CHECK ((idempotency_key_hash IS NULL) !=
               (server_replay_discriminator_hash IS NULL)),
        CHECK ((phase = 'terminal') = (terminal_at IS NOT NULL)),
        FOREIGN KEY (connection_id, owner_id)
            REFERENCES provider_connections(id, user_id) ON DELETE CASCADE
    )""")
    db.execute("""CREATE UNIQUE INDEX ux_connection_auth_operations_owner_key
        ON connection_auth_operations(owner_id,idempotency_key_hash)
        WHERE idempotency_key_hash IS NOT NULL""")
    db.execute("""CREATE UNIQUE INDEX ux_connection_auth_operations_owner_discriminator
        ON connection_auth_operations(owner_id,server_replay_discriminator_hash)
        WHERE server_replay_discriminator_hash IS NOT NULL""")
    db.execute("""CREATE UNIQUE INDEX ux_connection_auth_operations_active_connection
        ON connection_auth_operations(owner_id,connection_id)
        WHERE terminal_at IS NULL""")
