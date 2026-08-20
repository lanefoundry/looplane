from coding_agent.prompts import CODING_AGENT_PROMPT_VERSION, CODING_AGENT_SYSTEM_PROMPT


def test_m3_prompt_versions_the_observed_exact_edit_guidance() -> None:
    assert CODING_AGENT_PROMPT_VERSION == "m3-exact-edit-v1"
    assert "read a file before editing" in CODING_AGENT_SYSTEM_PROMPT
    assert "Prefer replace_text" in CODING_AGENT_SYSTEM_PROMPT
    assert "Use apply_patch for multi-hunk" in CODING_AGENT_SYSTEM_PROMPT
