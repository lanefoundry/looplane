from __future__ import annotations

import json
from pathlib import Path


def test_vscode_extension_manifest_packages_rivumi_ide_bridge() -> None:
    root = Path(__file__).resolve().parents[1] / "editors" / "vscode"
    manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
    tsconfig = json.loads((root / "tsconfig.json").read_text(encoding="utf-8"))
    source = (root / "src" / "extension.ts").read_text(encoding="utf-8")
    vscodeignore = (root / ".vscodeignore").read_text(encoding="utf-8").splitlines()

    assert manifest["name"] == "rivumi-vscode"
    assert manifest["main"] == "./dist/extension.js"
    assert manifest["repository"] == {
        "type": "git",
        "url": "https://github.com/vincentxuu/rivumi.git",
        "directory": "editors/vscode",
    }
    assert "onStartupFinished" in manifest["activationEvents"]
    assert "onCommand:rivumi.pushIdeContext" in manifest["activationEvents"]
    assert manifest["scripts"]["compile"] == "tsc -p ./"
    assert manifest["scripts"]["package"] == "vsce package"
    assert {
        command["command"] for command in manifest["contributes"]["commands"]
    } == {"rivumi.pushIdeContext"}
    assert "rivumi.ideContext.enabled" in manifest["contributes"]["configuration"]["properties"]
    assert (
        "rivumi.ideContext.webSocketUrl"
        in manifest["contributes"]["configuration"]["properties"]
    )
    assert "DOM" in tsconfig["compilerOptions"]["lib"]
    assert "@types/vscode" in manifest["devDependencies"]
    assert "typescript" in manifest["devDependencies"]
    assert manifest["devDependencies"]["@vscode/vsce"] == "^3.9.2"
    assert lock["packages"][""]["devDependencies"]["@vscode/vsce"] == "^3.9.2"
    assert (root / "LICENSE").is_file()
    assert "node_modules" in vscodeignore
    assert ".gitignore" in vscodeignore
    assert "*.vsix" in vscodeignore

    assert 'path.join(".rivumi", "ide")' in source
    assert '"diagnostics.json"' in source
    assert '"open-files.json"' in source
    assert "vscode.languages.onDidChangeDiagnostics" in source
    assert "vscode.window.onDidChangeVisibleTextEditors" in source
    assert "vscode.window.onDidChangeActiveTextEditor" in source
    assert "pushIdeContextToWebSocket" in source
    assert 'type: "ide_context"' in source
    assert "open_files: openFiles" in source
    assert "ideContext.webSocketUrl" in source
    assert "diagnostic.severity + 1" in source
    assert "atomicWriteJson" in source
