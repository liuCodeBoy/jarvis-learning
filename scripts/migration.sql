-- J.A.R.V.I.S. legacy database migration
-- Schema version: 1 (2026-07-10 runtime snapshot)
--
-- Supported source schema:
--   A legacy database containing only the base sessions table (the nine
--   columns checked below), before learning fields were introduced.
--
-- Run offline with the SQLite CLI's fail-fast option:
--   sqlite3 -bail data/jarvis_learning.db < scripts/migration.sql
--
-- Normal application startup uses LearningDatabaseSchema and is the supported
-- idempotent upgrade path for databases that do not match this exact baseline.

PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

-- Refuse an unknown or already-migrated source schema. The CHECK failure is
-- intentionally fatal; callers must use `sqlite3 -bail` as shown above.
CREATE TEMP TABLE jarvis_migration_guard (
    valid INTEGER NOT NULL CHECK (valid = 1)
);
INSERT INTO jarvis_migration_guard (valid)
SELECT CASE
    WHEN (
        SELECT COUNT(*) FROM pragma_table_info('sessions')
    ) = 9
    AND (
        SELECT COUNT(*) FROM pragma_table_info('sessions')
        WHERE name IN (
            'session_id', 'user_id', 'platform', 'started_at', 'ended_at',
            'token_count', 'cost', 'metadata', 'created_at'
        )
    ) = 9
    AND NOT EXISTS (
        SELECT 1 FROM sqlite_schema
        WHERE type IN ('table', 'view')
          AND name NOT LIKE 'sqlite_%'
          AND name != 'sessions'
    )
    THEN 1 ELSE 0
END;

ALTER TABLE sessions RENAME TO sessions__legacy_v0;

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO users (id)
SELECT DISTINCT user_id
FROM sessions__legacy_v0
WHERE user_id IS NOT NULL AND user_id != '';

CREATE TABLE episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT,
    timestamp REAL,
    action TEXT,
    context TEXT,
    result TEXT,
    metadata TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT,
    pattern_data TEXT,
    support REAL,
    confidence REAL,
    created_at REAL
);

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT,
    platform TEXT,
    started_at REAL,
    ended_at REAL,
    token_count INTEGER DEFAULT 0,
    cost REAL DEFAULT 0.0,
    metadata JSON,
    learning_enabled BOOLEAN DEFAULT FALSE,
    evolution_session_id TEXT,
    adaptation_metrics JSON,
    parent_session_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

INSERT INTO sessions (
    session_id, user_id, platform, started_at, ended_at, token_count, cost,
    metadata, created_at
)
SELECT
    session_id, user_id, platform, started_at, ended_at, token_count, cost,
    metadata, created_at
FROM sessions__legacy_v0;

DROP TABLE sessions__legacy_v0;

CREATE TABLE interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    interaction_type TEXT NOT NULL,
    user_input TEXT,
    agent_response TEXT,
    context JSON,
    metadata JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE TABLE learning_state (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    habit_model_path TEXT,
    knowledge_model_path TEXT,
    evolution_checkpoint_path TEXT,
    last_learning_time REAL,
    learning_metrics JSON,
    online_learning_enabled BOOLEAN DEFAULT TRUE,
    offline_learning_enabled BOOLEAN DEFAULT TRUE,
    evolution_enabled BOOLEAN DEFAULT FALSE,
    prediction_accuracy REAL DEFAULT 0.0,
    knowledge_quality_score REAL DEFAULT 0.0,
    adaptation_score REAL DEFAULT 0.0,
    efficiency_score REAL DEFAULT 0.0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE evolution_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    evolution_type TEXT NOT NULL,
    generation INTEGER DEFAULT 0,
    fitness_score REAL DEFAULT 0.0,
    parent_id INTEGER,
    mutation_description TEXT,
    mutation_details JSON,
    train_score REAL,
    holdout_score REAL,
    train_failures JSON,
    holdout_failures JSON,
    approved INTEGER DEFAULT 0,
    timestamp REAL NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (parent_id) REFERENCES evolution_history(id)
);

CREATE TABLE eval_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interaction_id INTEGER,
    user_input TEXT NOT NULL,
    agent_response TEXT NOT NULL,
    expected TEXT,
    feedback_score REAL,
    source TEXT DEFAULT 'dialog',
    created_at REAL NOT NULL,
    used_in_evolution INTEGER DEFAULT 0,
    FOREIGN KEY (interaction_id) REFERENCES interactions(id)
);

