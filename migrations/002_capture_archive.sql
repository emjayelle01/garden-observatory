CREATE TABLE IF NOT EXISTS captures (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    absolute_path TEXT NOT NULL UNIQUE,
    captured_at_utc TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    filesize_bytes INTEGER NOT NULL,
    camera_backend TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    extra_metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_captures_captured_at_utc
    ON captures(captured_at_utc);
