#!/usr/bin/env python3
"""
贾维斯系统 - 交互式访问界面
支持命令行对话和功能演示
"""

import sys
import os
import importlib
import secrets
import time
import sqlite3
from pathlib import Path

# 添加项目根目录到路径
PROJECT_DIR = Path(__file__).parent.parent.parent.resolve()
sys.path.insert(0, str(PROJECT_DIR))

class JarvisInterface:
    """贾维斯交互界面"""

    def __init__(self):
        self.running = True
        configured_db = os.environ.get('JARVIS_DB_PATH')
        self.db_path = (
            Path(configured_db).expanduser()
            if configured_db else PROJECT_DIR / 'data' / 'jarvis_learning.db'
        )
        if not self.db_path.is_absolute():
            self.db_path = (PROJECT_DIR / self.db_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.db_path.parent.chmod(0o700)
        except OSError:
            pass
        from jarvis.database.schema import LearningDatabaseSchema

        LearningDatabaseSchema(str(self.db_path)).initialize_schema()
        try:
            self.db_path.chmod(0o600)
        except OSError:
            pass
        self.commands = {
            'help': self.show_help,
            'status': self.show_status,
            'chat': self.chat_mode,
            'learn': self.learning_demo,
            'evolve': self.evolution_demo,
            'memory': self.memory_demo,
            'stats': self.show_stats,
            'clear': self.clear_screen,
            'exit': self.exit_system,
            'quit': self.exit_system,
        }

        print("=" * 70)
        print("贾维斯自学习自进化系统 V4.1 - 交互界面")
        print("=" * 70)
        print()
        print("✅ 系统已启动,欢迎使用!")
        print()
        print("输入 'help' 查看可用命令")
        print("输入 'chat' 开始对话模式")
        print("输入 'exit' 或 'quit' 退出系统")
        print()

    def show_help(self):
        """显示帮助信息"""
        print()
        print("=" * 70)
        print("可用命令")
        print("=" * 70)
        print()
        print("系统命令:")
        print("  help     - 显示帮助信息")
        print("  status   - 显示系统状态")
        print("  stats    - 显示数据库统计")
        print("  clear    - 清屏")
        print("  exit     - 退出系统")
        print()
        print("功能演示:")
        print("  chat     - 进入对话模式(与已配置模型交互)")
        print("  learn    - 学习系统演示")
        print("  evolve   - Darwinian进化演示")
        print("  memory   - 记忆系统演示")
        print()
        print("=" * 70)
        print()

    def show_status(self):
        """显示系统状态"""
        print()
        print("=" * 70)
        print("系统状态")
        print("=" * 70)
        print()

        # 检查数据库
        if self.db_path.exists():
            db_size = self.db_path.stat().st_size / 1024  # KB
            print(f"✅ 数据库: {self.db_path} ({db_size:.1f} KB)")
        else:
            print("❌ 数据库: 未找到")

        # 检查日志
        log_path = PROJECT_DIR / 'logs' / 'jarvis.log'
        if log_path.exists():
            log_size = log_path.stat().st_size / 1024  # KB
            print(f"✅ 日志文件: {log_path} ({log_size:.1f} KB)")
        else:
            print("❌ 日志文件: 未找到")

        # 检查核心模块
        try:
            for module_name in (
                "jarvis.database.schema",
                "jarvis.memory.system",
                "jarvis.learning.habits",
            ):
                importlib.import_module(module_name)
            print("✅ 核心模块: 全部加载成功")
        except Exception as e:
            print(f"❌ 核心模块: {e}")

        print()
        from jarvis.core.llm import get_llm
        llm = get_llm()
        print("模型配置:")
        print(f"  模型: {llm.model}")
        print(f"  状态: {'可用' if llm.available else '未配置'}")
        print(f"  最大Tokens: {llm.max_tokens}")
        print()
        print("=" * 70)
        print()

    def show_stats(self):
        """显示数据库统计"""
        print()
        print("=" * 70)
        print("数据库统计")
        print("=" * 70)
        print()

        if not self.db_path.exists():
            print("❌ 数据库不存在")
            return

        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                cursor = conn.cursor()

                # 统计各表记录数
                tables = ['episodes', 'patterns', 'interactions', 'sessions',
                         'user_feedback', 'learning_state', 'evolution_history',
                         'error_records', 'knowledge_nodes', 'knowledge_edges']

                print("记录统计:")
                for table in tables:
                    try:
                        cursor.execute(f"SELECT COUNT(*) FROM {table}")
                        count = cursor.fetchone()[0]
                        print(f"  {table}: {count} 条记录")
                    except sqlite3.OperationalError:
                        pass

                print()

                # 数据库文件大小
                db_size = self.db_path.stat().st_size / 1024
                print(f"数据库大小: {db_size:.1f} KB")

                # 表数量
                cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
                table_count = cursor.fetchone()[0]
                print(f"表数量: {table_count} 个")

        except Exception as e:
            print(f"❌ 查询失败: {e}")

        print()
        print("=" * 70)
        print()

    def chat_mode(self):
        """对话模式"""
        print()
        print("=" * 70)
        print("对话模式 (输入 'back' 返回主菜单)")
        print("=" * 70)
        print()
        session_id = secrets.token_hex(16)
        message_count = 0
        history = []
        from jarvis.core.llm import get_llm
        from jarvis.memory.bridge import get_memory_bridge
        llm = get_llm()
        memory_bridge = get_memory_bridge(str(self.db_path), llm=llm)
        self._ensure_session(session_id)

        while True:
            try:
                user_input = input("你: ").strip()

                if user_input.lower() == 'back':
                    print()
                    print(f"会话结束 - 共 {message_count} 轮")
                    print()
                    break

                if not user_input:
                    continue

                if not llm.available:
                    print("贾维斯: 模型未配置，请设置 ANTHROPIC_API_KEY。")
                    continue

                relevant = memory_bridge.retrieve_relevant(
                    user_input, max_items=5, namespace="cli-user"
                )
                memory_context = (
                    memory_bridge.format_for_prompt(relevant) if relevant else None
                )
                response = llm.chat_with_memory(
                    user_input, history, memory_context=memory_context,
                    db_path=str(self.db_path),
                )
                if llm.response_is_error(response):
                    print("贾维斯: 模型服务请求失败，请稍后重试。")
                    print()
                    continue
                print(f"贾维斯: {response}")
                print()
                self._store_exchange(session_id, user_input, response)
                history.extend([
                    {'role': 'user', 'content': user_input},
                    {'role': 'assistant', 'content': response},
                ])
                history = history[-20:]
                message_count += 1

            except KeyboardInterrupt:
                print("\n\n会话中断")
                break
            except EOFError:
                break

    def _ensure_session(self, session_id: str):
        """Create the CLI user and session if they do not exist."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("INSERT OR IGNORE INTO users (id) VALUES ('cli-user')")
            conn.execute("""
                INSERT OR IGNORE INTO sessions
                    (session_id, user_id, platform, started_at)
                VALUES (?, 'cli-user', 'cli', ?)
            """, (session_id, time.time()))

    def _store_exchange(self, session_id: str, user_input: str, response: str):
        """Persist one complete user/assistant exchange."""
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("""
                    INSERT INTO interactions
                        (session_id, timestamp, interaction_type,
                         user_input, agent_response)
                    VALUES (?, ?, 'cli_message', ?, ?)
                """, (session_id, time.time(), user_input, response))
        except Exception as e:
            print(f"存储失败: {e}")

    def learning_demo(self):
        """学习系统演示"""
        print()
        print("=" * 70)
        print("学习系统演示")
        print("=" * 70)
        print()

        try:
            from jarvis.learning.habits import PrefixSpan

            print("1. PrefixSpan序列模式挖掘演示")
            print()

            # 示例序列数据
            sequences = [
                ['登录', '查看订单', '退出'],
                ['登录', '查看订单', '下单', '支付', '退出'],
                ['登录', '浏览商品', '加入购物车', '下单', '支付'],
                ['登录', '查看订单', '下单', '退出'],
                ['登录', '浏览商品', '下单', '支付'],
            ]

            print("示例用户行为序列:")
            for i, seq in enumerate(sequences, 1):
                print(f"  序列{i}: {' → '.join(seq)}")
            print()

            # 运行PrefixSpan
            prefixspan = PrefixSpan(min_support=0.4)
            patterns = prefixspan.mine_patterns(sequences)

            print("发现的序列模式:")
            if patterns:
                for pattern, support, confidence in patterns[:5]:
                    print(
                        f"  {' → '.join(pattern)} "
                        f"(支持度: {support:.2f}, 置信度: {confidence:.2f})"
                    )
            else:
                print("  (未发现显著模式)")

            print()
            print("✅ 学习系统演示完成")

        except Exception as e:
            print(f"❌ 演示失败: {e}")

        print()
        print("=" * 70)
        print()

    def evolution_demo(self):
        """Darwinian进化演示"""
        print()
        print("=" * 70)
        print("Darwinian进化引擎演示")
        print("=" * 70)
        print()

        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                rows = conn.execute("""
                    SELECT generation, fitness_score, train_score, holdout_score
                    FROM evolution_history
                    WHERE evolution_type = 'prompt'
                    ORDER BY id DESC LIMIT 10
                """).fetchall()
                unused = conn.execute("""
                    SELECT COUNT(*) FROM eval_cases
                    WHERE used_in_evolution = 0
                      AND expected IS NOT NULL AND TRIM(expected) != ''
                """).fetchone()[0]

            print(f"可用评估样本: {unused}")
            if not rows:
                print("暂无真实进化记录")
            for generation, fitness, train_score, holdout_score in rows:
                print(
                    f"第 {generation} 代: fitness={fitness:.3f}, "
                    f"train={train_score or 0:.3f}, holdout={holdout_score or 0:.3f}"
                )

        except Exception as e:
            print(f"❌ 演示失败: {e}")

        print()
        print("=" * 70)
        print()

    def memory_demo(self):
        """记忆系统演示"""
        print()
        print("=" * 70)
        print("记忆系统演示")
        print("=" * 70)
        print()

        try:
            from jarvis.memory.system import MemorySystem

            memory = MemorySystem(str(self.db_path))

            print("1. 存储测试数据到记忆系统")
            test_data = {
                'user': '演示用户',
                'action': '系统演示',
                'timestamp': time.time()
            }

            memory.store('demo_key', test_data)
            print("✅ 数据已存储到记忆系统")
            print()

            print("2. 从记忆系统检索数据")
            retrieved = memory.retrieve('demo_key')
            if retrieved:
                print("✅ 数据检索成功")
                print(f"   数据内容: {retrieved}")
            else:
                print("❌ 数据检索失败")

            print()
            print("✅ 记忆系统演示完成")

        except Exception as e:
            print(f"❌ 演示失败: {e}")

        print()
        print("=" * 70)
        print()

    def clear_screen(self):
        """清屏"""
        if sys.stdout.isatty():
            print("\033[2J\033[H", end="")
        print("=" * 70)
        print("贾维斯自学习自进化系统 V4.1 - 交互界面")
        print("=" * 70)
        print()

    def exit_system(self):
        """退出系统"""
        print()
        print("=" * 70)
        print("感谢使用贾维斯系统!")
        print("=" * 70)
        print()
        self.running = False

    def run(self):
        """运行交互界面"""
        while self.running:
            try:
                command = input("贾维斯> ").strip().lower()

                if not command:
                    continue

                if command in self.commands:
                    self.commands[command]()
                else:
                    print(f"未知命令: {command}")
                    print("输入 'help' 查看可用命令")

            except KeyboardInterrupt:
                print("\n")
                self.exit_system()
            except EOFError:
                self.exit_system()
            except Exception as e:
                print(f"错误: {e}")


def main():
    """主函数"""
    interface = JarvisInterface()
    interface.run()


if __name__ == '__main__':
    main()
