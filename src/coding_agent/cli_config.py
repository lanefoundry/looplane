"""Non-secret defaults for the human-facing command line."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator

from coding_agent.events import atomic_write_json

SUPPORTED_PROVIDERS = frozenset(
    {
        "anthropic",
        "gemini",
        "ollama",
        "openai-codex",
        "openai-compatible",
        "workers-ai",
    }
)
MAX_CONFIG_BYTES = 64 * 1024


class CliConfig(BaseModel):
    """Safe-to-persist CLI defaults; credentials are deliberately absent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str | None = None
    model: str | None = None
    api_url: str | None = None

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

    configured = os.environ.get("PCA_CONFIG")
    if configured:
        return Path(configured)
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_root / "python-coding-agent" / "config.json"


def load_cli_config(path: Path | None = None) -> CliConfig:
    """Load a small, strict config file and fail closed on unsafe file types."""

    path = path or default_cli_config_path()
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
