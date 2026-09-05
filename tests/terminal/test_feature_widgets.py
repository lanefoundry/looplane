"""Feature ownership, compatibility, and standalone Textual interaction contracts."""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from looplane.approvals import ApprovalDecision, ApprovalReason, ApprovalRequest, ToolEffect
from looplane.contracts import ToolCall
from looplane.runtime_semantics import PermissionMode
from looplane.terminal.approvals import ApprovalModal, TextualApprovalPolicy
from looplane.terminal.tool_widgets import ToolActionBlock, ToolGroupBlock


@pytest.mark.parametrize(
    ('module', 'names'),
    [
        ('approvals', ('ApprovalModal', 'ApprovalPreview', 'InlineApprovalChoices',
                       'InlineApprovalBlock')),
        ('composer', ('MessageComposer',)),
        ('scroll', ('TranscriptScroll',)),
        ('transcript', ('MessageBlock', 'TimelineEntry')),
        ('tool_widgets', ('ToolActionBlock', 'ToolGroupBlock')),
        ('selectors', ('InlineSelectorChoices', 'InlineSelectorBlock')),
        ('status_widgets', ('RuntimeLoadingIndicator', 'RuntimeStatus')),
        ('onboarding', ('OnboardingModal',)),
    ],
)
def test_facade_reexports_canonical_widget_objects(module, names) -> None:
    from looplane import tui

    canonical = importlib.import_module(f'looplane.terminal.{module}')
    for name in names:
        assert getattr(tui, name) is getattr(canonical, name)


@pytest.mark.parametrize('module', [
    'approvals', 'composer', 'scroll', 'transcript', 'tool_widgets', 'selectors',
    'status_widgets', 'onboarding', 'clipboard', 'links',
])
def test_feature_imports_do_not_load_compatibility_facades(module) -> None:
    completed = subprocess.run(
        [sys.executable, '-c', f"""
import importlib
import sys
importlib.import_module('looplane.terminal.{module}')
for facade in ('looplane.tui', 'looplane.tui_clipboard', 'looplane.tui_links', 'looplane.cli'):
    assert facade not in sys.modules, facade
"""],
        capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr


def approval_request(preview: str = 'Change source file') -> ApprovalRequest:
    return ApprovalRequest(
        run_id='widget-contract', action_id='edit', effect=ToolEffect.MODIFY,
        reason=ApprovalReason.MODEL_TOOL, preview=preview,
        tool_call=ToolCall(name='replace_text', arguments={'grant_scope': 'edit:source'}),
    )


@pytest.mark.parametrize(('keys', 'expected'), [
    (('enter',), ApprovalDecision.ALLOW_ONCE),
    (('down', 'enter'), ApprovalDecision.ALLOW_SESSION),
    (('down', 'down', 'up', 'enter'), ApprovalDecision.ALLOW_SESSION),
    (('1',), ApprovalDecision.ALLOW_ONCE),
    (('2',), ApprovalDecision.ALLOW_SESSION),
    (('3',), ApprovalDecision.DENY),
    (('4',), ApprovalDecision.CANCEL),
    (('escape',), ApprovalDecision.CANCEL),
])
async def test_modal_focus_keyboard_decision_and_unmount(keys, expected) -> None:
    selected = []
    unmounted = asyncio.Event()

    class ObservedModal(ApprovalModal):
        def on_unmount(self) -> None:
            unmounted.set()

    modal = ObservedModal(approval_request())

    class Host(App):
        def on_mount(self) -> None:
            self.push_screen(modal, selected.append)

    async with Host().run_test() as pilot:
        await pilot.pause()
        assert pilot.app.focused is modal.query_one('#approval-choices', OptionList)
        await pilot.press(*keys)
        await pilot.pause()
        assert selected == [expected]
        assert modal not in pilot.app.screen_stack
        await asyncio.wait_for(unmounted.wait(), timeout=2)


async def test_policy_reads_live_permission_callback_and_shares_explicit_grants() -> None:
    mode = PermissionMode.ASK
    calls = []
    grants = set()

    async def ask(request: ApprovalRequest) -> ApprovalDecision:
        calls.append(request)
        return ApprovalDecision.ALLOW_SESSION

    policy = TextualApprovalPolicy(ask, grants, permission_mode=lambda: mode)
    request = approval_request()
    assert await policy.decide(request) == ApprovalDecision.ALLOW_SESSION
    assert await policy.decide(request) == ApprovalDecision.ALLOW_ONCE
    assert len(calls) == 1
    assert len(grants) == 1
    mode = PermissionMode.READ_ONLY
    assert await policy.decide(request) == ApprovalDecision.DENY
    assert len(calls) == 1


@pytest.mark.parametrize('verbose', [False, True])
async def test_tool_group_owns_actions_and_uses_explicit_verbose_port(verbose) -> None:
    first = ToolActionBlock('read-1', 'Read first')
    second = ToolActionBlock('read-2', 'Read second')
    group = ToolGroupBlock(first, is_verbose=lambda: verbose)

    class Host(App):
        def compose(self) -> ComposeResult:
            yield group

    async with Host().run_test() as pilot:
        group.add_action(second)
        await pilot.pause()
        first.set_state('completed')
        second.set_state('completed')
        assert group.title == 'Explored 2 items'
        assert group.collapsed is (not verbose)
        assert first.group is second.group is group
        group.set_verbose(True)
        first.set_state('completed')
        assert group.collapsed is False


def test_clipboard_and_link_legacy_dependency_patch_targets_are_shared() -> None:
    from looplane import tui_clipboard, tui_links
    from looplane.terminal import clipboard, links

    assert tui_clipboard.copy_with_native_command is clipboard.copy_with_native_command
    assert tui_clipboard.selected_text_for_copy is clipboard.selected_text_for_copy
    assert tui_clipboard.shutil is clipboard.shutil
    assert tui_clipboard.subprocess is clipboard.subprocess
    assert tui_links.TranscriptMarkdown is links.TranscriptMarkdown
    assert tui_links.resolve_transcript_link is links.resolve_transcript_link
