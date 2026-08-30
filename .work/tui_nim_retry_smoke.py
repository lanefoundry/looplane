"""One-off retry smoke: real TUI + AgentRunner against an injected flaky transport.

The model client is OpenAICompatibleModel over an httpx MockTransport that
answers the first two chat-completion calls with 503 (Retry-After: 0) and then
a canned valid completion. This deterministically exercises
AgentRunner._complete_model_with_retry() and `model.retry` events inside the
real Textual app.

Usage: uv run python .work/tui_nim_retry_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

import looplane.runtime_registry as runtime_registry  # noqa: E402
from looplane.cli import DEFAULT_RUN_ROOT, ONBOARDING_PROVIDERS  # noqa: E402
from looplane.cli_config import CliConfig  # noqa: E402
from looplane.contracts import Limits, TaskContract, VerificationCommand  # noqa: E402
from looplane.conversation import ConversationStore  # noqa: E402
from looplane.loop import AgentRunner  # noqa: E402
from looplane.models import OpenAICompatibleModel  # noqa: E402
from looplane.tui import InlineApprovalBlock, looplaneApp  # noqa: E402

PROVIDER = "nvidia-nim"
MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
PROMPT = "嗨"
FAILURES_BEFORE_SUCCESS = int(os.environ.get("SMOKE_FAIL_LIMIT", "2"))

CANNED_COMPLETION = {
    "id": "chatcmpl-retry-smoke",
    "object": "chat.completion",
    "created": 0,
    "model": MODEL,
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "RETRY_SMOKE_OK：我在兩次 503 後成功回覆了。",
                "refusal": None,
            },
            "finish_reason": "stop",
            "logprobs": None,
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}

state = {"requests": 0}


def flaky_handler(request: httpx.Request) -> httpx.Response:
    state["requests"] += 1
    if state["requests"] > FAILURES_BEFORE_SUCCESS:
        return httpx.Response(200, json=CANNED_COMPLETION)
    return httpx.Response(
        503,
        headers={"retry-after": "0"},
        json={"error": {"message": "simulated NIM overload"}},
    )


def make_flaky_model():
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(flaky_handler))
    client = AsyncOpenAI(
        api_key="smoke-not-used",
        base_url="http://127.0.0.1:9/v1",
        http_client=http_client,
        max_retries=0,
    )
    return OpenAICompatibleModel(
        model=MODEL,
        client=client,
        supports_tool_calling=True,
        provider_name=PROVIDER,
    )


def make_runner(request, approval_policy, event_sink):
    adapter = runtime_registry.RUNTIME_REGISTRY.get(request.runtime)
    if adapter is None:
        raise ValueError(f"Unknown runtime: {request.runtime}")
    task = TaskContract(
        repository=request.repository,
        instruction=request.instruction,
        allowed_paths=("**",),
        verification=(
            VerificationCommand(name="check-1", argv=tuple(shlex.split("git diff --check"))),
        ),
        limits=Limits(wall_time_seconds=900.0),
    )
    return AgentRunner(
        task,
        make_flaky_model(),
        DEFAULT_RUN_ROOT,
        approval_policy=approval_policy,
        event_sink=event_sink,
    ), None


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
        await pilot.press("enter")
        started = await wait_until(app, pilot, lambda: app._agent_running, timeout=60)
        finished = await wait_until(
            app, pilot, lambda: not app._agent_running, timeout=180, interval=1.0
        )
        print(f"[smoke] started={started} finished={finished}", flush=True)
        await pilot.pause(2.0)
        result = app._result
        print("=== RESULT ===", flush=True)
        if result is not None:
            print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False)[:1500], flush=True)
        else:
            print(f"result=None last_error={app.last_error!r}", flush=True)

    runs_after = {
        p: p.stat().st_mtime
        for p in DEFAULT_RUN_ROOT.glob("*/events.jsonl")
        if p.stat().st_mtime not in runs_before
    }
    newest = max(runs_after, key=lambda p: runs_after[p]) if runs_after else None
    print(f"=== RUN DIR ===\n{newest}", flush=True)
    print(f"[smoke] total upstream requests seen by transport: {state['requests']}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
