"""Bounded session filesystem reads and query matching."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SessionIndex:
    run_root: Path
    query: str | None = None
    max_json_bytes: int = 16 * 1024 * 1024
    max_event_search_parts: int = 256
    max_event_search_part_chars: int = 4096

    @property
    def normalized_query(self) -> str | None:
        return self.query.casefold().strip() if self.query else None

    def read_json(self, path: Path) -> dict[str, object] | None:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > self.max_json_bytes:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def safe_session_dir(self, path: Path) -> bool:
        return (
            not path.name.startswith(".")
            and "/" not in path.name
            and ("\\" not in path.name)
            and (path.name not in {".", ".."})
            and (not path.is_symlink())
            and path.is_dir()
        )

    def matches(self, parts: list[object]) -> bool:
        if self.normalized_query is None:
            return True
        haystack = " ".join(str(part) for part in parts if part is not None).casefold()
        return self.normalized_query in haystack

    def bounded_event_search_parts(self, value: object, parts: list[str]) -> None:
        if len(parts) >= self.max_event_search_parts:
            return
        if isinstance(value, str):
            text = value.strip()
            if text:
                parts.append(text[: self.max_event_search_part_chars])
            return
        if isinstance(value, dict):
            for item in value.values():
                self.bounded_event_search_parts(item, parts)
                if len(parts) >= self.max_event_search_parts:
                    return
            return
        if isinstance(value, list | tuple):
            for item in value:
                self.bounded_event_search_parts(item, parts)
                if len(parts) >= self.max_event_search_parts:
                    return

    def event_search_parts(self, path: Path) -> list[str]:
        if self.normalized_query is None:
            return []
        parts: list[str] = []
        for event in self.read_events(path):
            self.bounded_event_search_parts(event, parts)
            if len(parts) >= self.max_event_search_parts:
                break
        return parts

    def conversation_event_search_parts(self, events: object) -> list[str]:
        if self.normalized_query is None:
            return []
        parts: list[str] = []
        for event in events if isinstance(events, tuple) else ():
            payload = event.model_dump(mode="json") if hasattr(event, "model_dump") else event
            self.bounded_event_search_parts(payload, parts)
            if len(parts) >= self.max_event_search_parts:
                break
        return parts

    def usage_total(self, source: dict[str, object]) -> object:
        usage = source.get("usage") or {}
        if not isinstance(usage, dict):
            return 0
        total = usage.get("provider_total_tokens") or usage.get("input_tokens", 0)
        return 0 if isinstance(total, dict) else total

    def resolve_run_dir(self, identifier: str) -> Path | None:
        if not identifier or "/" in identifier or "\\" in identifier or (identifier in {".", ".."}):
            return None
        if not self.run_root.exists() or self.run_root.is_symlink() or (not self.run_root.is_dir()):
            return None
        exact = self.run_root / identifier
        if self.safe_session_dir(exact):
            return exact
        matches = [
            path
            for path in self.run_root.iterdir()
            if self.safe_session_dir(path) and path.name.startswith(identifier)
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def read_events(self, path: Path) -> list[dict[str, object]]:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > self.max_json_bytes:
            return []
        events: list[dict[str, object]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if isinstance(value, dict):
                    events.append(value)
        except (OSError, UnicodeError, ValueError):
            return []
        return sorted(
            events,
            key=lambda value: (
                value.get("sequence") if isinstance(value.get("sequence"), int) else -1
            ),
        )

    def event_detail(self, event: dict[str, object]) -> str:
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        for key in (
            "summary",
            "terminal_reason",
            "reason",
            "tool",
            "name",
            "model",
            "provider",
            "base_sha",
        ):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
