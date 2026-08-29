from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from rivumi.cli_config import (
    CliConfig,
    default_cli_config_path,
    load_cli_config,
    save_cli_config,
)
from rivumi.policy_config import ProjectPolicyError, load_project_policy_config


async def test_cli_config_round_trip_is_strict_non_secret_and_private(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.json"
    config = CliConfig(
        provider="ollama",
        model="qwen3:4b",
        api_url="http://127.0.0.1:11434/v1/",
        deny_rules=("read_file(.env*)",),
        allow_rules=("run_check(pytest:*)",),
        sandbox_profile="verification",
        sandbox_read_roots=(" ~/cache ", "~/cache", "/opt/toolchain"),
    )

    await save_cli_config(config, path)

    assert load_cli_config(path) == CliConfig(
        provider="ollama",
        model="qwen3:4b",
        api_url="http://127.0.0.1:11434/v1",
        deny_rules=("read_file(.env*)",),
        allow_rules=("run_check(pytest:*)",),
        sandbox_profile="verification",
        sandbox_read_roots=("~/cache", "/opt/toolchain"),
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert set(json.loads(path.read_text())) == {
        "provider",
        "model",
        "api_url",
        "runtime",
        "runtime_model",
        "statusline_command",
        "deny_rules",
        "allow_rules",
        "sandbox_profile",
        "sandbox_read_roots",
    }


def test_cli_config_rejects_credentials_unknown_fields_and_symlinks(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="credentials"):
        CliConfig(api_url="https://secret@example.test/v1")
    with pytest.raises(ValidationError, match="Extra inputs"):
        CliConfig.model_validate({"provider": "ollama", "api_key": "must-not-persist"})
    with pytest.raises(ValidationError, match="invalid deny rule"):
        CliConfig(deny_rules=("not valid",))
    with pytest.raises(ValidationError, match="invalid allow rule"):
        CliConfig(allow_rules=("not valid",))
    with pytest.raises(ValidationError, match="sandbox_profile"):
        CliConfig(sandbox_profile="networked")
    with pytest.raises(ValidationError, match="NUL"):
        CliConfig(sandbox_read_roots=("bad\x00root",))

    real = tmp_path / "real.json"
    real.write_text('{"provider":"ollama"}')
    link = tmp_path / "config.json"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        load_cli_config(link)


def test_cli_config_uses_rivumi_path_and_normalizes_legacy_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("RIVUMI_CONFIG", raising=False)
    monkeypatch.delenv("PCA_CONFIG", raising=False)

    assert default_cli_config_path() == tmp_path / "rivumi" / "config.json"
    assert CliConfig(runtime="pca-agent").runtime == "rivumi-agent"


def test_cli_config_reads_legacy_default_when_new_path_is_absent(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("RIVUMI_CONFIG", raising=False)
    monkeypatch.delenv("PCA_CONFIG", raising=False)
    legacy = tmp_path / "python-coding-agent" / "config.json"
    legacy.parent.mkdir()
    legacy.write_text('{"runtime":"pca-agent","provider":"ollama","model":"qwen3:4b"}')

    assert load_cli_config() == CliConfig(
        runtime="rivumi-agent", provider="ollama", model="qwen3:4b"
    )


@pytest.mark.parametrize("runtime", ["opencode", "pi", "omp"])
async def test_cli_config_accepts_headless_external_runtimes(runtime: str, tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = CliConfig(runtime=runtime, provider=None, model=None)

    await save_cli_config(config, path)

    assert load_cli_config(path) == CliConfig(runtime=runtime)


def test_cli_config_previously_broke_app_startup_for_headless_external_runtimes(
    tmp_path: Path,
) -> None:
    """Regression test for the SUPPORTED_RUNTIMES/runtime_registry drift bug.

    Before this fix, selecting opencode/pi/omp in the runtime picker and saving
    would write a config.json that load_cli_config() could never read back,
    raising ValueError and preventing the CLI/TUI from starting at all.
    """
    path = tmp_path / "config.json"
    path.write_text('{"runtime":"opencode","provider":null,"model":null}')

    assert load_cli_config(path).runtime == "opencode"


def test_project_policy_config_loads_missing_valid_and_invalid_policy(
    tmp_path: Path,
) -> None:
    assert load_project_policy_config(tmp_path).deny_rules == ()

    policy_dir = tmp_path / ".rivumi"
    policy_dir.mkdir()
    policy_path = policy_dir / "policy.json"
    policy_path.write_text(
        json.dumps(
            {
                "deny_rules": [" read_file(.env*) "],
                "allow_rules": ["run_check(pytest:*)"],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_project_policy_config(tmp_path)
    assert loaded.deny_rules == ("read_file(.env*)",)
    assert loaded.allow_rules == ("run_check(pytest:*)",)

    policy_path.write_text('{"deny_rules":["not valid"]}', encoding="utf-8")
    with pytest.raises(ProjectPolicyError, match="project policy is invalid"):
        load_project_policy_config(tmp_path)


def test_implicit_cli_config_load_keeps_project_policy_out_of_config(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": "ollama",
                "deny_rules": ["read_file(.env*)"],
                "allow_rules": ["run_check(pytest:*)"],
            }
        ),
        encoding="utf-8",
    )
    policy_dir = tmp_path / ".rivumi"
    policy_dir.mkdir()
    (policy_dir / "policy.json").write_text(
        json.dumps(
            {
                "deny_rules": ["run_check(git push:*)"],
                "allow_rules": ["read_file(docs/**)"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RIVUMI_CONFIG", str(config_path))

    loaded = load_cli_config()

    assert loaded.provider == "ollama"
    assert loaded.deny_rules == ("read_file(.env*)",)
    assert loaded.allow_rules == ("run_check(pytest:*)",)


def test_explicit_cli_config_load_does_not_validate_cwd_project_policy(
    tmp_path: Path, monkeypatch
) -> None:
    policy_dir = tmp_path / ".rivumi"
    policy_dir.mkdir()
    (policy_dir / "policy.json").write_text('{"allow_rules":["not valid"]}', encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text('{"provider":"ollama"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert load_cli_config(config_path).provider == "ollama"
