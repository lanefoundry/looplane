from __future__ import annotations

import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from rivumi.codex_oauth import CodexAuthorization
from rivumi.oauth_login import parse_codex_callback, wait_for_codex_callback


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


def test_listener_is_bound_before_ready_and_ignores_invalid_first_request() -> None:
    authorization = CodexAuthorization(
        url="https://example.test/authorize",
        verifier="verifier",
        state="expected",
    )
    sender: threading.Thread | None = None

    def send_callbacks(address: tuple[str, int]) -> None:
        nonlocal sender

        def send() -> None:
            base = f"http://127.0.0.1:{address[1]}/auth/callback"
            with pytest.raises(HTTPError):
                urlopen(f"{base}?code=wrong&state=invalid", timeout=1)
            with urlopen(f"{base}?code=accepted&state=expected", timeout=1) as response:
                assert response.status == 200

        sender = threading.Thread(target=send)
        sender.start()

    code = wait_for_codex_callback(
        authorization,
        timeout_seconds=1,
        on_listening=send_callbacks,
        bind_port=0,
    )

    assert code == "accepted"
    assert sender is not None
    sender.join(timeout=1)
    assert not sender.is_alive()


def test_listener_timeout_is_bounded() -> None:
    authorization = CodexAuthorization(
        url="https://example.test/authorize",
        verifier="verifier",
        state="expected",
    )

    with pytest.raises(TimeoutError, match="not received"):
        wait_for_codex_callback(authorization, timeout_seconds=0.01, bind_port=0)
