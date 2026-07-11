"""
贾维斯自学习系统 - 三层记忆系统
继承Hermes架构: WAL模式 + FTS5 + 线程安全

三层记忆架构:
- ImmediateMemory: 即时记忆(容量1000, <1ms)
- ShortTermMemory: 短期记忆(容量5000, <10ms)
- LongTermMemory: 长期记忆(容量10000, <50ms)
"""

import threading
import time
import sqlite3
import json
from collections import OrderedDict
from typing import Dict, Any, Optional, List, Tuple


class ImmediateMemory:
    """
    即时记忆 - 线程安全LRU缓存
    容量: 1000
    延迟目标: < 1ms
    特性: TTL过期、溢出到短期记忆
    """

    def __init__(self, capacity: int = 1000, ttl_seconds: int = 300):
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()
        self._cache: OrderedDict[str, Dict] = OrderedDict()
        self._overflow_callback = None

    def set_overflow_callback(self, callback):
        """设置溢出回调函数(溢出到短期记忆)"""
        self._overflow_callback = callback

    def store(self, key: str, value: Any, metadata: Optional[Dict] = None) -> bool:
        """
        存储即时记忆 - 线程安全LRU
        """
        overflow_item = None
        with self._lock:
            # Updating an existing key must not evict an unrelated entry.
            self._cache.pop(key, None)

            # 检查容量,执行LRU淘汰
            if len(self._cache) >= self.capacity:
                # 淘汰最旧的项
                overflow_item = self._cache.popitem(last=False)

            # 存储新项
            self._cache[key] = {
                'value': value,
                'metadata': metadata or {},
                'timestamp': time.time(),
                'ttl': self.ttl_seconds
            }

            # 移到末尾(最近使用)
            self._cache.move_to_end(key)

        # Avoid holding the in-memory cache lock during a SQLite write.
        if overflow_item and self._overflow_callback:
            self._overflow_callback(*overflow_item)
        return True

    def retrieve(self, key: str) -> Optional[Any]:
        """
        检索即时记忆 - <1ms延迟
        """
        with self._lock:
            if key not in self._cache:
                return None

            item = self._cache[key]

            # 检查TTL过期
            if time.time() - item['timestamp'] > item['ttl']:
                del self._cache[key]
                return None

            # 更新LRU顺序
            self._cache.move_to_end(key)
            return item['value']

    def get_all_keys(self) -> List[str]:
        """获取所有键(用于预取)"""
        with self._lock:
            return list(self._cache.keys())

    def clear_expired(self) -> int:
        """清理过期项"""
        with self._lock:
            current_time = time.time()
            expired_keys = []

            for key, item in self._cache.items():
                if current_time - item['timestamp'] > item['ttl']:
                    expired_keys.append(key)

            for key in expired_keys:
                del self._cache[key]

            return len(expired_keys)


