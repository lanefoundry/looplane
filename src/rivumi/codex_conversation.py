"""Secure disposable-workspace host for a live Codex app-server session."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from rivumi.approvals import ApprovalDecision
from rivumi.codex_app_server import CodexAppServerSession
from rivumi.conversation_runtime import (
    ConversationRuntimeEvent,
    RuntimeToolKind,
    RuntimeToolStatus,
    RuntimeTurnStatus,
    ToolCompletedEvent,
    ToolStartedEvent,
    TurnCompletedEvent,
)
from rivumi.conversation_workspace import (
    ConversationWorkspace,
    ConversationWorkspaceIntegrityError,
)


class IsolatedCodexConversation:
    """Keep one Codex thread inside one audited disposable HEAD clone."""

    def __init__(
        self,
        source_repository: str | Path,
        *,
        executable: str | Path = "codex",
        model: str | None = None,
        allowed_paths: tuple[str, ...] = ("**",),
    ) -> None:
        self.source_repository = Path(source_repository)
        self.executable = executable
        self.model = model
        self.allowed_paths = allowed_paths
        self.workspace: ConversationWorkspace | None = None
        self.session: CodexAppServerSession | None = None
        self._claimed_paths: set[str] = set()
        self._action_paths: dict[str, tuple[str, ...]] = {}
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()

    @property
    def source_snapshot_warning(self) -> str | None:
        return self.workspace.source_snapshot_warning if self.workspace else None

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.workspace is not None or self._closed:
                raise RuntimeError("conversation cannot be started more than once")
            workspace = await ConversationWorkspace.create(self.source_repository)
            try:
                session = CodexAppServerSession(
                    working_directory=workspace.workspace_path,
                    runtime_workspace_roots=(workspace.root_path,),
                    executable=self.executable,
                    model=self.model,
                    sandbox_mode="workspace-write",
                )
                await session.start()
            except BaseException:
                await workspace.aclose()
                raise
            self.workspace = workspace
            self.session = session

    async def send_turn(self, text: str) -> str:
        return await self._session().send_turn(text)

    def events(self) -> AsyncIterator[ConversationRuntimeEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[ConversationRuntimeEvent]:
        async for event in self._session().events():
            if (
                isinstance(event, ToolStartedEvent)
                and event.kind == RuntimeToolKind.FILE_CHANGE
                and (event.paths or event.path)
            ):
                raw_paths = event.paths or ((event.path,) if event.path else ())
                normalized = tuple(self._relative_path(path) for path in raw_paths)
                self._action_paths[event.action_id] = normalized
                event = event.model_copy(
                    update={"path": normalized[0] if normalized else None, "paths": normalized}
                )
            elif (
                isinstance(event, ToolCompletedEvent)
                and event.status == RuntimeToolStatus.COMPLETED
                and event.diff
                and (paths := self._action_paths.get(event.action_id))
            ):
                self._claimed_paths.update(paths)
            if isinstance(event, TurnCompletedEvent):
                event = await self._audited_terminal(event)
            yield event

    def _relative_path(self, value: str) -> str:
        workspace = self._workspace().workspace_path
        candidate = Path(value)
        if candidate.is_absolute():
            try:
                candidate = candidate.resolve(strict=False).relative_to(workspace)
            except ValueError as exc:
                raise ConversationWorkspaceIntegrityError(
                    "runtime reported a file change outside the disposable workspace"
                ) from exc
        normalized = candidate.as_posix()
        if normalized in {"", "."} or normalized.startswith("../"):
            raise ConversationWorkspaceIntegrityError("runtime reported an unsafe changed path")
        return normalized

    async def _audited_terminal(self, event: TurnCompletedEvent) -> TurnCompletedEvent:
        workspace = self._workspace()
        try:
            review = await workspace.review(allowed_paths=self.allowed_paths)
            actual = set(review.changed_paths)
            if actual != self._claimed_paths:
                raise ConversationWorkspaceIntegrityError(
                    "runtime file-change events do not match the audited workspace patch"
                )
        except Exception as exc:
            return TurnCompletedEvent(
                sequence=event.sequence,
                turn_id=event.turn_id,
                status=RuntimeTurnStatus.FAILED,
                error=f"Workspace audit failed: {exc}",
            )
        return event

    async def changed_paths(self) -> tuple[str, ...]:
        review = await self._workspace().review(allowed_paths=self.allowed_paths)
        return review.changed_paths

    async def respond_approval(self, request_id: str, decision: ApprovalDecision) -> None:
        await self._session().respond_approval(request_id, decision)

    async def interrupt(self, turn_id: str) -> None:
        await self._session().interrupt(turn_id)

    async def aclose(self) -> None:
        async with self._lifecycle_lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        if self._closed:
            return
        self._closed = True
        error: BaseException | None = None
        if self.session is not None:
            try:
                await self.session.aclose()
            except BaseException as exc:
                error = exc
        if self.workspace is not None:
            await self.workspace.aclose()
        if error is not None:
            raise error

    def _session(self) -> CodexAppServerSession:
        if self.session is None:
            raise RuntimeError("conversation is not started")
        return self.session

    def _workspace(self) -> ConversationWorkspace:
        if self.workspace is None:
            raise RuntimeError("conversation is not started")
        return self.workspace
