-- J.A.R.V.I.S. schema version 1 rollback
--
-- WARNING: This is a destructive rollback to the legacy base sessions schema.
-- It deletes learning, interaction, feedback, Skill, memory, and FTS data.
-- Prefer restoring a verified backup when application data must be retained.
--
-- Run offline with the SQLite CLI's fail-fast option:
--   sqlite3 -bail data/jarvis_learning.db < scripts/rollback.sql

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- Accept only the current sessions shape and the two latest safety fields.
CREATE TEMP TABLE jarvis_rollback_guard (
    valid INTEGER NOT NULL CHECK (valid = 1)
);
INSERT INTO jarvis_rollback_guard (valid)
SELECT CASE
    WHEN (
        SELECT COUNT(*) FROM pragma_table_info('sessions')
    ) = 13
    AND (
        SELECT COUNT(*) FROM pragma_table_info('sessions')
        WHERE name IN (
            'session_id', 'user_id', 'platform', 'started_at', 'ended_at',
            'token_count', 'cost', 'metadata', 'learning_enabled',
            'evolution_session_id', 'adaptation_metrics', 'parent_session_id',
            'created_at'
        )
    ) = 13
    AND EXISTS (
        SELECT 1 FROM pragma_table_info('evolution_history')
        WHERE name = 'approved'
    )
    AND EXISTS (
        SELECT 1 FROM pragma_table_info('eval_cases')
        WHERE name = 'interaction_id'
    )
    THEN 1 ELSE 0
END;

CREATE TABLE sessions__legacy_v0 (
    session_id TEXT PRIMARY KEY,
    user_id TEXT,
    platform TEXT,
    started_at REAL,
    ended_at REAL,
    token_count INTEGER DEFAULT 0,
    cost REAL DEFAULT 0.0,
    metadata JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO sessions__legacy_v0 (
    session_id, user_id, platform, started_at, ended_at, token_count, cost,
    metadata, created_at
)
SELECT
    session_id, user_id, platform, started_at, ended_at, token_count, cost,
    metadata, created_at
FROM sessions;

-- Drop virtual tables first so their FTS shadow tables are removed by SQLite.
DROP TABLE IF EXISTS short_term_memory_fts_trigram;
DROP TABLE IF EXISTS short_term_memory_fts;

-- Drop children before their referenced parents.
DROP TABLE IF EXISTS eval_cases;
DROP TABLE IF EXISTS user_feedback;
DROP TABLE IF EXISTS knowledge_edges;
DROP TABLE IF EXISTS learning_trajectories;
DROP TABLE IF EXISTS error_records;
DROP TABLE IF EXISTS learning_state;
DROP TABLE IF EXISTS operation_patterns;
DROP TABLE IF EXISTS app_preferences;
DROP TABLE IF EXISTS evolution_history;
DROP TABLE IF EXISTS short_term_memory;
DROP TABLE IF EXISTS long_term_memory;
DROP TABLE IF EXISTS skills;
DROP TABLE IF EXISTS interactions;
DROP TABLE IF EXISTS knowledge_nodes;
DROP TABLE IF EXISTS episodes;
DROP TABLE IF EXISTS patterns;
DROP TABLE IF EXISTS schema_version;

DROP TABLE sessions;
ALTER TABLE sessions__legacy_v0 RENAME TO sessions;
DROP TABLE IF EXISTS users;

DROP TABLE jarvis_rollback_guard;
COMMIT;
PRAGMA foreign_keys = ON;

-- Expected output: no rows from foreign_key_check, followed by "ok".
PRAGMA foreign_key_check;
PRAGMA integrity_check;
