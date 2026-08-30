from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from looplane.cloudflare_provider_setup import (
    ProviderSetupError,
    load_provider_manifest,
    load_secret_env_file,
    parse_provider_manifest,
    provider_catalog_json,
    resolve_provider_secrets,
    setup_cloudflare_providers,
)


def _manifest(profiles: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "default": "groq-fast",
        "profiles": profiles
        or {"groq-fast": {"provider": "groq", "model": "llama-3.3-70b-versatile"}},
    }


def _write_manifest(tmp_path: Path, value: object) -> Path:
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_shorthand_derives_endpoint_binding_and_existing_key_env() -> None:
    manifest = parse_provider_manifest(json.dumps(_manifest()))
    profile = manifest.profiles["groq-fast"]

    assert profile.protocol == "openai-chat"
    assert profile.api_url == "https://api.groq.com/openai/v1/chat/completions"
    assert profile.api_key_binding == "MODEL_PROVIDER_KEY_GROQ"
    assert profile.api_key_env == "GROQ_API_KEY"


def test_catalog_strips_api_key_env() -> None:
    catalog = provider_catalog_json(parse_provider_manifest(json.dumps(_manifest())))

    assert "GROQ_API_KEY" not in catalog
    assert json.loads(catalog) == {
        "default": "groq-fast",
        "profiles": {
            "groq-fast": {
                "provider": "groq",
                "protocol": "openai-chat",
                "model": "llama-3.3-70b-versatile",
                "apiUrl": "https://api.groq.com/openai/v1/chat/completions",
                "apiKeyBinding": "MODEL_PROVIDER_KEY_GROQ",
            }
        },
    }


def test_custom_provider_requires_all_safe_routing_fields() -> None:
    custom = {
        "custom": {
            "provider": "acme",
            "model": "acme-1",
            "apiUrl": "https://models.example/v1/chat/completions",
            "apiKeyBinding": "MODEL_PROVIDER_KEY_ACME",
            "apiKeyEnv": "ACME_API_KEY",
        }
    }
    value = {"default": "custom", "profiles": custom}

    with pytest.raises(ProviderSetupError, match="explicit opt-in"):
        parse_provider_manifest(json.dumps(value))

    profile = parse_provider_manifest(json.dumps(value), allow_custom_endpoint=True).profiles[
        "custom"
    ]
    assert profile.api_key_env == "ACME_API_KEY"

    del custom["custom"]["apiKeyEnv"]
    with pytest.raises(ProviderSetupError, match="require.*together"):
        parse_provider_manifest(json.dumps(value), allow_custom_endpoint=True)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(extra=True),
        lambda value: value["profiles"]["groq-fast"].update(extra=True),
        lambda value: value["profiles"]["groq-fast"].update(protocol="responses"),
        lambda value: value.update(default="missing"),
        lambda value: value["profiles"].update(
            bad={
                "provider": "acme",
                "model": "m",
                "apiUrl": "https://user:pass@example.com/v1/chat/completions",
                "apiKeyBinding": "MODEL_PROVIDER_KEY_ACME",
                "apiKeyEnv": "ACME_API_KEY",
            }
        ),
    ],
)
def test_invalid_or_unknown_manifest_fields_are_rejected(mutation) -> None:
    value = _manifest()
    mutation(value)

    with pytest.raises(ProviderSetupError):
        parse_provider_manifest(json.dumps(value), allow_custom_endpoint=True)


def test_rejects_query_fragment_bad_binding_and_unsafe_env() -> None:
    base = {
        "provider": "acme",
        "model": "m",
        "apiUrl": "https://example.com/v1/chat/completions?token=bad#fragment",
        "apiKeyBinding": "RUN_TOKEN_SECRET",
        "apiKeyEnv": "BAD-ENV",
    }
    value = {"default": "custom", "profiles": {"custom": base}}

    with pytest.raises(ProviderSetupError):
        parse_provider_manifest(json.dumps(value), allow_custom_endpoint=True)


def test_resolve_all_secrets_fails_before_returning_partial_values() -> None:
    profiles = {
        "groq-fast": {"provider": "groq", "model": "model-a"},
        "router": {"provider": "openrouter", "model": "model-b"},
    }
    manifest = parse_provider_manifest(json.dumps({"default": "groq-fast", "profiles": profiles}))

    with pytest.raises(ProviderSetupError, match="OPENROUTER_API_KEY") as raised:
        resolve_provider_secrets(manifest, {"GROQ_API_KEY": "never-leak-this"})
    assert "never-leak-this" not in str(raised.value)


def test_setup_runs_secret_bulk_then_build_then_deploy_without_secret_in_argv(
    tmp_path: Path,
) -> None:
    path = _write_manifest(tmp_path, _manifest())
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    result = setup_cloudflare_providers(
        path,
        cloudflare_dir=tmp_path,
        environ={"GROQ_API_KEY": "super-secret"},
        wrangler_env="production",
        runner=runner,
    )

    assert calls[0][0][:4] == ["npx", "wrangler", "secret", "bulk"]
    assert calls[1][0] == ["npm", "run", "build:runtime"]
    assert calls[2][0][:3] == ["npx", "wrangler", "deploy"]
    assert calls[0][0] == ["npx", "wrangler", "secret", "bulk", "--env", "production"]
    assert json.loads(calls[0][1]["input"]) == {"MODEL_PROVIDER_KEY_GROQ": "super-secret"}
    assert all("super-secret" not in argument for argv, _ in calls for argument in argv)
    assert all(kwargs["cwd"] == tmp_path for _, kwargs in calls)
    assert all(kwargs["text"] is True and kwargs["check"] is True for _, kwargs in calls)
    assert all("GROQ_API_KEY" not in kwargs["env"] for _, kwargs in calls)
    assert all("super-secret" not in kwargs["env"].values() for _, kwargs in calls)
    assert "GROQ_API_KEY" not in calls[2][0][-1]
    assert result.profile_count == 1


