from __future__ import annotations

import os
from pathlib import Path

_SAFE_ENV_KEYS = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "TZ",
}


_SENSITIVE_ENV_MARKERS = ("API", "AUTH", "CREDENTIAL", "GITHUB", "PASSWORD", "SECRET", "TOKEN")


def sanitized_subprocess_env(*, task_home: Path | None = None) -> dict[str, str]:
    """Build a minimal environment that excludes host credentials and API secrets."""

    env = {key: value for key, value in os.environ.items() if key in _SAFE_ENV_KEYS}
    env["PATH"] = env.get("PATH", os.defpath)
    env.update(
        {
            "GIT_ASKPASS": "/usr/bin/false",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if task_home is not None:
        task_home.mkdir(parents=True, exist_ok=True)
        task_tmp = task_home / "tmp"
        task_tmp.mkdir(parents=True, exist_ok=True)
        env["CODING_AGENT_TASK_HOME"] = str(task_home)
        env["XDG_CACHE_HOME"] = str(task_home / "cache")
        env["XDG_CONFIG_HOME"] = str(task_home / "config")
        env["PIP_CACHE_DIR"] = str(task_home / "pip-cache")
        env["UV_CACHE_DIR"] = str(task_home / "uv-cache")
        env["TMPDIR"] = str(task_tmp)
    assert not any(marker in key.upper() for key in env for marker in _SENSITIVE_ENV_MARKERS)
    return env
