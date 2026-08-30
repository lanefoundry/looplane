"""Non-secret defaults for the human-facing command line."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

from looplane.events import atomic_write_json

SUPPORTED_PROVIDERS = frozenset(
    {
        "anthropic",
        "gemini",
        "ollama",
        "openai-codex",
        "openai-compatible",
        "workers-ai",
        "openrouter",
        "deepseek",
        "groq",
        "moonshotai",
        "zai",
        "xai",
        "nvidia-nim",
        "opencode-zen",
        "ollama-cloud",
    }
)
SUPPORTED_RUNTIMES = frozenset(
    {"looplane-agent", "claude-code", "codex-cli", "opencode", "pi", "omp"}
)
MAX_CONFIG_BYTES = 64 * 1024
MAX_DENY_RULES = 128
MAX_DENY_RULE_CHARS = 1024
MAX_ALLOW_RULES = 128
MAX_ALLOW_RULE_CHARS = 1024
MAX_SANDBOX_READ_ROOTS = 64
SUPPORTED_SANDBOX_PROFILES = frozenset({"verification"})
SUPPORTED_SANDBOX_BACKENDS = frozenset({"auto", "bubblewrap", "landlock"})


class CliConfig(BaseModel):
    """Safe-to-persist CLI defaults; credentials are deliberately absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str | None = None
    model: str | None = None
    api_url: str | None = None
    runtime: str | None = None
    runtime_model: str | None = None
    statusline_command: str | None = None
    deny_rules: tuple[str, ...] = ()
    allow_rules: tuple[str, ...] = ()
    sandbox_profile: str | None = None
    sandbox_backend: str | None = None
    sandbox_read_roots: tuple[str, ...] = ()

    @field_validator("runtime")
    @classmethod
    def validate_runtime(cls, value: str | None) -> str | None:
        value = _normalized(value)
        if value == "pca-agent":
            value = "looplane-agent"
        if value is not None and value not in SUPPORTED_RUNTIMES:
            choices = ", ".join(sorted(SUPPORTED_RUNTIMES))
            raise ValueError(f"runtime must be one of: {choices}")
        return value

    @field_validator("runtime_model")
    @classmethod
    def validate_runtime_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized or len(normalized) > 256 or not normalized.isprintable():
            raise ValueError("runtime_model must be a printable model name")
        return normalized

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str | None) -> str | None:
        value = _normalized(value)
        if value is not None and value not in SUPPORTED_PROVIDERS:
            choices = ", ".join(sorted(SUPPORTED_PROVIDERS))
            raise ValueError(f"provider must be one of: {choices}")
        return value

    @field_validator("model")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        return _normalized(value)

    @field_validator("deny_rules")
    @classmethod
    def validate_deny_rules(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_DENY_RULES:
            raise ValueError(f"deny_rules cannot contain more than {MAX_DENY_RULES} entries")
        normalized: list[str] = []
        from looplane.permissions import DenyRule

        for rule in value:
            rule = _normalized(rule)
            assert rule is not None
            if len(rule) > MAX_DENY_RULE_CHARS:
                raise ValueError(
                    f"deny_rules entries cannot exceed {MAX_DENY_RULE_CHARS} characters"
                )
            DenyRule.parse(rule)
            normalized.append(rule)
        return tuple(normalized)

    @field_validator("allow_rules")
    @classmethod
    def validate_allow_rules(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_ALLOW_RULES:
            raise ValueError(f"allow_rules cannot contain more than {MAX_ALLOW_RULES} entries")
        normalized: list[str] = []
        from looplane.permissions import AllowRule

        for rule in value:
            rule = _normalized(rule)
            assert rule is not None
            if len(rule) > MAX_ALLOW_RULE_CHARS:
                raise ValueError(
                    f"allow_rules entries cannot exceed {MAX_ALLOW_RULE_CHARS} characters"
                )
            AllowRule.parse(rule)
            normalized.append(rule)
        return tuple(normalized)

    @field_validator("sandbox_profile")
    @classmethod
    def validate_sandbox_profile(cls, value: str | None) -> str | None:
        value = _normalized(value)
        if value is not None and value not in SUPPORTED_SANDBOX_PROFILES:
            choices = ", ".join(sorted(SUPPORTED_SANDBOX_PROFILES))
            raise ValueError(f"sandbox_profile must be one of: {choices}")
        return value

    @field_validator("sandbox_backend")
    @classmethod
    def validate_sandbox_backend(cls, value: str | None) -> str | None:
        value = _normalized(value)
        if value is not None and value not in SUPPORTED_SANDBOX_BACKENDS:
            choices = ", ".join(sorted(SUPPORTED_SANDBOX_BACKENDS))
            raise ValueError(f"sandbox_backend must be one of: {choices}")
        return value

    @field_validator("sandbox_read_roots")
    @classmethod
    def validate_sandbox_read_roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > MAX_SANDBOX_READ_ROOTS:
            raise ValueError(
                f"sandbox_read_roots cannot contain more than {MAX_SANDBOX_READ_ROOTS} entries"
            )
        normalized: list[str] = []
        for root in value:
            normalized_root = _normalized(root)
            assert normalized_root is not None
            if not normalized_root.isprintable():
                raise ValueError("sandbox_read_roots entries must be printable paths")
            normalized.append(normalized_root)
        return tuple(dict.fromkeys(normalized))

    @field_validator("api_url")
    @classmethod
    def validate_api_url(cls, value: str | None) -> str | None:
        value = _normalized(value)
        if value is None:
            return None
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError("api_url must be an absolute URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("api_url cannot contain credentials, a query, or a fragment")
        return value.rstrip("/")


def _normalized(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("configured values cannot be blank")
    if "\x00" in value:
        raise ValueError("configured values cannot contain NUL")
    return value


def default_cli_config_path() -> Path:
    """Resolve the application config path without ever placing secrets in it."""

    configured = os.environ.get("LOOPLANE_CONFIG") or os.environ.get("PCA_CONFIG")
    if configured:
        return Path(configured)
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_root / "looplane" / "config.json"


def _legacy_cli_config_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_root / "python-coding-agent" / "config.json"


def load_cli_config(path: Path | None = None) -> CliConfig:
    """Load a small, strict config file and fail closed on unsafe file types."""

    if path is None:
        path = default_cli_config_path()
        if (
            not path.exists()
            and not os.environ.get("LOOPLANE_CONFIG")
            and not os.environ.get("PCA_CONFIG")
        ):
            legacy_path = _legacy_cli_config_path()
            if legacy_path.exists():
                path = legacy_path
    if not path.exists():
        return CliConfig()
    if path.is_symlink() or not path.is_file():
        raise ValueError("CLI config must be a regular file, not a symlink")
    with path.open("rb") as file:
        payload = file.read(MAX_CONFIG_BYTES + 1)
    if len(payload) > MAX_CONFIG_BYTES:
        raise ValueError("CLI config exceeds 64 KiB")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("CLI config is not valid UTF-8 JSON") from exc
    return CliConfig.model_validate(value)


async def save_cli_config(config: CliConfig, path: Path | None = None) -> Path:
    """Atomically persist only the explicitly non-secret config schema."""

    path = path or default_cli_config_path()
    if path.is_symlink():
        raise ValueError("refusing to replace a symlink CLI config")
    await atomic_write_json(path, config)
    os.chmod(path, 0o600)
    return path
