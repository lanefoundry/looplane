# Rivumi VS Code Extension

This extension pushes VS Code editor context into Rivumi's repository-local IDE
bridge files:

- `.rivumi/ide/diagnostics.json`
- `.rivumi/ide/open-files.json`

Rivumi reads those files before model requests and injects changed diagnostics
or open-file state as bounded harness context. The extension does not grant
Rivumi additional editor permissions; it only writes JSON snapshots inside the
current workspace folder. Set `rivumi.ideContext.webSocketUrl` to a Rivumi
conversation WebSocket attach URL to also push the same snapshots as typed
`ide_context` messages.

Development:

```bash
npm install
npm run compile
npm run package
```
