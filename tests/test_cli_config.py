from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from coding_agent.cli_config import CliConfig, load_cli_config, save_cli_config


async def test_cli_config_round_trip_is_strict_non_secret_and_private(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.json"
    config = CliConfig(
        provider="ollama",
        model="qwen3:4b",
        api_url="http://127.0.0.1:11434/v1/",
    )

    await save_cli_config(config, path)

    assert load_cli_config(path) == CliConfig(
        provider="ollama",
        model="qwen3:4b",
        api_url="http://127.0.0.1:11434/v1",
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert set(json.loads(path.read_text())) == {"provider", "model", "api_url"}


def test_cli_config_rejects_credentials_unknown_fields_and_symlinks(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="credentials"):
        CliConfig(api_url="https://secret@example.test/v1")
    with pytest.raises(ValidationError, match="Extra inputs"):
        CliConfig.model_validate({"provider": "ollama", "api_key": "must-not-persist"})

    real = tmp_path / "real.json"
    real.write_text('{"provider":"ollama"}')
    link = tmp_path / "config.json"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        load_cli_config(link)
