# TUI-safe logging patterns across mature coding-agent CLIs

Research for fixing `src/looplane/cli.py`: a stray `logging.basicConfig(level=logging.DEBUG, ...)`
sits directly in the body of `DefaultChatCommand` (a `TyperCommand` subclass), so it executes
unconditionally at import time. This sets the *root* logger to `DEBUG` with a default
`StreamHandler` to stderr, which means every third-party library that uses the stdlib `logging`
module (httpx, httpcore, markdown_it, urllib3, etc.) starts emitting DEBUG lines straight to
stderr — corrupting looplane's Textual alt-screen TUI (confirmed via screenshots: debug text
interleaved with / overwriting rendered widgets).

Five mature coding-agent CLIs were inspected for how they solve exactly this problem. All were
read directly from local clones under `~/Projects/coding-agent-reference/`.

---

## pi (badlogic/pi-mono)

- **Setup mechanism**: No structured app logger at all in the common path. Debug tracing is
  opt-in via the `PI_DEBUG_REDRAW=1` env var, or a manual `/debug` slash command at runtime.
  Log path resolution: `pi-mono/packages/coding-agent/src/config.ts#getDebugLogPath`.
- **Output destination**: File only — `~/.pi/agent/pi-debug.log`. The `tui` package
  (`pi-mono/packages/tui`) makes **zero** `console.*` calls anywhere in its source.
- **Alt-screen safety**: The one app-level `console.error` call for a fatal crash
  (`pi-mono/packages/coding-agent/src/interactive-mode.ts#uncaughtCrash`) calls `ui.stop()` first
  to leave the alt-screen / restore cooked terminal mode *before* printing anything — teardown
  always precedes any raw stderr write.
- **Third-party HTTP libs**: Not applicable in the common path since nothing writes to
  console/stderr by default; there is nothing to leak.
- **Default level**: No logging is active by default; `PI_DEBUG_REDRAW=1` or `/debug` must be
  used explicitly. Never DEBUG-by-default.

## omp (oh-my-pi, fork of pi)

- **Setup mechanism**: A real centralized logger, `oh-my-pi/packages/utils/src/logger.ts`,
  explicitly documented in-source as producing "no console output (writing to stdout/stderr
  would corrupt the TUI)". Console/stderr transport is opt-in only, wired via
  `packages/utils/src/logger.ts#setTransports()`, and used only by headless/background services
  that are known not to be sharing a terminal with a TUI.
- **Output destination**: A rotating log file, XDG-aware, at
  `~/.omp/logs/omp.<DATE>.<PID>.log`.
- **Alt-screen safety**: Instead of ever dumping to stderr, omp built a genuine **in-TUI debug
  log viewer overlay** — `oh-my-pi/packages/coding-agent/src/debug/log-viewer.ts#DebugLogViewer`
  — so a user can inspect live logs without anything touching the raw terminal stream.
- **Third-party HTTP/library logs**: Explicitly silenced. Local ML worker / subprocess code
  force-sets third-party library log levels to `error` before spawning
  (`packages/.../tts-worker.ts`, `packages/.../subprocess/worker-runtime.ts`).
- **Default level**: File-only by default; console transport requires an explicit
  `setTransports()` call from a context that is known to be headless. Never DEBUG-to-console by
  default.
- **pi → omp evolution**: pi has essentially no logging infrastructure (opt-in file trace only);
  omp generalized that into a first-class logger module, added an in-TUI viewer, and added
  explicit third-party log-level suppression for spawned worker processes — the two generations
  read like a design document on their own.

## opencode (sst/opencode)

- **Setup mechanism**: `opencode/packages/core/src/observability/logging.ts#minimumLogLevel`
  reads `OPENCODE_LOG_LEVEL` (`DEBUG`/`INFO`/`WARN`/`ERROR`). CLI flags `--print-logs` and
  `--log-level` are parsed in `opencode/packages/opencode/src/index.ts` and translated into
  `OPENCODE_PRINT_LOGS` / `OPENCODE_LOG_LEVEL` env vars via a yargs `.middleware()` before any
  command runs. Wired into the app's Effect runtime via
  `core/src/observability.ts#layer`.
- **Output destination**: `core/src/observability/logging.ts#fileLogger` always writes to
  `Global.Path.log/opencode.log` (`core/src/global.ts` — an XDG-style app-data directory, not
  project-local). `core/src/observability/logging.ts#stderrLogger` is a **second, additive**
  sink that is only registered when `OPENCODE_PRINT_LOGS === "1"`
  (`core/src/observability/logging.ts#loggers`).
