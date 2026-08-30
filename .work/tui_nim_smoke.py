"""One-off live smoke: drive the real looplaneApp (Textual) against nvidia-nim.

Usage: uv run python .work/tui_nim_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import looplane.runtime_registry as runtime_registry  # noqa: E402
from looplane.cli import (  # noqa: E402
    DEFAULT_RUN_ROOT,
    ONBOARDING_PROVIDERS,
    _credential_hint,
    _model_from_env,
)
from looplane.cli_config import CliConfig  # noqa: E402
from looplane.contracts import Limits, TaskContract, VerificationCommand  # noqa: E402
from looplane.conversation import ConversationStore  # noqa: E402
from looplane.loop import AgentRunner  # noqa: E402
from looplane.tui import InlineApprovalBlock, looplaneApp  # noqa: E402

PROVIDER = "nvidia-nim"
MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
PROMPT = "嗨"


def make_runner(request, approval_policy, event_sink):
    """Mirror cli.chat make_runner's plain looplane-agent path."""
    adapter = runtime_registry.RUNTIME_REGISTRY.get(request.runtime)
    if adapter is None:
        raise ValueError(f"Unknown runtime: {request.runtime}")
    assert adapter.native_session is None, "unexpected native conversation runtime"
    task = TaskContract(
        repository=request.repository,
        instruction=request.instruction,
        allowed_paths=("**",),
        verification=(
            VerificationCommand(name="check-1", argv=tuple(shlex.split("git diff --check"))),
        ),
        limits=Limits(wall_time_seconds=900.0),
    )
    if hint := _credential_hint(request.provider, api_url=None):
        raise ValueError(f"Provider is not ready. {hint}")
    selected_model = _model_from_env(
        provider=request.provider,
        model=request.model,
        base_url=None,
        tool_calling=True,
        allow_custom_provider_endpoint=False,
    )
    return (
        AgentRunner(
            task,
            selected_model,
            DEFAULT_RUN_ROOT,
            approval_policy=approval_policy,
            event_sink=event_sink,
        ),
        selected_model,
    )


async def wait_until(app, pilot, pred, timeout: float, interval: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        block = next(iter(app.query(InlineApprovalBlock)), None)
        if block is not None:
            print("[smoke] approving pending tool action", flush=True)
            await pilot.press("1")
            await asyncio.sleep(interval)
            continue
        if pred():
            return True
        await asyncio.sleep(interval)
    return False


async def main() -> None:
    runs_before = {p.stat().st_mtime for p in DEFAULT_RUN_ROOT.glob("*/events.jsonl")}
    app = looplaneApp(
        repository=ROOT,
        config=CliConfig(
            runtime="looplane-agent",
            provider=PROVIDER,
            model=MODEL,
            api_url=None,
        ),
        runner_factory=make_runner,
        providers=ONBOARDING_PROVIDERS,
        conversation_store=ConversationStore(),
    )
    async with app.run_test(size=(110, 42)) as pilot:
        await pilot.pause()
        composer = app.query_one("#task")
        composer.set_text(PROMPT)
        await pilot.pause()
        print(f"[smoke] submitting prompt: {PROMPT!r}", flush=True)
        await pilot.press("enter")

        started = await wait_until(app, pilot, lambda: app._agent_running, timeout=60)
        print(f"[smoke] agent started: {started}", flush=True)
        finished = await wait_until(
            app, pilot, lambda: not app._agent_running, timeout=360, interval=1.0
        )
        print(f"[smoke] agent stopped again: {finished}", flush=True)
        await pilot.pause(2.0)

        screen_text = None
        if hasattr(app, "export_text"):
            try:
                screen_text = app.export_text()
            except Exception as exc:  # noqa: BLE001
                print(f"[smoke] export_text failed: {exc}", flush=True)
        if screen_text is None:
            svg = app.export_screenshot()
            (ROOT / ".work" / "tui_nim_smoke.svg").write_text(svg, encoding="utf-8")
            screen_text = "(screenshot saved to .work/tui_nim_smoke.svg)"
        else:
            (ROOT / ".work" / "tui_nim_smoke_screen.txt").write_text(screen_text, encoding="utf-8")

        result = app._result
        print("=== SCREEN ===", flush=True)
        print(screen_text[-4000:], flush=True)
        print("=== RESULT ===", flush=True)
        if result is not None:
            print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False)[:2000], flush=True)
        else:
            print(f"result=None last_error={app.last_error!r}", flush=True)

    runs_after = {
        p: p.stat().st_mtime
        for p in DEFAULT_RUN_ROOT.glob("*/events.jsonl")
        if p.stat().st_mtime not in runs_before
    }
    newest = max(runs_after, key=lambda p: runs_after[p]) if runs_after else None
    print(f"=== RUN DIR ===\n{newest}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
