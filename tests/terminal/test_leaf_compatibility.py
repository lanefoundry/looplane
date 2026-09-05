"""Compatibility and behavior contracts for the first terminal extraction."""

from __future__ import annotations

import dataclasses
import pickle
import subprocess
import sys
from pathlib import Path
from typing import get_type_hints

import pytest
from textual import on
from textual.app import App

from looplane import tui
from looplane.contracts import Usage
from looplane.terminal import events, status, types


@pytest.mark.parametrize(
    ("module", "names"),
    [
        (types, (
            "ProviderOption", "RuntimeOption", "RuntimeModelOption", "InteractionState",
            "LoadingPhase", "TuiRunner", "TuiResource", "TuiRunRequest", "RunnerFactory",
            "TuiConfigurationSelection", "CommandMenuChoice", "InlineSelectorOption",
        )),
        (events, (
            "RunEventMessage", "ExternalRunEventMessage", "ConversationRuntimeEventMessage",
        )),
        (status, ("format_token_count", "_add_usage", "_usage_bar")),
    ],
)
def test_old_exports_are_canonical_objects(module, names) -> None:
    for name in names:
        assert getattr(tui, name) is getattr(module, name)


def test_request_defaults_frozen_fields_and_pickle_compatibility() -> None:
    request = types.TuiRunRequest(Path("repo"), "fix", "native", None, None, None)
    assert [field.name for field in dataclasses.fields(request)] == [
        "repository", "instruction", "runtime", "provider", "model", "api_url",
        "mode", "context_id", "continuation_run_dir",
    ]
    assert request.mode == "agent"
    assert request.context_id is None
    assert request.continuation_run_dir is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.mode = "ask"
    assert pickle.loads(pickle.dumps(request)) == request
    # A pre-extraction pickle resolves the global through its original facade.
    assert pickle.loads(b"clooplane.tui\nTuiRunRequest\n.") is types.TuiRunRequest
    assert get_type_hints(types.TuiRunRequest)["repository"] is Path


def test_selection_and_enum_contracts() -> None:
    option = types.InlineSelectorOption("native", "Native", "Local runtime")
    assert option.selected is False
    assert types.CommandMenuChoice("/help", "/help", True).execute is True
    assert [state.value for state in types.InteractionState] == [
        "approval", "selector", "command-menu", "running", "composer", "transcript",
    ]
    assert [phase.value for phase in types.LoadingPhase] == [
        "requesting", "responding", "thinking", "tool-use", "verifying",
    ]


@pytest.mark.parametrize(
    ("count", "expected"),
    [(-1, "-1"), (0, "0"), (999, "999"), (1000, "1k"), (1540, "1.5k"),
     (12340, "12.3k"), (1000000, "1000k")],
)
def test_token_formatting_boundaries(count, expected) -> None:
    assert status.format_token_count(count) == expected


@pytest.mark.parametrize(
    ("percent", "width", "filled"),
    [(-10, 10, 0), (0, 10, 0), (5, 10, 0), (15, 10, 2), (50, 10, 5),
     (100, 10, 10), (200, 10, 10), (50, 4, 2), (100, 0, 0)],
)
def test_usage_bar_clamping_and_rounding(percent, width, filled) -> None:
    expected = "\u25b0" * filled + "\u25b1" * (width - filled)
    assert status._usage_bar(percent, width=width) == expected


@pytest.mark.parametrize("provider_total", [None, 0, 100])
def test_usage_arithmetic_preserves_total_fallback(provider_total) -> None:
    left = Usage(input_tokens=10, output_tokens=5, cached_input_tokens=3,
                 reasoning_tokens=2, provider_total_tokens=provider_total)
    right = Usage(input_tokens=20, output_tokens=7, cached_input_tokens=4,
                  reasoning_tokens=3, provider_total_tokens=50)
    original = left.model_dump()
    combined = status._add_usage(left, right)
    assert combined.input_tokens == 30
    assert combined.output_tokens == 12
    assert combined.cached_input_tokens == 7
    assert combined.reasoning_tokens == 5
    assert combined.provider_total_tokens == (provider_total or left.total_tokens) + 50
    assert left.model_dump() == original


@pytest.mark.parametrize(
    ("message_type", "handler_name"),
    [(events.RunEventMessage, "on_run_event_message"),
     (events.ExternalRunEventMessage, "on_external_run_event_message"),
     (events.ConversationRuntimeEventMessage, "on_conversation_runtime_event_message")],
)
async def test_message_routing_payload_and_generation(message_type, handler_name) -> None:
    payload = object()
    message = message_type(payload, 7)
    assert message.handler_name == handler_name
    assert message.event is payload
    assert message.generation == 7
    assert "event" in get_type_hints(message_type.__init__)
    delivered = []

    class Receiver(App):
        @on(message_type)
        def receive(self, event) -> None:
            if event.generation == 7:
                delivered.append(event.event)

    async with Receiver().run_test() as pilot:
        pilot.app.post_message(message_type(object(), 6))
        pilot.app.post_message(message)
        await pilot.pause()
    assert delivered == [payload]


def test_widget_still_uses_facade_formatter_monkeypatch(monkeypatch) -> None:
    monkeypatch.setattr(tui, "format_token_count", lambda count: f"patched:{count}")
    rendered = []
    metrics = tui.RuntimeMetrics(id="metrics")
    monkeypatch.setattr(metrics, "update", rendered.append)
    metrics.set_metrics(input_tokens=1000, output_tokens=20)
    assert "patched:1000" in rendered[0].plain
    assert "patched:20" in rendered[0].plain


@pytest.mark.parametrize("module", ["types", "events", "status"])
def test_canonical_import_does_not_load_app_or_facades(module) -> None:
    code = f"""
import importlib
import sys
importlib.import_module('looplane.terminal.{module}')
for name in ('looplane.tui', 'looplane.cli', 'looplane.backends', 'textual.app'):
    assert name not in sys.modules, name
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
