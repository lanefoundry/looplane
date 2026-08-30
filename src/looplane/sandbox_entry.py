"""Fixed Cloudflare Sandbox entrypoint for one bounded looplane run.

The Worker writes a validated text source tree and request file. This module never
receives a provider credential: it receives only a short-lived model-proxy capability.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import stat
import sys
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field

from looplane.approvals import ApprovalDecision, ApprovalPolicy, ApprovalRequest
from looplane.console import EventSink
from looplane.contracts import ContractModel, Limits, TaskContract, VerificationCommand
from looplane.events import RunEvent, atomic_write_json
from looplane.loop import AgentRunner
from looplane.models import ModelProvider, OpenAICompatibleModel
from looplane.runtime import run_bounded_command, sanitized_subprocess_env


class SandboxRunRequest(ContractModel):
    """Validated task metadata; source bytes are staged separately by the Worker."""

    task_id: str = Field(min_length=1, max_length=128)
    instruction: str = Field(min_length=1, max_length=32_000)
    allowed_paths: tuple[str, ...] = Field(min_length=1, max_length=64)
    verification: tuple[VerificationCommand, ...] = Field(min_length=1, max_length=16)
    limits: Limits = Field(default_factory=Limits)


class SandboxEntrypointError(RuntimeError):
    """Safe terminal failure from the fixed sandbox entrypoint."""


_PR_SET_DUMPABLE = 4
_MAX_RUN_TOKEN_BYTES = 8_192
_MAX_EVENT_BATCH_BYTES = 128_000
_APPROVAL_POLL_INTERVAL_SECONDS = 0.5
_DEFAULT_APPROVAL_TIMEOUT_SECONDS = 210.0


class SandboxControlPlaneEventSink:
    """Best-effort live event mirror from Sandbox to the Worker control plane."""

    def __init__(self, *, base_url: str, run_id: str, token: str) -> None:
        self._url = f"{base_url.rstrip('/')}/runs/{run_id}/events"
        self._token = token
        self._errors = 0
        self._disabled = False

    async def emit(self, event: RunEvent) -> None:
        if self._disabled:
            return
        line = event.model_dump_json() + "\n"
        if len(line.encode("utf-8")) > _MAX_EVENT_BATCH_BYTES:
            self._disabled = True
            return
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self._url,
                    headers={
                        "authorization": f"Bearer {self._token}",
                        "content-type": "application/json",
                    },
                    json={"lines": [line]},
                )
            if response.status_code >= 400:
                self._errors += 1
        except httpx.HTTPError:
            self._errors += 1
        if self._errors >= 3:
            self._disabled = True


class SandboxControlPlaneApprovalPolicy:
    """Poll the control plane until a submitted approval decision is available."""

    def __init__(
        self,
        *,
        base_url: str,
        run_id: str,
        token: str,
        timeout_seconds: float = _DEFAULT_APPROVAL_TIMEOUT_SECONDS,
        poll_interval_seconds: float = _APPROVAL_POLL_INTERVAL_SECONDS,
    ) -> None:
        if timeout_seconds <= 0:
            raise SandboxEntrypointError("approval timeout must be positive")
        if poll_interval_seconds <= 0:
            raise SandboxEntrypointError("approval poll interval must be positive")
        self._base_url = base_url.rstrip("/")
        self._run_id = run_id
        self._token = token
        self._timeout_seconds = timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        url = f"{self._base_url}/runs/{self._run_id}/approvals/{request.request_id}"
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                response = await client.get(
                    url,
                    headers={"authorization": f"Bearer {self._token}"},
                )
                if response.status_code == 200:
                    return self._validated_decision(response)
                if response.status_code != 202:
                    raise SandboxEntrypointError("approval bridge rejected the request")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return ApprovalDecision.CANCEL
                await asyncio.sleep(min(self._poll_interval_seconds, remaining))

    @staticmethod
    def _validated_decision(response: httpx.Response) -> ApprovalDecision:
        try:
            payload = response.json()
        except ValueError as exc:
            raise SandboxEntrypointError("approval bridge returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("status") != "decided":
            raise SandboxEntrypointError("approval bridge returned an invalid decision")
        try:
            return ApprovalDecision(payload.get("decision"))
        except ValueError as exc:
            raise SandboxEntrypointError("approval bridge returned an invalid decision") from exc


def _harden_linux_process() -> None:
    """Prevent same-UID repository checks from inspecting the agent process."""

    if sys.platform != "linux":
        raise SandboxEntrypointError("sandbox process hardening requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    if prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise SandboxEntrypointError("could not harden sandbox agent process")


def _read_and_remove_token(root: Path, filename: str) -> str:
    """Consume the fixed, owner-only capability file without following links."""

    path = root / filename
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SandboxEntrypointError("sandbox run capability is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
        ):
            raise SandboxEntrypointError("sandbox run capability has unsafe metadata")
        payload = os.read(descriptor, _MAX_RUN_TOKEN_BYTES + 1)
        if len(payload) > _MAX_RUN_TOKEN_BYTES:
            raise SandboxEntrypointError("sandbox run capability is oversized")
    finally:
        os.close(descriptor)
    try:
        token = payload.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise SandboxEntrypointError("sandbox run capability is invalid") from exc
    if not token or token.strip() != token or "\x00" in token:
        raise SandboxEntrypointError("sandbox run capability is invalid")
    try:
        path.unlink()
        directory = os.open(root, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise SandboxEntrypointError("sandbox run capability could not be consumed") from exc
    return token


def _read_and_remove_run_token(root: Path) -> str:
    return _read_and_remove_token(root, ".looplane-run-token")


def _read_and_remove_event_token(root: Path) -> str:
    return _read_and_remove_token(root, ".looplane-event-token")


def _read_and_remove_approval_token(root: Path) -> str:
    return _read_and_remove_token(root, ".looplane-approval-token")


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SandboxEntrypointError(f"required sandbox environment is missing: {name}")
    return value


def _git(source: Path, *argv: str):
    return run_bounded_command(
        ("git", *argv),
        cwd=source,
        timeout_seconds=30,
        max_output_chars=20_000,
        env=sanitized_subprocess_env(task_home=source.parent / ".git-task-env"),
    )


def _initialize_source_repository(source: Path) -> str:
    if source.is_symlink() or not source.is_dir():
        raise SandboxEntrypointError("staged source directory is unavailable")
    if (source / ".git").exists() or (source / ".git").is_symlink():
        raise SandboxEntrypointError("uploaded source must not contain Git metadata")
    commands = (
        ("init", "-q"),
        ("config", "user.name", "looplane Sandbox"),
        ("config", "user.email", "looplane-sandbox@example.invalid"),
        ("add", "--all", "--"),
        ("commit", "-q", "-m", "sandbox source snapshot"),
    )
    for command in commands:
        result = _git(source, *command)
        if not result.ok:
            raise SandboxEntrypointError("could not initialize staged source repository")
    head = _git(source, "rev-parse", "HEAD")
    sha = head.stdout.strip()
    if not head.ok or len(sha) != 40:
        raise SandboxEntrypointError("could not resolve staged source commit")
    return sha


def _read_request(path: Path) -> SandboxRunRequest:
    if path.is_symlink() or not path.is_file():
        raise SandboxEntrypointError("sandbox request file is unavailable")
    max_request_bytes = 256_000
    with path.open("rb") as handle:
        payload = handle.read(max_request_bytes + 1)
    if len(payload) > max_request_bytes:
        raise SandboxEntrypointError("sandbox request exceeds the entrypoint limit")
    try:
        return SandboxRunRequest.model_validate_json(payload)
    except ValueError as exc:
        raise SandboxEntrypointError("sandbox request is invalid") from exc


def _read_artifact(
    path: str,
    *,
    expected_directory: Path,
    expected_name: str,
    max_bytes: int,
) -> str:
    artifact = Path(path)
    if artifact.is_symlink() or not artifact.is_file():
        raise SandboxEntrypointError("expected run artifact is unavailable")
    resolved = artifact.resolve(strict=True)
    if resolved.parent != expected_directory or resolved.name != expected_name:
        raise SandboxEntrypointError("run returned an artifact outside its run directory")
    with resolved.open("rb") as handle:
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise SandboxEntrypointError("run artifact exceeds response bundle limit")
    return payload.decode("utf-8", errors="strict")


async def run_sandbox_request(
    request_path: str | Path,
    *,
    workspace_root: str | Path = "/workspace",
    model: ModelProvider | None = None,
) -> dict[str, Any]:
    """Execute one staged task and return a bounded JSON-serializable result bundle."""

    root = Path(workspace_root).resolve(strict=True)
    source = (root / "source").resolve(strict=True)
    runs = root / "runs"
    if runs.exists():
        raise SandboxEntrypointError("sandbox run directory already exists")
    request = _read_request(Path(request_path).resolve(strict=True))
    base_sha = await asyncio.to_thread(_initialize_source_repository, source)
    task = TaskContract(
        repository=source,
        instruction=request.instruction,
        allowed_paths=request.allowed_paths,
        verification=request.verification,
        limits=request.limits,
        task_id=request.task_id,
        base_sha=base_sha,
    )
    owns_model = model is None
    if model is None:
        _harden_linux_process()
        run_token = _read_and_remove_run_token(root)
        event_token = _read_and_remove_event_token(root)
        approval_token = _read_and_remove_approval_token(root)
        model_gateway_url = _required_env("LOOPLANE_MODEL_GATEWAY_URL")
        selected_model = OpenAICompatibleModel(
            model=_required_env("LOOPLANE_MODEL_ID"),
            api_key=run_token,
            base_url=model_gateway_url,
            supports_tool_calling=True,
            provider_name="cloudflare-model-proxy",
        )
        event_sink: EventSink | None = SandboxControlPlaneEventSink(
            base_url=model_gateway_url,
            run_id=request.task_id,
            token=event_token,
        )
        approval_policy: ApprovalPolicy | None = SandboxControlPlaneApprovalPolicy(
            base_url=model_gateway_url,
            run_id=request.task_id,
            token=approval_token,
            timeout_seconds=float(
                os.environ.get(
                    "LOOPLANE_APPROVAL_TIMEOUT_SECONDS",
                    str(_DEFAULT_APPROVAL_TIMEOUT_SECONDS),
                )
            ),
        )
    else:
        selected_model = model
        event_sink = None
        approval_policy = None
    try:
        result = await AgentRunner(
            task,
            selected_model,
            runs,
            allow_unsafe_local_exec=True,
            approval_policy=approval_policy,
            event_sink=event_sink,
        ).run()
    finally:
        if owns_model:
            await selected_model.aclose()

    max_bundle_bytes = int(os.environ.get("LOOPLANE_MAX_BUNDLE_BYTES", "1000000"))
    if max_bundle_bytes <= 0 or max_bundle_bytes > 5_000_000:
        raise SandboxEntrypointError("LOOPLANE_MAX_BUNDLE_BYTES is outside the allowed range")
    artifact_names = {
        "request": "request.json",
        "events": "events.jsonl",
        "checkpoint": "checkpoint.json",
        "patch": "changes.patch",
        "test_log": "test.log",
        "result": "result.json",
    }
    artifacts: dict[str, str] = {}
    remaining = max_bundle_bytes
    expected_run_directory = (runs / result.run_id).resolve(strict=True)
    if expected_run_directory.parent != runs.resolve(strict=True):
        raise SandboxEntrypointError("run returned an unexpected run directory")
    for name, filename in artifact_names.items():
        artifact_path = result.artifacts.get(name)
        if artifact_path is None:
            raise SandboxEntrypointError("run returned an unexpected artifact map")
        value = _read_artifact(
            artifact_path,
            expected_directory=expected_run_directory,
            expected_name=filename,
            max_bytes=remaining,
        )
        remaining -= len(value.encode("utf-8"))
        artifacts[name] = value
    return {
        "ok": result.status == "completed",
        "result": result.model_dump(mode="json"),
        "artifacts": artifacts,
    }


async def _main(argv: list[str]) -> int:
    response_path = Path("/workspace/response.json")
    if len(argv) != 2:
        await atomic_write_json(
            response_path,
            {"ok": False, "error": "invalid_entrypoint_arguments"},
        )
        return 2
    try:
        response = await run_sandbox_request(argv[1])
        exit_code = 0 if response["ok"] else 1
    except SandboxEntrypointError:
        response = {"ok": False, "error": "sandbox_entrypoint_failed"}
        exit_code = 1
    except Exception:  # noqa: BLE001 - process boundary returns one non-sensitive failure code
        response = {"ok": False, "error": "sandbox_agent_failed"}
        exit_code = 1
    await atomic_write_json(response_path, response)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv)))
