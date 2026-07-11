"""
记忆桥接层 - 把三层记忆系统接入对话流

职责：
1. 从用户消息中抽取可长期记忆的事实（姓名/偏好/日程/重要约束）
2. 从长期记忆中检索与当前问题相关的事实，拼进 system prompt
3. 命中记忆时强化重要性，未命中时按需写入

设计取舍：
- 事实抽取走 LLM（小模型即可，这里用 astron-code-latest），不走规则——
  规则覆盖面太窄，"我叫张三"和"我通常下午3点开会"句式千变万化。
- 检索走 LongTermMemory 的有界关键词查询，不声明未实现的语义嵌入。
- key 用"类型:主键"格式（如 "user_name" / "preference:编程语言"），
  方便精确 retrieve；同时存一份原文便于 search 检索。
"""

import json
import hashlib
import re
import threading
import time
from typing import Dict, List, Optional

from jarvis.memory.system import MemorySystem
from jarvis.core.llm import LLMConfig, get_llm


_FACT_EXTRACTION_PROMPT = """你的任务是从用户消息中抽取可长期记忆的事实。

只抽取符合以下条件的事实：
- 关于用户身份的稳定信息：姓名、年龄、职业、所在地
- 用户偏好：语言、技术栈、饮食、作息、沟通风格
- 重要约束：日程、截止日期、长期目标
- 用户明确告知的背景知识（项目名、团队结构等）

不要抽取：
- 临时性/一次性的问题（"今天天气"）
- 对当前任务的指令（"帮我写个函数"）
- 模糊无 specifics 的寒暄（"你好"）

输出格式（严格 JSON，无则返回空数组）：
[
  {"key": "user_name", "value": "张三", "type": "identity"},
  {"key": "preference:编程语言", "value": "Python", "type": "preference"}
]

key 命名规范：
- 身份类用 user_name / user_age / user_location / user_occupation
- 偏好类用 preference:<具体维度>，如 preference:编程语言
- 日程类用 schedule:<事件>，如 schedule:周会
- 其他用 misc:<简短描述>

用户消息：
{user_msg}

之前的对话上下文（仅供理解，不要从中抽取，只从最新用户消息抽取）：
{recent_context}

只输出 JSON 数组，不要任何解释。"""