- **Alt-screen safety**: The mechanism is simply "stderr is opt-in, never ambient" — there is no
  TUI-detection guard found anywhere (checked `opencode/packages/opencode/src/cli/cmd/tui.ts`).
  By default (no `--print-logs`), nothing ever touches stderr, so there's no corruption risk
  regardless of TUI state. Using `--print-logs` while the TUI is active is on the user.
  Structurally, opencode's TUI worker runs in a separate `Worker` thread
  (`cli/cmd/tui.ts#TuiThreadCommand`, `cli/cmd/tui.ts#target`) from the parent terminal-rendering
  process — a structural difference from a single-process Python CLI, not directly portable.
- **Third-party HTTP libs**: No explicit silencing of `undici`/`fetch`/`NODE_DEBUG` was found —
  and none is needed, because the app's own logger is file-sink-only by default and nothing in
  the dependency tree was found configured to log to stderr/stdout on its own.
- **Default level**: `INFO` (`core/src/observability/logging.ts#minimumLogLevel` fallback).
  Confirmed not DEBUG.

## codex (openai/codex, Rust)

Codex is the clearest illustration of "different binaries get different logging defaults
depending on whether they own the terminal."

- **Interactive TUI (`codex/codex-rs/tui`)** — setup in
  `tui/src/startup_orchestration.rs`: a file-writing `tracing_subscriber` layer
  (`tui_file_layer`) is constructed **only if** the user's config.toml explicitly sets
  `log_dir` (gate: `startup_orchestration.rs#config_toml_log_dir_configured`). If so, it opens
  `codex-tui.log` (`tui/src/lib.rs#TUI_LOG_FILE_NAME`) inside `Config.log_dir` — which itself
  defaults to `$CODEX_HOME/log` (doc comment + default computation in
  `core/src/config/mod.rs` — `Config.log_dir` field, `pub fn log_dir`) — via a
  `tracing_appender::non_blocking` writer, permissions `0600`
  (`OpenOptionsExt::mode(0o600)`), and filters through
  `EnvFilter::try_from_default_env().unwrap_or_else(|| EnvFilter::new("codex_core=info,codex_tui=info,codex_rmcp_client=info"))`.
  **If `log_dir` isn't explicitly configured, no tracing layer writes anywhere at all** — the
  TUI is silent by default, not merely quiet. There is no stderr layer in this path at all —
  `tracing_subscriber::registry().with(tui_file_layer)...try_init()` never includes a
  stdout/stderr `fmt::layer`.
- **Headless / IDE-integration server (`codex/codex-rs/app-server`)** — a completely different
  default: `app-server/src/lib.rs` installs a `StderrLogLayer` unconditionally
  (`with_writer(std::io::stderr)`), filtered by `EnvFilter::from_default_env()`, controllable
  via `RUST_LOG` and `LOG_FORMAT=json` (`app-server/src/lib.rs#log_format_from_env`). This is
  safe *because* app-server is a stdio-JSON-RPC background process with no alt-screen — stdout
  is reserved for protocol frames, stderr for logs, and no TUI ever shares that terminal.
- **Alt-screen safety on fatal errors**: Any unavoidable `eprintln!` for a fatal exit path in
  `tui/src/lib.rs` is always preceded by `restore_terminal_before_fatal_exit()`, which drops raw
  mode / restores the terminal before the message is printed — mirrors pi's `ui.stop()`-then-print
  pattern.
- **Third-party crate logs**: Silenced by construction, not by exception list — the fallback
  `EnvFilter` directive `codex_core=info,codex_tui=info,codex_rmcp_client=info` is an **allowlist**
  of codex's own crate targets. Anything not named (reqwest, hyper, tokio, h2, tonic, etc.)
  produces no output at all unless the user overrides `RUST_LOG` themselves.
- **Default level**: Interactive TUI mode: no logging output at all by default (opt-in via
  `log_dir` config). Headless app-server: `info`-ish via `EnvFilter::from_default_env()`, but
  restricted to codex's own crates unless the user widens `RUST_LOG`. Never DEBUG-by-default in
  either binary.

## claude-code (anthropics/claude-code, TypeScript/Ink, decompiled v2.1.88)

