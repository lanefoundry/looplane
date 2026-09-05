"""Terminal approvals feature owner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList, Static
from textual.widgets.option_list import Option

from looplane.approvals import ApprovalDecision, ApprovalReason, ApprovalRequest, ToolEffect
from looplane.runtime_semantics import (
    PermissionDecision,
    PermissionMode,
    ProcessLocalGrant,
    decide_permission,
)


class TextualApprovalPolicy:
    def __init__(
        self,
        request_approval: Callable[[ApprovalRequest], Awaitable[ApprovalDecision]],
        session_grants: set[ProcessLocalGrant],
        *,
        permission_mode: Callable[[], PermissionMode] = lambda: PermissionMode.ASK,
    ) -> None:
        self._request_approval = request_approval
        self._permission_mode = permission_mode
        self._session_grants = session_grants

    @staticmethod
    def _grant_scope(request: ApprovalRequest) -> str | None:
        if request.tool_call is not None:
            supplied = request.tool_call.arguments.get("grant_scope")
            if isinstance(supplied, str) and supplied.strip():
                return supplied.strip()[:4_096]
            if request.tool_call.name == "external_agent":
                backend = request.tool_call.arguments.get("backend")
                if isinstance(backend, str) and backend:
                    return f"external_agent:{backend}"[:4_096]
            if request.tool_call.name == "run_check":
                name = request.tool_call.arguments.get("name")
                if isinstance(name, str) and name.strip():
                    return f"run_check:{name.strip()}"[:4_096]
        if request.command is not None:
            return "command:" + "\u0000".join(request.command.argv)[:4_088]
        return None

    async def decide(self, request: ApprovalRequest) -> ApprovalDecision:
        scope = self._grant_scope(request) or f"action:{request.action_id}"
        policy_decision = decide_permission(
            PermissionMode(self._permission_mode()),
            request.effect,
            scope=scope,
            grants=self._session_grants,
        )
        if policy_decision is PermissionDecision.ALLOW:
            return ApprovalDecision.ALLOW_ONCE
        if policy_decision is PermissionDecision.DENY:
            return ApprovalDecision.DENY
        decision = await self._request_approval(request)
        if decision == ApprovalDecision.ALLOW_SESSION:
            self._session_grants.add(ProcessLocalGrant(effect=request.effect, scope=scope))
        return decision


class ApprovalModal(ModalScreen[ApprovalDecision]):
    BINDINGS = [
        Binding("escape", "cancel", "Cancel run", show=False),
        Binding("1", "choose_index(0)", "Choice 1", show=False),
        Binding("2", "choose_index(1)", "Choice 2", show=False),
        Binding("3", "choose_index(2)", "Choice 3", show=False),
        Binding("4", "choose_index(3)", "Choice 4", show=False),
    ]
    DEFAULT_CSS = """
    ApprovalModal { align: center bottom; background: $background 35%; }
    ApprovalModal > .approval-sheet {
        width: 100%; max-width: 100%; height: auto; max-height: 16;
        padding: 1 2; border-top: solid $warning; background: $surface;
    }
    ApprovalModal .title { height: 1; text-style: bold; color: $warning; }
    ApprovalModal .preview {
        height: auto; max-height: 7; margin: 1 0 0 2; color: $text-muted;
        overflow-y: auto;
    }
    ApprovalModal OptionList {
        height: auto; max-height: 4; margin-top: 1; padding: 0;
        background: transparent; border: none; scrollbar-size: 0 0;
    }
    ApprovalModal OptionList > .option-list--option { padding: 0 1; }
    ApprovalModal OptionList > .option-list--option-highlighted,
    ApprovalModal OptionList:focus > .option-list--option-highlighted {
        background: transparent; color: $warning; text-style: bold;
    }
    """

    _DECISION_LABELS = {
        ApprovalDecision.ALLOW_ONCE: "Allow once",
        ApprovalDecision.ALLOW_SESSION: "Allow for this session",
        ApprovalDecision.DENY: "Deny this action",
        ApprovalDecision.CANCEL: "Cancel run",
    }

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__()
        self.request = request
        self.available_decisions = self._available_decisions(request) or frozenset(
            {ApprovalDecision.CANCEL}
        )

    @staticmethod
    def _available_decisions(request: ApprovalRequest) -> frozenset[ApprovalDecision]:
        if request.tool_call is None:
            return frozenset(ApprovalDecision)
        raw = request.tool_call.arguments.get("available_decisions")
        if not isinstance(raw, list):
            return frozenset(ApprovalDecision)
        try:
            return frozenset(ApprovalDecision(value) for value in raw)
        except ValueError:
            return frozenset()

    def compose(self) -> ComposeResult:
        preview = self._preview_text(self.request)
        default = self._default_decision()
        with Vertical(classes="approval-sheet"):
            yield Label(self._question(), classes="title")
            yield Static(
                preview,
                classes="preview",
                markup=False,
            )
            yield OptionList(
                *(
                    Option(
                        self._choice_prompt(
                            index,
                            decision,
                            highlighted=decision == default,
                        ),
                        id=decision.value,
                    )
                    for index, decision in enumerate(self._ordered_decisions(), start=1)
                ),
                id="approval-choices",
                compact=True,
            )

    def _question(self) -> str:
        if self.request.reason == ApprovalReason.FINAL_VERIFICATION:
            return "Run final verification?"
        if self.request.effect == ToolEffect.MODIFY:
            return "Allow this file change?"
        return "Run this command?"

    def _ordered_decisions(self) -> tuple[ApprovalDecision, ...]:
        return tuple(
            decision
            for decision in (
                ApprovalDecision.ALLOW_ONCE,
                ApprovalDecision.ALLOW_SESSION,
                ApprovalDecision.DENY,
                ApprovalDecision.CANCEL,
            )
            if decision in self.available_decisions
        )

    def _default_decision(self) -> ApprovalDecision:
        if not self.request.preview.strip() and ApprovalDecision.DENY in self.available_decisions:
            return ApprovalDecision.DENY
        if (
            self.request.action_id == "external-runtime"
            and ApprovalDecision.ALLOW_SESSION in self.available_decisions
        ):
            return ApprovalDecision.ALLOW_SESSION
        return next(
            decision
            for decision in (
                ApprovalDecision.ALLOW_ONCE,
                ApprovalDecision.DENY,
                ApprovalDecision.CANCEL,
            )
            if decision in self.available_decisions
        )

    def _choice_prompt(
        self,
        index: int,
        decision: ApprovalDecision,
        *,
        highlighted: bool,
    ) -> str:
        pointer = "›" if highlighted else " "
        return f"{pointer} {index}  {self._DECISION_LABELS[decision]}"

    def _sync_choice_prompts(self, highlighted: int | None) -> None:
        choices = self.query_one("#approval-choices", OptionList)
        for index, decision in enumerate(self._ordered_decisions()):
            choices.replace_option_prompt_at_index(
                index,
                self._choice_prompt(index + 1, decision, highlighted=index == highlighted),
            )

    @staticmethod
    def _preview_text(request: ApprovalRequest) -> str:
        if request.preview.strip():
            if request.policy_reason:
                return f"{request.preview}\n\nPolicy: {request.policy_reason}"
            return request.preview
        if request.command is not None:
            action = f"verification command ({request.command.name})"
        elif request.tool_call is not None:
            action = request.tool_call.name.removeprefix("external_").replace("_", " ")
        else:  # The approval contract rejects this, but keep the renderer fail-safe.
            action = "unknown action"
        lines = [
            f"Action: {action}",
            f"Effect: {request.effect.value}",
        ]
        if request.policy_reason:
            lines.append(f"Policy: {request.policy_reason}")
        lines.extend(
            (
                "Details: The runtime did not provide a command, file list, or diff.",
                "Recommendation: Deny unless the preceding tool activity makes the impact clear.",
            )
        )
        return "\n".join(lines)

    def on_mount(self) -> None:
        choices = self.query_one("#approval-choices", OptionList)
        default = self._default_decision()
        choices.highlighted = self._ordered_decisions().index(default)
        self._sync_choice_prompts(choices.highlighted)
        choices.focus()

    @on(OptionList.OptionHighlighted, "#approval-choices")
    def highlight_choice(self, event: OptionList.OptionHighlighted) -> None:
        self._sync_choice_prompts(event.option_index)

    @on(OptionList.OptionSelected, "#approval-choices")
    def choose(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.dismiss(ApprovalDecision(event.option.id))

    def action_choose_index(self, index: int) -> None:
        decisions = self._ordered_decisions()
        if 0 <= index < len(decisions):
            self.dismiss(decisions[index])

    def action_cancel(self) -> None:
        self.dismiss(ApprovalDecision.CANCEL)


class ApprovalPreview(VerticalScroll):
    """Bounded evidence pane controlled from the adjacent approval choices."""

    def __init__(self, content: str) -> None:
        super().__init__(classes="preview")
        self.content = content

    def compose(self) -> ComposeResult:
        yield Static(self.content, markup=False)


class InlineApprovalChoices(OptionList):
    """Approval list that keeps numeric shortcuts local to the inline prompt."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key in {"pageup", "pagedown", "home", "end"}:
            event.stop()
            event.prevent_default()
            parent = self.parent
            if isinstance(parent, InlineApprovalBlock):
                parent.scroll_preview(event.key)
            return
        if event.key in {"1", "2", "3", "4"}:
            event.stop()
            event.prevent_default()
            parent = self.parent
            if isinstance(parent, InlineApprovalBlock):
                parent.action_choose_index(int(event.key) - 1)
            return
        if event.key == "escape":
            event.stop()
            event.prevent_default()
            parent = self.parent
            if isinstance(parent, InlineApprovalBlock):
                parent.action_cancel()
            return
        await super()._on_key(event)


