"""Controlled host tools exposed to the model through Anthropic tool use."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any


def _root() -> Path:
    return Path(os.environ.get("JARVIS_WORKSPACE_PATH", "~/Desktop")).expanduser().resolve()


def _path(value: str) -> Path:
    candidate = Path(value).expanduser()
    resolved = (candidate if candidate.is_absolute() else _root() / candidate).resolve()
    root = _root()
    if resolved != root and root not in resolved.parents:
        raise ValueError("path is outside the configured workspace")
    return resolved


TOOL_DEFINITIONS = [
    {"name": "create_directory", "description": "Create a directory in the configured workspace.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    {"name": "write_file", "description": "Write UTF-8 text to a file in the configured workspace.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "open_file", "description": "Open a local file with the operating system default application.", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
]


def execute(name: str, arguments: dict[str, Any]) -> str:
    try:
        target = _path(str(arguments.get("path", "")))
        if name == "create_directory":
            target.mkdir(parents=True, exist_ok=True)
            return json.dumps({"ok": True, "operation": name, "path": str(target)})
        if name == "write_file":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(arguments.get("content", "")), encoding="utf-8")
            return json.dumps({"ok": target.is_file(), "operation": name, "path": str(target), "bytes": target.stat().st_size})
        if name == "open_file":
            if not target.is_file():
                raise FileNotFoundError(str(target))
            subprocess.run(["open", str(target)], check=True, timeout=10)
            return json.dumps({"ok": True, "operation": name, "path": str(target)})
        raise ValueError(f"unknown tool: {name}")
    except Exception as exc:
        return json.dumps({"ok": False, "operation": name, "error": str(exc)})
