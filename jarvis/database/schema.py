"""
贾维斯自学习系统 - 数据库Schema扩展
继承Hermes架构: WAL模式 + FTS5 + Compression机制
"""

import sqlite3
import logging
import re
import threading
from typing import Dict


logger = logging.getLogger(__name__)


class LearningDatabaseSchema:
    """
    学习数据库Schema管理器
    扩展Hermes的SQLite架构以支持自学习自进化
    """

    def __init__(self, db_path: str = "jarvis_learning.db"):
        self.db_path = db_path
        self._lock = threading.RLock()
        self.schema_version = 1

    def initialize_schema(self):
        """
        初始化完整的学习数据库Schema
        包含所有学习、进化、知识表
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            # Legacy releases created three invalid FKs to non-unique
            # sessions.user_id. Keep checks off while those tables are rebuilt.
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    timestamp REAL,
                    action TEXT,
                    context TEXT,
                    result TEXT,
                    metadata TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pattern_type TEXT,
                    pattern_data TEXT,
                    support REAL,
                    confidence REAL,
                    created_at REAL
                )
            """)

            # === 1. 扩展sessions表(Hermes继承) ===
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    platform TEXT,
                    started_at REAL,
                    ended_at REAL,
                    token_count INTEGER DEFAULT 0,
                    cost REAL DEFAULT 0.0,
                    metadata JSON,

                    -- 学习扩展字段
                    learning_enabled BOOLEAN DEFAULT FALSE,
                    evolution_session_id TEXT,
                    adaptation_metrics JSON,
                    parent_session_id TEXT,  -- Compression链机制(继承Hermes)

                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            """)
            self._ensure_columns(conn, "sessions", {
                "learning_enabled": "BOOLEAN DEFAULT FALSE",
                "evolution_session_id": "TEXT",
                "adaptation_metrics": "JSON",
                "parent_session_id": "TEXT",
            })

            # Runtime interaction tables live here so every entry point gets
            # the same constraints.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS interactions (
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
                )
            """)
            self._repair_interactions_session_fk(conn)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_feedback (
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
                )
            """)
            self._repair_user_feedback_foreign_keys(conn)
            # Legacy pipeline releases maintained independent FTS tables that
            # Web writes never updated and no runtime reader queried.
            conn.execute("DROP TABLE IF EXISTS interactions_fts_trigram")
            conn.execute("DROP TABLE IF EXISTS interactions_fts")

            # === 2. 学习状态表 ===
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learning_state (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    habit_model_path TEXT,
                    knowledge_model_path TEXT,
                    evolution_checkpoint_path TEXT,
                    last_learning_time REAL,
                    learning_metrics JSON,

                    -- 学习配置
                    online_learning_enabled BOOLEAN DEFAULT TRUE,
                    offline_learning_enabled BOOLEAN DEFAULT TRUE,
                    evolution_enabled BOOLEAN DEFAULT FALSE,

                    -- 性能指标
                    prediction_accuracy REAL DEFAULT 0.0,
                    knowledge_quality_score REAL DEFAULT 0.0,
                    adaptation_score REAL DEFAULT 0.0,
                    efficiency_score REAL DEFAULT 0.0,

                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            """)

            # === 3. 进化历史表 ===
            conn.execute("""
                CREATE TABLE IF NOT EXISTS evolution_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    evolution_type TEXT NOT NULL,
                    -- 类型: 'prompt', 'tool', 'behavior', 'model'

                    generation INTEGER DEFAULT 0,
                    fitness_score REAL DEFAULT 0.0,
                    parent_id INTEGER,  -- 进化树结构

                    mutation_description TEXT,
                    mutation_details JSON,

                    -- 验证结果
                    train_score REAL,
                    holdout_score REAL,
                    train_failures JSON,
                    holdout_failures JSON,
                    approved INTEGER DEFAULT 0,

                    timestamp REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY (parent_id) REFERENCES evolution_history(id)
                )
            """)
            self._ensure_columns(conn, "evolution_history", {
                "approved": "INTEGER DEFAULT 0",
            })

            conn.execute("""
                CREATE TABLE IF NOT EXISTS eval_cases (
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
                )
            """)
            self._ensure_columns(conn, "eval_cases", {
                "interaction_id": "INTEGER",
            })
            self._repair_eval_cases_interaction_fk(conn)
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_cases_interaction
                ON eval_cases(interaction_id) WHERE interaction_id IS NOT NULL
            """)

            # === 4. 知识节点表 ===
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_nodes (
                    id TEXT PRIMARY KEY,
                    entity_type TEXT NOT NULL,
                    -- 类型: person, organization, project, date, location, event, technology, product, preference, skill, resource

                    entity_name TEXT NOT NULL,
                    aliases JSON,  -- 别名列表
                    properties JSON,

                    confidence REAL DEFAULT 0.0,
                    source_count INTEGER DEFAULT 0,

                    -- 实体消解
                    canonical_entity TEXT,  -- 规范实体引用
                    merge_history JSON,

                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 创建索引(分离语句)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_type ON knowledge_nodes(entity_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_entity_name ON knowledge_nodes(entity_name)")
            conn.execute("DROP TABLE IF EXISTS knowledge_fts_trigram")
            conn.execute("DROP TABLE IF EXISTS knowledge_fts")

            # === 5. 知识边表 ===
            conn.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_edges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_node TEXT NOT NULL,
                    target_node TEXT NOT NULL,
                    relation_type TEXT NOT NULL,
                    -- 类型: responsible_for, participates_in, deadline, location,
                    --       prefers, skilled_in, depends_on, creates, uses, related_to

                    confidence REAL DEFAULT 0.0,
                    evidence TEXT,
                    source_interaction_id INTEGER,

                    -- 关系推理
                    inferred BOOLEAN DEFAULT FALSE,
                    inference_method TEXT,

                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                    FOREIGN KEY (source_node) REFERENCES knowledge_nodes(id),
                    FOREIGN KEY (target_node) REFERENCES knowledge_nodes(id),
                    FOREIGN KEY (source_interaction_id) REFERENCES interactions(id)
                )
            """)

            # 创建索引(分离语句)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_relation_type ON knowledge_edges(relation_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_source_node ON knowledge_edges(source_node)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_target_node ON knowledge_edges(target_node)")

            # === 6. 学习轨迹表 ===
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learning_trajectories (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,

                    -- 学习数据格式(继承Hermes ShareGPT格式)
                    format TEXT DEFAULT 'ShareGPT',
                    content JSON,

                    -- 学习结果
                    success BOOLEAN,
                    user_feedback INTEGER,  -- 1-5星
                    learning_gain REAL,  -- 学习增益

                    -- 错误信息
                    error_type TEXT,
                    error_message TEXT,

                    timestamp REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                )
            """)

            # === 7. 操作模式表 ===
            conn.execute("""
                CREATE TABLE IF NOT EXISTS operation_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,

                    -- 序列模式(PrefixSpan)
                    operation_sequence JSON,
                    sequence_length INTEGER,
                    support REAL,  -- 支持度
                    confidence REAL,  -- 置信度

                    -- 频繁项集(FP-Growth)
                    itemset JSON,
                    itemset_frequency INTEGER,

                    -- 上下文模式
                    context_features JSON,

                    -- 时间模式(GMM)
                    time_distribution JSON,

                    pattern_type TEXT,  -- 'sequence', 'itemset', 'context', 'time'
                    first_discovered REAL,
                    last_observed REAL,
                    observation_count INTEGER DEFAULT 1,

                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            """)

            # === 8. 应用偏好表 ===
            conn.execute("""
                CREATE TABLE IF NOT EXISTS app_preferences (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    app_name TEXT NOT NULL,

                    -- 偏好评分
                    preference_score REAL DEFAULT 0.0,
                    usage_frequency INTEGER DEFAULT 0,

                    -- 上下文偏好
                    time_preference JSON,  -- 不同时间段的使用频率
                    task_preference JSON,  -- 不同任务类型的使用频率
                    location_preference JSON,

                    -- 关联偏好(Apriori)
                    co_occurrence_apps JSON,  -- 共现应用列表
                    co_occurrence_score REAL,

                    -- LightGBM特征
                    embedding_features JSON,

                    last_used REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
            """)

            # 创建索引
            conn.execute("CREATE INDEX IF NOT EXISTS idx_app_name ON app_preferences(app_name)")

            # === 9. 错误记录表 ===
            conn.execute("""
                CREATE TABLE IF NOT EXISTS error_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,

                    error_type TEXT NOT NULL,
                    -- 类型: 'validation', 'execution', 'semantic'

                    error_message TEXT,
                    stack_trace TEXT,
                    context JSON,

                    -- 修正策略
                    correction_strategy TEXT,
                    -- 策略: 'retry', 'fallback', 'alternative', 'rollback', 'escalation'

                    correction_attempts INTEGER DEFAULT 0,
                    correction_success BOOLEAN,
                    resolution_details JSON,

                    -- 从错误中学习
                    pattern_matched BOOLEAN DEFAULT FALSE,
                    pattern_id TEXT,

                    timestamp REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                )
            """)

            # Schema版本记录
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    description TEXT
                )
            """)

            conn.execute("""
                INSERT OR IGNORE INTO schema_version (version, description)
                VALUES (1, 'Initial learning schema with Hermes inheritance')
            """)

            self._repair_legacy_user_foreign_keys(conn)
            conn.execute("""
                UPDATE evolution_history SET session_id = NULL
                WHERE session_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM sessions
                      WHERE sessions.session_id = evolution_history.session_id
                  )
            """)
            conn.execute("""
                UPDATE evolution_history SET parent_id = NULL
                WHERE parent_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM evolution_history AS parent
                      WHERE parent.id = evolution_history.parent_id
                  )
            """)
            conn.execute("""
                UPDATE error_records SET session_id = NULL
                WHERE session_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM sessions
                      WHERE sessions.session_id = error_records.session_id
                  )
            """)
            conn.commit()
            conn.execute("PRAGMA foreign_keys=ON")
            conn.close()

        logger.info("Database schema ready at %s (WAL + FTS5)", self.db_path)

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str,
                        columns: Dict[str, str]) -> None:
        existing = {
            row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {definition}')

    @staticmethod
    def _repair_eval_cases_interaction_fk(conn: sqlite3.Connection) -> None:
        """Rebuild legacy eval_cases tables whose added column has no FK."""
        foreign_keys = conn.execute(
            'PRAGMA foreign_key_list("eval_cases")'
        ).fetchall()
        if any(
            row[2] == "interactions"
            and row[3] == "interaction_id"
            and row[4] == "id"
            for row in foreign_keys
        ):
            return

        has_interactions = conn.execute("""
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'interactions'
        """).fetchone() is not None
        interaction_id = "NULL"
        if has_interactions:
            interaction_id = """
                CASE
                    WHEN source.interaction_id IS NULL THEN NULL
                    WHEN NOT EXISTS (
                        SELECT 1 FROM interactions
                        WHERE interactions.id = source.interaction_id
                    ) THEN NULL
                    WHEN EXISTS (
                        SELECT 1 FROM eval_cases AS earlier
                        WHERE earlier.interaction_id = source.interaction_id
                          AND earlier.id < source.id
                    ) THEN NULL
                    ELSE source.interaction_id
                END
            """

        replacement = "eval_cases__fk_repair"
        conn.execute(f'DROP TABLE IF EXISTS "{replacement}"')
        conn.execute(f"""
            CREATE TABLE "{replacement}" (
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
            )
        """)
        conn.execute(f"""
            INSERT INTO "{replacement}" (
                id, interaction_id, user_input, agent_response, expected,
                feedback_score, source, created_at, used_in_evolution
            )
            SELECT
                source.id, {interaction_id}, source.user_input,
                source.agent_response, source.expected, source.feedback_score,
                source.source, source.created_at, source.used_in_evolution
            FROM eval_cases AS source
            ORDER BY source.id
        """)
        conn.execute('DROP TABLE "eval_cases"')
        conn.execute(
            f'ALTER TABLE "{replacement}" RENAME TO "eval_cases"'
        )

    @staticmethod
    def _archive_missing_sessions(conn: sqlite3.Connection, source_table: str,
                                  timestamp_column: str) -> None:
        """Attach preserved legacy rows to non-resumable archive sessions."""
        conn.execute(f"""
            INSERT OR IGNORE INTO users (id)
            SELECT DISTINCT 'legacy:' || source.session_id
            FROM "{source_table}" AS source
            WHERE source.session_id IS NOT NULL
        """)
        conn.execute(f"""
            INSERT OR IGNORE INTO sessions (
                session_id, user_id, platform, started_at
            )
            SELECT
                source.session_id,
                'legacy:' || source.session_id,
                'legacy',
                MIN(source."{timestamp_column}")
            FROM "{source_table}" AS source
            WHERE source.session_id IS NOT NULL
            GROUP BY source.session_id
        """)

    @staticmethod
    def _repair_interactions_session_fk(conn: sqlite3.Connection) -> None:
        """Preserve legacy interactions and add their missing session FK."""
        foreign_keys = conn.execute(
            'PRAGMA foreign_key_list("interactions")'
        ).fetchall()
        if any(
            row[2] == "sessions"
            and row[3] == "session_id"
            and row[4] == "session_id"
            for row in foreign_keys
        ):
            return

        LearningDatabaseSchema._archive_missing_sessions(
            conn, "interactions", "timestamp"
        )
        replacement = "interactions__fk_repair"
        conn.execute(f'DROP TABLE IF EXISTS "{replacement}"')
        conn.execute(f"""
            CREATE TABLE "{replacement}" (
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
            )
        """)
        conn.execute(f"""
            INSERT INTO "{replacement}" (
                id, session_id, timestamp, interaction_type, user_input,
                agent_response, context, metadata, created_at
            )
            SELECT
                id, session_id, timestamp, interaction_type, user_input,
                agent_response, context, metadata, created_at
            FROM interactions
        """)
        conn.execute('DROP TABLE "interactions"')
        conn.execute(
            f'ALTER TABLE "{replacement}" RENAME TO "interactions"'
        )

    @staticmethod
    def _repair_user_feedback_foreign_keys(conn: sqlite3.Connection) -> None:
        """Add session ownership to feedback created by legacy releases."""
        foreign_keys = conn.execute(
            'PRAGMA foreign_key_list("user_feedback")'
        ).fetchall()
        has_session_fk = any(
            row[2] == "sessions"
            and row[3] == "session_id"
            and row[4] == "session_id"
            for row in foreign_keys
        )
        has_interaction_fk = any(
            row[2] == "interactions"
            and row[3] == "interaction_id"
            and row[4] == "id"
            for row in foreign_keys
        )
        if has_session_fk and has_interaction_fk:
            return

        LearningDatabaseSchema._archive_missing_sessions(
            conn, "user_feedback", "timestamp"
        )
        replacement = "user_feedback__fk_repair"
        conn.execute(f'DROP TABLE IF EXISTS "{replacement}"')
        conn.execute(f"""
            CREATE TABLE "{replacement}" (
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
            )
        """)
        conn.execute(f"""
            INSERT INTO "{replacement}" (
                id, session_id, interaction_id, feedback_type,
                feedback_value, feedback_text, implicit_signals, timestamp,
                created_at
            )
            SELECT
                source.id,
                source.session_id,
                CASE
                    WHEN source.interaction_id IS NULL THEN NULL
                    WHEN EXISTS (
                        SELECT 1 FROM interactions
                        WHERE interactions.id = source.interaction_id
                    ) THEN source.interaction_id
                    ELSE NULL
                END,
                source.feedback_type,
                source.feedback_value,
                source.feedback_text,
                source.implicit_signals,
                source.timestamp,
                source.created_at
            FROM user_feedback AS source
        """)
        conn.execute('DROP TABLE "user_feedback"')
        conn.execute(
            f'ALTER TABLE "{replacement}" RENAME TO "user_feedback"'
        )

    @staticmethod
    def _repair_legacy_user_foreign_keys(conn: sqlite3.Connection) -> None:
        """Rebuild tables that referenced the non-unique sessions.user_id."""
        tables = ("learning_state", "operation_patterns", "app_preferences")
        conn.execute("""
            INSERT OR IGNORE INTO users (id)
            SELECT DISTINCT user_id FROM sessions
            WHERE user_id IS NOT NULL AND user_id != ''
        """)
        session_foreign_keys = conn.execute(
            'PRAGMA foreign_key_list("sessions")'
        ).fetchall()
        has_session_user_fk = any(
            row[2] == "users" and row[3] == "user_id" and row[4] == "id"
            for row in session_foreign_keys
        )
        if not has_session_user_fk:
            LearningDatabaseSchema._rebuild_sessions_user_fk(conn)

        for table in tables:
            conn.execute(f"""
                INSERT OR IGNORE INTO users (id)
                SELECT DISTINCT user_id FROM "{table}"
                WHERE user_id IS NOT NULL AND user_id != ''
            """)
            foreign_keys = conn.execute(
                f'PRAGMA foreign_key_list("{table}")'
            ).fetchall()
            has_legacy_fk = any(
                row[2] == "sessions" and row[3] == "user_id"
                for row in foreign_keys
            )
            if not has_legacy_fk:
                continue

            create_row = conn.execute("""
                SELECT sql FROM sqlite_master
                WHERE type = 'table' AND name = ?
            """, (table,)).fetchone()
            if not create_row or not create_row[0]:
                continue
            replacement = f"{table}__fk_repair"
            create_sql = re.sub(
                rf'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"?{re.escape(table)}"?',
                f'CREATE TABLE "{replacement}"',
                create_row[0],
                count=1,
                flags=re.IGNORECASE,
            )
            create_sql = re.sub(
                r'FOREIGN\s+KEY\s*\(\s*user_id\s*\)\s*'
                r'REFERENCES\s+sessions\s*\(\s*user_id\s*\)',
                'FOREIGN KEY (user_id) REFERENCES users(id)',
                create_sql,
                flags=re.IGNORECASE,
            )
            conn.execute(f'DROP TABLE IF EXISTS "{replacement}"')
            conn.execute(create_sql)
            columns = [
                row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')
            ]
            quoted = ", ".join(f'"{name}"' for name in columns)
            conn.execute(
                f'INSERT INTO "{replacement}" ({quoted}) '
                f'SELECT {quoted} FROM "{table}"'
            )
            conn.execute(f'DROP TABLE "{table}"')
            conn.execute(f'ALTER TABLE "{replacement}" RENAME TO "{table}"')

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_app_name ON app_preferences(app_name)"
        )

    @staticmethod
    def _rebuild_sessions_user_fk(conn: sqlite3.Connection) -> None:
        """Add the users foreign key to a legacy sessions table."""
        create_row = conn.execute("""
            SELECT sql FROM sqlite_master
            WHERE type = 'table' AND name = 'sessions'
        """).fetchone()
        if not create_row or not create_row[0]:
            return

        replacement = "sessions__fk_repair"
        create_sql = re.sub(
            r'^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"?sessions"?',
            f'CREATE TABLE "{replacement}"',
            create_row[0],
            count=1,
            flags=re.IGNORECASE,
        )
        create_sql = re.sub(
            r'\)\s*$',
            ',\nFOREIGN KEY (user_id) REFERENCES users(id)\n)',
            create_sql,
            count=1,
        )
        conn.execute(f'DROP TABLE IF EXISTS "{replacement}"')
        conn.execute(create_sql)
        columns = [
            row[1] for row in conn.execute('PRAGMA table_info("sessions")')
        ]
        quoted = ", ".join(f'"{name}"' for name in columns)
        conn.execute(
            f'INSERT INTO "{replacement}" ({quoted}) '
            f'SELECT {quoted} FROM "sessions"'
        )
        conn.execute('DROP TABLE "sessions"')
        conn.execute(f'ALTER TABLE "{replacement}" RENAME TO "sessions"')
