"""Provider-neutral discovery and parsing for Rivumi slash commands.

This module deliberately describes commands without executing them.  A UI can use
the same registry for its command palette, completion, help, and dispatch while
retaining control over application-specific side effects.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final


class SlashCommand(StrEnum):
    """Canonical command identifiers understood by Rivumi."""

    MODEL = "model"
    RUNTIME = "runtime"
    NEW = "new"
    RESUME = "resume"
    REWIND = "rewind"
    CLEAR = "clear"
    HISTORY = "history"
    STATUS = "status"
    HELP = "help"
    COMPACT = "compact"
    CONTEXT = "context"
    PERMISSIONS = "permissions"
    EXIT = "exit"


class ArgumentExpectation(StrEnum):
    """Whether text following a slash command is accepted."""

    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


@dataclass(frozen=True, slots=True)
class CommandMetadata:
    """Discoverable presentation and parsing information for one command."""

    command: SlashCommand
    description: str
    argument_expectation: ArgumentExpectation = ArgumentExpectation.NONE
    argument_name: str | None = None
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValueError("command description cannot be blank")
        if self.argument_expectation is ArgumentExpectation.NONE and self.argument_name:
            raise ValueError("a command that takes no argument cannot have an argument name")
        if self.argument_expectation is not ArgumentExpectation.NONE and not self.argument_name:
            raise ValueError("a command that accepts an argument must name it")
        for alias in self.aliases:
            if not _is_command_name(alias):
                raise ValueError(f"invalid command alias: {alias!r}")

    @property
    def name(self) -> str:
        """Canonical command name without the leading slash."""

        return self.command.value

    @property
    def invocation(self) -> str:
        """Human-readable usage suitable for a command palette or help view."""

        base = f"/{self.name}"
        if self.argument_expectation is ArgumentExpectation.NONE:
            return base
        if self.argument_expectation is ArgumentExpectation.OPTIONAL:
            return f"{base} [{self.argument_name}]"
        return f"{base} <{self.argument_name}>"

    @property
    def names(self) -> tuple[str, ...]:
        """Canonical name followed by accepted aliases, all without slashes."""

        return (self.name, *self.aliases)


@dataclass(frozen=True, slots=True)
class ParsedSlashCommand:
    """A validated command for an application layer to execute."""

    command: SlashCommand
    argument: str | None
    invoked_as: str
    metadata: CommandMetadata

    @property
    def name(self) -> str:
        return self.command.value


class SlashCommandError(ValueError):
    """Base class for local slash-command validation failures."""


class NotSlashCommand(SlashCommandError):
    """Raised when input is not a slash-command invocation."""


class UnknownSlashCommand(SlashCommandError):
    """Raised for commands absent from the registry.

    Unknown input must never be forwarded to a model or provider as a command.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"unknown slash command: /{name}" if name else "unknown slash command: /")


class InvalidSlashCommand(SlashCommandError):
    """Raised when a known command receives arguments of the wrong shape."""

    def __init__(self, metadata: CommandMetadata, reason: str) -> None:
        self.metadata = metadata
        self.reason = reason
        super().__init__(f"invalid {metadata.invocation}: {reason}")


