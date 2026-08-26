"""Repro: inline approval 無法切換選項？"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_tui import FakeModel, FakeRunner  # noqa: E402

from rivumi.cli_config import CliConfig
from rivumi.contracts import RunResult
from rivumi.approvals import ApprovalReason, ApprovalRequest, ToolEffect
from rivumi.contracts import RunResult, ToolCall
from rivumi.tui import InlineApprovalBlock, RivumiApp
from textual.widgets import OptionList


async def main() -> None:
    decision: dict[str, object] = {}

    class ApprovalRunner(FakeRunner):
        async def run(self) -> RunResult:
            request = ApprovalRequest(
                run_id="approval-run",
                action_id="edit-1",
                effect=ToolEffect.MODIFY,
                reason=ApprovalReason.MODEL_TOOL,
                preview="replace src/example.py",
                tool_call=ToolCall(name="replace_text"),
            )
            decision["value"] = await self.approval_policy.decide(request)
            return await super().run()

    def factory(_request, approval_policy, event_sink):
        return ApprovalRunner(approval_policy=approval_policy, event_sink=event_sink), FakeModel()

    app = RivumiApp(
        repository=Path("/tmp/rivumi-repro"),
        config=CliConfig(provider="ollama", model="qwen3:4b"),
        runner_factory=factory,
        providers=(("ollama", "Ollama local"),),
        initial_prompt="Edit the example.",
    )

    async def wait_until(pred, timeout=5.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while not pred():
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("condition not met")
            await asyncio.sleep(0.05)

    async with app.run_test(size=(100, 30)) as pilot:
        await wait_until(lambda: bool(app.query(InlineApprovalBlock)))
        await pilot.pause()
        approval = app.query_one(InlineApprovalBlock)
        choices = approval.query_one(".approval-choices", OptionList)
        print("focused widget:", app.focused)
        print("choices focused:", choices.has_focus)
        print("highlighted:", choices.highlighted)
        print("choices can_focus:", type(choices).can_focus if hasattr(type(choices), "can_focus") else "?")

        await pilot.press("down")
        await pilot.pause()
        print("after down, highlighted:", choices.highlighted)
        await pilot.press("down")
        await pilot.pause()
        print("after down x2, highlighted:", choices.highlighted)

        # focus composer manually then try number key
        app.query_one("#task").focus()
        await pilot.pause()
        print("focused after composer focus:", app.focused)
        await pilot.press("3")
        await pilot.pause()
        print("decision after pressing 3 with composer focused:", decision.get("value"))

        if "value" not in decision:
            choices.focus()
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            print("decision after pressing 3 with choices focused:", decision.get("value"))

    print("done")


asyncio.run(main())
