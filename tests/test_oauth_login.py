from __future__ import annotations

import pytest

from coding_agent.oauth_login import parse_codex_callback


def test_parse_codex_callback_validates_state_and_loopback() -> None:
    code = parse_codex_callback(
        "http://localhost:1455/auth/callback?code=short-lived&state=expected",
        expected_state="expected",
    )

    assert code == "short-lived"


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example/auth/callback?code=x&state=expected",
        "http://localhost:1455/other?code=x&state=expected",
        "http://localhost:1455/auth/callback?code=x&state=wrong",
        "http://localhost:1455/auth/callback?state=expected",
    ],
)
def test_parse_codex_callback_rejects_unsafe_or_incomplete_values(url: str) -> None:
    with pytest.raises(ValueError):
        parse_codex_callback(url, expected_state="expected")