class InlineApprovalBlock(Vertical):
    """One focused approval attached to the pending transcript action."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel run", show=False),
        Binding("1", "choose_index(0)", "Choice 1", show=False),
        Binding("2", "choose_index(1)", "Choice 2", show=False),
        Binding("3", "choose_index(2)", "Choice 3", show=False),
        Binding("4", "choose_index(3)", "Choice 4", show=False),
    ]
    DEFAULT_CSS = """
    InlineApprovalBlock {
        height: auto; max-height: 16; margin: 0 0 1 1; padding: 1 1;
        border-left: thick $warning; background: $surface;
    }
    InlineApprovalBlock .title { height: 1; color: $warning; text-style: bold; }
    InlineApprovalBlock .preview {
        height: auto; max-height: 7; margin: 1 0 0 2; color: $text-muted;
        overflow-y: auto;
    }
    InlineApprovalBlock OptionList {
        height: auto; max-height: 4; margin-top: 1; padding: 0;
        background: transparent; border: none; scrollbar-size: 0 0;
    }
    InlineApprovalBlock OptionList > .option-list--option { padding: 0 1; }
    InlineApprovalBlock OptionList > .option-list--option-highlighted,
    InlineApprovalBlock OptionList:focus > .option-list--option-highlighted {
        background: transparent; color: $warning; text-style: bold;
    }
    """

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__(classes="inline-approval")
        self.request = request
        self.available_decisions = ApprovalModal._available_decisions(request) or frozenset(
            {ApprovalDecision.CANCEL}
        )
        self.decision: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()

    def compose(self) -> ComposeResult:
        yield Label(self._question(), classes="title")
        yield ApprovalPreview(ApprovalModal._preview_text(self.request))
        yield InlineApprovalChoices(
            *(
                Option(
                    self._choice_prompt(index, decision, highlighted=decision == self._default()),
                    id=decision.value,
                )
                for index, decision in enumerate(self._ordered(), start=1)
            ),
            classes="approval-choices",
            compact=True,
        )

    def _question(self) -> str:
        if self.request.reason == ApprovalReason.FINAL_VERIFICATION:
            return "Run final verification?"
        if self.request.effect == ToolEffect.MODIFY:
            return "Allow this file change?"
        return "Run this command?"

    def _ordered(self) -> tuple[ApprovalDecision, ...]:
        return tuple(
            decision
            for decision in (
                ApprovalDecision.ALLOW_ONCE,
                ApprovalDecision.ALLOW_SESSION,
                ApprovalDecision.DENY,
                ApprovalDecision.CANCEL,
            )
            if decision in self.available_decisions
        )

    def _default(self) -> ApprovalDecision:
        if not self.request.preview.strip() and ApprovalDecision.DENY in self.available_decisions:
            return ApprovalDecision.DENY
        if (
            self.request.action_id == "external-runtime"
            and ApprovalDecision.ALLOW_SESSION in self.available_decisions
        ):
            return ApprovalDecision.ALLOW_SESSION
        return next(
            decision
            for decision in (
                ApprovalDecision.ALLOW_ONCE,
                ApprovalDecision.DENY,
                ApprovalDecision.CANCEL,
            )
            if decision in self.available_decisions
        )

    @staticmethod
    def _choice_prompt(index: int, decision: ApprovalDecision, *, highlighted: bool) -> str:
        pointer = "›" if highlighted else " "
        return f"{pointer} {index}  {ApprovalModal._DECISION_LABELS[decision]}"

    def _sync_prompts(self, highlighted: int | None) -> None:
        choices = self.query_one(".approval-choices", OptionList)
        for index, decision in enumerate(self._ordered()):
            choices.replace_option_prompt_at_index(
                index,
                self._choice_prompt(index + 1, decision, highlighted=index == highlighted),
            )

    def on_mount(self) -> None:
        choices = self.query_one(".approval-choices", OptionList)
        choices.highlighted = self._ordered().index(self._default())
        self._sync_prompts(choices.highlighted)
        choices.focus()

    @on(OptionList.OptionHighlighted, ".approval-choices")
    def highlight_choice(self, event: OptionList.OptionHighlighted) -> None:
        self._sync_prompts(event.option_index)

    @on(OptionList.OptionSelected, ".approval-choices")
    def choose(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is not None:
            self.resolve(ApprovalDecision(event.option.id))

    def resolve(self, decision: ApprovalDecision) -> None:
        if not self.decision.done():
            self.decision.set_result(decision)

    def action_choose_index(self, index: int) -> None:
        decisions = self._ordered()
        if 0 <= index < len(decisions):
            self.resolve(decisions[index])

    def action_cancel(self) -> None:
        self.resolve(ApprovalDecision.CANCEL)

    def scroll_preview(self, key: str) -> None:
        """Scroll evidence without moving focus away from the decision choices."""

        preview = self.query_one(".preview", ApprovalPreview)
        if key == "pageup":
            preview.scroll_page_up(animate=False)
        elif key == "pagedown":
            preview.scroll_page_down(animate=False)
        elif key == "home":
            preview.scroll_home(animate=False)
        else:
            preview.scroll_end(animate=False)
