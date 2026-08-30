"""Opt-in repository-local hook commands for native agent extension points."""

from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator

from looplane.contracts import ContractModel
from looplane.plugins import PluginError, load_project_plugins
from looplane.runtime import bounded_text, run_bounded_command, sanitized_subprocess_env

PROJECT_HOOKS_FILE = Path(".looplane") / "hooks.json"
MAX_HOOK_CONFIG_BYTES = 64 * 1024
MAX_HOOKS_PER_EVENT = 16
MAX_HOOK_ARGV_ITEMS = 32
MAX_HOOK_OUTPUT_CHARS = 8_000


class HookError(ValueError):
    """Raised when hook config or hook output is unsafe."""


class HookEventName(StrEnum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    APPROVAL_REQUEST = "approval_request"
    PRE_COMPACT = "pre_compact"
    POST_COMPACT = "post_compact"


class HookDecision(ContractModel):
    """Result of a blocking hook event."""

    decision: Literal["deny"] | None = None
    reason: str = Field(default="", max_length=2_000)
    hook: str | None = Field(default=None, max_length=512)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        value = value.strip()
        if "\x00" in value:
            raise ValueError("hook reason cannot contain NUL")
        return value


class HookCommandConfig(ContractModel):
    """One exact-argv hook command.

    Project hook commands are not loaded unless `LOOPLANE_ENABLE_PROJECT_HOOKS=1`.
    They receive the hook payload as JSON on stdin and may only deny; they cannot
    grant approval or bypass looplane policy.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    command: tuple[str, ...] = Field(min_length=1, max_length=MAX_HOOK_ARGV_ITEMS)
    timeout_seconds: float = Field(default=5.0, gt=0, le=30.0)
    tools: tuple[str, ...] = ("*",)

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not item or "\x00" in item:
                raise ValueError("hook command argv entries must be non-empty and NUL-free")
        return value

    @field_validator("tools")
    @classmethod
    def validate_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for tool in value:
            tool = tool.strip()
            if not tool or "\x00" in tool:
                raise ValueError("hook tools must be non-empty and NUL-free")
            normalized.append(tool)
        return tuple(dict.fromkeys(normalized))

    def matches_tool(self, tool_name: str | None) -> bool:
        if "*" in self.tools:
            return True
        if tool_name is None:
            return False
        return tool_name in self.tools


class HookConfig(ContractModel):
    """Strict hook config loaded from `.looplane/hooks.json`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pre_tool_use: tuple[HookCommandConfig, ...] = Field(default=(), max_length=MAX_HOOKS_PER_EVENT)
    post_tool_use: tuple[HookCommandConfig, ...] = Field(default=(), max_length=MAX_HOOKS_PER_EVENT)
    approval_request: tuple[HookCommandConfig, ...] = Field(
        default=(), max_length=MAX_HOOKS_PER_EVENT
    )
    pre_compact: tuple[HookCommandConfig, ...] = Field(default=(), max_length=MAX_HOOKS_PER_EVENT)
    post_compact: tuple[HookCommandConfig, ...] = Field(default=(), max_length=MAX_HOOKS_PER_EVENT)

    def commands_for(self, event: HookEventName) -> tuple[HookCommandConfig, ...]:
        return tuple(getattr(self, event.value))


class HookRunner:
    """Run configured hook commands with bounded IO and deny-only decisions."""

    def __init__(self, config: HookConfig | None = None, *, cwd: Path | None = None) -> None:
        self.config = config or HookConfig()
        self.cwd = Path.cwd() if cwd is None else Path(cwd)

    @property
    def enabled(self) -> bool:
        return any(self.config.commands_for(event) for event in HookEventName)

    def run(self, event: HookEventName, payload: dict[str, Any]) -> HookDecision | None:
        tool_name = _payload_tool_name(payload)
        for hook in self.config.commands_for(event):
            if not hook.matches_tool(tool_name):
                continue
            decision = self._run_one(event, hook, payload)
            if decision is not None and decision.decision == "deny":
                return decision
        return None

    def _run_one(
        self,
        event: HookEventName,
        hook: HookCommandConfig,
        payload: dict[str, Any],
    ) -> HookDecision | None:
        hook_payload = json.dumps(
            {"event": event.value, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
        )
        result = run_bounded_command(
            hook.command,
            cwd=self.cwd,
            timeout_seconds=hook.timeout_seconds,
            max_output_chars=MAX_HOOK_OUTPUT_CHARS,
            env=sanitized_subprocess_env(),
            stdin=hook_payload,
        )
        hook_name = " ".join(hook.command)
        if result.timed_out:
            return HookDecision(
                decision="deny",
                reason=f"hook timed out after {hook.timeout_seconds:g}s",
                hook=hook_name,
            )
        if result.returncode != 0:
            return HookDecision(
                decision="deny",
                reason=bounded_text(
                    f"hook exited {result.returncode}: {result.stderr or result.stdout}",
                    2_000,
                ),
                hook=hook_name,
            )
        output = result.stdout.strip()
        if not output:
            return None
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise HookError(f"hook output is not valid JSON: {hook_name}") from exc
        if not isinstance(value, dict):
            raise HookError(f"hook output must be a JSON object: {hook_name}")
        decision_value = value.get("decision")
        if decision_value in {None, "", "allow"}:
            return None
        if decision_value != "deny":
            raise HookError(f"hook decision must be deny or omitted: {hook_name}")
        return HookDecision(
            decision="deny",
            reason=str(value.get("reason") or "denied by hook"),
            hook=hook_name,
        )


def load_project_hook_config(project_root: Path) -> HookConfig:
    """Load `.looplane/hooks.json`; absent config returns an empty registry."""

    value: dict[str, Any] = {}
    path = project_root / PROJECT_HOOKS_FILE
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise HookError("project hook config must be a regular file")
        with path.open("rb") as file:
            payload = file.read(MAX_HOOK_CONFIG_BYTES + 1)
        if len(payload) > MAX_HOOK_CONFIG_BYTES:
            raise HookError(f"project hook config exceeds {MAX_HOOK_CONFIG_BYTES} bytes")
        try:
            loaded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HookError("project hook config is not valid UTF-8 JSON") from exc
        if not isinstance(loaded, dict):
            raise HookError("project hook config must be a JSON object")
        value = dict(loaded)
    try:
        for plugin in load_project_plugins(project_root):
            for event in HookEventName:
                hooks = plugin.hooks.get(event.value)
                if hooks is None:
                    continue
                if not isinstance(hooks, list):
                    raise HookError("plugin hook entries must be arrays")
                value.setdefault(event.value, [])
                if not isinstance(value[event.value], list):
                    raise HookError("project hook entries must be arrays")
                value[event.value].extend(hooks)
    except PluginError as exc:
        raise HookError(str(exc)) from exc
    try:
        return HookConfig.model_validate(value)
    except ValueError as exc:
        raise HookError(f"project hook config is invalid: {exc}") from exc


def project_hooks_enabled() -> bool:
    """Whether repo-local hook commands may execute on this host."""

    return os.environ.get("LOOPLANE_ENABLE_PROJECT_HOOKS") == "1"


def load_project_hook_runner(project_root: Path) -> HookRunner:
    """Load an enabled project hook runner, or an empty runner when disabled."""

    if not project_hooks_enabled():
        return HookRunner()
    return HookRunner(load_project_hook_config(project_root), cwd=project_root)


def _payload_tool_name(payload: dict[str, Any]) -> str | None:
    tool_call = payload.get("tool_call")
    if isinstance(tool_call, dict):
        name = tool_call.get("name")
        return name if isinstance(name, str) else None
    observation = payload.get("observation")
    if isinstance(observation, dict):
        name = observation.get("name")
        return name if isinstance(name, str) else None
    return None
