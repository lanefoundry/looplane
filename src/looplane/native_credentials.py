"""Local credential storage for the native ``looplane-agent`` runtime's own providers.

Mirrors ``codex_oauth.CodexCredentialStore``: a single-file-per-provider JSON store with
symlink rejection, strict permission checks, and atomic ``0600`` writes. This is a distinct
concept from ``cli_config.CliConfig``, which stays deliberately credential-free.

Every other runtime (``claude-code``, ``codex-cli``, ``opencode``, ``pi``, ``omp``) owns its
authentication entirely outside looplane; looplane never opens, stores, or forwards their
credentials. ``looplane-agent`` is the one runtime where looplane itself calls the provider API, so
it is the one runtime with a looplane-owned credential store.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

# Field names, in prompt order, required to authenticate each provider natively.
NATIVE_CREDENTIAL_FIELDS: dict[str, tuple[str, ...]] = {
    "anthropic": ("api_key",),
    "gemini": ("api_key",),
    "openai-compatible": ("api_key",),
    "workers-ai": ("account_id", "api_token"),
    # Single API key, OpenAI-compatible endpoint providers; base_url is fixed per provider
    # in cli._model_from_env, not user-supplied. Base URLs/env var names verified against
    # @earendil-works/pi-ai's own provider source (the package pi/omp depend on).
    "openrouter": ("api_key",),
    "deepseek": ("api_key",),
    "groq": ("api_key",),
    "moonshotai": ("api_key",),
    "zai": ("api_key",),
    "xai": ("api_key",),
    "nvidia-nim": ("api_key",),
    "opencode-zen": ("api_key",),
    "ollama-cloud": ("api_key",),
}

# Environment variables checked, in order, before falling back to the stored credential.
_ENV_VARS: dict[str, dict[str, tuple[str, ...]]] = {
    "anthropic": {"api_key": ("ANTHROPIC_API_KEY",)},
    "gemini": {"api_key": ("GEMINI_API_KEY", "GOOGLE_API_KEY")},
    "openai-compatible": {"api_key": ("OPENAI_API_KEY",)},
    "workers-ai": {
        "account_id": ("CLOUDFLARE_ACCOUNT_ID",),
        "api_token": ("CLOUDFLARE_API_TOKEN",),
    },
    "openrouter": {"api_key": ("OPENROUTER_API_KEY",)},
    "deepseek": {"api_key": ("DEEPSEEK_API_KEY",)},
    "groq": {"api_key": ("GROQ_API_KEY",)},
    "moonshotai": {"api_key": ("MOONSHOT_API_KEY",)},
    "zai": {"api_key": ("ZAI_API_KEY",)},
    "xai": {"api_key": ("XAI_API_KEY",)},
    "nvidia-nim": {"api_key": ("NVIDIA_API_KEY",)},
    "opencode-zen": {"api_key": ("OPENCODE_ZEN_API_KEY",)},
    "ollama-cloud": {"api_key": ("OLLAMA_CLOUD_API_KEY",)},
}


def native_credential_path(provider: str) -> Path:
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_root / "looplane" / "auth" / f"native-{provider}.json"


class NativeCredentialStore:
    """Single-provider JSON store with symlink rejection and atomic 0600 writes."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    def load(self) -> dict[str, str] | None:
        try:
            metadata = self.path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PermissionError("Native credential path must be a regular file")
        if metadata.st_mode & 0o077:
            raise PermissionError("Native credential file permissions must be 0600")
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Native credential file could not be read") from exc
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise ValueError("Native credential file must contain a flat string object")
        return value

    def save(self, values: Mapping[str, str]) -> None:
        import secrets

        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError as exc:
            raise PermissionError("Native credential directory cannot be secured") from exc
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        try:
            payload = json.dumps(dict(values), separators=(",", ":")).encode()
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def clear(self) -> bool:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True


def resolve_native_field(provider: str, field: str) -> str | None:
    """Env var(s) first, then the stored credential; ``None`` when neither is set."""

    for env_name in _ENV_VARS.get(provider, {}).get(field, ()):
        value = os.environ.get(env_name)
        if value:
            return value
    stored = NativeCredentialStore(native_credential_path(provider)).load()
    if stored is None:
        return None
    value = stored.get(field)
    return value or None


def missing_native_fields(provider: str) -> tuple[str, ...]:
    """Required fields for ``provider`` not satisfied by an env var or the stored credential."""

    fields = NATIVE_CREDENTIAL_FIELDS.get(provider, ())
    return tuple(field for field in fields if resolve_native_field(provider, field) is None)


def save_native_credential(provider: str, values: Mapping[str, str]) -> Path:
    fields = NATIVE_CREDENTIAL_FIELDS.get(provider)
    if fields is None:
        raise ValueError(f"unsupported provider for stored credentials: {provider}")
    if set(values) != set(fields):
        raise ValueError(f"{provider} requires exactly: {', '.join(fields)}")
    for field, value in values.items():
        if not value or not value.strip() or "\x00" in value:
            raise ValueError(f"{field} cannot be blank")
    path = native_credential_path(provider)
    NativeCredentialStore(path).save(values)
    return path


def clear_native_credential(provider: str) -> bool:
    return NativeCredentialStore(native_credential_path(provider)).clear()
