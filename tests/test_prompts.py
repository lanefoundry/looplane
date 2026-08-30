from looplane.contracts import Message, ToolDefinition, ToolObservation, VerificationCommand
from looplane.prompts import (
    A10_SUBAGENT_PLANNER_POLICY_VERSION,
    CODING_AGENT_PROMPT_VERSION,
    CODING_AGENT_SYSTEM_PROMPT,
    CONTEXT_PRESSURE_REMINDER_VERSION,
    CONTEXT_SUMMARY_FALLBACK_VERSION,
    INTERACTION_CONTEXT_VERSION,
    WORKSPACE_CONTEXT_REMINDER_VERSION,
    PromptSection,
    build_coding_agent_system_prompt,
    build_context_pressure_reminder,
    build_history_summary_fallback_message,
    build_workspace_context_reminder,
    render_interaction_prompt_context,
    render_prompt_sections,
    render_runtime_prompt_context,
    render_subagent_planner_policy,
    render_tool_prompt_context,
    render_workspace_prompt_context,
)


def test_m3_prompt_versions_the_observed_exact_edit_guidance() -> None:
    assert CODING_AGENT_PROMPT_VERSION == "m3-exact-edit-v4"
    assert "read a file before editing" in CODING_AGENT_SYSTEM_PROMPT
    assert "Prefer replace_text" in CODING_AGENT_SYSTEM_PROMPT
    assert "Use apply_patch for multi-hunk" in CODING_AGENT_SYSTEM_PROMPT
    assert "Correct replace_text flow" in CODING_AGENT_SYSTEM_PROMPT
    assert "Incorrect replace_text flow" in CODING_AGENT_SYSTEM_PROMPT


def test_prompt_directs_conversational_input_to_a_plain_reply() -> None:
    assert "greetings" in CODING_AGENT_SYSTEM_PROMPT
    assert "small talk" in CODING_AGENT_SYSTEM_PROMPT
    assert "capability questions" in CODING_AGENT_SYSTEM_PROMPT
    assert "answer in text without calling tools" in CODING_AGENT_SYSTEM_PROMPT
    assert "do not call tools" in CODING_AGENT_SYSTEM_PROMPT
    assert "Do not explore the repository" in CODING_AGENT_SYSTEM_PROMPT


def test_prompt_builder_appends_known_context() -> None:
    prompt = build_coding_agent_system_prompt(known_context="Known context:\n- [user] terse")

    assert "<section name='core_policy' cache='stable'>" in prompt
    assert "Known context:\n- [user] terse" in prompt


def test_prompt_builder_appends_instruction_context_before_memory() -> None:
    prompt = build_coding_agent_system_prompt(
        tool_context="Tool policy",
        interaction_context="Interaction policy",
        runtime_context="Runtime facts",
        instruction_context="Additional instructions:\n- project rule",
        skill_context="Project skills:\n- review carefully",
        workspace_context="Workspace state",
        known_context="Known context:\n- memory rule",
    )

    assert prompt.index("Tool policy") < prompt.index("Runtime facts")
    assert prompt.index("Tool policy") < prompt.index("Interaction policy")
    assert prompt.index("Interaction policy") < prompt.index("Runtime facts")
    assert prompt.index("Runtime facts") < prompt.index("Additional instructions")
    assert prompt.index("Additional instructions") < prompt.index("Known context")
    assert prompt.index("Additional instructions") < prompt.index("Project skills")
    assert prompt.index("Project skills") < prompt.index("Known context")
    assert prompt.index("Workspace state") < prompt.index("Known context")


