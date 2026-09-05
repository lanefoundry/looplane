"""Pure Codex ID validation, byte bounds, and JSON frame parsing."""

from __future__ import annotations

import json
from typing import Any

from looplane.conversation_runtime import ConversationProtocolError


def safe_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256 and "\x00" not in value


def preview_diff(diff: str | None) -> tuple[str | None, int | None, bool]:
    if diff is None:
        return None, None, False
    encoded = diff.encode("utf-8")
    shown = encoded[:64000].decode("utf-8", errors="ignore")
    return shown, len(encoded), len(shown.encode("utf-8")) < len(encoded)


def bounded_text(value: str, *, max_frame_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_frame_bytes:
        return value
    return encoded[:max_frame_bytes].decode("utf-8", errors="ignore")


def parse_frame(
    raw: bytes, *, frame_count: int, max_frames: int, max_frame_bytes: int
) -> dict[str, Any]:
    if frame_count > max_frames or len(raw) > max_frame_bytes:
        raise ConversationProtocolError("app-server output exceeded protocol bounds")
    try:
        frame = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversationProtocolError("app-server emitted invalid JSON") from exc
    if not isinstance(frame, dict):
        raise ConversationProtocolError("app-server frame must be an object")
    return frame