- **Setup mechanism**: `claude-code-source/src/utils/debug.ts#isDebugMode` — `true` if any of:
  `DEBUG`/`DEBUG_SDK` env vars (via `envUtils.ts#isEnvTruthy`), CLI flags `--debug`/`-d`,
  `--debug=<pattern>` (scoped filter parsed by `debugFilter.ts#parseDebugFilter`),
  `--debug-to-stderr`/`-d2e`, or `--debug-file=<path>` (its mere presence implies debug mode).
  Runtime toggle without restart via `debug.ts#enableDebugLogging` (backs the `/debug` slash
  command). Minimum level via `CLAUDE_CODE_DEBUG_LOG_LEVEL` (`debug.ts#getMinDebugLogLevel`,
  `debug.ts#LEVEL_ORDER`), gate function `debug.ts#shouldLogDebugMessage` (also always-on for
  `USER_TYPE === 'ant'` internal builds, for `/share`/bug-report capture).
- **Output destination**: `debug.ts#getDebugLogPath` → `~/.claude/debug/<sessionId>.txt`
  (honors `CLAUDE_CONFIG_DIR` via `envUtils.ts#getClaudeConfigHomeDir`, overridable by
  `CLAUDE_CODE_DEBUG_LOGS_DIR` or `--debug-file=<path>`), plus a `latest` symlink kept current by
  `debug.ts#updateLatestDebugLogSymlink`. Writes go through
  `bufferedWriter.ts#createBufferedWriter`: synchronous `appendFileSync` when debug mode is on
  (so logs survive an abrupt `process.exit()` — comment cites issue #22257), buffered/async
  flush (~1/sec, `debug.ts#appendAsync`) when debug mode is off (silent internal capture).
  `--debug-to-stderr` is the one explicit path that bypasses the file and calls
  `utils/process.ts#writeToStderr` directly.
- **Alt-screen safety — the standout mechanism**: `src/ink/ink.tsx`, class `Ink`, methods
  `Ink#patchConsole` and `Ink#patchStderr`, gated by `RenderOptions.patchConsole` (default
  `true`, set in `src/ink/root.ts`). `patchConsole` replaces every `console.log/info/debug/...`
  method (`ink.tsx#CONSOLE_STDOUT_METHODS`) to redirect into `debug.ts#logForDebugging`, and every
  `console.warn/error/trace`+`console.assert` (`ink.tsx#CONSOLE_STDERR_METHODS`) into
  `logError` — so no `console.*` call ever reaches the real terminal while Ink is mounted, with a
  restore closure for cleanup. `patchStderr` additionally intercepts `process.stderr.write`
  itself, with a source comment explaining exactly looplane's failure mode: "stray writes
  (config.ts, hooks.ts, third-party deps) don't corrupt the alt-screen buffer... direct stderr
  writes bypass [patchConsole], land at the parked cursor, scroll the alt-screen, and desync
  frontFrame from the physical terminal... interleaved garbage." It swallows the write, logs it
  at `'warn'`, and if the alt-screen is active, marks the frame contaminated and forces a full
  repaint as recovery. A re-entrancy guard lets `--debug-to-stderr` still reach the real stream.
- **Third-party HTTP libs**: No named suppression of `undici`/Anthropic-SDK logging was found —
  none is needed because the interception happens one level up, at `console.*` and
  `process.stderr.write` themselves (point above), so *any* library's output is automatically
  captured regardless of origin. The `patchStderr` comment explicitly names "third-party deps"
  as a source it guards against.
- **Default level**: Off. `debug.ts#isDebugMode` is `false` unless explicitly enabled; when on,
  default minimum level is `'debug'` (`'verbose'` filtered out unless
  `CLAUDE_CODE_DEBUG_LOG_LEVEL=verbose`).

---

## Synthesis: the common pattern, and a concrete fix for looplane

**The common pattern across all five projects, without exception:**

1. **Nothing writes to stdout/stderr by default.** Every project's default state is either no
   logging at all (pi, codex-TUI) or file-only logging (omp, opencode, claude-code). DEBUG-to-console-by-default, which is exactly looplane's bug, appears in *none* of them.
2. **stderr/console output is always an explicit opt-in**, gated by a CLI flag and/or env var
   (`--debug`/`DEBUG`, `--print-logs`/`OPENCODE_PRINT_LOGS`, `--debug-to-stderr`, `RUST_LOG` in
   the *headless* codex binary only), never the ambient default.
3. **Log files live in a per-user state/cache directory**, not the project directory:
   `~/.pi/agent/`, `~/.omp/logs/`, XDG app-data (`Global.Path.log`), `$CODEX_HOME/log`,
   `~/.claude/debug/`. Several honor an env var override for the directory itself.
4. **Third-party library noise is handled one of two structurally different ways**:
   - *Allowlist filtering* (codex): the tracing `EnvFilter` only shows codex's own crate targets
     by default (`codex_core=info,codex_tui=info,...`); everything else is silent unless the user
     explicitly widens `RUST_LOG`. Python's `logging` module supports the equivalent: root at a
     high level, only your own logger names lowered.
   - *Stream interception* (claude-code, and structurally omp's "never console" logger): intercept
     the actual output sink (`console.*`, `process.stderr.write`) rather than trusting every
     library's own logger to behave — this is a defense-in-depth net that catches stray/rogue
     writes from libraries that don't even go through your logging framework.
   No project relies on a global `basicConfig`-style call that captures every third-party logger
   indiscriminately — that pattern doesn't appear anywhere, which is precisely the anti-pattern
   looplane fell into.
5. **When a fatal error must be printed to the real stderr**, the terminal/alt-screen is torn
   down *first* (pi's `ui.stop()`, codex's `restore_terminal_before_fatal_exit()`) — print-then-crash
   never happens while the alt-screen is still active.

**Concrete recommendation for `looplane/src/looplane/cli.py`:**

1. **Delete the stray `logging.basicConfig(...)` call from the `DefaultChatCommand` class body
   entirely.** It has no reason to exist there — it's dead code that happens to execute as a
   side effect of class definition, and it's a global mutation of the root logger with no way to
   turn it off.
2. **Add an explicit, opt-in debug mechanism**, following the near-universal shape above:
   - A `--debug` / `-v` Typer option (or `LOOPLANE_LOG_LEVEL` / `LOOPLANE_DEBUG` env var) that,
     when set, configures logging *once*, centrally, in `looplane/cli.py`'s app callback or in a
     small `looplane/logging_setup.py` module — not scattered `basicConfig` calls.
   - Default level should be `WARNING` (or `ERROR`) on the root logger, with looplane's own
     namespaced loggers (`looplane.*`, already used e.g. in
     `runtimes/codex/correlation.py`/`event_mapper.py`/`session.py` as
     `logging.getLogger("looplane.codex_app_server")`) settable to `INFO`/`DEBUG` only when the
     flag is passed.
3. **Route all log output to a file, never to stdout/stderr, whenever the Textual TUI is active.**
   looplane already has the XDG convention in place elsewhere (`XDG_STATE_HOME` used in
   `native_credentials.py`, `conversation.py`, `mcp_client.py`) — reuse that: a `logging.FileHandler`
   writing to `$XDG_STATE_HOME/looplane/logs/looplane.log` (fallback `~/.local/state/looplane/logs/`),
   attached to the root logger only when `--debug` is passed. Only add a `StreamHandler` to stderr
   in headless/non-TUI code paths (e.g. `exec`/`-p` one-shot mode, `looplane backend`, or when
   `--no-alt-screen`/non-interactive is in effect) — mirroring codex's app-server-vs-TUI split and
   opencode's file-always/stderr-opt-in split.
4. **Explicitly cap third-party loggers regardless of the above**, as defense in depth (the
   allowlist approach, applied to Python): after configuring the root logger, explicitly set
   `logging.getLogger("httpx").setLevel(logging.WARNING)`,
   `logging.getLogger("httpcore").setLevel(logging.WARNING)`, and the same for
   `urllib3`/`markdown_it`/any other verbose dependency — don't rely solely on the root logger's
   level, since a library that calls `getLogger(__name__).setLevel(DEBUG)` itself (or one whose
   effective level resolves through `NOTSET`→root) can still leak through if the root ever gets
   set to DEBUG for any reason in the future.
5. **Optional hardening, following claude-code's `patchStderr` pattern**: since Textual owns the
   alt-screen, consider wrapping/monkeypatching `sys.stderr` (or configuring Textual's own
   `App` to capture stray writes) while the TUI is mounted, so that any *future* rogue
   `print()`/`basicConfig()`/library write is caught and redirected to the debug log file instead
   of corrupting the screen — a safety net, not a substitute for fixing the root cause in step 1.
