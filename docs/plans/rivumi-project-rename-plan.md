# Rivumi project rename plan

- [x] Inventory current project, distribution, CLI, UI, documentation, and test naming surfaces.
- [x] Decide the safe compatibility boundary for package module and persisted configuration.
- [x] Rename user-facing brand, package metadata, CLI entry point, documentation, and tests to Rivumi.
- [x] Run focused tests, full test suite, CLI help/startup smoke checks, and screenshot verification.
- [x] Review the final diff for accidental changes and document compatibility decisions.
- [x] Rename the workspace folder from `python-coding-agent` to `rivumi`.
- [x] Rename the Python import package from `coding_agent` to `rivumi`.
- [x] Rebuild editable installs and verify no stale module or absolute-path references remain.

Safety constraints:

- Preserve all pre-existing dirty-worktree edits.
- Rename the Python import package now; the user chose a complete pre-release rename over import
  compatibility with the temporary engineering name.
- Do not retain `pca` or `coding-agent` console-command aliases; the project is pre-release and the
  user explicitly requested one clean Rivumi command surface.
- Preserve existing local data through read-only fallback/migration paths, not through command aliases.
- Do not commit or push unless explicitly requested.
