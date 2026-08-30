import * as fs from "node:fs/promises";
import * as path from "node:path";
import * as vscode from "vscode";

type looplanePosition = {
  line: number;
  character: number;
};

type looplaneRange = {
  start: looplanePosition;
  end: looplanePosition;
};

type looplaneDiagnostic = {
  uri: string;
  range: looplaneRange;
  severity: number;
  source: string;
  code?: string | number;
  message: string;
};

type looplaneOpenFile = {
  uri: string;
  active: boolean;
  cursor?: looplanePosition;
  selection?: looplaneRange;
};

const OUTPUT_DIR = path.join(".looplane", "ide");
const DIAGNOSTICS_FILE = "diagnostics.json";
const OPEN_FILES_FILE = "open-files.json";

let pushTimer: NodeJS.Timeout | undefined;

export function activate(context: vscode.ExtensionContext): void {
  const pushNow = () => {
    void pushIdeContext();
  };
  context.subscriptions.push(
    vscode.commands.registerCommand("looplane.pushIdeContext", pushNow),
    vscode.languages.onDidChangeDiagnostics(() => schedulePush()),
    vscode.window.onDidChangeVisibleTextEditors(() => schedulePush()),
    vscode.window.onDidChangeActiveTextEditor(() => schedulePush()),
    vscode.window.onDidChangeTextEditorSelection(() => schedulePush()),
  );
  schedulePush();
}

export function deactivate(): void {
  if (pushTimer) {
    clearTimeout(pushTimer);
    pushTimer = undefined;
  }
}

function schedulePush(): void {
  if (!enabled()) {
    return;
  }
  if (pushTimer) {
    clearTimeout(pushTimer);
  }
  pushTimer = setTimeout(() => {
    pushTimer = undefined;
    void pushIdeContext();
  }, 250);
}

async function pushIdeContext(): Promise<void> {
  if (!enabled()) {
    return;
  }
  const folder = firstWorkspaceFolder();
  if (!folder) {
    return;
  }
  const root = folder.uri.fsPath;
  const outDir = path.join(root, OUTPUT_DIR);
  const diagnostics = { diagnostics: diagnosticsSnapshot(folder) };
  const openFiles = { files: openFilesSnapshot(folder) };
  await fs.mkdir(outDir, { recursive: true });
  await Promise.all([
    atomicWriteJson(path.join(outDir, DIAGNOSTICS_FILE), diagnostics),
    atomicWriteJson(path.join(outDir, OPEN_FILES_FILE), openFiles),
    pushIdeContextToWebSocket(diagnostics, openFiles),
  ]);
}

function diagnosticsSnapshot(folder: vscode.WorkspaceFolder): looplaneDiagnostic[] {
  const items: looplaneDiagnostic[] = [];
  for (const [uri, diagnostics] of vscode.languages.getDiagnostics()) {
    if (!withinFolder(folder, uri)) {
      continue;
    }
    for (const diagnostic of diagnostics.slice(0, 200 - items.length)) {
      items.push({
        uri: uri.toString(),
        range: range(diagnostic.range),
        severity: diagnostic.severity + 1,
        source: diagnostic.source ?? "vscode",
        code: diagnosticCode(diagnostic.code),
        message: diagnostic.message,
      });
      if (items.length >= 200) {
        return items;
      }
    }
  }
  return items;
}

function openFilesSnapshot(folder: vscode.WorkspaceFolder): looplaneOpenFile[] {
  const active = vscode.window.activeTextEditor?.document.uri.toString();
  return vscode.window.visibleTextEditors
    .filter((editor) => withinFolder(folder, editor.document.uri))
    .slice(0, 32)
    .map((editor) => ({
      uri: editor.document.uri.toString(),
      active: editor.document.uri.toString() === active,
      cursor: position(editor.selection.active),
      selection: range(editor.selection),
    }));
}

function diagnosticCode(code: vscode.Diagnostic["code"]): string | number | undefined {
  if (typeof code === "number" || typeof code === "string") {
    return code;
  }
  if (code && typeof code === "object" && "value" in code) {
    return String(code.value);
  }
  return undefined;
}

function position(value: vscode.Position): looplanePosition {
  return { line: value.line, character: value.character };
}

function range(value: vscode.Range): looplaneRange {
  return { start: position(value.start), end: position(value.end) };
}

function enabled(): boolean {
  return vscode.workspace
    .getConfiguration("looplane")
    .get<boolean>("ideContext.enabled", true);
}

function webSocketUrl(): string {
  return vscode.workspace
    .getConfiguration("looplane")
    .get<string>("ideContext.webSocketUrl", "")
    .trim();
}

function firstWorkspaceFolder(): vscode.WorkspaceFolder | undefined {
  return vscode.workspace.workspaceFolders?.[0];
}

function withinFolder(folder: vscode.WorkspaceFolder, uri: vscode.Uri): boolean {
  return uri.scheme === "file" && uri.fsPath.startsWith(folder.uri.fsPath + path.sep);
}

async function atomicWriteJson(file: string, value: unknown): Promise<void> {
  const tmp = `${file}.${process.pid}.tmp`;
  await fs.writeFile(tmp, JSON.stringify(value, null, 2) + "\n", "utf8");
  await fs.rename(tmp, file);
}

async function pushIdeContextToWebSocket(
  diagnostics: { diagnostics: looplaneDiagnostic[] },
  openFiles: { files: looplaneOpenFile[] },
): Promise<void> {
  const url = webSocketUrl();
  if (!url) {
    return;
  }
  const WebSocketCtor = globalThis.WebSocket;
  if (!WebSocketCtor) {
    return;
  }
  const payload = JSON.stringify({
    type: "ide_context",
    diagnostics,
    open_files: openFiles,
  });
  await new Promise<void>((resolve) => {
    const socket = new WebSocketCtor(url);
    const done = () => resolve();
    const timer = setTimeout(() => {
      socket.close();
      resolve();
    }, 1000);
    socket.addEventListener("open", () => {
      socket.send(payload);
      socket.close();
    });
    socket.addEventListener("close", () => {
      clearTimeout(timer);
      done();
    });
    socket.addEventListener("error", () => {
      clearTimeout(timer);
      done();
    });
  });
}
