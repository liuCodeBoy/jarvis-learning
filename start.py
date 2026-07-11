#!/usr/bin/env python3
"""
J.A.R.V.I.S. 自学习自进化系统 -- 统一启动入口

支持三种启动模式:
  web    -- Web界面启动 (默认, 端口8000)
  local  -- 本地CLI交互模式
  docker -- Docker容器化部署

用法:
  python3 start.py                  # 默认Web模式
  python3 start.py --mode web       # Web模式, 可指定 --port
  python3 start.py --mode local     # 本地CLI交互
  python3 start.py --mode docker    # Docker部署
  python3 start.py --test           # 运行系统测试
"""

import sys
import os
import argparse
from pathlib import Path

# 确保项目根目录在 sys.path 中
PROJECT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(PROJECT_DIR))


def ensure_dirs():
    """确保运行所需的目录结构存在"""
    required_dirs = [
        "data",
        "logs",
        "backups",
    ]
    for d in required_dirs:
        Path(PROJECT_DIR / d).mkdir(parents=True, exist_ok=True)


def run_web(port=8000, host=None):
    """启动Web界面 (Flask)"""
    ensure_dirs()
    print("=" * 60)
    print("J.A.R.V.I.S. 自学习自进化系统 -- Web界面启动")
    host = host or os.environ.get("JARVIS_HOST", "127.0.0.1")
    display_host = "localhost" if host in ("127.0.0.1", "localhost") else host
    print(f"监听: {host}:{port}")
    print(f"访问: http://{display_host}:{port}")
    print("=" * 60)

    from jarvis.api.web_app import create_app
    app = create_app()
    app.run(host=host, port=port, debug=False)


def run_local():
    """启动本地CLI交互模式"""
    ensure_dirs()
    print("=" * 60)
    print("J.A.R.V.I.S. 自学习自进化系统 -- 本地交互模式")
    print("=" * 60)

    from jarvis.api.cli import JarvisInterface
    interface = JarvisInterface()
    interface.run()


def run_docker():
    """启动Docker容器化部署"""
    print("=" * 60)
    print("J.A.R.V.I.S. 自学习自进化系统 -- Docker部署")
    print("=" * 60)

    deploy_script = PROJECT_DIR / "scripts" / "deploy.sh"
    if not deploy_script.exists():
        print("错误: scripts/deploy.sh 不存在, 无法执行Docker部署")
        sys.exit(1)

    import subprocess
    result = subprocess.run(["bash", str(deploy_script)], cwd=str(PROJECT_DIR))
    sys.exit(result.returncode)


def run_tests(report=False):
    """运行系统测试"""
    print("=" * 60, flush=True)
    print("J.A.R.V.I.S. 自学习自进化系统 -- 测试模式", flush=True)
    print("=" * 60, flush=True)

    import subprocess
    cmd = [sys.executable, "-m", "pytest", "-q"]
    if report:
        cmd.extend(["--cov=jarvis", "--cov-report=term-missing"])
    result = subprocess.run(cmd, cwd=str(PROJECT_DIR))
    sys.exit(result.returncode)


def main():
    parser = argparse.ArgumentParser(
        description="J.A.R.V.I.S. 自学习自进化系统 -- 统一启动入口"
    )
    parser.add_argument(
        "--mode",
        choices=["web", "local", "docker"],
        default="web",
        help="启动模式: web(默认)/local/docker",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Web服务端口 (仅web模式, 默认8000)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("JARVIS_HOST", "127.0.0.1"),
        help="Web监听地址 (默认127.0.0.1; 容器内使用0.0.0.0)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="运行系统测试",
    )
    parser.add_argument(
        "--test-report",
        action="store_true",
        help="运行测试并生成报告",
    )

    args = parser.parse_args()

    # 测试模式优先
    if args.test or args.test_report:
        run_tests(report=args.test_report)
        return

    # 按模式启动
    mode_handlers = {
        "web": lambda: run_web(args.port, args.host),
        "local": run_local,
        "docker": run_docker,
    }
    mode_handlers[args.mode]()


if __name__ == "__main__":
    main()
