from __future__ import annotations

import json
import sys

import pytest

from looplane.context_providers import (
    ContextProviderCommand,
    ContextProviderConfig,
    ContextProviderError,
    ContextProviderRunner,
    load_project_context_provider_config,
)


def test_load_project_context_provider_config_is_strict(tmp_path) -> None:
    directory = tmp_path / ".looplane"
    directory.mkdir()
    (directory / "context-providers.json").write_text(
        json.dumps(
            {
                "providers": [
                    {
                        "name": "ide",
                        "command": [sys.executable, "context.py"],
                        "timeout_seconds": 3,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    config = load_project_context_provider_config(tmp_path)

    assert config.providers[0].name == "ide"
    assert config.providers[0].command == (sys.executable, "context.py")


def test_context_provider_runner_collects_runtime_injected_context(tmp_path) -> None:
    provider = tmp_path / "provider.py"
    provider.write_text(
        """
import json
import sys

payload = json.loads(sys.stdin.read())
print(json.dumps({"source": "ide", "content": f"step={payload['payload']['step']}"}))
""".lstrip(),
        encoding="utf-8",
    )
    runner = ContextProviderRunner(
        ContextProviderConfig(
            providers=(
                ContextProviderCommand(
                    name="ide",
                    command=(sys.executable, str(provider)),
                ),
            )
        ),
        cwd=tmp_path,
    )

    items = runner.collect({"step": 2})

    assert len(items) == 1
    assert items[0].source == "ide"
    assert items[0].content == "step=2"


def test_context_provider_runner_rejects_malformed_output(tmp_path) -> None:
    provider = tmp_path / "provider.py"
    provider.write_text("print('not json')\n", encoding="utf-8")
    runner = ContextProviderRunner(
        ContextProviderConfig(
            providers=(
                ContextProviderCommand(
                    name="bad",
                    command=(sys.executable, str(provider)),
                ),
            )
        ),
        cwd=tmp_path,
    )

    with pytest.raises(ContextProviderError, match="not valid JSON"):
        runner.collect({"step": 1})
