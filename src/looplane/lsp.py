"""Managed LSP process supervision with request/response and IDE diagnostics."""

from __future__ import annotations

import asyncio
import itertools
import json
from contextlib import suppress
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field, field_validator

from looplane.contracts import ContractModel
from looplane.events import atomic_write_json
from looplane.ide import (
    PROJECT_DIAGNOSTICS_FILE,
    IdeDiagnosticsSnapshot,
    parse_ide_diagnostics,
)
from looplane.runtime import bounded_text, sanitized_subprocess_env

MAX_LSP_ARGV_ITEMS = 32
MAX_LSP_MESSAGE_BYTES = 512 * 1024


class LspSupervisorError(RuntimeError):
    """Raised when a managed LSP process cannot be supervised safely."""


class LspServerCommand(ContractModel):
    """Exact-argv command for one managed LSP server process."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    command: tuple[str, ...] = Field(min_length=1, max_length=MAX_LSP_ARGV_ITEMS)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("LSP server name must be non-empty and NUL-free")
        return value

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not item or "\x00" in item:
                raise ValueError("LSP server argv entries must be non-empty and NUL-free")
        return value


class ManagedLspServer:
    """Own a long-lived LSP subprocess and bridge publishDiagnostics events."""

    def __init__(
        self,
        command: LspServerCommand,
        *,
        project_root: Path,
        diagnostics_path: Path | None = None,
    ) -> None:
        self.command = command
        self.project_root = project_root.resolve(strict=True)
        self.diagnostics_path = (
            diagnostics_path
            if diagnostics_path is not None
            else self.project_root / PROJECT_DIAGNOSTICS_FILE
        )
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._diagnostics_event = asyncio.Event()
        self.last_diagnostics: IdeDiagnosticsSnapshot | None = None
        self.last_error: str | None = None
        self._request_id_counter = itertools.count(1)
        self._pending_responses: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._initialized = False

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        if self.running:
            return
        self.diagnostics_path.parent.mkdir(parents=True, exist_ok=True)
        self._process = await asyncio.create_subprocess_exec(
            *self.command.command,
            cwd=self.project_root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=sanitized_subprocess_env(
                task_home=self.project_root / ".looplane" / "ide" / ".lsp"
            ),
        )
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._reader_task = asyncio.create_task(self._read_stdout(self._process.stdout))
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))

    def _send_message(self, msg: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise LspSupervisorError("LSP server is not running")
        payload = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
        self._process.stdin.write(header + payload)

    async def initialize(self) -> dict[str, Any]:
        if self._initialized:
            return {}
        result = await self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": self.project_root.as_uri(),
                "capabilities": {
                    "textDocument": {
                        "definition": {"dynamicRegistration": False},
                        "references": {"dynamicRegistration": False},
                        "documentSymbol": {"dynamicRegistration": False},
                        "publishDiagnostics": {"relatedInformation": True},
                    },
                    "workspace": {
                        "symbol": {"dynamicRegistration": False},
                    },
                },
            },
        )
        self._send_message({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        self._initialized = True
        return result

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        if not self.running:
            raise LspSupervisorError("LSP server is not running")
        request_id = next(self._request_id_counter)
        msg: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            msg["params"] = params
        future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending_responses[request_id] = future
        try:
            self._send_message(msg)
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError as exc:
            raise LspSupervisorError(
                f"LSP request {method!r} timed out after {timeout_seconds}s"
            ) from exc
        finally:
            self._pending_responses.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send_message(msg)

    async def open_document(self, path: Path) -> None:
        uri = path.resolve().as_uri()
        text = path.read_text(encoding="utf-8", errors="replace")
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": _language_id(path),
                    "version": 1,
                    "text": text,
                },
            },
        )

    async def wait_for_diagnostics(self, timeout_seconds: float = 30.0) -> IdeDiagnosticsSnapshot:
        await asyncio.wait_for(self._diagnostics_event.wait(), timeout=timeout_seconds)
        assert self.last_diagnostics is not None
        return self.last_diagnostics

    async def aclose(self) -> None:
        for future in self._pending_responses.values():
            if not future.done():
                future.cancel()
        self._pending_responses.clear()
        self._initialized = False
        process = self._process
        tasks = (self._reader_task, self._stderr_task)
        for task in tasks:
            if task is not None:
                task.cancel()
        for task in tasks:
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                process.kill()
                await process.wait()
        self._process = None

    async def _read_stdout(self, stream: asyncio.StreamReader) -> None:
        while True:
            try:
                message = await _read_lsp_message(stream)
            except (EOFError, asyncio.IncompleteReadError):
                return
            except Exception as exc:
                self.last_error = bounded_text(f"{type(exc).__name__}: {exc}", 2_000)
                return
            await self._handle_message(message)

    async def _handle_message(self, message: dict[str, object]) -> None:
        msg_id = message.get("id")
        if msg_id is not None and isinstance(msg_id, int) and msg_id in self._pending_responses:
            future = self._pending_responses.pop(msg_id)
            error = message.get("error")
            if error:
                future.set_exception(LspSupervisorError(f"LSP error: {error}"))
            else:
                future.set_result(message.get("result", {}))  # type: ignore[arg-type]
            return

        if message.get("method") != "textDocument/publishDiagnostics":
            return
        params = message.get("params")
        if not isinstance(params, dict):
            return
        snapshot = parse_ide_diagnostics(params, project_root=self.project_root)
        await atomic_write_json(self.diagnostics_path, snapshot)
        self.last_diagnostics = snapshot
        self._diagnostics_event.set()

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while await stream.read(4096):
            pass


async def _read_lsp_message(stream: asyncio.StreamReader) -> dict[str, object]:
    headers = await stream.readuntil(b"\r\n\r\n")
    length: int | None = None
    for raw_line in headers.decode("ascii", errors="strict").split("\r\n"):
        if raw_line.lower().startswith("content-length:"):
            length = int(raw_line.split(":", 1)[1].strip())
            break
    if length is None:
        raise LspSupervisorError("LSP message is missing Content-Length")
    if length <= 0 or length > MAX_LSP_MESSAGE_BYTES:
        raise LspSupervisorError("LSP message size is outside the allowed range")
    payload = await stream.readexactly(length)
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise LspSupervisorError("LSP message payload must be a JSON object")
    return value


_LANG_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".swift": "swift",
    ".kt": "kotlin",
    ".lua": "lua",
    ".sh": "shellscript",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
}


def _language_id(path: Path) -> str:
    return _LANG_MAP.get(path.suffix.lower(), "plaintext")
