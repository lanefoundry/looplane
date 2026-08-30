from __future__ import annotations

import stat
from pathlib import Path

import pytest

from looplane.native_credentials import (
    NATIVE_CREDENTIAL_FIELDS,
    NativeCredentialStore,
    clear_native_credential,
    missing_native_fields,
    native_credential_path,
    resolve_native_field,
    save_native_credential,
)


def test_native_credential_path_uses_xdg_state_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    expected = tmp_path / "looplane" / "auth" / "native-anthropic.json"
    assert native_credential_path("anthropic") == expected


def test_save_load_roundtrip_is_0600_and_never_a_symlink(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert missing_native_fields("anthropic") == ("api_key",)
    path = save_native_credential("anthropic", {"api_key": "sk-secret"})

    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert resolve_native_field("anthropic", "api_key") == "sk-secret"
    assert missing_native_fields("anthropic") == ()


def test_env_var_takes_precedence_over_stored_credential(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    save_native_credential("anthropic", {"api_key": "stored-secret"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-secret")

    assert resolve_native_field("anthropic", "api_key") == "env-secret"


def test_gemini_accepts_either_env_alias(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-secret")

    assert resolve_native_field("gemini", "api_key") == "google-secret"


def test_workers_ai_requires_both_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

    assert set(missing_native_fields("workers-ai")) == {"account_id", "api_token"}
    save_native_credential("workers-ai", {"account_id": "acc", "api_token": "tok"})
    assert missing_native_fields("workers-ai") == ()


def test_clear_native_credential_removes_the_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    save_native_credential("anthropic", {"api_key": "sk-secret"})

    assert clear_native_credential("anthropic") is True
    assert clear_native_credential("anthropic") is False
    assert missing_native_fields("anthropic") == ("api_key",)


def test_save_native_credential_rejects_unsupported_provider() -> None:
    with pytest.raises(ValueError, match="unsupported provider"):
        save_native_credential("ollama", {"api_key": "x"})


def test_save_native_credential_rejects_wrong_fields(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="requires exactly"):
        save_native_credential("anthropic", {"api_key": "x", "extra": "y"})


def test_save_native_credential_rejects_blank_value(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    with pytest.raises(ValueError, match="cannot be blank"):
        save_native_credential("anthropic", {"api_key": "  "})


def test_load_rejects_symlinked_credential_file(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text('{"api_key": "sk-secret"}', encoding="utf-8")
    link = tmp_path / "auth.json"
    link.symlink_to(real)

    with pytest.raises(PermissionError, match="regular file"):
        NativeCredentialStore(link).load()


def test_load_rejects_loose_permissions(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    path.write_text('{"api_key": "sk-secret"}', encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(PermissionError, match="0600"):
        NativeCredentialStore(path).load()


def test_native_credential_fields_cover_every_key_based_provider() -> None:
    assert NATIVE_CREDENTIAL_FIELDS.keys() == {
        "anthropic",
        "gemini",
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


@pytest.mark.parametrize(
    ("provider", "env_var"),
    [
        ("openrouter", "OPENROUTER_API_KEY"),
        ("deepseek", "DEEPSEEK_API_KEY"),
        ("groq", "GROQ_API_KEY"),
        ("moonshotai", "MOONSHOT_API_KEY"),
        ("zai", "ZAI_API_KEY"),
        ("xai", "XAI_API_KEY"),
        ("nvidia-nim", "NVIDIA_API_KEY"),
        ("opencode-zen", "OPENCODE_ZEN_API_KEY"),
        ("ollama-cloud", "OLLAMA_CLOUD_API_KEY"),
    ],
)
def test_new_openai_compatible_providers_resolve_from_env_or_store(
    provider: str, env_var: str, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv(env_var, raising=False)

    assert missing_native_fields(provider) == ("api_key",)
    save_native_credential(provider, {"api_key": "stored-secret"})
    assert resolve_native_field(provider, "api_key") == "stored-secret"

    monkeypatch.setenv(env_var, "env-secret")
    assert resolve_native_field(provider, "api_key") == "env-secret"
