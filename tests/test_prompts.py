from rivumi.contracts import Message, ToolObservation
from rivumi.prompts import (
    CODING_AGENT_PROMPT_VERSION,
    CODING_AGENT_SYSTEM_PROMPT,
    CONTEXT_PRESSURE_REMINDER_VERSION,
    CONTEXT_SUMMARY_FALLBACK_VERSION,
    WORKSPACE_CONTEXT_REMINDER_VERSION,
    build_coding_agent_system_prompt,
    build_context_pressure_reminder,
    build_history_summary_fallback_message,
    build_workspace_context_reminder,
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

    assert prompt.startswith(CODING_AGENT_SYSTEM_PROMPT.splitlines()[0])
    assert "Known context:\n- [user] terse" in prompt


def test_prompt_builder_appends_instruction_context_before_memory() -> None:
    prompt = build_coding_agent_system_prompt(
        instruction_context="Additional instructions:\n- project rule",
        known_context="Known context:\n- memory rule",
    )

    assert prompt.index("Additional instructions") < prompt.index("Known context")


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
