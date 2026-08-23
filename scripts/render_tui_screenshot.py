#!/usr/bin/env python3
"""Render a deterministic Rivumi transcript screenshot for UI review."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import subprocess
import tempfile
from pathlib import Path
from time import monotonic

from rivumi.approvals import (
    ApprovalDecision,
    ApprovalReason,
    ApprovalRequest,
    ToolEffect,
)
from rivumi.cli_config import CliConfig
from rivumi.contracts import RunResult, RunStatus, ToolCall
from rivumi.conversation_runtime import (
    ApprovalRequestedEvent,
    RuntimeApprovalKind,
    RuntimeApprovalRequest,
    RuntimeToolKind,
    TextDeltaEvent,
    ToolStartedEvent,
    TurnStartedEvent,
)
from rivumi.tui import (
    ConversationRuntimeEventMessage,
    InlineApprovalBlock,
    MessageComposer,
    RivumiApp,
    RuntimeLoadingIndicator,
    RuntimeStatus,
)


class ScreenshotRunner:
    def request_cancel(self) -> None:
        return None

    async def run(self) -> RunResult:
        return RunResult(
            run_id="screenshot",
            task_id="screenshot",
            status=RunStatus.COMPLETED,
            summary="Screenshot fixture completed.",
            terminal_reason="verified",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=120)
    parser.add_argument("--height", type=int, default=36)
    parser.add_argument("--name", default="rivumi-transcript")
    parser.add_argument(
        "--state",
        choices=(
            "empty",
            "runtime-menu",
            "idle",
            "thinking",
            "streaming",
            "tool",
            "permission",
        ),
        default="idle",
    )
    parser.add_argument(
        "--loading-frame",
        type=int,
        choices=range(len(RuntimeLoadingIndicator._FRAMES)),
        help="Freeze an active loading indicator at a deterministic frame.",
    )
    parser.add_argument("--output-dir", type=Path, default=Path(".artifacts/tui"))
    return parser.parse_args()


async def render(args: argparse.Namespace) -> tuple[Path, Path | None]:
    if args.width < 40 or args.height < 16:
        raise SystemExit("terminal screenshot must be at least 40x16")

    project_root = Path(__file__).resolve().parents[1]
    output_dir = (project_root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    app = RivumiApp(
        repository=project_root,
        config=CliConfig(runtime="codex-cli"),
        runner_factory=lambda *_args: (ScreenshotRunner(), None),
        runtimes=(("codex-cli", "Codex CLI"),),
        providers=(("ollama", "Ollama"),),
    )
    approval_task: asyncio.Task[ApprovalDecision] | None = None
    async with app.run_test(size=(args.width, args.height)) as pilot:
        if args.state not in {"empty", "runtime-menu"}:
            app._write_turn("You", "Why did the conversation begin at the top of the screen?")
            action = app._ensure_tool_action(
                "inspect-layout",
                "Inspect transcript layout",
                detail="Textual viewport and scroll anchoring",
            )
            action.set_state(
                "running" if args.state == "tool" else "completed",
                detail=(
                    "Inspecting Textual viewport and scroll anchoring"
                    if args.state == "tool"
                    else "Sparse transcript anchored above the composer"
                ),
            )
            app._write_turn(
                "Assistant",
                "The conversation now grows upward from the composer while long history "
                "remains scrollable.",
            )
        app._generation = 1
        if args.state == "runtime-menu":
            composer = app.query_one("#task", MessageComposer)
            composer.set_text("/runtime")
            composer.focus()
        elif args.state not in {"empty", "idle"}:
            app.conversation_runtime_event_received(
                ConversationRuntimeEventMessage(
                    TurnStartedEvent(sequence=0, turn_id="screenshot-turn"),
                    generation=1,
                )
            )
        if args.state == "streaming":
            app.conversation_runtime_event_received(
                ConversationRuntimeEventMessage(
                    TextDeltaEvent(
                        sequence=1,
                        turn_id="screenshot-turn",
                        text="Streaming response is now visible.\n",
                    ),
                    generation=1,
                )
            )
        elif args.state in {"tool", "permission"}:
            app.conversation_runtime_event_received(
                ConversationRuntimeEventMessage(
                    ToolStartedEvent(
                        sequence=1,
                        turn_id="screenshot-turn",
                        action_id="inspect-layout",
                        kind=RuntimeToolKind.COMMAND,
                        tool_name="Inspect transcript layout",
                        effect=ToolEffect.EXECUTE,
                    ),
                    generation=1,
                )
            )
            if args.state == "permission":
                approval = RuntimeApprovalRequest(
                    request_id="screenshot-approval",
                    turn_id="screenshot-turn",
                    action_id="inspect-layout",
                    kind=RuntimeApprovalKind.COMMAND,
                    effect=ToolEffect.EXECUTE,
                    preview="Inspect transcript layout",
                    available_decisions=(ApprovalDecision.ALLOW_ONCE, ApprovalDecision.DENY),
                )
                app.conversation_runtime_event_received(
                    ConversationRuntimeEventMessage(
                        ApprovalRequestedEvent(
                            sequence=2,
                            turn_id="screenshot-turn",
                            approval=approval,
                        ),
                        generation=1,
                    )
                )
                approval_task = asyncio.create_task(
                    app.request_approval(
                        ApprovalRequest(
                            run_id="screenshot-turn",
                            action_id="inspect-layout",
                            effect=ToolEffect.EXECUTE,
                            reason=ApprovalReason.MODEL_TOOL,
                            preview="uv run pytest tests/test_tui.py -q",
                            tool_call=ToolCall(
                                name="external_command",
                                arguments={
                                    "available_decisions": [
                                        ApprovalDecision.ALLOW_ONCE.value,
                                        ApprovalDecision.DENY.value,
                                        ApprovalDecision.CANCEL.value,
                                    ]
                                },
                            ),
                        )
                    )
                )
                for _ in range(50):
                    if app.query(InlineApprovalBlock):
                        break
                    await asyncio.sleep(0.01)
        await pilot.pause()
        if args.loading_frame is not None:
            if args.state in {"empty", "runtime-menu", "idle"}:
                raise SystemExit("--loading-frame requires a non-idle --state")
            indicator = app.query_one("#loading-indicator", RuntimeLoadingIndicator)
            frame_elapsed = (args.loading_frame + 0.5) * indicator._CADENCE
            indicator._phase_started_at = monotonic() - frame_elapsed
            indicator.auto_refresh = None
            indicator.refresh()
            status = app.query_one("#status", RuntimeStatus)
            status._loading_started_at = monotonic() - frame_elapsed
            status.auto_refresh = None
            status.refresh()
        svg_path = Path(app.save_screenshot(filename=f"{args.name}.svg", path=str(output_dir)))
        if approval_task is not None:
            app.query_one(InlineApprovalBlock).resolve(ApprovalDecision.CANCEL)
            await approval_task

    png_path = output_dir / f"{args.name}.png"
    qlmanage = shutil.which("qlmanage")
    magick = shutil.which("magick")
    if qlmanage is not None:
        with tempfile.TemporaryDirectory(prefix="rivumi-tui-screenshot-") as temporary:
            subprocess.run(
                [qlmanage, "-t", "-s", "1600", "-o", temporary, str(svg_path)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            rendered = Path(temporary) / f"{svg_path.name}.png"
            shutil.move(rendered, png_path)
    elif magick is not None:
        png_path = output_dir / f"{args.name}.png"
        subprocess.run([magick, str(svg_path), str(png_path)], check=True)
    else:
        png_path = None
    return svg_path, png_path


def main() -> None:
    svg_path, png_path = asyncio.run(render(parse_args()))
    print(svg_path)
    if png_path is not None:
        print(png_path)
    else:
        print("PNG conversion skipped: no supported SVG converter was found")


if __name__ == "__main__":
    main()
