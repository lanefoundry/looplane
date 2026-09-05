"""Compatibility host retaining the historical session factory patch point."""

from pathlib import Path

from looplane.codex_app_server import CodexAppServerSession
from looplane.conversation_workspace import ConversationWorkspace as ConversationWorkspace
from looplane.runtimes.codex.conversation import IsolatedCodexConversation as _CanonicalHost


class IsolatedCodexConversation(_CanonicalHost):
    def __init__(
        self,
        source_repository: str | Path,
        *,
        executable: str | Path = "codex",
        model: str | None = None,
        allowed_paths: tuple[str, ...] = ("**",),
    ) -> None:
        super().__init__(
            source_repository,
            executable=executable,
            model=model,
            allowed_paths=allowed_paths,
            _session_factory=lambda **options: CodexAppServerSession(**options),
        )
