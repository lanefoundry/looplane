from rivumi.prompts import CODING_AGENT_PROMPT_VERSION, CODING_AGENT_SYSTEM_PROMPT


def test_m3_prompt_versions_the_observed_exact_edit_guidance() -> None:
    assert CODING_AGENT_PROMPT_VERSION == "m3-exact-edit-v3"
    assert "read a file before editing" in CODING_AGENT_SYSTEM_PROMPT
    assert "Prefer replace_text" in CODING_AGENT_SYSTEM_PROMPT
    assert "Use apply_patch for multi-hunk" in CODING_AGENT_SYSTEM_PROMPT


def test_prompt_directs_conversational_input_to_a_plain_reply() -> None:
    assert "Greetings" in CODING_AGENT_SYSTEM_PROMPT
    assert "small talk" in CODING_AGENT_SYSTEM_PROMPT
    assert "capability questions" in CODING_AGENT_SYSTEM_PROMPT
    assert "direct text reply" in CODING_AGENT_SYSTEM_PROMPT
    assert "do not call tools" in CODING_AGENT_SYSTEM_PROMPT
    assert "do not explore the repository" in CODING_AGENT_SYSTEM_PROMPT
