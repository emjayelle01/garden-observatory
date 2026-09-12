-- Durable recognition jobs and their model-independent results (Task 15.1).
--
-- Recognition observes the capture catalogue from outside the capture
-- workflow. A job is created only for a capture that has already been
-- published to ``captures``; nothing here is read or written by the capture,
-- event-capture, retention or backup code paths, and nothing here references
-- media except through ``captures(id)``.
--
-- Work state and biological outcome are deliberately two tables. A job says
-- what happened to an attempt to recognise one capture under one pipeline
-- version; a result says what that attempt concluded. This schema enforces
-- AT MOST ONE result per job (job_id UNIQUE); "a result only for a job that
-- succeeded" is not something a CHECK can express across two tables, and is
-- held instead by recognition_results having exactly one writer, which inserts
-- the result and sets 'succeeded' in the same transaction under an owned claim.
-- See docs/Recognition.md section 4.
--
-- Additive only. No existing table is rebuilt, renamed or altered, so the
-- version-3 schema beneath is byte-for-byte what it was.
--
-- Deliberately NOT "CREATE TABLE IF NOT EXISTS", for the reason migration 003
-- gives: the runner already guarantees from schema_migrations that this file
-- executes only when version 4 is genuinely pending, so IF NOT EXISTS could
-- only let a pre-existing table of some other shape satisfy the statement
-- while the runner records version 4 over it.
--
-- Every timestamp is written as UTC ISO-8601 with microseconds and an explicit
-- "+00:00" offset. Leases and retry times are compared as TEXT inside SQLite,
-- so each timestamp column is constrained to that shape AND to being text of
-- exactly 32 bytes. All three parts are load-bearing: a BLOB carrying the same
-- characters matches the GLOB but sorts after every text value, and a NUL byte
-- hides whatever follows it from length() -- either would make a lease that can
-- never expire or a retry time that is never due. What the shape cannot
-- exclude is a value that looks like a stamp but is not an instant (month 99);
-- no writer can produce one, and such a row would simply never come due.