CREATE UNIQUE INDEX idx_eval_cases_interaction
ON eval_cases(interaction_id) WHERE interaction_id IS NOT NULL;

CREATE TABLE knowledge_nodes (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_name TEXT NOT NULL,
    aliases JSON,
    properties JSON,
    confidence REAL DEFAULT 0.0,
    source_count INTEGER DEFAULT 0,
    canonical_entity TEXT,
    merge_history JSON,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_entity_type ON knowledge_nodes(entity_type);
CREATE INDEX idx_entity_name ON knowledge_nodes(entity_name);

CREATE TABLE knowledge_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_node TEXT NOT NULL,
    target_node TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    confidence REAL DEFAULT 0.0,
    evidence TEXT,
    source_interaction_id INTEGER,
    inferred BOOLEAN DEFAULT FALSE,
    inference_method TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (source_node) REFERENCES knowledge_nodes(id),
    FOREIGN KEY (target_node) REFERENCES knowledge_nodes(id),
    FOREIGN KEY (source_interaction_id) REFERENCES interactions(id)
);

CREATE INDEX idx_relation_type ON knowledge_edges(relation_type);
CREATE INDEX idx_source_node ON knowledge_edges(source_node);
CREATE INDEX idx_target_node ON knowledge_edges(target_node);

CREATE TABLE learning_trajectories (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    format TEXT DEFAULT 'ShareGPT',
    content JSON,
    success BOOLEAN,
    user_feedback INTEGER,
    learning_gain REAL,
    error_type TEXT,
    error_message TEXT,
    timestamp REAL NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE TABLE operation_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    operation_sequence JSON,
    sequence_length INTEGER,
    support REAL,
    confidence REAL,
    itemset JSON,
    itemset_frequency INTEGER,
    context_features JSON,
    time_distribution JSON,
    pattern_type TEXT,
    first_discovered REAL,
    last_observed REAL,
    observation_count INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE app_preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    app_name TEXT NOT NULL,
    preference_score REAL DEFAULT 0.0,
    usage_frequency INTEGER DEFAULT 0,
    time_preference JSON,
    task_preference JSON,
    location_preference JSON,
    co_occurrence_apps JSON,
    co_occurrence_score REAL,
    embedding_features JSON,
    last_used REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX idx_app_name ON app_preferences(app_name);

CREATE TABLE error_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    error_type TEXT NOT NULL,
    error_message TEXT,
    stack_trace TEXT,
    context JSON,
    correction_strategy TEXT,
    correction_attempts INTEGER DEFAULT 0,
    correction_success BOOLEAN,
    resolution_details JSON,
    pattern_matched BOOLEAN DEFAULT FALSE,
    pattern_id TEXT,
    timestamp REAL NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE TABLE user_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    interaction_id INTEGER,
    feedback_type TEXT NOT NULL,
    feedback_value REAL,
    feedback_text TEXT,
    implicit_signals JSON,
    timestamp REAL NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (interaction_id) REFERENCES interactions(id)
);

CREATE TABLE skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    trigger_keywords JSON,
    trigger_regex TEXT,
    prompt_template TEXT,
    source_pattern_id INTEGER,
    created_at REAL NOT NULL,
    last_triggered_at REAL,
    trigger_count INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1,
    reviewed INTEGER DEFAULT 0
);

CREATE TABLE short_term_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value JSON NOT NULL,
    metadata JSON,
    importance REAL DEFAULT 0.5,
    access_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    last_accessed REAL NOT NULL,
    expires_at REAL
);

CREATE TABLE long_term_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT UNIQUE NOT NULL,
    value JSON NOT NULL,
    metadata JSON,
    importance REAL DEFAULT 0.5,
    importance_decay REAL DEFAULT 0.0,
    created_at REAL NOT NULL,
    last_accessed REAL NOT NULL,
    last_reinforced REAL,
    knowledge_node_id TEXT,
    access_count INTEGER DEFAULT 0,
    reinforcement_count INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE short_term_memory_fts
USING fts5(key, value, tokenize='unicode61');

CREATE VIRTUAL TABLE short_term_memory_fts_trigram
USING fts5(key, value, tokenize='trigram');

CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    description TEXT
);

INSERT INTO schema_version (version, description)
VALUES (1, 'Current web runtime schema with reviewed evolution and feedback links');

DROP TABLE jarvis_migration_guard;
COMMIT;
PRAGMA foreign_keys = ON;

-- Expected output: no rows from foreign_key_check, followed by "ok".
PRAGMA foreign_key_check;
PRAGMA integrity_check;
