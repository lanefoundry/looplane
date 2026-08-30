# looplane VS Code Extension

This extension pushes VS Code editor context into looplane's repository-local IDE
bridge files:

- `.looplane/ide/diagnostics.json`
- `.looplane/ide/open-files.json`

looplane reads those files before model requests and injects changed diagnostics
or open-file state as bounded harness context. The extension does not grant
looplane additional editor permissions; it only writes JSON snapshots inside the
current workspace folder. Set `looplane.ideContext.webSocketUrl` to a looplane
conversation WebSocket attach URL to also push the same snapshots as typed
`ide_context` messages.

Development:

```bash
npm install
npm run compile
npm run package
```