class ShortTermMemory:
    """
    短期记忆 - SQLite WAL模式
    容量: 5000
    延迟目标: < 10ms
    特性: FTS5全文搜索、FIFO淘汰、压缩机制
    """

    def __init__(self, db_path: str = "jarvis_learning.db", capacity: int = 5000):
        self.db_path = db_path
        self.capacity = capacity
        self._lock = threading.RLock()
        self._init_database()

    def _init_database(self):
        """初始化短期记忆数据库 - WAL模式"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            # 短期记忆表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS short_term_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    value JSON NOT NULL,
                    metadata JSON,
                    importance REAL DEFAULT 0.5,
                    access_count INTEGER DEFAULT 0,
                    created_at REAL NOT NULL,
                    last_accessed REAL NOT NULL,
                    expires_at REAL
                )
            """)

            # FTS5全文搜索 - 双tokenizer
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS short_term_memory_fts
                USING fts5(key, value, tokenize='unicode61')
            """)

            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS short_term_memory_fts_trigram
                USING fts5(key, value, tokenize='trigram')
            """)

            # Normalize legacy escaped JSON and rebuild contentless FTS tables so
            # stale documents from previous upserts cannot survive an upgrade.
            rows = conn.execute(
                "SELECT key, value, metadata FROM short_term_memory"
            ).fetchall()
            conn.execute("DELETE FROM short_term_memory_fts")
            conn.execute("DELETE FROM short_term_memory_fts_trigram")
            for key, value_json, metadata_json in rows:
                try:
                    normalized_value = json.dumps(
                        json.loads(value_json), ensure_ascii=False
                    )
                    normalized_metadata = json.dumps(
                        json.loads(metadata_json or '{}'), ensure_ascii=False
                    )
                except (TypeError, json.JSONDecodeError):
                    normalized_value = value_json
                    normalized_metadata = metadata_json or '{}'
                conn.execute("""
                    UPDATE short_term_memory SET value = ?, metadata = ? WHERE key = ?
                """, (normalized_value, normalized_metadata, key))
                conn.execute(
                    "INSERT INTO short_term_memory_fts (key, value) VALUES (?, ?)",
                    (key, normalized_value),
                )
                conn.execute("""
                    INSERT INTO short_term_memory_fts_trigram (key, value)
                    VALUES (?, ?)
                """, (key, normalized_value))

            conn.commit()
            conn.close()

    def store(self, key: str, value: Any, metadata: Optional[Dict] = None,
              importance: float = 0.5, ttl_seconds: Optional[int] = None) -> bool:
        """
        存储短期记忆 - SQLite WAL模式
        """
        current_time = time.time()
        expires_at = current_time + ttl_seconds if ttl_seconds else None

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("BEGIN IMMEDIATE")

            try:
                exists = conn.execute(
                    "SELECT 1 FROM short_term_memory WHERE key = ?", (key,)
                ).fetchone()
                cursor = conn.execute("SELECT COUNT(*) FROM short_term_memory")
                count = cursor.fetchone()[0]

                if not exists and count >= self.capacity:
                    # 淘汰最旧且重要性最低的10%
                    evict_count = max(1, int(self.capacity * 0.1))
                    evicted_keys = [
                        row[0]
                        for row in conn.execute("""
                            SELECT key FROM short_term_memory
                            ORDER BY importance ASC, created_at ASC
                            LIMIT ?
                        """, (evict_count,)).fetchall()
                    ]
                    for evicted_key in evicted_keys:
                        conn.execute(
                            "DELETE FROM short_term_memory WHERE key = ?",
                            (evicted_key,),
                        )
                        self._delete_fts_rows(conn, evicted_key)

                # Upsert without REPLACE, which would delete and recreate the row.
                conn.execute("""
                    INSERT INTO short_term_memory
                    (key, value, metadata, importance, access_count,
                     created_at, last_accessed, expires_at)
                    VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        metadata = excluded.metadata,
                        importance = excluded.importance,
                        last_accessed = excluded.last_accessed,
                        expires_at = excluded.expires_at
                """, (key, json.dumps(value, ensure_ascii=False),
                      json.dumps(metadata or {}, ensure_ascii=False),
                      importance, current_time, current_time, expires_at))

                # FTS virtual tables have no UNIQUE key constraint, so remove the
                # previous document before indexing the current value.
                self._delete_fts_rows(conn, key)
                conn.execute("""
                    INSERT INTO short_term_memory_fts (key, value)
                    VALUES (?, ?)
                """, (key, json.dumps(value, ensure_ascii=False)))

                conn.execute("""
                    INSERT INTO short_term_memory_fts_trigram (key, value)
                    VALUES (?, ?)
                """, (key, json.dumps(value, ensure_ascii=False)))

                conn.commit()
                return True

            except Exception as e:
                conn.rollback()
                raise e
            finally:
                conn.close()

    @staticmethod
    def _delete_fts_rows(conn: sqlite3.Connection, key: str) -> None:
        conn.execute("DELETE FROM short_term_memory_fts WHERE key = ?", (key,))
        conn.execute(
            "DELETE FROM short_term_memory_fts_trigram WHERE key = ?", (key,)
        )

    def retrieve(self, key: str) -> Optional[Any]:
        """
        检索短期记忆 - <10ms延迟
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)

            cursor = conn.execute("""
                SELECT value, expires_at FROM short_term_memory
                WHERE key = ?
            """, (key,))

            row = cursor.fetchone()
            conn.close()

            if not row:
                return None

            value_json, expires_at = row

            # 检查过期
            if expires_at and time.time() > expires_at:
                self.delete(key)
                return None

            # 更新访问计数和最后访问时间
            self._update_access_stats(key)

            return json.loads(value_json)

    def search(self, query: str, limit: int = 10) -> List[Tuple[str, Any, float]]:
        """
        全文搜索 - FTS5双tokenizer
        返回: [(key, value, relevance_score), ...]
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)

            # 使用unicode61搜索
            safe_query = '"' + query.replace('"', '""') + '"'
            cursor = conn.execute("""
                SELECT key, value, bm25(short_term_memory_fts) as score
                FROM short_term_memory_fts
                WHERE short_term_memory_fts MATCH ?
                ORDER BY score ASC
                LIMIT ?
            """, (safe_query, limit))

            results = []
            for row in cursor.fetchall():
                key, value_json, score = row
                results.append((key, json.loads(value_json), -score))  # 负号转换为正相关

            conn.close()
            return results

    def _update_access_stats(self, key: str):
        """更新访问统计"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                UPDATE short_term_memory
                SET access_count = access_count + 1,
                    last_accessed = ?
                WHERE key = ?
            """, (time.time(), key))
            conn.commit()
            conn.close()

    def delete(self, key: str) -> bool:
        """删除记忆"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("BEGIN IMMEDIATE")

            conn.execute("DELETE FROM short_term_memory WHERE key = ?", (key,))
            self._delete_fts_rows(conn, key)

            conn.commit()
            conn.close()
            return True


class LongTermMemory:
    """
    长期记忆 - SQLite + 有界关键词检索
    容量: 10000
    延迟目标: < 50ms
    特性: 关键词搜索、重要性衰减、知识图谱关联
    """

    def __init__(self, db_path: str = "jarvis_learning.db", capacity: int = 10000):
        self.db_path = db_path
        self.capacity = capacity
        self._lock = threading.RLock()
        self._init_database()

    def _init_database(self):
        """初始化长期记忆数据库"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            legacy_index = conn.execute("""
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'embedding_index'
            """).fetchone()
            legacy_index_rows = (
                conn.execute("SELECT COUNT(*) FROM embedding_index").fetchone()[0]
                if legacy_index else 0
            )
            if legacy_index and legacy_index_rows == 0:
                conn.execute("DROP TABLE embedding_index")

            # 长期记忆表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS long_term_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE NOT NULL,
                    value JSON NOT NULL,
                    metadata JSON,

                    -- 重要性评分
                    importance REAL DEFAULT 0.5,
                    importance_decay REAL DEFAULT 0.0,

                    -- 时间信息
                    created_at REAL NOT NULL,
                    last_accessed REAL NOT NULL,
                    last_reinforced REAL,

                    -- 知识图谱关联
                    knowledge_node_id TEXT,

                    -- 统计信息
                    access_count INTEGER DEFAULT 0,
                    reinforcement_count INTEGER DEFAULT 0
                )
            """)
            self._remove_legacy_embedding_column(conn, legacy_index_rows)

            for key, value_json, metadata_json in conn.execute(
                "SELECT key, value, metadata FROM long_term_memory"
            ).fetchall():
                try:
                    normalized_value = json.dumps(
                        json.loads(value_json), ensure_ascii=False
                    )
                    normalized_metadata = json.dumps(
                        json.loads(metadata_json or '{}'), ensure_ascii=False
                    )
                except (TypeError, json.JSONDecodeError):
                    continue
                if normalized_value != value_json or normalized_metadata != metadata_json:
                    conn.execute("""
                        UPDATE long_term_memory SET value = ?, metadata = ? WHERE key = ?
                    """, (normalized_value, normalized_metadata, key))

            conn.commit()
            conn.close()

    @staticmethod
    def _remove_legacy_embedding_column(
        conn: sqlite3.Connection, legacy_index_rows: int
    ) -> None:
        columns = {
            row[1] for row in conn.execute('PRAGMA table_info("long_term_memory")')
        }
        if "embedding" not in columns:
            return
        embedded_rows = conn.execute("""
            SELECT COUNT(*) FROM long_term_memory WHERE embedding IS NOT NULL
        """).fetchone()[0]
        if embedded_rows or legacy_index_rows:
            # Preserve legacy vectors even though this release does not query
            # them. Destructive cleanup requires an explicit offline migration.
            return

        replacement = "long_term_memory__keyword_only"
        conn.execute(f'DROP TABLE IF EXISTS "{replacement}"')
        conn.execute(f"""
            CREATE TABLE "{replacement}" (
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
            )
        """)
        conn.execute(f"""
            INSERT INTO "{replacement}" (
                id, key, value, metadata, importance, importance_decay,
                created_at, last_accessed, last_reinforced,
                knowledge_node_id, access_count, reinforcement_count
            )
            SELECT
                id, key, value, metadata, importance, importance_decay,
                created_at, last_accessed, last_reinforced,
                knowledge_node_id, access_count, reinforcement_count
            FROM long_term_memory
        """)
        conn.execute('DROP TABLE "long_term_memory"')
        conn.execute(
            f'ALTER TABLE "{replacement}" RENAME TO "long_term_memory"'
        )

    def store(self, key: str, value: Any, metadata: Optional[Dict] = None,
              importance: float = 0.5, knowledge_node_id: Optional[str] = None) -> bool:
        """
        存储长期记忆
        """
        current_time = time.time()

        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("BEGIN IMMEDIATE")

            try:
                exists = conn.execute(
                    "SELECT 1 FROM long_term_memory WHERE key = ?", (key,)
                ).fetchone()
                cursor = conn.execute("SELECT COUNT(*) FROM long_term_memory")
                count = cursor.fetchone()[0]

                if not exists and count >= self.capacity:
                    # 淘汰重要性最低的10%
                    conn.execute("""
                        DELETE FROM long_term_memory
                        WHERE id IN (
                            SELECT id FROM long_term_memory
                            ORDER BY importance ASC, last_accessed ASC
                            LIMIT ?
                        )
                    """, (max(1, int(self.capacity * 0.1)),))

                # 插入长期记忆
                conn.execute("""
                    INSERT INTO long_term_memory
                    (key, value, metadata, importance, importance_decay,
                     created_at, last_accessed, knowledge_node_id)
                    VALUES (?, ?, ?, ?, 0.0, ?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        metadata = excluded.metadata,
                        importance = MAX(long_term_memory.importance, excluded.importance),
                        last_accessed = excluded.last_accessed,
                        knowledge_node_id = excluded.knowledge_node_id
                """, (key, json.dumps(value, ensure_ascii=False),
                      json.dumps(metadata or {}, ensure_ascii=False),
                      importance, current_time, current_time, knowledge_node_id))

                conn.commit()
                return True

            except Exception as e:
                conn.rollback()
                raise e
            finally:
                conn.close()

    def retrieve(self, key: str) -> Optional[Any]:
        """
        检索长期记忆 - <50ms延迟
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)

            cursor = conn.execute("""
                SELECT value, importance, importance_decay
                FROM long_term_memory
                WHERE key = ?
            """, (key,))

            row = cursor.fetchone()
            conn.close()

            if not row:
                return None

            value_json, importance, decay = row

            # 应用重要性衰减
            current_importance = importance - decay
            if current_importance < 0.1:
                # 重要性过低,考虑删除
                return None

            # 更新访问统计
            self._update_access_stats(key)

            return json.loads(value_json)

    def search(self, query: str, limit: int = 10,
               key_prefix: Optional[str] = None) -> List[Tuple[str, Any, float]]:
        """Run a bounded keyword lookup, optionally inside one key prefix."""
        with self._lock:
            conn = sqlite3.connect(self.db_path)

            if key_prefix:
                cursor = conn.execute("""
                    SELECT key, value
                    FROM long_term_memory
                    WHERE key LIKE ? AND (key LIKE ? OR value LIKE ?)
                    LIMIT ?
                """, (
                    f"{key_prefix}%", f"%{query}%", f"%{query}%", limit,
                ))
            else:
                cursor = conn.execute("""
                    SELECT key, value
                    FROM long_term_memory
                    WHERE key LIKE ? OR value LIKE ?
                    LIMIT ?
                """, (f'%{query}%', f'%{query}%', limit))

            results = [(key, json.loads(value_json), 1.0)
                      for key, value_json in cursor.fetchall()]

            conn.close()
            return results

    def _update_access_stats(self, key: str):
        """更新访问统计"""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                UPDATE long_term_memory
                SET access_count = access_count + 1,
                    last_accessed = ?
                WHERE key = ?
            """, (time.time(), key))
            conn.commit()
            conn.close()

    def reinforce_memory(self, key: str, reinforcement_factor: float = 0.1):
        """
        强化记忆 - 增加重要性
        用于从错误中学习、用户显式反馈
        """
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                UPDATE long_term_memory
                SET importance = MIN(1.0, importance + ?),
                    reinforcement_count = reinforcement_count + 1,
                    last_reinforced = ?
                WHERE key = ?
            """, (reinforcement_factor, time.time(), key))
            conn.commit()
            conn.close()


class MemorySystem:
    """
    三层记忆系统统一管理器
    实现自动溢出、预取机制、统一接口
    """

    def __init__(self, db_path: str = "jarvis_learning.db"):
        # 初始化三层记忆
        self.immediate = ImmediateMemory(capacity=1000, ttl_seconds=300)
        self.short_term = ShortTermMemory(db_path, capacity=5000)
        self.long_term = LongTermMemory(db_path, capacity=10000)

        # 设置溢出回调
        self.immediate.set_overflow_callback(self._overflow_to_short_term)

        # 预取缓存
        self._prefetch_cache: Dict[str, Any] = {}
        self._prefetch_lock = threading.RLock()

    def store(self, key: str, value: Any, memory_tier: str = 'auto',
              importance: float = 0.5, metadata: Optional[Dict] = None) -> bool:
        """
        统一存储接口 - 自动选择层级
        """
        if memory_tier == 'auto':
            # 根据重要性自动选择
            if importance > 0.7:
                memory_tier = 'long_term'
            elif importance > 0.3:
                memory_tier = 'short_term'
            else:
                memory_tier = 'immediate'

        if memory_tier == 'immediate':
            return self.immediate.store(key, value, metadata)
        elif memory_tier == 'short_term':
            return self.short_term.store(key, value, metadata, importance)
        elif memory_tier == 'long_term':
            return self.long_term.store(key, value, metadata, importance)
        else:
            raise ValueError(f"Unknown memory tier: {memory_tier}")

    def retrieve(self, key: str, search_all_tiers: bool = True) -> Optional[Any]:
        """
        统一检索接口 - 自动搜索所有层级
        """
        # 1. 检索即时记忆
        value = self.immediate.retrieve(key)
        if value is not None:
            return value

        if not search_all_tiers:
            return None

        # 2. 检索短期记忆
        value = self.short_term.retrieve(key)
        if value is not None:
            # 提升到即时记忆
            self.immediate.store(key, value)
            return value

        # 3. 检索长期记忆
        value = self.long_term.retrieve(key)
        if value is not None:
            # 提升到短期记忆
            self.short_term.store(key, value, importance=0.6)
            return value

        return None

    def search(self, query: str, limit: int = 10) -> List[Tuple[str, Any, float]]:
        """
        统一搜索接口 - 搜索短期和长期记忆
        """
        # 搜索短期记忆
        short_term_results = self.short_term.search(query, limit)

        # 搜索长期记忆
        long_term_results = self.long_term.search(query, limit)

        # 合并结果
        all_results = short_term_results + long_term_results

        # 去重并排序
        seen_keys = set()
        unique_results = []
        for key, value, score in all_results:
            if key not in seen_keys:
                seen_keys.add(key)
                unique_results.append((key, value, score))

        # 按分数排序
        unique_results.sort(key=lambda x: x[2], reverse=True)

        return unique_results[:limit]

    def _overflow_to_short_term(self, key: str, value: Dict):
        """即时记忆溢出到短期记忆"""
        self.short_term.store(
            key,
            value['value'],
            value.get('metadata'),
            importance=0.4
        )

    def prefetch(self, keys: List[str]):
        """
        预取机制 - 批量加载到即时记忆
        """
        with self._prefetch_lock:
            for key in keys:
                if key in self._prefetch_cache:
                    continue

                value = self.retrieve(key, search_all_tiers=True)
                if value is not None:
                    self._prefetch_cache[key] = value
                    self.immediate.store(key, value)
