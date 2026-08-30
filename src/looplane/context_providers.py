"""Opt-in repository-local runtime context providers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field, field_validator

from looplane.contracts import ContractModel
from looplane.conversation_runtime import RuntimeInjectedContext
from looplane.runtime import bounded_text, run_bounded_command, sanitized_subprocess_env

PROJECT_CONTEXT_PROVIDERS_FILE = Path(".looplane") / "context-providers.json"
MAX_CONTEXT_PROVIDER_CONFIG_BYTES = 64 * 1024
MAX_CONTEXT_PROVIDERS = 16
MAX_CONTEXT_PROVIDER_ARGV_ITEMS = 32
MAX_CONTEXT_PROVIDER_OUTPUT_CHARS = 64_000


class ContextProviderError(ValueError):
    """Raised when a runtime context provider config or output is unsafe."""


class ContextProviderCommand(ContractModel):
    """One exact-argv runtime context provider command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    command: tuple[str, ...] = Field(min_length=1, max_length=MAX_CONTEXT_PROVIDER_ARGV_ITEMS)
    timeout_seconds: float = Field(default=5.0, gt=0, le=30.0)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("context provider name must be non-empty and NUL-free")
        return value

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not item or "\x00" in item:
                raise ValueError("context provider argv entries must be non-empty and NUL-free")
        return value


class ContextProviderConfig(ContractModel):
    """Strict config loaded from `.looplane/context-providers.json`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    providers: tuple[ContextProviderCommand, ...] = Field(
        default=(), max_length=MAX_CONTEXT_PROVIDERS
    )

    @field_validator("providers")
    @classmethod
    def provider_names_are_unique(
        cls, value: tuple[ContextProviderCommand, ...]
    ) -> tuple[ContextProviderCommand, ...]:
        names = [provider.name for provider in value]
        if len(names) != len(set(names)):
            raise ValueError("context provider names cannot contain duplicates")
        return value


class ContextProviderRunner:
    """Run configured providers with bounded IO and schema-checked context output."""

    def __init__(
        self,
        config: ContextProviderConfig | None = None,
        *,
        cwd: Path | None = None,
    ) -> None:
        self.config = config or ContextProviderConfig()
        self.cwd = Path.cwd() if cwd is None else Path(cwd)

    @property
    def enabled(self) -> bool:
        return bool(self.config.providers)

    def collect(self, payload: dict[str, Any]) -> tuple[RuntimeInjectedContext, ...]:
        items: list[RuntimeInjectedContext] = []
        for provider in self.config.providers:
            items.extend(self._run_one(provider, payload))
        return tuple(items)

    def _run_one(
        self,
        provider: ContextProviderCommand,
        payload: dict[str, Any],
    ) -> tuple[RuntimeInjectedContext, ...]:
        provider_payload = json.dumps(
            {"provider": provider.name, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
        )
        result = run_bounded_command(
            provider.command,
            cwd=self.cwd,
            timeout_seconds=provider.timeout_seconds,
            max_output_chars=MAX_CONTEXT_PROVIDER_OUTPUT_CHARS,
            env=sanitized_subprocess_env(),
            stdin=provider_payload,
        )
        if result.timed_out:
            raise ContextProviderError(
                f"context provider timed out after {provider.timeout_seconds:g}s: {provider.name}"
            )
        if result.returncode != 0:
            raise ContextProviderError(
                bounded_text(
                    f"context provider exited {result.returncode}: "
                    f"{result.stderr or result.stdout}",
                    2_000,
                )
            )
        output = result.stdout.strip()
        if not output:
            return ()
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ContextProviderError(
                f"context provider output is not valid JSON: {provider.name}"
            ) from exc
        return _parse_provider_output(provider.name, value)


def load_project_context_provider_config(project_root: Path) -> ContextProviderConfig:
    """Load `.looplane/context-providers.json`; absent config returns an empty registry."""

    path = project_root / PROJECT_CONTEXT_PROVIDERS_FILE
    if not path.exists():
        return ContextProviderConfig()
    if path.is_symlink() or not path.is_file():
        raise ContextProviderError("project context provider config must be a regular file")
    with path.open("rb") as file:
        payload = file.read(MAX_CONTEXT_PROVIDER_CONFIG_BYTES + 1)
    if len(payload) > MAX_CONTEXT_PROVIDER_CONFIG_BYTES:
        raise ContextProviderError(
            f"project context provider config exceeds {MAX_CONTEXT_PROVIDER_CONFIG_BYTES} bytes"
        )
    try:
        loaded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContextProviderError("project context provider config is not valid JSON") from exc
    if not isinstance(loaded, dict):
        raise ContextProviderError("project context provider config must be a JSON object")
    try:
        return ContextProviderConfig.model_validate(loaded)
    except ValueError as exc:
        raise ContextProviderError(f"project context provider config is invalid: {exc}") from exc


def project_context_providers_enabled() -> bool:
    """Whether repo-local provider commands may execute on this host."""

    return os.environ.get("LOOPLANE_ENABLE_PROJECT_HOOKS") == "1"


def load_project_context_provider_runner(project_root: Path) -> ContextProviderRunner:
    """Load an enabled context provider runner, or an empty runner when disabled."""

    if not project_context_providers_enabled():
        return ContextProviderRunner()
    config = load_project_context_provider_config(project_root)
    return ContextProviderRunner(config, cwd=project_root)


def _parse_provider_output(
    provider_name: str,
    value: object,
) -> tuple[RuntimeInjectedContext, ...]:
    raw_items = value["items"] if isinstance(value, dict) and "items" in value else [value]
    if not isinstance(raw_items, list):
        raise ContextProviderError(f"context provider items must be an array: {provider_name}")
    items: list[RuntimeInjectedContext] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ContextProviderError(f"context provider item must be an object: {provider_name}")
        try:
            items.append(RuntimeInjectedContext.model_validate(raw))
        except ValueError as exc:
            raise ContextProviderError(
                f"context provider item is invalid: {provider_name}: {exc}"
            ) from exc
    return tuple(items)
