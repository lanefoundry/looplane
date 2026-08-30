"""Loopback callback helper for this application's Codex OAuth grant."""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from looplane.codex_oauth import CodexAuthorization


def parse_codex_callback(value: str, *, expected_state: str) -> str:
    """Validate a full callback URL and return only its short-lived code."""

    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError("OAuth callback must use the local loopback host")
    if parsed.path != "/auth/callback":
        raise ValueError("OAuth callback path is invalid")
    values = parse_qs(parsed.query)
    state = values.get("state", [""])[0]
    code = values.get("code", [""])[0]
    if not state or not hmac.compare_digest(state, expected_state):
        raise ValueError("OAuth callback state does not match")
    if not code:
        raise ValueError("OAuth callback does not contain a code")
    return code


def wait_for_codex_callback(
    authorization: CodexAuthorization,
    *,
    timeout_seconds: float = 300.0,
    on_listening: Callable[[tuple[str, int]], None] | None = None,
    bind_host: str = "127.0.0.1",
    bind_port: int = 1455,
) -> str:
    """Bind first, announce readiness, then wait until one valid callback or timeout."""

    if timeout_seconds <= 0:
        raise ValueError("OAuth callback timeout must be positive")

    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            try:
                code = parse_codex_callback(
                    f"http://localhost{self.path}",
                    expected_state=authorization.state,
                )
            except ValueError:
                self.send_response(400)
                message = b"OAuth login failed. Return to the terminal."
            else:
                captured["code"] = code
                self.send_response(200)
                message = b"OAuth login completed. You can close this tab."
            self.send_header("content-type", "text/plain; charset=utf-8")
            self.send_header("content-length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer((bind_host, bind_port), Handler)
    try:
        deadline = time.monotonic() + timeout_seconds
        if on_listening is not None:
            address = server.server_address
            on_listening((str(address[0]), int(address[1])))
        while "code" not in captured:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            server.timeout = remaining
            server.handle_request()
    finally:
        server.server_close()
    code = captured.get("code")
    if code is None:
        raise TimeoutError("OAuth callback was not received or was invalid")
    return code
