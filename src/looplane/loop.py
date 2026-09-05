"""Compatibility entry point for the canonical native agent runner.

Legacy monkeypatches resolve through explicit late-bound dependency seams. Native
agent leaves never import this module, and durable lifecycle remains in agent owners.
"""

from __future__ import annotations

import asyncio as asyncio
import contextlib as contextlib
from uuid import uuid4

from looplane.agent import context as context
from looplane.agent import model_calls as model_calls
from looplane.agent import subagent_dispatch as subagent_dispatch
from looplane.agent import tool_scheduler as tool_scheduler
from looplane.agent.checkpoints import (
    RunPersistence as RunPersistence,
)
from looplane.agent.checkpoints import (
    check_resume_identity as check_resume_identity,
)
from looplane.agent.checkpoints import (
    claim_session,
)
from looplane.agent.checkpoints import (
    session_phase as session_phase,
)
from looplane.agent.model_calls import (
    MODEL_ATTEMPTS as MODEL_ATTEMPTS,
)
from looplane.agent.model_calls import (
    RETRY_BACKOFF_BASE_SECONDS as RETRY_BACKOFF_BASE_SECONDS,
)
from looplane.agent.model_calls import (
    RETRY_JITTER_FRACTION as RETRY_JITTER_FRACTION,
)
from looplane.agent.model_calls import (
    RETRY_MAX_DELAY_SECONDS as RETRY_MAX_DELAY_SECONDS,
)
from looplane.agent.model_calls import (
    RETRY_SERVER_HINT_MAX_SECONDS as RETRY_SERVER_HINT_MAX_SECONDS,
)
from looplane.agent.model_calls import (
    retry_delay_seconds,
)
from looplane.agent.runner import (
    READ_ONLY_STALL_THRESHOLD as READ_ONLY_STALL_THRESHOLD,
)
from looplane.agent.runner import AgentRunner as _AgentRunner
from looplane.agent.runner import (
    UnsafeLocalExecutionError as UnsafeLocalExecutionError,
)
from looplane.agent.state import ContextState as ContextState
from looplane.agent.state import TurnState as TurnState
from looplane.context_providers import load_project_context_provider_runner
from looplane.events import EventWriter, atomic_write_json
from looplane.hooks import load_project_hook_runner
from looplane.mcp_client import load_native_mcp_server_configs
from looplane.runtime import (
    LocalGitWorkspace,
    run_bounded_command,
    sanitized_subprocess_env,
)
from looplane.runtime import (
    WorkspacePreparationError as WorkspacePreparationError,
)
from looplane.runtime import (
    bounded_text as bounded_text,
)
from looplane.tools import ToolExecutionError as ToolExecutionError
from looplane.tools import ToolExecutor


class AgentRunner(_AgentRunner):
    """Legacy constructor and inherited resume retain explicit patch interception."""

    @staticmethod
    def _tool_executor_factory(*args, **kwargs):
        return ToolExecutor(*args, **kwargs)

    @staticmethod
    def _workspace_factory(*args, **kwargs):
        return LocalGitWorkspace(*args, **kwargs)

    @staticmethod
    def _event_writer_factory(*args, **kwargs):
        return EventWriter(*args, **kwargs)

    @staticmethod
    def _load_hooks(*args, **kwargs):
        return load_project_hook_runner(*args, **kwargs)

    @staticmethod
    def _load_context_providers(*args, **kwargs):
        return load_project_context_provider_runner(*args, **kwargs)

    @staticmethod
    def _load_mcp_configs(*args, **kwargs):
        return load_native_mcp_server_configs(*args, **kwargs)

    @staticmethod
    def _run_command(*args, **kwargs):
        return run_bounded_command(*args, **kwargs)

    @staticmethod
    def _environment(*args, **kwargs):
        return sanitized_subprocess_env(*args, **kwargs)

    @staticmethod
    async def _write_json(*args, **kwargs):
        return await atomic_write_json(*args, **kwargs)

    @staticmethod
    async def _claim_session(*args, **kwargs):
        return await claim_session(*args, **kwargs)

    @staticmethod
    def _retry_delay(attempt: int, retry_after_seconds: float | None) -> float:
        return retry_delay_seconds(attempt, retry_after_seconds)

    @staticmethod
    def _new_id():
        return uuid4()
