"""Persistent prompt history backed by a JSONL file."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path

_MAX_ENTRIES = 500


def _history_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_root / "looplane" / "prompt_history.jsonl"


def load_prompt_history() -> list[str]:
    path = _history_path()
    if not path.exists():
        return []
    entries: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                continue
    except OSError:
        return []
    return entries[-_MAX_ENTRIES:]


def append_prompt_history(prompt: str) -> None:
    path = _history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(prompt, ensure_ascii=False) + "\n")
    except OSError:
        return
    _maybe_truncate(path)


def _maybe_truncate(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= _MAX_ENTRIES * 2:
        return
    with suppress(OSError):
        path.write_text(
            "\n".join(lines[-_MAX_ENTRIES:]) + "\n",
            encoding="utf-8",
        )