CREATE TABLE recognition_jobs (
    -- NOT NULL is explicit for the reason migration 003 records: a TEXT
    -- PRIMARY KEY is otherwise nullable in SQLite.
    id TEXT NOT NULL PRIMARY KEY
        CHECK (length(id) BETWEEN 1 AND 64),
    capture_id TEXT NOT NULL
        REFERENCES captures(id),
    pipeline_version TEXT NOT NULL
        CHECK (length(pipeline_version) BETWEEN 1 AND 64),
    -- Reserved for multi-camera support; nothing writes it yet.
    camera_id TEXT
        CHECK (camera_id IS NULL OR length(camera_id) BETWEEN 1 AND 64),
    state TEXT NOT NULL
        CHECK (state IN ('pending', 'running', 'succeeded', 'failed', 'skipped', 'superseded')),
    attempt_count INTEGER NOT NULL
        CHECK (typeof(attempt_count) = 'integer' AND attempt_count >= 0),
    max_attempts INTEGER NOT NULL
        CHECK (typeof(max_attempts) = 'integer' AND max_attempts BETWEEN 1 AND 100),
    next_attempt_at TEXT
        CHECK (next_attempt_at IS NULL OR (typeof(next_attempt_at) = 'text'
            AND length(CAST(next_attempt_at AS BLOB)) = 32
            AND next_attempt_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00')),
    lease_owner TEXT
        CHECK (lease_owner IS NULL OR length(lease_owner) BETWEEN 1 AND 128),
    lease_expires_at TEXT
        CHECK (lease_expires_at IS NULL OR (typeof(lease_expires_at) = 'text'
            AND length(CAST(lease_expires_at AS BLOB)) = 32
            AND lease_expires_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00')),
    created_at TEXT NOT NULL
        CHECK (typeof(created_at) = 'text'
            AND length(CAST(created_at AS BLOB)) = 32
            AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
    started_at TEXT
        CHECK (started_at IS NULL OR (typeof(started_at) = 'text'
            AND length(CAST(started_at AS BLOB)) = 32
            AND started_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00')),
    finished_at TEXT
        CHECK (finished_at IS NULL OR (typeof(finished_at) = 'text'
            AND length(CAST(finished_at AS BLOB)) = 32
            AND finished_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00')),
    -- A bounded vocabulary, never free text: no exception message and no
    -- path can be stored here.
    error_category TEXT
        CHECK (error_category IS NULL OR error_category IN ('media_missing', 'unsafe_path', 'size_mismatch', 'decode_error', 'model_unavailable', 'timeout', 'resource_limit', 'unexpected')),
    -- The final idempotency guarantee. Reconciliation may run concurrently or
    -- repeatedly; however its reads interleave, one capture receives at most
    -- one job per pipeline version. A new pipeline version is a new job.
    UNIQUE (capture_id, pipeline_version),
    CHECK (attempt_count <= max_attempts),
    -- A lease exists exactly while a job is running, and a running job has
    -- always been started.
    CHECK (
        (state = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL)
        OR (state <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    ),
    -- Terminal states record when they ended; live states have not ended.
    CHECK (
        (state IN ('pending', 'running') AND finished_at IS NULL)
        OR (state IN ('succeeded', 'failed', 'skipped', 'superseded') AND finished_at IS NOT NULL)
    ),
    -- A pending job is claimable at a known time and still has an attempt left.
    CHECK (state <> 'pending' OR (next_attempt_at IS NOT NULL AND attempt_count < max_attempts)),
    -- Success carries no error, and a failure or a skip always says why.
    CHECK (state <> 'succeeded' OR error_category IS NULL),
    CHECK (state NOT IN ('failed', 'skipped') OR error_category IS NOT NULL)
);

-- The two claim paths: due pending work, and running work whose lease has
-- expired. Both are always filtered by pipeline version first. The UNIQUE
-- constraint above already indexes (capture_id, pipeline_version), which is
-- the reconciler's "is this capture already queued" lookup.
CREATE INDEX idx_recognition_jobs_due
    ON recognition_jobs(pipeline_version, state, next_attempt_at);

CREATE INDEX idx_recognition_jobs_lease
    ON recognition_jobs(pipeline_version, state, lease_expires_at);

CREATE TABLE recognition_results (
    id TEXT NOT NULL PRIMARY KEY
        CHECK (length(id) BETWEEN 1 AND 64),
    -- One result per job, enforced here rather than by the runner's care.
    job_id TEXT NOT NULL UNIQUE
        REFERENCES recognition_jobs(id),
    outcome TEXT NOT NULL
        CHECK (outcome IN ('species', 'uncertain', 'unknown_species', 'no_bird', 'person_present_only')),
    -- Provenance. Nullable because a development adapter, or a pipeline stage
    -- that does not exist yet, cannot honestly supply it; paired, because an
    -- identity without its digest (or the reverse) is not provenance at all.
    detector_model_id TEXT
        CHECK (detector_model_id IS NULL OR length(detector_model_id) BETWEEN 1 AND 128),
    detector_model_sha256 TEXT
        CHECK (detector_model_sha256 IS NULL OR (length(detector_model_sha256) = 64 AND detector_model_sha256 NOT GLOB '*[^0-9a-f]*')),
    classifier_model_id TEXT
        CHECK (classifier_model_id IS NULL OR length(classifier_model_id) BETWEEN 1 AND 128),
    classifier_model_sha256 TEXT
        CHECK (classifier_model_sha256 IS NULL OR (length(classifier_model_sha256) = 64 AND classifier_model_sha256 NOT GLOB '*[^0-9a-f]*')),
    label_set_id TEXT
        CHECK (label_set_id IS NULL OR length(label_set_id) BETWEEN 1 AND 128),
    label_set_sha256 TEXT
        CHECK (label_set_sha256 IS NULL OR (length(label_set_sha256) = 64 AND label_set_sha256 NOT GLOB '*[^0-9a-f]*')),
    taxonomy_id TEXT
        CHECK (taxonomy_id IS NULL OR length(taxonomy_id) BETWEEN 1 AND 128),
    taxonomy_version TEXT
        CHECK (taxonomy_version IS NULL OR length(taxonomy_version) BETWEEN 1 AND 64),
    preprocessing_version TEXT
        CHECK (preprocessing_version IS NULL OR length(preprocessing_version) BETWEEN 1 AND 64),
    thresholds_version TEXT
        CHECK (thresholds_version IS NULL OR length(thresholds_version) BETWEEN 1 AND 64),
    inference_duration_ms INTEGER
        CHECK (inference_duration_ms IS NULL OR (typeof(inference_duration_ms) = 'integer' AND inference_duration_ms >= 0)),
    peak_rss_bytes INTEGER
        CHECK (peak_rss_bytes IS NULL OR (typeof(peak_rss_bytes) = 'integer' AND peak_rss_bytes >= 0)),
    cpu_time_ms INTEGER
        CHECK (cpu_time_ms IS NULL OR (typeof(cpu_time_ms) = 'integer' AND cpu_time_ms >= 0)),
    image_width INTEGER
        CHECK (image_width IS NULL OR (typeof(image_width) = 'integer' AND image_width > 0)),
    image_height INTEGER
        CHECK (image_height IS NULL OR (typeof(image_height) = 'integer' AND image_height > 0)),
    created_at TEXT NOT NULL
        CHECK (typeof(created_at) = 'text'
            AND length(CAST(created_at AS BLOB)) = 32
            AND created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]+00:00'),
    CHECK ((detector_model_id IS NULL) = (detector_model_sha256 IS NULL)),
    CHECK ((classifier_model_id IS NULL) = (classifier_model_sha256 IS NULL)),
    CHECK ((label_set_id IS NULL) = (label_set_sha256 IS NULL)),
    CHECK ((taxonomy_id IS NULL) = (taxonomy_version IS NULL)),
    CHECK ((image_width IS NULL) = (image_height IS NULL))
);
