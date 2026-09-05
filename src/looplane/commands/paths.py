"""Paths command services."""

from __future__ import annotations

import os
from pathlib import Path


def _default_run_root() -> Path:
    configured = os.environ.get("LOOPLANE_RUN_ROOT") or os.environ.get("PCA_RUN_ROOT")
    if configured:
        return Path(configured)
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    root = state_root / "looplane" / "runs"
    legacy_root = state_root / "python-coding-agent" / "runs"
    return legacy_root if not root.exists() and legacy_root.exists() else root


DEFAULT_RUN_ROOT = _default_run_root()


def _codex_credential_path() -> Path:
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    path = state_root / "looplane" / "auth" / "openai-codex.json"
    legacy_path = state_root / "python-coding-agent" / "auth" / "openai-codex.json"
    return legacy_path if not path.exists() and legacy_path.exists() else path
