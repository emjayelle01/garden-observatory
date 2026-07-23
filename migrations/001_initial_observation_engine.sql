PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    observed_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_observations_observed_at
    ON observations(observed_at);

CREATE INDEX IF NOT EXISTS idx_observations_kind
    ON observations(kind);

CREATE INDEX IF NOT EXISTS idx_observations_source
    ON observations(source);

CREATE INDEX IF NOT EXISTS idx_observations_correlation_id
    ON observations(correlation_id);
