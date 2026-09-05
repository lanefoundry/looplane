"""Delegation to the user-installed OpenCode CLI (``opencode run --format json``).

OpenCode owns its authentication, model loop, permissions, and session. looplane never opens,
stores, refreshes, or forwards OpenCode credentials; the child retains ``HOME`` and its own
provider configuration through the forwarded environment. The headless ``run --format json``
surface emits raw JSON events; the normalizer below is permissive and is finalized against live
captures in the M13 stage report.
"""

from __future__ import annotations

from typing import Any

from looplane.backends import ExternalAgentEvent
from looplane.external_cli_base import StreamJsonCliBackend


class OpenCodeRunner(StreamJsonCliBackend):
    backend_name = "opencode"
    local_only = True
    experimental = True

    def _argv(self, executable: str, instruction: str) -> tuple[str, ...]:
        # looplane already gates the whole delegation on --allow-external-modify and
        # confines opencode to a disposable clone, so skip opencode's interactive
        # permission prompts; otherwise headless edit tasks hang waiting for an
        # approval that can never arrive (stdin is /dev/null).
        argv = [executable, "run", "--format", "json", "--dangerously-skip-permissions"]
        if self.model is not None:
            argv += ["--model", self.model]
        argv.append(instruction)
        return tuple(argv)

    @staticmethod
    def _text_of(value: dict[str, Any]) -> str | None:
        content = value.get("content")
        if isinstance(content, str) and content:
            return content
        if isinstance(content, list):
            parts = [
                item.get("text")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            joined = "".join(parts)
            return joined or None
        message = value.get("message")
        if isinstance(message, dict):
            inner = message.get("content")
            if isinstance(inner, str):
                return inner
            if isinstance(inner, list):
                parts = [
                    item.get("text")
                    for item in inner
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                ]
                return "".join(parts) or None
        part = value.get("part")
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            return part["text"]
        if isinstance(value.get("text"), str):
            return value["text"]
        return None

    def _normalize_event(self, sequence: int, value: dict[str, Any]) -> ExternalAgentEvent | None:
        event_type = value.get("type")
        if event_type in {"result", "done", "exit", "session_end", "completion"}:
            text = self._text_of(value)
            data: dict[str, Any] = {"source": "opencode"}
            if value.get("is_error") or value.get("error"):
                data["is_error"] = True
            return ExternalAgentEvent(
                sequence=sequence,
                event_type="result",
                text=text,
                data=data,
            )
        if event_type == "error":
            message = None
            err = value.get("error")
            if isinstance(err, dict):
                err_data = err.get("data")
                if isinstance(err_data, dict) and isinstance(err_data.get("message"), str):
                    message = err_data["message"]
                elif isinstance(err.get("message"), str):
                    message = err["message"]
            if message is None and isinstance(value.get("message"), str):
                message = value["message"]
            return ExternalAgentEvent(
                sequence=sequence,
                event_type="result",
                text=message,
                data={"source": "opencode", "is_error": True},
            )
        if event_type in {"tool", "tool_use", "tool_call", "function", "tool_execution"}:
            data: dict[str, Any] = {"source": "opencode"}
            name = value.get("name")
            part = value.get("part")
            if isinstance(part, dict) and isinstance(part.get("tool"), str):
                name = part["tool"]
            if isinstance(name, str):
                data["tool"] = name
            return ExternalAgentEvent(sequence=sequence, event_type="tool", data=data)
        text = self._text_of(value)
        if text:
            return ExternalAgentEvent(
                sequence=sequence, event_type="message", text=text, data={"source": "opencode"}
            )
        return None


# Temporary compatibility name; implementation stays here until the runtime migration.
OpenCodeBackend = OpenCodeRunner
