"""Loopback callback helper for this application's Codex OAuth grant."""

from __future__ import annotations

import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from coding_agent.codex_oauth import CodexAuthorization


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
) -> str:
    """Listen for exactly one browser callback on the fixed loopback redirect."""

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

    server = HTTPServer(("127.0.0.1", 1455), Handler)
    try:
        server.timeout = timeout_seconds
        server.handle_request()
    finally:
        server.server_close()
    code = captured.get("code")
    if code is None:
        raise TimeoutError("OAuth callback was not received or was invalid")
    return code
