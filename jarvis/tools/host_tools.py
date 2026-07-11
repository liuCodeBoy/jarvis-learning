"""Controlled host filesystem tools exposed through Anthropic tool use."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml


MAX_TEXT_BYTES = 1_000_000
MAX_DIRECTORY_ENTRIES = 500
PROJECT_DIR = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


def _configured_workspace() -> str:
    if os.environ.get("JARVIS_DISABLE_LOCAL_CONFIG", "").lower() in {
        "1", "true", "yes", "on",
    }:
        return ""
    configured = ""
    for path in (PROJECT_DIR / "config.yaml", PROJECT_DIR / "config.local.yaml"):
        if not path.is_file():
            continue
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        tools = loaded.get("tools", {}) if isinstance(loaded, dict) else {}
        if isinstance(tools, dict) and tools.get("workspace_path"):
            configured = str(tools["workspace_path"])
    return configured


def workspace_root() -> Path:
    """Return the only directory that model tools may read or modify."""
    configured = (
        os.environ.get("JARVIS_WORKSPACE_PATH")
        or _configured_workspace()
        or "~/Desktop"
    )
    return Path(configured).expanduser().resolve()


def _resolve_path(value: str) -> Path:
    """Resolve a tool path and reject traversal or symlink escape."""
    if not value or "\x00" in value:
        raise ValueError("path must not be empty")
    candidate = Path(value).expanduser()
    root = workspace_root()
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("path is outside the configured workspace")
    return resolved


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOL_DEFINITIONS = [
    _schema(
        "list_directory",
        "List files and directories inside the configured local workspace.",
        {"path": {"type": "string", "description": "Workspace-relative path."}},
        ["path"],
    ),
    _schema(
        "read_file",
        "Read a UTF-8 text file inside the configured local workspace.",
        {"path": {"type": "string", "description": "Workspace-relative file path."}},
        ["path"],
    ),
    _schema(
        "create_directory",
        "Create a directory inside the configured local workspace.",
        {"path": {"type": "string", "description": "Workspace-relative directory path."}},
        ["path"],
    ),
    _schema(
        "write_file",
        "Create or replace a UTF-8 text file inside the configured local workspace.",
        {
            "path": {"type": "string", "description": "Workspace-relative file path."},
            "content": {"type": "string", "description": "Complete file content."},
        },
        ["path", "content"],
    ),
    _schema(
        "open_file",
        "Open an existing local file with the operating system default application.",
        {"path": {"type": "string", "description": "Workspace-relative file path."}},
        ["path"],
    ),
]


def _success(operation: str, **details: Any) -> str:
    logger.info(
        "Host tool completed operation=%s path=%s",
        operation,
        details.get("path", ""),
    )
    return json.dumps({"ok": True, "operation": operation, **details}, ensure_ascii=False)


def _failure(operation: str, error: str) -> str:
    logger.warning("Host tool failed operation=%s error=%s", operation, error)
    return json.dumps(
        {"ok": False, "operation": operation, "error": error}, ensure_ascii=False
    )


def execute(name: str, arguments: dict[str, Any]) -> str:
    """Execute one registered tool and return a machine-readable result."""
    try:
        if name not in {definition["name"] for definition in TOOL_DEFINITIONS}:
            raise ValueError(f"unknown tool: {name}")
        target = _resolve_path(str(arguments.get("path", "")))

        if name == "list_directory":
            if not target.is_dir():
                raise NotADirectoryError(str(target))
            entries = [
                {"name": item.name, "type": "directory" if item.is_dir() else "file"}
                for item in sorted(target.iterdir(), key=lambda item: item.name.lower())
            ][:MAX_DIRECTORY_ENTRIES]
            return _success(name, path=str(target), entries=entries)

        if name == "read_file":
            if not target.is_file():
                raise FileNotFoundError(str(target))
            if target.stat().st_size > MAX_TEXT_BYTES:
                raise ValueError("file exceeds the 1 MB text limit")
            return _success(name, path=str(target), content=target.read_text(encoding="utf-8"))

        if name == "create_directory":
            target.mkdir(parents=True, exist_ok=True)
            return _success(name, path=str(target), exists=target.is_dir())

        if name == "write_file":
            content = arguments.get("content")
            if not isinstance(content, str):
                raise ValueError("content must be a string")
            if len(content.encode("utf-8")) > MAX_TEXT_BYTES:
                raise ValueError("content exceeds the 1 MB text limit")
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                dir=target.parent,
                prefix=".jarvis-write-",
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            return _success(
                name,
                path=str(target),
                exists=target.is_file(),
                bytes=target.stat().st_size,
            )

        if not target.is_file():
            raise FileNotFoundError(str(target))
        subprocess.run(["open", str(target)], check=True, timeout=10)
        return _success(name, path=str(target), opened=True)
    except PermissionError:
        return _failure(
            name,
            f"permission denied for workspace path: {workspace_root()}",
        )
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        return _failure(name, str(exc))