class SlashCommandRegistry:
    """Immutable collection used for discovery, completion, and safe parsing."""

    def __init__(self, commands: Iterable[CommandMetadata]) -> None:
        ordered = tuple(commands)
        by_name: dict[str, CommandMetadata] = {}
        for metadata in ordered:
            for name in metadata.names:
                normalized = name.casefold()
                if normalized in by_name:
                    raise ValueError(f"duplicate slash command name: /{name}")
                by_name[normalized] = metadata
        self._commands = ordered
        self._by_name: Mapping[str, CommandMetadata] = MappingProxyType(by_name)

    @property
    def commands(self) -> tuple[CommandMetadata, ...]:
        """All canonical commands in stable display order."""

        return self._commands

    def resolve(self, name: str) -> CommandMetadata | None:
        """Resolve a canonical name or alias, with or without a leading slash."""

        normalized = name.strip().removeprefix("/").casefold()
        return self._by_name.get(normalized)

    def complete(self, text: str) -> tuple[CommandMetadata, ...]:
        """Return commands matching a partial leading-slash input.

        Completion only activates for slash-prefixed input.  Once an argument is
        being entered, the selected command remains the sole result.
        """

        if not text.startswith("/"):
            return ()
        token = text[1:].split(maxsplit=1)[0].casefold() if text[1:] else ""
        if any(character.isspace() for character in text[1:]):
            selected = self._by_name.get(token)
            return (selected,) if selected is not None else ()
        return tuple(
            metadata
            for metadata in self._commands
            if any(name.casefold().startswith(token) for name in metadata.names)
        )

    def parse(self, text: str) -> ParsedSlashCommand:
        """Parse registered input without forwarding or executing anything."""

        stripped = text.strip()
        if not stripped.startswith("/"):
            raise NotSlashCommand("input must start with /")

        invocation = stripped[1:]
        parts = invocation.split(maxsplit=1)
        invoked_as = parts[0].casefold() if parts else ""
        metadata = self._by_name.get(invoked_as)
        if metadata is None:
            raise UnknownSlashCommand(invoked_as)

        argument = parts[1].strip() if len(parts) == 2 else None
        argument = argument or None
        if metadata.argument_expectation is ArgumentExpectation.NONE and argument is not None:
            raise InvalidSlashCommand(metadata, "this command does not accept an argument")
        if metadata.argument_expectation is ArgumentExpectation.REQUIRED and argument is None:
            raise InvalidSlashCommand(metadata, "an argument is required")
        return ParsedSlashCommand(
            command=metadata.command,
            argument=argument,
            invoked_as=invoked_as,
            metadata=metadata,
        )


def _is_command_name(value: str) -> bool:
    return (
        bool(value)
        and value == value.strip()
        and not value.startswith("/")
        and not any(character.isspace() for character in value)
    )


DEFAULT_COMMANDS: Final[tuple[CommandMetadata, ...]] = (
    CommandMetadata(
        SlashCommand.MODEL,
        "Choose or inspect the active model.",
        ArgumentExpectation.OPTIONAL,
        "model",
    ),
    CommandMetadata(
        SlashCommand.RUNTIME,
        "Choose or inspect the active runtime.",
        ArgumentExpectation.OPTIONAL,
        "runtime",
    ),
    CommandMetadata(SlashCommand.NEW, "Start a new conversation."),
    CommandMetadata(
        SlashCommand.RESUME,
        "Resume a saved conversation (defaults to the latest).",
        ArgumentExpectation.OPTIONAL,
        "id|last",
    ),
    CommandMetadata(
        SlashCommand.REWIND,
        "Fork the conversation before a previous prompt.",
    ),
    CommandMetadata(SlashCommand.CLEAR, "Clear the current conversation."),
    CommandMetadata(
        SlashCommand.HISTORY,
        "Browse saved conversations.",
        aliases=("conversations",),
    ),
    CommandMetadata(SlashCommand.STATUS, "Show runtime, model, and conversation status."),
    CommandMetadata(SlashCommand.HELP, "Show available slash commands."),
    CommandMetadata(
        SlashCommand.COMPACT,
        "Compact conversation context, optionally with extra guidance.",
        ArgumentExpectation.OPTIONAL,
        "instructions",
    ),
    CommandMetadata(SlashCommand.CONTEXT, "Inspect conversation context usage."),
    CommandMetadata(
        SlashCommand.PERMISSIONS,
        "Inspect or change process-local tool permissions.",
        ArgumentExpectation.OPTIONAL,
        "ask|accept-edits|read-only|clear",
    ),
    CommandMetadata(
        SlashCommand.EXIT,
        "Stop the active agent and close Rivumi.",
        aliases=("quit",),
    ),
)

DEFAULT_SLASH_COMMAND_REGISTRY: Final = SlashCommandRegistry(DEFAULT_COMMANDS)


def parse_slash_command(text: str) -> ParsedSlashCommand:
    """Parse input using Rivumi's built-in registry."""

    return DEFAULT_SLASH_COMMAND_REGISTRY.parse(text)


def complete_slash_commands(text: str) -> tuple[CommandMetadata, ...]:
    """Complete input using Rivumi's built-in registry."""

    return DEFAULT_SLASH_COMMAND_REGISTRY.complete(text)
