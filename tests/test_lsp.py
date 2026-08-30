from __future__ import annotations

import sys
import textwrap

import pytest
from pydantic import ValidationError

from rivumi.ide import load_project_ide_diagnostics
from rivumi.lsp import LspServerCommand, ManagedLspServer


@pytest.mark.asyncio
async def test_managed_lsp_server_writes_publish_diagnostics_snapshot(tmp_path) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('hello')\n")
    fake_lsp = tmp_path / "fake_lsp.py"
    fake_lsp.write_text(
        textwrap.dedent(
            """
            import json
            import sys
            import time

            uri = sys.argv[1]
            payload = {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": uri,
                    "diagnostics": [
                        {
                            "range": {
                                "start": {"line": 0, "character": 0},
                                "end": {"line": 0, "character": 5},
                            },
                            "severity": 1,
                            "source": "fake-lsp",
                            "message": "synthetic diagnostic",
                        }
                    ],
                },
            }
            body = json.dumps(payload).encode()
            sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
            sys.stdout.buffer.flush()
            time.sleep(30)
            """
        )
    )
    server = ManagedLspServer(
        LspServerCommand(name="fake", command=(sys.executable, str(fake_lsp), source.as_uri())),
        project_root=tmp_path,
    )

    try:
        await server.start()
        snapshot = await server.wait_for_diagnostics(timeout_seconds=5.0)

        assert server.running
        assert snapshot.diagnostics[0].path == "src/app.py"
        assert snapshot.diagnostics[0].message == "synthetic diagnostic"
        loaded = load_project_ide_diagnostics(tmp_path)
        assert loaded is not None
        assert loaded.diagnostics[0].source == "fake-lsp"
    finally:
        await server.aclose()

    assert not server.running


def test_lsp_server_command_rejects_unsafe_argv() -> None:
    with pytest.raises(ValidationError):
        LspServerCommand(name="fake", command=("python", "bad\x00arg"))
