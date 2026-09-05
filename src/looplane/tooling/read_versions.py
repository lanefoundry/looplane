"""Complete-read version ownership shared by file reads, edits and restoration."""

from __future__ import annotations

import hashlib

from looplane.tooling.types import ToolExecutionError


class ReadVersionStore:
    def __init__(self) -> None:
        self.versions: dict[str, str] = {}

    def record(self, relative_path: str, content: bytes) -> None:
        self.versions[relative_path] = hashlib.sha256(content).hexdigest()

    def require_current(self, relative_path: str, content: bytes) -> None:
        read_version = self.versions.get(relative_path)
        current_version = hashlib.sha256(content).hexdigest()
        if read_version is None:
            raise ToolExecutionError("read_file must be called before replace_text")
        if read_version != current_version:
            raise ToolExecutionError("file changed after read_file; read it again before editing")

    def forget(self, relative_path: str) -> None:
        self.versions.pop(relative_path, None)