def test_prompt_builder_renders_broader_context_sections() -> None:
    tool_context = render_tool_prompt_context(
        (
            ToolDefinition(
                name="read_file",
                description="Read one file.",
                input_schema={"type": "object"},
                read_only=True,
                concurrency_safe=True,
            ),
        )
    )
    workspace_context = render_workspace_prompt_context(
        base_sha="a" * 40,
        allowed_paths=("src/**",),
        verification=(VerificationCommand(name="tests", argv=("pytest", "-q")),),
        git_status=("## main", " M src/app.py"),
    )
    interaction_context = render_interaction_prompt_context()
    runtime_context = render_runtime_prompt_context({"mode": "native_loop", "sandbox_checks": True})
    prompt = build_coding_agent_system_prompt(
        tool_context=tool_context,
        interaction_context=interaction_context,
        workspace_context=workspace_context,
        runtime_context=runtime_context,
    )

    assert "<section name='tool_policy' cache='stable'>" in prompt
    assert "<section name='interaction_policy' cache='stable'>" in prompt
    assert "<section name='runtime_context' cache='dynamic'>" in prompt
    assert "<section name='workspace_state' cache='dynamic'>" in prompt
    assert "[b1-tool-policy-v1]" in prompt
    assert f"[{INTERACTION_CONTEXT_VERSION}]" in prompt
    assert "ask_mode: ask_only_when_required_or_high_risk" in prompt
    assert "- read_file (read_only, concurrency_safe): Read one file." in prompt
    assert "[b1-workspace-state-v1]" in prompt
    assert "base_sha: " + "a" * 40 in prompt
    assert "git_status_short:" in prompt
    assert "- M src/app.py" in prompt
    assert "[b1-runtime-context-v1]" in prompt


def test_subagent_planner_policy_is_versioned_and_actionable() -> None:
    prompt = render_subagent_planner_policy()

    assert prompt.startswith(f"[{A10_SUBAGENT_PLANNER_POLICY_VERSION}]")
    assert "dispatch_subagents" in prompt
    assert "scout" in prompt
    assert "analyst" in prompt
    assert "reviewer" in prompt
    assert "depends_on" in prompt
    assert "proposed_transaction" in prompt
    assert "tool_transaction" in prompt
    assert "trivial single-file edits" in prompt


def test_prompt_sections_are_named_and_cache_annotated() -> None:
    prompt = render_prompt_sections(
        (
            PromptSection("core", "Stable rules", cache_stable=True),
            PromptSection("workspace", "Dynamic state"),
        )
    )

    assert "<section name='core' cache='stable'>" in prompt
    assert "<section name='workspace' cache='dynamic'>" in prompt
    assert prompt.index("Stable rules") < prompt.index("Dynamic state")


def test_context_pressure_reminder_builder_is_versioned_and_concrete() -> None:
    prompt = build_context_pressure_reminder(total_tokens=85_000, max_total_tokens=100_000)

    assert prompt.startswith(f"[{CONTEXT_PRESSURE_REMINDER_VERSION}]")
    assert "85,000" in prompt
    assert "100,000" in prompt
    assert "hard token budget" in prompt


def test_history_summary_fallback_message_is_versioned_and_bounded() -> None:
    message = build_history_summary_fallback_message(
        [
            Message(role="assistant", content="Read the file."),
            ToolObservation(
                tool_call_id="call-1",
                name="read_file",
                ok=True,
                content="line\n" * 500,
            ),
        ],
        source_start_index=2,
        source_end_index=4,
        max_chars=700,
        max_field_chars=120,
    )

    assert message.role == "user"
    assert message.content is not None
    assert message.content.startswith(f"[{CONTEXT_SUMMARY_FALLBACK_VERSION}]")
    assert "2..3" in message.content
    assert "assistant" in message.content
    assert "tool read_file" in message.content
    assert len(message.content) <= 700


def test_workspace_context_reminder_is_versioned_deterministic_and_bounded() -> None:
    message = build_workspace_context_reminder(
        changed_files=[f"src/file_{index}.py" for index in range(20)],
        check_status=["tests: failed (exit 1)"],
        recent_paths=["src/file_0.py", "tests/test_file.py"],
        constraints=["allowed_paths=src/**, tests/**", "verification=tests"],
        max_chars=900,
        max_items=3,
    )

    assert message.role == "user"
    assert message.content is not None
    assert message.content.startswith(f"[{WORKSPACE_CONTEXT_REMINDER_VERSION}]")
    assert "Changed files:" in message.content
    assert "src/file_0.py" in message.content
    assert "... 17 more omitted" in message.content
    assert "tests: failed" in message.content
    assert "Active constraints:" in message.content
    assert len(message.content) <= 900
