"""Delegation to the user-installed Pi coding agent (``pi --mode json``).

Pi owns its authentication, model loop, permissions, and session. looplane never proxies Pi
credentials. Pi's documented JSON event stream (``pi --mode json "prompt"``) is normalized
below using its published event vocabulary (``message_update`` deltas, ``toolcall_*`` lifecycle,
``message_end``, and terminal ``error`` frames).
"""

from __future__ import annotations

from typing import Any

from looplane.backends import ExternalAgentEvent
from looplane.external_cli_base import StreamJsonCliBackend


class PiBackend(StreamJsonCliBackend):
    backend_name = "pi"
    local_only = True
    experimental = True

    def _argv(self, executable: str, instruction: str) -> tuple[str, ...]:
        argv = [executable, "--mode", "json"]
        if self.model is not None:
            argv += ["--model", self.model]
        argv.append(instruction)
        return tuple(argv)

    @staticmethod
    def _message_text(value: dict[str, Any]) -> str | None:
        message = value.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if isinstance(content, str):
            return content or None
        if isinstance(content, list):
            parts = [
                item.get("text")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            return "".join(parts) or None
        return None

    def _normalize_event(self, sequence: int, value: dict[str, Any]) -> ExternalAgentEvent | None:
        event_type = value.get("type")
        if event_type in {"session", "agent_start", "turn_start", "agent_end", "turn_end"}:
            return None
        if event_type == "message_update":
            update = value.get("assistantMessageEvent")
            if not isinstance(update, dict):
                return None
            sub = update.get("type")
            if sub == "text_delta":
                delta = update.get("delta")
                if isinstance(delta, str) and delta:
                    return ExternalAgentEvent(
                        sequence=sequence,
                        event_type="message",
                        text=delta,
                        data={"source": "pi"},
                    )
            if sub in {"toolcall_start", "toolcall_end"}:
                data: dict[str, Any] = {"source": "pi"}
                name = update.get("toolName")
                tool_call = update.get("toolCall")
                if isinstance(tool_call, dict) and isinstance(tool_call.get("name"), str):
                    name = tool_call["name"]
                if isinstance(name, str):
                    data["tool"] = name
                return ExternalAgentEvent(sequence=sequence, event_type="tool", data=data)
            return None
        if event_type == "tool_execution_start":
            data = {"source": "pi"}
            if isinstance(value.get("toolName"), str):
                data["tool"] = value["toolName"]
            return ExternalAgentEvent(sequence=sequence, event_type="tool", data=data)
        if event_type in {"message_end", "message"}:
            text = self._message_text(value)
            if text:
                return ExternalAgentEvent(
                    sequence=sequence, event_type="message", text=text, data={"source": "pi"}
                )
            return None
        if event_type == "error":
            return ExternalAgentEvent(
                sequence=sequence,
                event_type="result",
                text=value.get("message") if isinstance(value.get("message"), str) else None,
                data={"source": "pi", "is_error": True},
            )
        return None

    def _status_from_events(
        self, events: tuple[ExternalAgentEvent, ...], *, returncode: int
    ) -> tuple[Any, str]:
        from looplane.backends import ExternalRunStatus

        if returncode != 0:
            return ExternalRunStatus.FAILED, "external_agent_error"
        if any(event.data.get("is_error") for event in events if event.event_type == "result"):
            return ExternalRunStatus.FAILED, "external_agent_error"
        return ExternalRunStatus.COMPLETED, "completed"
