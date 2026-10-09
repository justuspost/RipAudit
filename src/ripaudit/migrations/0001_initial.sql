CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);

-- One row per normalized container path. A file's "signature" is size + mtime_ns.
CREATE TABLE files (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    root TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    signature TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'present',       -- present | missing
    stable_count INTEGER NOT NULL DEFAULT 0,
    ready INTEGER NOT NULL DEFAULT 0,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    signature_changed_at REAL NOT NULL
);
CREATE INDEX idx_files_state ON files(state);

CREATE TABLE probes (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    signature TEXT NOT NULL,
    status TEXT NOT NULL,      -- ok | ok_with_warnings | invalid_duration | failed | timeout | changed
    duration_seconds REAL,
    duration_source TEXT,
    container_duration REAL,
    video_codec TEXT,
    width INTEGER,
    height INTEGER,
    video_stream_count INTEGER,
    audio_count INTEGER,
    audio_languages TEXT,
    subtitle_count INTEGER,
    subtitle_languages TEXT,
    warnings TEXT,             -- JSON list
    diagnostics TEXT,          -- truncated stderr / error text
    probed_at REAL NOT NULL
);
CREATE INDEX idx_probes_file ON probes(file_id, probed_at);

-- Plex identity for a file, refreshed by Plex sync.
CREATE TABLE identities (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    state TEXT NOT NULL,       -- pending | matched | unmatched | ambiguous | manual
    plex_rating_key TEXT,
    plex_media_id TEXT,
    plex_part_id TEXT,
    part_index INTEGER,
    part_count INTEGER,
    plex_title TEXT,
    plex_year INTEGER,
    plex_edition TEXT,
    plex_library TEXT,
    guids TEXT,                -- JSON list
    tmdb_id INTEGER,
    imdb_id TEXT,
    match_method TEXT,         -- plex_tmdb | plex_imdb | title_search | manual
    candidates TEXT,           -- JSON list for manual resolution
    note TEXT,
    updated_at REAL NOT NULL
);

CREATE TABLE tmdb_cache (
    tmdb_id INTEGER PRIMARY KEY,
    title TEXT,
    year INTEGER,
    runtime_minutes INTEGER,
    imdb_id TEXT,
    fetched_at REAL NOT NULL
);

CREATE TABLE overrides (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    signature TEXT NOT NULL,
    expected_seconds REAL NOT NULL,
    note TEXT,
    actor TEXT NOT NULL,
    created_at REAL NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE approvals (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    signature TEXT NOT NULL,
    reason TEXT NOT NULL,
    preserve_on_replace INTEGER NOT NULL DEFAULT 0,
    actor TEXT NOT NULL,
    created_at REAL NOT NULL,
    revoked_at REAL,
    revoked_by TEXT,
    revoke_reason TEXT
);

CREATE TABLE results (
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    signature TEXT NOT NULL,
    outcome TEXT NOT NULL,
    severity TEXT NOT NULL,      -- none | review | high
    probe_state TEXT NOT NULL,
    identity_state TEXT NOT NULL,
    runtime_state TEXT NOT NULL,
    health_state TEXT NOT NULL,
    actual_seconds REAL,
    expected_seconds REAL,
    difference_seconds REAL,
    expected_source TEXT,
    reasons TEXT NOT NULL,       -- JSON list
    computed_at REAL NOT NULL
);
CREATE INDEX idx_results_outcome ON results(outcome);

CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT,
    actor TEXT NOT NULL DEFAULT 'system'
);
CREATE INDEX idx_events_file ON events(file_id, ts);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,          -- probe | metadata
    file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
    status TEXT NOT NULL,        -- queued | running | done | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    not_before REAL NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX idx_jobs_active ON jobs(kind, file_id) WHERE status IN ('queued', 'running');

CREATE TABLE notifications (
    id INTEGER PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    priority TEXT NOT NULL,
    status TEXT NOT NULL,        -- pending | sent | failed
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL,
    sent_at REAL
);

CREATE TABLE scans (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,          -- discovery | plex_sync
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL,        -- running | ok | partial | failed | interrupted
    summary TEXT
);

CREATE TABLE kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
