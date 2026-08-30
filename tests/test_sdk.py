from __future__ import annotations

import rivumi.sdk as sdk


def test_sdk_facade_exports_stable_replay_and_role_helpers() -> None:
    assert sdk.SDK_STABILITY.startswith("0.x")
    assert sdk.role_candidates(sdk.ModelRole.SUMMARIZER)
    assert callable(sdk.replay_run_events)
    assert callable(sdk.fork_run_at_event)
    assert sdk.ConversationWebSocketApp is not None
    assert sdk.ContextProviderCommand(name="ide", command=("python", "ctx.py")).name == "ide"
    assert sdk.ContextProviderConfig().providers == ()
    assert sdk.ContextProviderRunner().enabled is False
    assert callable(sdk.load_project_context_provider_config)
    assert callable(sdk.load_project_context_provider_runner)
    assert sdk.RuntimeInjectedContext(source="app", content="context").source == "app"
    assert sdk.RuntimeAttachment(name="notes.txt", content="body").media_type == "text/plain"
    assert sdk.RuntimeSkillsChangedEvent(
        sequence=0,
        turn_id="turn",
        skill_names=("review",),
    ).skill_names == ("review",)
    assert sdk.HookEventName.PRE_TOOL_USE.value == "pre_tool_use"
    assert sdk.HookEventName.PRE_COMPACT.value == "pre_compact"
    assert sdk.HookEventName.POST_COMPACT.value == "post_compact"
    assert sdk.InjectedContext(source="workspace", content="state").source == "workspace"
    assert sdk.InstructionDocument(source="AGENTS.md", content="rules").scope == "project"
    assert callable(sdk.resolve_instruction_documents)
    assert callable(sdk.render_instruction_diagnostics)
    assert callable(sdk.project_context_watch_snapshot)
    assert callable(sdk.watch_project_context_changes)
    assert callable(sdk.project_context_watch_capabilities)
    assert sdk.ProjectContextWatchBackend.PORTABLE_POLLING.value == "portable_polling"
    assert sdk.ProjectContextWatchBackendCapability is not None
    assert sdk.ProjectContextWatchChange is not None
    assert sdk.ProjectContextWatchSnapshot is not None
    assert sdk.CacheAwarePromptOrdering is not None
    assert sdk.CacheAwarePromptOrderingMode.TRACE_READY.value == "trace_ready"
    assert callable(sdk.cache_aware_prompt_ordering)
    assert sdk.provider_cache_trace("openai-responses", {"prompt_cache_key": "x"}).cache_ready
    assert sdk.IdeDiagnosticSeverity.ERROR.value == 1
    assert sdk.EditorDeepLinkStyle.VSCODE.value == "vscode"
    assert sdk.IdeDiagnosticsSnapshot(diagnostics=()).diagnostics == ()
    assert sdk.IdeOpenFilesSnapshot(files=()).files == ()
    assert sdk.render_prompt_sections((sdk.PromptSection("core", "rules"),))
    assert sdk.A10_SUBAGENT_PLANNER_POLICY_VERSION == "a10-subagent-planner-policy-v1"
    assert "proposed_transaction" in sdk.render_subagent_planner_policy()
    assert sdk.render_tool_prompt_context(
        (sdk.ToolDefinition(name="read_file", read_only=True),)
    )
    assert sdk.render_workspace_prompt_context(
        base_sha="a" * 40,
        allowed_paths=("src/**",),
        verification=(sdk.VerificationCommand(name="tests", argv=("pytest", "-q")),),
    )
    assert sdk.render_runtime_prompt_context({"mode": "native_loop"})
    assert callable(sdk.render_ide_diagnostics_context)
    assert callable(sdk.render_ide_open_files_context)
    assert callable(sdk.build_editor_deep_link)
    assert sdk.LspServerCommand(name="fake", command=("python",)).name == "fake"
    assert sdk.ManagedLspServer is not None
    assert issubclass(sdk.LspSupervisorError, RuntimeError)
    assert callable(sdk.load_project_skills)
    assert callable(sdk.select_project_skills)
    assert callable(sdk.load_project_plugins)
    assert sdk.PluginDiscoveryMetadata(keywords=("review",)).keywords == ("review",)
    assert sdk.ProjectPlugin is not None
    assert sdk.BackendTurnLimiter(max_active_turns=1).max_active_turns == 1
    assert sdk.SubagentRole.REVIEWER.value == "reviewer"
    assert sdk.normalize_subagent_schedule(
        [{"id": "scout", "role": "scout", "instruction": "Inspect."}]
    )[0].wave == 0
    assert sdk.ScheduledSubagent is not None
    assert sdk.SubagentScheduleTraceAnalysis is not None
    assert callable(sdk.analyze_subagent_schedule_events)
    assert callable(sdk.analyze_subagent_schedule_jsonl)
    assert callable(sdk.derive_subagent_task)
    assert callable(sdk.run_subagent_task)
