"""Validated batch setup for Cloudflare-hosted model provider profiles."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from rivumi.provider_catalog import OPENAI_COMPATIBLE_BASE_URLS

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_BINDING_RE = re.compile(r"^MODEL_PROVIDER_KEY_[A-Z0-9_]{1,64}$")
_ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_WRANGLER_ENV_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PROFILE_FIELDS = frozenset(
    {"provider", "protocol", "model", "apiUrl", "apiKeyBinding", "apiKeyEnv"}
)
_PROVIDER_KEY_ENVS = {
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "groq": "GROQ_API_KEY",
    "moonshotai": "MOONSHOT_API_KEY",
    "zai": "ZAI_API_KEY",
    "xai": "XAI_API_KEY",
    "nvidia-nim": "NVIDIA_API_KEY",
    "opencode-zen": "OPENCODE_ZEN_API_KEY",
    "ollama-cloud": "OLLAMA_CLOUD_API_KEY",
}
_OPTIONAL_CONTROL_SECRETS = {
    "CONTROL_PLANE_TOKEN": 16,
    "RUN_TOKEN_SECRET": 32,
}


class ProviderSetupError(ValueError):
    """A safe, non-secret-bearing provider setup failure."""


@dataclass(frozen=True)
class ProviderProfile:
    provider: str
    protocol: str
    model: str
    api_url: str
    api_key_binding: str
    api_key_env: str


@dataclass(frozen=True)
class ProviderManifest:
    default: str
    profiles: Mapping[str, ProviderProfile]


@dataclass(frozen=True)
class ProviderSetupResult:
    catalog_json: str
    profile_count: int
    dry_run: bool


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderSetupError("provider manifest contains a duplicate field")
        result[key] = value
    return result


def _safe_json_load(raw: str) -> object:
    if len(raw.encode("utf-8")) > 64_000:
        raise ProviderSetupError("provider manifest exceeds 64000 bytes")
    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except ProviderSetupError:
        raise
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderSetupError("provider manifest is not valid JSON") from exc


def _validate_url(value: object) -> str:
    if not isinstance(value, str):
        raise ProviderSetupError("profile apiUrl must be a string")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ProviderSetupError("profile apiUrl is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith("/chat/completions")
        or "//" in parsed.path
    ):
        raise ProviderSetupError(
            "profile apiUrl must be a credential-free HTTPS chat-completions endpoint"
        )
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    authority = hostname if port is None else f"{hostname}:{port}"
    return urlunsplit(("https", authority, parsed.path, "", ""))


def _required_text(candidate: Mapping[str, object], field: str) -> str:
    value = candidate.get(field)
    if not isinstance(value, str):
        raise ProviderSetupError(f"profile {field} must be a string")
    return value


def _parse_profile(candidate: object, *, allow_custom_endpoint: bool) -> ProviderProfile:
    if not isinstance(candidate, dict):
        raise ProviderSetupError("each provider profile must be an object")
    unknown = set(candidate) - _PROFILE_FIELDS
    if unknown:
        raise ProviderSetupError("provider profile contains an unknown field")

    provider = _required_text(candidate, "provider")
    model = _required_text(candidate, "model")
    protocol = candidate.get("protocol", "openai-chat")
    if not _ID_RE.fullmatch(provider):
        raise ProviderSetupError("profile provider is invalid")
    if protocol != "openai-chat":
        raise ProviderSetupError("profile protocol must be openai-chat")
    if not (1 <= len(model) <= 256) or model.strip() != model or _CONTROL_RE.search(model):
        raise ProviderSetupError("profile model is invalid")

    override_fields = {"apiUrl", "apiKeyBinding", "apiKeyEnv"}
    present_overrides = override_fields.intersection(candidate)
    known_base_url = OPENAI_COMPATIBLE_BASE_URLS.get(provider)
    if known_base_url is not None:
        if present_overrides:
            raise ProviderSetupError("known providers must use the shorthand profile form")
        api_url = _validate_url(f"{known_base_url.rstrip('/')}/chat/completions")
        binding = f"MODEL_PROVIDER_KEY_{provider.upper().replace('-', '_')}"
        key_env = _PROVIDER_KEY_ENVS[provider]
    else:
        if not allow_custom_endpoint:
            raise ProviderSetupError("custom provider endpoints require explicit opt-in")
        if present_overrides != override_fields:
            raise ProviderSetupError(
                "custom providers require apiUrl, apiKeyBinding, and apiKeyEnv together"
            )
        api_url = _validate_url(candidate.get("apiUrl"))
        binding = _required_text(candidate, "apiKeyBinding")
        key_env = _required_text(candidate, "apiKeyEnv")

    if not _BINDING_RE.fullmatch(binding):
        raise ProviderSetupError("profile apiKeyBinding is invalid")
    if not _ENV_RE.fullmatch(key_env):
        raise ProviderSetupError("profile apiKeyEnv is invalid")
    return ProviderProfile(
        provider=provider,
        protocol="openai-chat",
        model=model,
        api_url=api_url,
        api_key_binding=binding,
        api_key_env=key_env,
    )


def parse_provider_manifest(
    raw: str,
    *,
    allow_custom_endpoint: bool = False,
) -> ProviderManifest:
    """Parse and strictly validate one provider setup manifest."""

    value = _safe_json_load(raw)
    if not isinstance(value, dict) or set(value) != {"default", "profiles"}:
        raise ProviderSetupError("provider manifest must contain only default and profiles")
    default = value["default"]
    candidates = value["profiles"]
    if not isinstance(default, str) or not _ID_RE.fullmatch(default):
        raise ProviderSetupError("provider manifest default is invalid")
    if not isinstance(candidates, dict) or not 1 <= len(candidates) <= 16:
        raise ProviderSetupError("provider manifest must contain 1 to 16 profiles")

    profiles: dict[str, ProviderProfile] = {}
    binding_sources: dict[str, str] = {}
    for profile_id, candidate in candidates.items():
        if not _ID_RE.fullmatch(profile_id):
            raise ProviderSetupError("provider profile id is invalid")
        profile = _parse_profile(candidate, allow_custom_endpoint=allow_custom_endpoint)
        prior_source = binding_sources.setdefault(profile.api_key_binding, profile.api_key_env)
        if prior_source != profile.api_key_env:
            raise ProviderSetupError("one secret binding cannot use multiple environment variables")
        profiles[profile_id] = profile
    if default not in profiles:
        raise ProviderSetupError("provider manifest default does not name a profile")
    return ProviderManifest(default=default, profiles=profiles)


def load_provider_manifest(
    path: str | Path,
    *,
    allow_custom_endpoint: bool = False,
) -> ProviderManifest:
    """Read a UTF-8 JSON manifest from disk and validate it."""

    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ProviderSetupError("provider manifest could not be read") from exc
    return parse_provider_manifest(raw, allow_custom_endpoint=allow_custom_endpoint)


def provider_catalog_json(manifest: ProviderManifest) -> str:
    """Return the compact Worker catalog, excluding local environment variable names."""

    profiles = {
        profile_id: {
            "provider": profile.provider,
            "protocol": profile.protocol,
            "model": profile.model,
            "apiUrl": profile.api_url,
            "apiKeyBinding": profile.api_key_binding,
        }
        for profile_id, profile in manifest.profiles.items()
    }
    return json.dumps(
        {"default": manifest.default, "profiles": profiles},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def resolve_provider_secrets(
    manifest: ProviderManifest,
    environ: Mapping[str, str],
) -> dict[str, str]:
    """Resolve every binding at once, failing without disclosing secret values."""

    secrets: dict[str, str] = {}
    missing: list[str] = []
    for profile in manifest.profiles.values():
        value = environ.get(profile.api_key_env)
        if not value or "\0" in value:
            missing.append(profile.api_key_env)
            continue
        secrets[profile.api_key_binding] = value
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise ProviderSetupError(f"missing provider API key environment variables: {names}")
    return secrets


def _include_control_secrets(secrets: dict[str, str], sources: Mapping[str, str]) -> None:
    """Include optional first-deploy control secrets when supplied in the same batch."""

    for name, minimum_bytes in _OPTIONAL_CONTROL_SECRETS.items():
        value = sources.get(name)
        if value is None:
            continue
        if "\0" in value or len(value.encode("utf-8")) < minimum_bytes:
            raise ProviderSetupError(f"{name} must be at least {minimum_bytes} UTF-8 bytes")
        secrets[name] = value


def load_secret_env_file(
    path: str | Path,
    *,
    referenced_names: set[str] | frozenset[str],
) -> dict[str, str]:
    """Read referenced secrets from a strict, private dotenv-style file."""

    secret_path = Path(path)
    try:
        metadata = secret_path.lstat()
    except OSError as exc:
        raise ProviderSetupError("provider secrets file could not be read") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ProviderSetupError("provider secrets file must be a regular non-symlink file")
    if metadata.st_mode & 0o077:
        raise ProviderSetupError("provider secrets file must not allow group or other access")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(secret_path, flags)
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            opened_metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened_metadata.st_mode) or opened_metadata.st_mode & 0o077:
                raise ProviderSetupError("provider secrets file permissions changed while reading")
            raw = handle.read()
    except ProviderSetupError:
        raise
    except (OSError, UnicodeError) as exc:
        raise ProviderSetupError("provider secrets file could not be read") from exc
    if "\0" in raw:
        raise ProviderSetupError("provider secrets file contains a NUL byte")

    selected: dict[str, str] = {}
    seen: set[str] = set()
    for line in raw.splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or "=" not in line:
            raise ProviderSetupError("provider secrets file contains invalid syntax")
        name, value = line.split("=", 1)
        if not _ENV_RE.fullmatch(name) or name in seen:
            raise ProviderSetupError("provider secrets file contains an invalid or duplicate name")
        seen.add(name)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        elif value.startswith(('"', "'")) or value.endswith(('"', "'")):
            raise ProviderSetupError("provider secrets file contains invalid quoting")
        if not value or "\r" in value or "\n" in value or "\0" in value:
            raise ProviderSetupError("provider secrets file contains an empty or multiline value")
        if name in referenced_names:
            selected[name] = value
    return selected


def _run(
    runner: Runner,
    argv: list[str],
    *,
    cloudflare_dir: Path,
    child_env: Mapping[str, str],
    input_text: str | None = None,
) -> None:
    kwargs: dict[str, object] = {
        "cwd": cloudflare_dir,
        "text": True,
        "check": True,
        "env": dict(child_env),
    }
    if input_text is not None:
        kwargs["input"] = input_text
    try:
        runner(argv, **kwargs)
    except (OSError, subprocess.CalledProcessError):
        command = " ".join(argv[:3])
        raise ProviderSetupError(f"provider setup command failed: {command}") from None


def setup_cloudflare_providers(
    manifest_path: str | Path,
    *,
    cloudflare_dir: str | Path,
    environ: Mapping[str, str] | None = None,
    secrets_env_file: str | Path | None = None,
    allow_custom_endpoint: bool = False,
    wrangler_env: str | None = None,
    dry_run: bool = False,
    runner: Runner = subprocess.run,
) -> ProviderSetupResult:
    """Provision all provider secrets and deploy the catalog in one validated batch."""

    manifest = load_provider_manifest(
        manifest_path,
        allow_custom_endpoint=allow_custom_endpoint,
    )
    catalog = provider_catalog_json(manifest)
    process_environ = os.environ if environ is None else environ
    secret_sources = dict(process_environ)
    if secrets_env_file is not None:
        referenced_names = frozenset(
            {profile.api_key_env for profile in manifest.profiles.values()}
            | set(_OPTIONAL_CONTROL_SECRETS)
        )
        secret_sources.update(
            load_secret_env_file(secrets_env_file, referenced_names=referenced_names)
        )
    secrets = resolve_provider_secrets(manifest, secret_sources)
    _include_control_secrets(secrets, secret_sources)
    secret_values = frozenset(secrets.values())
    secret_source_names = frozenset(
        {profile.api_key_env for profile in manifest.profiles.values()}
        | set(_OPTIONAL_CONTROL_SECRETS)
    )
    child_env = {
        name: value
        for name, value in os.environ.items()
        if name not in secret_source_names and value not in secret_values
    }
    cloudflare_path = Path(cloudflare_dir)
    if wrangler_env is not None and not _WRANGLER_ENV_RE.fullmatch(wrangler_env):
        raise ProviderSetupError("wrangler environment name is invalid")
    env_args = [] if wrangler_env is None else ["--env", wrangler_env]

    if not dry_run:
        secret_argv = ["npx", "wrangler", "secret", "bulk", *env_args]
        secret_payload = json.dumps(secrets, separators=(",", ":"))
        _run(
            runner,
            secret_argv,
            cloudflare_dir=cloudflare_path,
            child_env=child_env,
            input_text=secret_payload,
        )
    _run(
        runner,
        ["npm", "run", "build:runtime"],
        cloudflare_dir=cloudflare_path,
        child_env=child_env,
    )
    deploy_argv = [
        "npx",
        "wrangler",
        "deploy",
        *env_args,
        "--var",
        f"MODEL_PROFILES_JSON:{catalog}",
    ]
    if dry_run:
        deploy_argv.extend(
            ["--dry-run", "--outdir", ".wrangler/provider-setup-dry-run"]
        )
    _run(runner, deploy_argv, cloudflare_dir=cloudflare_path, child_env=child_env)
    return ProviderSetupResult(
        catalog_json=catalog,
        profile_count=len(manifest.profiles),
        dry_run=dry_run,
    )
