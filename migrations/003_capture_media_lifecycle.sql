-- Media lifecycle state for catalogued captures.
--
-- The ``captures`` table is the historical catalogue of captures that HAPPENED
-- and it is never deleted from: a capture record is evidence, and reclaiming
-- the JPEG it points at does not un-happen the capture. This table records the
-- separate fact of what became of that capture's media.
--
-- Absence of a row means the media is PRESENT. It is deliberately not a column
-- on ``captures``: that keeps the version-2 capture table's shape unchanged, so
-- an unversioned version-2 database stays adoptable by the legacy-schema logic,
-- and it keeps "a capture occurred" and "its media was later reclaimed" as the
-- two different facts they are.
--
-- Deliberately NOT "CREATE TABLE IF NOT EXISTS". The migration runner
-- already guarantees, from the schema_migrations history, that this file
-- executes only when version 3 is genuinely pending, so IF NOT EXISTS could
-- never make a legitimate re-run succeed -- it could only let a PRE-EXISTING
-- table of some other shape silently satisfy this statement while the runner
-- went on to record version 3. A database claiming version 3 with a table
-- this file never created is exactly the state that must fail closed, so the
-- CREATE is unconditional and a name collision aborts the migration.

CREATE TABLE capture_media_lifecycle (
    capture_id TEXT PRIMARY KEY
        REFERENCES captures(id),
    state TEXT NOT NULL
        CHECK (state IN ('pending_delete', 'deleted')),
    requested_at_utc TEXT NOT NULL,
    deleted_at_utc TEXT,
    reason TEXT NOT NULL
        CHECK (reason IN ('age', 'managed_bytes', 'age_and_managed_bytes')),
    -- Filesystem deletion and SQLite cannot share one transaction, so
    -- ``pending_delete`` is a durable statement of intent that a later run can
    -- recover. The database enforces that the two states cannot lie about their
    -- own timestamps: an intent has no completion time, and a completion has one.
    CHECK (
        (state = 'pending_delete' AND deleted_at_utc IS NULL)
        OR (state = 'deleted' AND deleted_at_utc IS NOT NULL)
    )
);

CREATE INDEX idx_capture_media_lifecycle_state
    ON capture_media_lifecycle(state);
