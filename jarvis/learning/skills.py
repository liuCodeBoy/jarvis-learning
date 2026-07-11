"""
Skill 系统 - 可复用能力的沉淀、匹配与调用

核心概念：
- Skill = 触发条件（关键词/正则）+ 专门的 prompt 模板
- 从反复出现的对话模式中自动沉淀
- 命中 skill 时用专门 prompt 调 LLM，比通用 prompt 更精准
"""

import json
import re
import sqlite3
import threading
import time
from typing import Dict, List, Optional


class SkillStore:
    """Skill 的 CRUD 管理"""

    def __init__(self, db_path: str = "data/jarvis_learning.db"):
        self.db_path = db_path
        self._init_table()

    def _init_table(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS skills (
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
            )
        """)
        columns = {
            row[1] for row in conn.execute('PRAGMA table_info("skills")')
        }
        if "reviewed" not in columns:
            conn.execute(
                "ALTER TABLE skills ADD COLUMN reviewed INTEGER DEFAULT 0"
            )
        conn.execute(
            "UPDATE skills SET trigger_regex = NULL WHERE trigger_regex IS NOT NULL"
        )
        conn.execute("UPDATE skills SET enabled = 0 WHERE reviewed = 0")
        conn.commit()
        conn.close()

    def add(self, name: str, description: str,
            trigger_keywords: List[str], trigger_regex: Optional[str],
            prompt_template: str, source_pattern_id: Optional[int] = None,
            enabled: bool = True) -> bool:
        """新增一个 skill。name 唯一，重复则更新。"""
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", name or ""):
            return False
        if not isinstance(trigger_keywords, list):
            return False
        trigger_keywords = [
            keyword.strip() for keyword in trigger_keywords
            if isinstance(keyword, str) and keyword.strip()
        ]
        if not trigger_keywords or len(trigger_keywords) > 20:
            return False
        if any(len(keyword) > 64 for keyword in trigger_keywords):
            return False
        if not isinstance(prompt_template, str) or not 1 <= len(prompt_template) <= 6000:
            return False
        description = str(description or "")[:500]
        # Keep the legacy column for compatibility, but do not execute
        # model-generated backtracking regexes against user-controlled input.
        trigger_regex = None

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("""
                INSERT INTO skills (name, description, trigger_keywords, trigger_regex,
                                    prompt_template, source_pattern_id, created_at,
                                    enabled, reviewed)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    description=excluded.description,
                    trigger_keywords=excluded.trigger_keywords,
                    trigger_regex=excluded.trigger_regex,
                    prompt_template=excluded.prompt_template,
                    enabled=excluded.enabled,
                    reviewed=excluded.reviewed
            """, (
                name, description,
                json.dumps(trigger_keywords, ensure_ascii=False),
                trigger_regex,
                prompt_template,
                source_pattern_id,
                time.time(),
                1 if enabled else 0,
                1 if enabled else 0,
            ))
            conn.commit()
            return True
        except Exception as e:
            print(f"[SkillStore] add 失败: {e}")
            return False
        finally:
            conn.close()

    def get_all(self, enabled_only: bool = True) -> List[Dict]:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        if enabled_only:
            cursor.execute("""
                SELECT id, name, description, trigger_keywords, trigger_regex,
                       prompt_template, trigger_count, last_triggered_at
                FROM skills WHERE enabled = 1 ORDER BY trigger_count DESC
            """)
        else:
            cursor.execute("""
                SELECT id, name, description, trigger_keywords, trigger_regex,
                       prompt_template, trigger_count, last_triggered_at,
                       enabled, reviewed
                FROM skills ORDER BY id DESC
            """)
        rows = cursor.fetchall()
        conn.close()

        result = []
        for r in rows:
            item = {
                "id": r[0], "name": r[1], "description": r[2],
                "trigger_keywords": json.loads(r[3]) if r[3] else [],
                "trigger_regex": r[4], "prompt_template": r[5],
                "trigger_count": r[6], "last_triggered_at": r[7],
            }
            if not enabled_only:
                item["enabled"] = r[8]
                item["reviewed"] = r[9]
            result.append(item)
        return result

    def record_trigger(self, skill_id: int):
        """记录一次 skill 触发"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            UPDATE skills SET trigger_count = trigger_count + 1, last_triggered_at = ?
            WHERE id = ?
        """, (time.time(), skill_id))
        conn.commit()
        conn.close()

    def toggle(self, skill_id: int, enabled: bool) -> bool:
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(
                "UPDATE skills SET enabled = ?, reviewed = 1 WHERE id = ?",
                (1 if enabled else 0, skill_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def delete(self, skill_id: int):
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM skills WHERE id = ?", (skill_id,))
        conn.commit()
        conn.close()


class SkillMatcher:
    """匹配用户消息到已注册 skill"""

    def __init__(self, store: SkillStore):
        self.store = store
        self._cache: Optional[List[Dict]] = None
        self._cache_ts: float = 0.0
        self._cache_lock = threading.RLock()

    def _load_skills(self) -> List[Dict]:
        """加载 skill 列表，5 分钟缓存"""
        with self._cache_lock:
            now = time.time()
            if self._cache is not None and (now - self._cache_ts) < 300:
                return self._cache
            self._cache = self.store.get_all(enabled_only=True)
            self._cache_ts = now
            return self._cache

    def match(self, user_message: str) -> Optional[Dict]:
        """匹配用户消息到 skill。返回命中的 skill dict 或 None。

        关键词命中任意一个即匹配。正则字段仅为旧数据库兼容保留。
        """
        if not user_message or not user_message.strip():
            return None

        skills = self._load_skills()
        msg_lower = user_message.lower()

        for skill in skills:
            keywords = skill.get("trigger_keywords", [])
            for kw in keywords:
                if kw.lower() in msg_lower:
                    return skill

        return None

    def invalidate_cache(self):
        with self._cache_lock:
            self._cache = None
            self._cache_ts = 0.0
