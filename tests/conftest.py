import os
from pathlib import Path


_GLOBAL_TEST_DB = Path("/tmp") / f"jarvis_pytest_{os.getpid()}.db"
os.environ["JARVIS_DB_PATH"] = str(_GLOBAL_TEST_DB)
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)
os.environ["JARVIS_DISABLE_LOCAL_CONFIG"] = "1"


def pytest_sessionfinish(session, exitstatus):
    for suffix in ("", "-shm", "-wal"):
        try:
            Path(str(_GLOBAL_TEST_DB) + suffix).unlink()
        except FileNotFoundError:
            pass