class MemoryBridge:
    """Thread-safe bridge between conversations and the memory system."""

    def __init__(self, db_path: str = "data/jarvis_learning.db", llm: Optional[LLMConfig] = None):
        self.memory = MemorySystem(db_path)
        # 复用全局 LLM 实例，避免重复初始化
        self.llm = llm or get_llm()
        self._retrieve_lock = threading.RLock()

    @classmethod
    def get_instance(cls, db_path: str = "data/jarvis_learning.db") -> "MemoryBridge":
        return get_memory_bridge(db_path)

    # -------- 事实抽取 --------

    def extract_facts(self, user_message: str, recent_context: str = "") -> List[Dict]:
        """从用户消息中抽取可记忆的事实列表。

        Returns:
            [{"key": ..., "value": ..., "type": ...}, ...]
            抽取失败或无事实返回空列表。
        """
        if not user_message or not user_message.strip():
            return []

        # 注意：prompt 里有 JSON 花括号，不能用 .format()，用字符串替换
        prompt = (_FACT_EXTRACTION_PROMPT
                  .replace("{user_msg}", user_message)
                  .replace("{recent_context}", recent_context or "(无)"))

        try:
            raw = self.llm.chat_completion([
                {"role": "user", "content": prompt}
            ], temperature=0.1)
        except Exception as e:
            print(f"[MemoryBridge] 事实抽取 LLM 调用失败: {e}")
            return []

        facts = self._parse_json_array(raw)
        if not facts:
            return []

        # 过滤掉字段不全的项
        valid = []
        for f in facts:
            if isinstance(f, dict) and f.get("key") and f.get("value") is not None:
                valid.append({
                    "key": str(f["key"]).strip(),
                    "value": f["value"],
                    "type": str(f.get("type", "misc")).strip(),
                })
        return valid

    def store_facts(self, facts: List[Dict], namespace: str = "default") -> int:
        """把抽取的事实存入长期记忆。

        已存在的 key 会更新 value 并强化重要性（reinforce）。
        返回成功写入/更新的条数。
        """
        if not facts:
            return 0

        written = 0
        for f in facts:
            raw_key = f["key"]
            key = self._scoped_key(namespace, raw_key)
            value = f["value"]
            ftype = f.get("type", "misc")

            # 重要度：身份类最高（永远记得），偏好次之，misc 较低
            importance = 0.8 if ftype == "identity" else 0.6 if ftype == "preference" else 0.5

            try:
                # 先查是否已存在
                existing = self.memory.long_term.retrieve(key)
                if existing is not None:
                    # 已存在：强化（提高 importance、更新 last_reinforced）
                    self.memory.long_term.reinforce_memory(key, reinforcement_factor=0.1)
                    # 更新 value（覆盖式）
                    self.memory.store(key, value, memory_tier="long_term",
                                      importance=importance,
                                      metadata={"type": ftype, "key": raw_key,
                                                "updated_at": time.time()})
                else:
                    self.memory.store(key, value, memory_tier="long_term",
                                      importance=importance,
                                      metadata={"type": ftype, "key": raw_key,
                                                "source": "dialog"})
                written += 1
            except Exception as e:
                print(f"[MemoryBridge] 存储事实失败 key={raw_key}: {e}")
        return written

    # -------- 相关记忆检索 --------

    def retrieve_relevant(self, user_message: str, max_items: int = 5,
                          namespace: str = "default") -> List[Dict]:
        """检索与当前用户问题相关的长期记忆。

        流程：
        1. 本地提取稳定键和搜索词，避免在主回答前增加一次模型调用
        2. 对每个查询词先精确 retrieve，再在当前用户命名空间内搜索
        3. 去重、按相关度排序，返回最多 max_items 条

        Returns:
            [{"key": ..., "value": ..., "type": ..., "source": "exact"/"search"}]
        """
        if not user_message or not user_message.strip():
            return []

        results: Dict[str, Dict] = {}  # unscoped key -> item
        prefix = self._scope_prefix(namespace)
        queries = self._query_candidates(user_message)

        with self._retrieve_lock:
            for q in queries:
                if not isinstance(q, str) or not q.strip():
                    continue
                q = q.strip()

                # 精确 retrieve（key 完全匹配）
                try:
                    scoped_query = prefix + q
                    val = self.memory.long_term.retrieve(scoped_query)
                    if val is not None and q not in results:
                        results[q] = {
                            "key": q,
                            "value": val,
                            "type": self._get_value_type(q),
                            "source": "exact",
                        }
                except Exception:
                    pass

                # 模糊 search
                try:
                    hits = self.memory.long_term.search(
                        q, limit=max(10, max_items * 2), key_prefix=prefix
                    )
                    for hit_key, hit_val, score in hits:
                        if not hit_key.startswith(prefix):
                            continue
                        raw_key = hit_key[len(prefix):]
                        if raw_key not in results and score > 0.1:
                            results[raw_key] = {
                                "key": raw_key,
                                "value": hit_val,
                                "type": self._get_value_type(raw_key),
                                "source": "search",
                                "score": float(score),
                            }
                except Exception:
                    pass

        # 排序：exact 优先，然后按 score
        ordered = sorted(
            results.values(),
            key=lambda x: (0 if x["source"] == "exact" else 1, -x.get("score", 0.5)),
        )
        return ordered[:max_items]

    @staticmethod
    def _query_candidates(user_message: str) -> List[str]:
        """Derive bounded local lookup terms for common personal-memory queries."""
        text = user_message.strip()
        lowered = text.lower()
        candidates: List[str] = []
        aliases = (
            (("姓名", "名字", "叫什么", "my name", "who am i"), "user_name"),
            (("年龄", "几岁", "多大", "my age"), "user_age"),
            (("住哪", "所在地", "位置", "my location"), "user_location"),
            (("职业", "工作", "做什么的", "my job"), "user_occupation"),
            (("喜欢", "偏好", "常用", "习惯"), "preference:"),
            (("日程", "安排", "会议", "几点", "什么时候"), "schedule:"),
        )
        for phrases, key in aliases:
            if any(phrase in lowered for phrase in phrases):
                candidates.append(key)

        candidates.append(text[:80])
        candidates.extend(
            token[:64]
            for token in re.findall(r"[A-Za-z0-9_:+.-]{2,}", text)
        )

        unique = []
        seen = set()
        for candidate in candidates:
            candidate = candidate.strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                unique.append(candidate)
        return unique[:6]

    def format_for_prompt(self, memories: List[Dict]) -> str:
        """把检索到的记忆格式化成可塞进 system prompt 的文本。"""
        if not memories:
            return ""
        lines = ["[关于用户的已知信息]"]
        for m in memories:
            val = m.get("value")
            if isinstance(val, (dict, list)):
                val = json.dumps(val, ensure_ascii=False)
            lines.append(f"- {m['key']}: {val}")
        return "\n".join(lines)

    # -------- 工具方法 --------

    def _get_value_type(self, key: str) -> str:
        """从 key 推断事实类型"""
        if key.startswith("user_"):
            return "identity"
        if key.startswith("preference:"):
            return "preference"
        if key.startswith("schedule:"):
            return "schedule"
        return "misc"

    @staticmethod
    def _scope_prefix(namespace: str) -> str:
        namespace_hash = hashlib.sha256(
            (namespace or "default").encode("utf-8")
        ).hexdigest()[:24]
        return f"user:{namespace_hash}:"

    @classmethod
    def _scoped_key(cls, namespace: str, key: str) -> str:
        return cls._scope_prefix(namespace) + key

    @classmethod
    def scoped_key(cls, namespace: str, key: str) -> str:
        """Return a storage key isolated to one browser/user namespace."""
        return cls._scoped_key(namespace, key)

    def _parse_json_array(self, raw: str) -> List:
        """从 LLM 输出里抠出 JSON 数组。容错：去 markdown 围栏、找首个 [ 到末尾 ]。"""
        if not raw:
            return []
        text = raw.strip()
        # 去 markdown 围栏
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        # 找首个 [ 到最后一个 ]
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end < 0 or end <= start:
            return []
        snippet = text[start:end + 1]
        try:
            parsed = json.loads(snippet)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            # 容错：尝试去掉尾逗号
            try:
                cleaned = re.sub(r",\s*([\]\}])", r"\1", snippet)
                parsed = json.loads(cleaned)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []


# Keep one bridge per database path. A single process may host test and runtime
# databases, so one global instance is not sufficient.
_memory_bridges: Dict[str, MemoryBridge] = {}
_memory_bridges_lock = threading.Lock()


def get_memory_bridge(db_path: str = "data/jarvis_learning.db",
                      llm: Optional[LLMConfig] = None) -> MemoryBridge:
    cache_key = str(db_path)
    with _memory_bridges_lock:
        bridge = _memory_bridges.get(cache_key)
        if bridge is None:
            bridge = MemoryBridge(cache_key, llm=llm)
            _memory_bridges[cache_key] = bridge
        return bridge
