from __future__ import annotations

import pytest

from looplane.slash_commands import (
    DEFAULT_SLASH_COMMAND_REGISTRY,
    ArgumentExpectation,
    CommandMetadata,
    InvalidSlashCommand,
    NotSlashCommand,
    ParsedSlashCommand,
    SlashCommand,
    SlashCommandRegistry,
    UnknownSlashCommand,
    complete_slash_commands,
    parse_slash_command,
)


def test_default_registry_exposes_discoverable_metadata() -> None:
    commands = DEFAULT_SLASH_COMMAND_REGISTRY.commands

    assert [metadata.command for metadata in commands] == [
        SlashCommand.PROVIDER,
        SlashCommand.MODEL,
        SlashCommand.RUNTIME,
        SlashCommand.NEW,
        SlashCommand.RESUME,
        SlashCommand.REWIND,
        SlashCommand.CLEAR,
        SlashCommand.HISTORY,
        SlashCommand.STATUS,
        SlashCommand.HELP,
        SlashCommand.COMPACT,
        SlashCommand.CONTEXT,
        SlashCommand.USAGE,
        SlashCommand.REMEMBER,
        SlashCommand.PERMISSIONS,
        SlashCommand.EXIT,
    ]
    assert all(metadata.description for metadata in commands)
    conversations = DEFAULT_SLASH_COMMAND_REGISTRY.resolve("/conversations")
    assert conversations is DEFAULT_SLASH_COMMAND_REGISTRY.resolve("history")


@pytest.mark.parametrize(
    ("text", "command", "argument"),
    [
        ("/model", SlashCommand.MODEL, None),
        ("/model opus", SlashCommand.MODEL, "opus"),
        ("/runtime claude", SlashCommand.RUNTIME, "claude"),
        ("/new", SlashCommand.NEW, None),
        ("/resume", SlashCommand.RESUME, None),
        ("/resume last", SlashCommand.RESUME, "last"),
        ("/clear", SlashCommand.CLEAR, None),
        ("/history", SlashCommand.HISTORY, None),
        ("/conversations", SlashCommand.HISTORY, None),
        ("/status", SlashCommand.STATUS, None),
        ("/help", SlashCommand.HELP, None),
        ("/compact retain the build logs", SlashCommand.COMPACT, "retain the build logs"),
        ("/context", SlashCommand.CONTEXT, None),
        ("/remember user: concise replies", SlashCommand.REMEMBER, "user: concise replies"),
        ("/permissions", SlashCommand.PERMISSIONS, None),
        ("/permissions clear", SlashCommand.PERMISSIONS, "clear"),
        ("/exit", SlashCommand.EXIT, None),
        ("/quit", SlashCommand.EXIT, None),
    ],
)
def test_parse_returns_typed_canonical_commands(
    text: str, command: SlashCommand, argument: str | None
) -> None:
    parsed = parse_slash_command(text)

    assert isinstance(parsed, ParsedSlashCommand)
    assert parsed.command is command
    assert parsed.argument == argument


def test_alias_preserves_invoked_name_while_canonicalizing_command() -> None:
    parsed = parse_slash_command("  /CONVERSATIONS  ")

    assert parsed.command is SlashCommand.HISTORY
    assert parsed.invoked_as == "conversations"
    assert parsed.name == "history"


def test_completion_requires_leading_slash_and_filters_names_and_aliases() -> None:
    assert complete_slash_commands("model") == ()
    assert [item.command for item in complete_slash_commands("/m")] == [SlashCommand.MODEL]
    assert [item.command for item in complete_slash_commands("/co")] == [
        SlashCommand.HISTORY,
        SlashCommand.COMPACT,
        SlashCommand.CONTEXT,
    ]
    assert [item.command for item in complete_slash_commands("/rem")] == [SlashCommand.REMEMBER]
    assert complete_slash_commands("/resume abc") == (
        DEFAULT_SLASH_COMMAND_REGISTRY.resolve("resume"),
    )
    assert complete_slash_commands("/unknown arg") == ()
    assert complete_slash_commands("/") == DEFAULT_SLASH_COMMAND_REGISTRY.commands


def test_argument_expectations_produce_usage_and_validate_input() -> None:
    resume = DEFAULT_SLASH_COMMAND_REGISTRY.resolve("resume")
    assert resume is not None
    assert resume.argument_expectation is ArgumentExpectation.OPTIONAL
    assert resume.invocation == "/resume [id|last]"

    with pytest.raises(InvalidSlashCommand, match="does not accept an argument"):
        parse_slash_command("/status verbose")

    registry = SlashCommandRegistry(
        (
            CommandMetadata(
                SlashCommand.HELP,
                "Open one help topic.",
                ArgumentExpectation.REQUIRED,
                "topic",
            ),
        )
    )
    with pytest.raises(InvalidSlashCommand, match="argument is required"):
        registry.parse("/help")


def test_unknown_and_non_command_input_are_rejected_locally() -> None:
    with pytest.raises(UnknownSlashCommand) as error:
        parse_slash_command("/provider-secret-command --unsafe")
    assert error.value.name == "provider-secret-command"

    with pytest.raises(UnknownSlashCommand):
        parse_slash_command("/")

    with pytest.raises(NotSlashCommand):
        parse_slash_command("hello")


def test_registry_rejects_duplicate_aliases() -> None:
    with pytest.raises(ValueError, match="duplicate slash command name"):
        SlashCommandRegistry(
            (
                CommandMetadata(SlashCommand.HISTORY, "History.", aliases=("list",)),
                CommandMetadata(SlashCommand.HELP, "Help.", aliases=("LIST",)),
            )
        )
