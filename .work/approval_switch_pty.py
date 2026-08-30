"""Run the real looplaneApp (not headless) with a fake approval-requesting runner."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_tui import FakeModel, FakeRunner  # noqa: E402

from looplane.approvals import ApprovalReason, ApprovalRequest, ToolEffect  # noqa: E402
from looplane.cli_config import CliConfig  # noqa: E402
from looplane.contracts import RunResult, ToolCall  # noqa: E402
from looplane.tui import looplaneApp  # noqa: E402

LOG = Path("/tmp/looplane-pty-debug.log")


def log(msg: str) -> None:
    with LOG.open("a") as fh:
        fh.write(msg + "\n")


async def main() -> None:
    LOG.write_text("")
    decision: dict[str, object] = {}

    class ApprovalRunner(FakeRunner):
        async def run(self) -> RunResult:
            for i in range(2):
                request = ApprovalRequest(
                    run_id=f"approval-run-{i}",
                    action_id=f"edit-{i}",
                    effect=ToolEffect.MODIFY,
                    reason=ApprovalReason.MODEL_TOOL,
                    preview=f"replace src/example-{i}.py",
                    tool_call=ToolCall(name="replace_text"),
                )
                decision[f"value-{i}"] = await self.approval_policy.decide(request)
                log(f"DECISION {i}: {decision[f'value-{i}']}")
            return await super().run()

    def factory(_request, approval_policy, event_sink):
        return ApprovalRunner(approval_policy=approval_policy, event_sink=event_sink), FakeModel()

    app = looplaneApp(
        repository=Path("/tmp/looplane-repro"),
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=factory,
        providers=(("ollama", "Ollama local"),),
        initial_prompt="Edit the example.",
    )

    async def watch_focus() -> None:
        last = None
        while True:
            try:
                focused = repr(app.focused)
            except Exception:
                focused = "<error>"
            if focused != last:
                log(f"FOCUS: {focused}")
                last = focused
            await asyncio.sleep(0.2)

    asyncio.create_task(watch_focus())
    await app.run_async()
    log(f"EXIT: result={app._result!r}")


asyncio.run(main())