def test_missing_secret_stops_before_any_subprocess(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, _manifest())
    calls: list[object] = []

    with pytest.raises(ProviderSetupError, match="GROQ_API_KEY"):
        setup_cloudflare_providers(
            path,
            cloudflare_dir=tmp_path,
            environ={},
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
    assert calls == []


def test_dry_run_skips_secret_bulk_and_adds_safe_output_directory(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, _manifest())
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    setup_cloudflare_providers(
        path,
        cloudflare_dir=tmp_path,
        environ={"GROQ_API_KEY": "secret"},
        dry_run=True,
        runner=runner,
    )

    assert calls[0] == ["npm", "run", "build:runtime"]
    assert calls[1][-3:] == [
        "--dry-run",
        "--outdir",
        ".wrangler/provider-setup-dry-run",
    ]
    assert "--dry-run" in calls[1]
    assert "secret" not in " ".join(argument for call in calls for argument in call)


def test_subprocess_failure_does_not_echo_secret(tmp_path: Path) -> None:
    path = _write_manifest(tmp_path, _manifest())

    def fail(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, argv)

    with pytest.raises(ProviderSetupError) as raised:
        setup_cloudflare_providers(
            path,
            cloudflare_dir=tmp_path,
            environ={"GROQ_API_KEY": "top-secret-value"},
            runner=fail,
        )
    assert "top-secret-value" not in str(raised.value)


def test_load_manifest_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    path.write_text('{"default":"a","default":"b","profiles":{}}', encoding="utf-8")

    with pytest.raises(ProviderSetupError, match="duplicate"):
        load_provider_manifest(path)


def test_private_secrets_file_overrides_environment_and_ignores_unreferenced_values(
    tmp_path: Path,
) -> None:
    manifest_path = _write_manifest(tmp_path, _manifest())
    secrets_path = tmp_path / "provider.env"
    secrets_path.write_text(
        "GROQ_API_KEY='file-secret'\nUNUSED_SECRET=do-not-upload\n", encoding="utf-8"
    )
    secrets_path.chmod(0o600)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    setup_cloudflare_providers(
        manifest_path,
        cloudflare_dir=tmp_path,
        environ={"GROQ_API_KEY": "environment-secret"},
        secrets_env_file=secrets_path,
        runner=runner,
    )

    payload = json.loads(calls[0][1]["input"])
    assert payload == {"MODEL_PROVIDER_KEY_GROQ": "file-secret"}
    assert "UNUSED_SECRET" not in calls[0][1]["input"]
    assert "do-not-upload" not in calls[0][1]["input"]


def test_first_deploy_control_secrets_join_the_same_bulk_upload(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path, _manifest())
    secrets_path = tmp_path / "provider.env"
    secrets_path.write_text(
        "CONTROL_PLANE_TOKEN=control-token-1234\n"
        "RUN_TOKEN_SECRET=run-token-secret-12345678901234567890\n"
        "GROQ_API_KEY=provider-secret\n",
        encoding="utf-8",
    )
    secrets_path.chmod(0o600)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    setup_cloudflare_providers(
        manifest_path,
        cloudflare_dir=tmp_path,
        environ={},
        secrets_env_file=secrets_path,
        runner=runner,
    )

    payload = json.loads(calls[0][1]["input"])
    assert set(payload) == {
        "CONTROL_PLANE_TOKEN",
        "RUN_TOKEN_SECRET",
        "MODEL_PROVIDER_KEY_GROQ",
    }
    assert all(
        secret not in value
        for _, kwargs in calls
        for value in kwargs["env"].values()
        for secret in payload.values()
    )


def test_invalid_optional_control_secret_stops_before_subprocess(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path, _manifest())
    calls: list[object] = []

    with pytest.raises(ProviderSetupError, match="CONTROL_PLANE_TOKEN"):
        setup_cloudflare_providers(
            manifest_path,
            cloudflare_dir=tmp_path,
            environ={"GROQ_API_KEY": "provider-secret", "CONTROL_PLANE_TOKEN": "short"},
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
    assert calls == []


@pytest.mark.parametrize(
    "contents",
    [
        "export GROQ_API_KEY=secret\n",
        "GROQ_API_KEY=secret\nGROQ_API_KEY=again\n",
        "GROQ_API_KEY=\n",
        'GROQ_API_KEY="unterminated\n',
        "GROQ_API_KEY=bad\0value\n",
    ],
)
def test_secrets_file_rejects_unsafe_syntax(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "provider.env"
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(ProviderSetupError):
        load_secret_env_file(path, referenced_names={"GROQ_API_KEY"})


def test_secrets_file_rejects_broad_permissions_and_symlinks(tmp_path: Path) -> None:
    path = tmp_path / "provider.env"
    path.write_text("GROQ_API_KEY=secret\n", encoding="utf-8")
    path.chmod(0o640)
    with pytest.raises(ProviderSetupError, match="group or other"):
        load_secret_env_file(path, referenced_names={"GROQ_API_KEY"})

    path.chmod(0o600)
    link = tmp_path / "provider-link.env"
    link.symlink_to(path)
    with pytest.raises(ProviderSetupError, match="non-symlink"):
        load_secret_env_file(link, referenced_names={"GROQ_API_KEY"})
