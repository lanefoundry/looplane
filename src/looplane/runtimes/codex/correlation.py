"""Owned native/local identity and turn lifecycle state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from looplane.conversation_runtime import (
    ConversationProtocolError,
)
from looplane.conversation_runtime import RuntimeToolStatus as RuntimeToolStatus
from looplane.runtimes.codex import parsing as _codex_parsing

_LOG = logging.getLogger("looplane.codex_app_server")


class CodexCorrelation:
    def __init__(self, *, new_id: Callable[[], str], stderr_tail: Callable[[], str]) -> None:
        self.new_id = new_id
        self.stderr_tail = stderr_tail
        self.native_thread_id: str | None = None

        self.native_turns: dict[str, str] = {}

        self.local_turns: dict[str, str] = {}

        self.starting_turn: str | None = None

        self.active_turn: str | None = None

        self.started_turns: set[str] = set()

        self.completed_turns: set[str] = set()

        self.native_actions: dict[tuple[str, str], str] = {}

        self.compaction_turns: set[str] = set()

        self.started_compactions: set[str] = set()

        self.completed_compactions: set[str] = set()

        self.compaction_start_future: asyncio.Future[str] | None = None

    def local_turn(self, native_turn: str, *, context: str) -> str:
        existing = self.native_turns.get(native_turn)
        if existing is not None:
            return existing
        if self.starting_turn is None:
            if self.active_turn is not None:
                # Codex 0.149+ may abandon its internal turn and continue the
                # same logical turn under a fresh id (see the turn/started
                # adoption); item-level notifications then reference an id
                # looplane never bound.  Adopt instead of failing the whole
                # conversation.
                _LOG.warning(
                    "codex app-server: %s adopts replacement native turn %r "
                    "into the active local turn",
                    context,
                    native_turn,
                )
                self.adopt_turn(native_turn, self.active_turn)
                return self.active_turn
            known = len(self.native_turns)
            _LOG.warning(
                "codex app-server: %s references an unbound turn "
                "(native_turn=%r, bound_turns=%d, thread=%r); recent stderr: %s",
                context,
                native_turn,
                known,
                self.native_thread_id,
                self.stderr_tail(),
            )
            raise ConversationProtocolError(
                f"{context} references an unknown turn "
                f"(native_turn={native_turn!r}, bound_turns={known})"
            )
        _LOG.debug(
            "codex app-server: %s binds native_turn=%r to the starting turn",
            context,
            native_turn,
        )
        self.bind_turn(native_turn, self.starting_turn)
        return self.starting_turn

    def bind_turn(self, native_turn: str, local_turn: str) -> None:
        existing = self.native_turns.get(native_turn)
        if existing is not None and existing != local_turn:
            raise ConversationProtocolError("native turn id was rebound")
        reverse = self.local_turns.get(local_turn)
        if reverse is not None and reverse != native_turn:
            raise ConversationProtocolError("local turn id was rebound")
        self.native_turns[native_turn] = local_turn
        self.local_turns[local_turn] = native_turn
        if local_turn in self.compaction_turns:
            binding = self.compaction_start_future
            if binding is not None and not binding.done():
                binding.set_result(local_turn)

    def adopt_turn(self, native_turn: str, local_turn: str) -> None:
        """Map a replacement native id onto an already-active local turn.

        Unlike :meth:`_bind_turn` this intentionally allows several native ids
        to share one local turn: Codex 0.149+ may abandon its internal turn and
        continue the same logical turn under a fresh id.  The original reverse
        binding is preserved so interrupts still target the id Codex knows.
        """

        existing = self.native_turns.get(native_turn)
        if existing is not None and existing != local_turn:
            raise ConversationProtocolError("native turn id was rebound")
        self.native_turns[native_turn] = local_turn

    def local_action(self, native_turn: str, native_item: str) -> str:
        key = (native_turn, native_item)
        value = self.native_actions.get(key)
        if value is None:
            value = self.new_id()
            self.native_actions[key] = value
        return value

    def correlated_turn(self, params: dict[str, Any], *, context: str) -> str:
        thread = params.get("threadId")
        native_turn = params.get("turnId")
        # An absent thread id is tolerated the same way as in the warning
        # handler: the app-server pipe is dedicated to one ephemeral thread,
        # so a missing field cannot point at a foreign conversation.  A
        # present but different id still fails closed.
        if (thread is not None and thread != self.native_thread_id) or not _codex_parsing.safe_id(
            native_turn
        ):
            _LOG.warning(
                "codex app-server: %s has invalid correlation "
                "(thread=%r, expected=%r, turnId=%r); recent stderr: %s",
                context,
                thread,
                self.native_thread_id,
                native_turn,
                self.stderr_tail(),
            )
            raise ConversationProtocolError(f"{context} notification correlation is invalid")
        return self.local_turn(native_turn, context=context)
