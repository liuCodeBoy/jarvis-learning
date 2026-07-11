"""
Skill 挖掘器 - 从对话历史中自动发现可沉淀的 skill

流程：
1. 从 interactions 表扫最近 N 条对话
2. 让 LLM 聚类"哪些诉求反复出现且适合沉淀"
3. 对每个聚类让 LLM 生成 skill 定义（name + 触发条件 + prompt 模板）
4. 存入 skills 表
"""

import json
import sqlite3
from typing import Dict, List, Optional

from jarvis.learning.skills import SkillStore
from jarvis.core.llm import LLMConfig, get_llm


_CLUSTER_PROMPT = """你是一个对话分析专家。下面是用户与 AI 助手最近的对话记录。

请从中找出"反复出现的、适合沉淀为可复用 skill 的对话模式"。

适合沉淀为 skill 的条件：
- 同类问题出现了 2 次及以上
- 回答有明确的模式/格式可提炼
- 不是一次性问题（"帮我算 37*59"不适合，"帮我做数学计算"适合）

不适合沉淀为 skill 的：
- 闲聊/寒暄
- 只出现过 1 次的特殊问题
- 需要实时数据的查询（当前天气、股票价格）

对话记录：
{dialogs}

输出格式（严格 JSON 数组，无合适的返回空数组 []）：
[
  {{
    "name": "skill_english_name",
    "description": "这个 skill 做什么",
    "sample_questions": ["用户可能怎么问1", "用户可能怎么问2"],
    "trigger_keywords": ["关键词1", "关键词2"],
    "prompt_template": "命中后给 LLM 的 system prompt，要具体、有针对性"
  }}
]

注意：
- name 用英文下划线命名（如 math_calc, translation）
- trigger_keywords 要覆盖用户各种问法的关键词
- prompt_template 要具体到这个 skill 的场景，不要写通用废话
"""

_MAX_DIALOGS = 100


class SkillMiner:
    """从对话历史挖掘 skill"""

    def __init__(self, db_path: str = "data/jarvis_learning.db", llm: Optional[LLMConfig] = None):
        self.db_path = db_path
        self.store = SkillStore(db_path)
        self.llm = llm or get_llm()

    def mine(self, max_dialogs: int = _MAX_DIALOGS) -> List[Dict]:
        """执行一次 skill 挖掘。返回挖掘到的新 skill 列表。"""
        dialogs = self._fetch_recent_dialogs(max_dialogs)
        if len(dialogs) < 5:
            print(f"[SkillMiner] 对话太少({len(dialogs)}条)，跳过挖掘")
            return []

        # 格式化对话给 LLM
        dialog_text = ""
        for i, d in enumerate(dialogs[:50], 1):  # 最多给 50 条，省 token
            dialog_text += f"{i}. 用户: {d['user'][:100]}\n   贾维斯: {d['assistant'][:100]}\n"

        prompt = _CLUSTER_PROMPT.replace("{dialogs}", dialog_text)

        try:
            raw = self.llm.chat_completion(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
            )
        except Exception as e:
            print(f"[SkillMiner] LLM 调用失败: {e}")
            return []

        candidates = self._parse_json_array(raw)
        if not candidates:
            return []

        # 存入 skills 表
        new_skills = []
        existing_names = {s["name"] for s in self.store.get_all(enabled_only=False)}

        for c in candidates:
            if not isinstance(c, dict):
                continue
            name = (c.get("name") or "").strip()
            if not name or name in existing_names:
                continue

            keywords = c.get("trigger_keywords", [])
            template = c.get("prompt_template", "")
            desc = c.get("description", "")

            if not keywords or not template:
                continue

            ok = self.store.add(
                name=name,
                description=desc,
                trigger_keywords=keywords,
                trigger_regex=None,
                prompt_template=template,
                enabled=False,
            )
            if ok:
                new_skills.append(c)
                existing_names.add(name)

        if new_skills:
            print(f"[SkillMiner] 挖掘到 {len(new_skills)} 个新 skill: {[s['name'] for s in new_skills]}")
        return new_skills

    def _fetch_recent_dialogs(self, limit: int) -> List[Dict]:
        """从 interactions 表取最近的有效对话"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_input, agent_response FROM interactions
                WHERE user_input IS NOT NULL AND user_input != ''
                  AND agent_response IS NOT NULL AND agent_response != ''
                ORDER BY id DESC LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            conn.close()
            return [{"user": r[0], "assistant": r[1]} for r in rows]
        except Exception as e:
            print(f"[SkillMiner] 读对话失败: {e}")
            return []

    def _parse_json_array(self, raw: str) -> List:
        """容错解析 JSON 数组"""
        if not raw:
            return []
        import re
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end < 0 or end <= start:
            return []
        snippet = text[start:end + 1]
        try:
            parsed = json.loads(snippet)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            cleaned = re.sub(r",\s*([\]\}])", r"\1", snippet)
            try:
                parsed = json.loads(cleaned)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []
