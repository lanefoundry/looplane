"""Terminal onboarding feature owner."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

import looplane.runtime_registry as runtime_registry
from looplane.cli_config import CliConfig
from looplane.terminal.status_widgets import RuntimeLoadingIndicator
from looplane.terminal.types import LoadingPhase as LoadingPhase
from looplane.terminal.types import ProviderOption as ProviderOption
from looplane.terminal.types import RuntimeModelOption as RuntimeModelOption
from looplane.terminal.types import RuntimeOption as RuntimeOption
from looplane.terminal.types import TuiConfigurationSelection as TuiConfigurationSelection

if TYPE_CHECKING:
    from looplane.provider_verification import VerificationResult

_AUTOMATIC_MODEL = "__automatic__"


class OnboardingModal(ModalScreen[TuiConfigurationSelection | None]):
    """Runtime/provider/credential/model setup, as one modal with four steps.

    Normal use (``credential_only=False``) composes all four step containers up front
    and toggles which one is visible; this keeps the single ``push_screen_wait`` call
    site in ``looplaneApp`` unchanged (one await, one dismiss) instead of pushing several
    screens. ``credential_only=True`` composes just the credential step, replacing the
    old standalone ``ApiKeyModal`` for the "an active runtime switch is missing a
    credential" case (see ``looplaneApp._ensure_native_credentials``).
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    DEFAULT_CSS = """
    OnboardingModal { align: center middle; background: $background 70%; }
    OnboardingModal > Vertical {
        width: 72; max-width: 92%; height: auto; padding: 1 2;
        border: round $accent; background: $surface;
    }
    OnboardingModal .title { text-style: bold; color: $accent; }
    OnboardingModal .hint { color: $text-muted; margin-bottom: 1; }
    OnboardingModal Label.field { margin-top: 1; }
    OnboardingModal Horizontal { height: auto; margin-top: 1; align-horizontal: right; }
    OnboardingModal Button { margin-left: 1; }
    OnboardingModal #overview-list { height: auto; max-height: 12; margin-bottom: 1; }
    OnboardingModal .overview-entry { width: 100%; margin-bottom: 1; }
    OnboardingModal #credential-result { margin-top: 1; }
    OnboardingModal #credential-spinner { height: 1; margin-top: 1; }
    """

    def __init__(
        self,
        *,
        current: CliConfig,
        runtimes: Iterable[RuntimeOption],
        providers: Iterable[ProviderOption],
        ollama_models: tuple[str, ...],
        runtime_models: Mapping[str, tuple[RuntimeModelOption, ...]] | None = None,
        locked_provider: str | None = None,
        defer_model: bool = False,
        verified_providers: Mapping[str, VerificationResult] | None = None,
        initial_step: str = "overview",
        focus_provider: str | None = None,
        credential_only: bool = False,
    ) -> None:
        super().__init__()
        self.current = current
        self.runtimes = tuple(runtimes)
        self.providers = tuple(providers)
        self.ollama_models = ollama_models
        self.runtime_models = runtime_models or {}
        self.locked_provider = locked_provider
        self.defer_model = defer_model
        self.verified_providers: dict[str, VerificationResult] = dict(verified_providers or {})
        self.credential_only = credential_only
        self._step = "credential" if credential_only else initial_step
        self._active_provider: str | None = focus_provider
        self._active_runtime: str = "looplane-agent"
        self._fetched_models: tuple[str, ...] = ()

    def _initial_runtime(self) -> str:
        slugs = [slug for slug, _ in self.runtimes]
        if self.locked_provider:
            return "looplane-agent"
        if self.current.runtime in slugs:
            return self.current.runtime
        if self.current.provider or self.current.model:
            return "looplane-agent"
        return slugs[0] if slugs else "looplane-agent"

    def _initial_provider(self) -> str:
        slugs = [slug for slug, _ in self.providers]
        if self.locked_provider:
            return self.locked_provider
        if self.current.provider in slugs:
            return self.current.provider
        return "ollama" if self.ollama_models else "openai-compatible"

    @staticmethod
    def _provider_fields(provider: str) -> tuple[str, ...]:
        from looplane.native_credentials import NATIVE_CREDENTIAL_FIELDS

        return NATIVE_CREDENTIAL_FIELDS.get(provider, ())

    @staticmethod
    def _configured_native_providers() -> tuple[str, ...]:
        from looplane.native_credentials import NATIVE_CREDENTIAL_FIELDS, missing_native_fields

        return tuple(
            provider
            for provider in sorted(NATIVE_CREDENTIAL_FIELDS)
            if not missing_native_fields(provider)
        )

    def _provider_label(self, provider: str) -> str:
        return dict(self.providers).get(provider, provider)

    def _status_icon(self, provider: str) -> str:
        result = self.verified_providers.get(provider)
        return "✓" if result is not None and result.ok else "⚠"

    def compose(self) -> ComposeResult:
        if self.credential_only:
            yield from self._compose_credential_only()
            return
        runtime = self._initial_runtime()
        provider = self._initial_provider()
        provider_options = list(self.providers)
        if provider not in {slug for slug, _ in provider_options}:
            provider_options.append((provider, provider))
        model_options = tuple((name, name) for name in self.ollama_models)
        model_value: Any = (
            self.current.model
            if self.current.model in self.ollama_models
            else (self.ollama_models[0] if self.ollama_models else Select.NULL)
        )
        with Vertical():
            with Vertical(id="step-overview"):
                yield Label("Welcome to looplane", classes="title")
                yield Static(
                    "Providers already set up for the looplane-agent runtime.",
                    classes="hint",
                )
                with VerticalScroll(id="overview-list"):
                    for configured in self._configured_native_providers():
                        icon = self._status_icon(configured)
                        yield Button(
                            f"{icon} {self._provider_label(configured)}",
                            id=f"overview-{configured}",
                            classes="overview-entry",
                        )
                with Horizontal():
                    yield Button("Cancel", id="overview-cancel")
                    yield Button("+ New Provider", id="overview-add", variant="primary")

            with Vertical(id="step-connection"):
                yield Label("Welcome to looplane", classes="title")
                yield Static(
                    "Choose who runs the coding loop. Credentials stay with the selected runtime.",
                    classes="hint",
                )
                yield Label("Runtime", classes="field")
                yield Select(
                    tuple((label, slug) for slug, label in self.runtimes),
                    value=runtime,
                    allow_blank=False,
                    disabled=self.locked_provider is not None,
                    id="runtime",
                )
                yield Static("", id="runtime-hint", markup=False, classes="hint")
                yield Label("Connection", classes="field", id="provider-label")
                yield Select(
                    tuple((label, slug) for slug, label in provider_options),
                    value=provider,
                    allow_blank=False,
                    disabled=self.locked_provider is not None,
                    id="provider",
                )
                yield Select(
                    (("Automatic", _AUTOMATIC_MODEL),),
                    value=_AUTOMATIC_MODEL,
                    allow_blank=False,
                    id="runtime-model",
                )
                yield Static("Automatic · managed by the selected runtime", id="automatic-model")
                with Horizontal():
                    yield Button("Cancel", id="connection-cancel")
                    yield Button("Use once", id="connection-use-once")
                    yield Button("Save & Continue", id="connection-save", variant="primary")
                    yield Button("Next", id="connection-next", variant="primary")

            with Vertical(id="step-credential"):
                yield Label("Connect provider", id="credential-title", classes="title")
                yield Static(
                    "Stored locally for looplane-agent only (0600, never sent elsewhere).",
                    classes="hint",
                )
                yield Vertical(id="credential-fields")
                yield RuntimeLoadingIndicator(id="credential-spinner")
                yield Static("", id="credential-result", markup=False)
                with Horizontal():
                    yield Button("Cancel", id="credential-cancel")
                    yield Button("Set up later", id="credential-later")
                    yield Button("Skip verification & Save", id="credential-skip")
                    yield Button("Verify & Continue", id="credential-continue", variant="primary")

            with Vertical(id="step-model"):
                yield Label("Model", classes="field", id="model-label")
                yield Select(
                    model_options,
                    value=model_value,
                    allow_blank=not bool(model_options),
                    id="ollama-model",
                )
                yield Input(
                    value=self.current.model or "",
                    placeholder="Provider model ID",
                    id="model-id",
                )
                yield Select((), allow_blank=True, id="fetched-model")
                yield Static(
                    "Automatic · managed by the selected runtime", id="model-automatic-hint"
                )
                yield Static("", id="model-fetch-status", markup=False)
                yield Button("Retry", id="model-retry")
                yield Static("", id="setup-error", markup=False)
                with Horizontal():
                    yield Button("Cancel", id="cancel")
                    yield Button("Use once", id="use-once")
                    yield Button("Save & Continue", id="save", variant="primary")

    def _compose_credential_only(self) -> ComposeResult:
        provider = self._active_provider or ""
        with Vertical():
            yield Label(f"Connect {provider}", id="credential-title", classes="title")
            yield Static(
                "Stored locally for looplane-agent only (0600, never sent elsewhere).",
                classes="hint",
            )
            yield Vertical(id="credential-fields")
            yield RuntimeLoadingIndicator(id="credential-spinner")
            yield Static("", id="credential-result", markup=False)
            with Horizontal():
                yield Button("Cancel", id="cancel")
                yield Button("Skip verification & Save", id="skip-verify")
                yield Button("Save", id="save", variant="primary")

    def on_mount(self) -> None:
        if self.credential_only:
            if self._active_provider:
                self._mount_credential_fields_worker(self._active_provider)
            return
        runtime = self._initial_runtime()
        provider = self._initial_provider()
        self._active_provider = provider
        self._active_runtime = runtime
        self._sync_connection_controls(runtime, provider)
        self._sync_model_controls()
        # ``self._step`` is always "overview" here (credential_only handled above; no
        # caller passes a different initial_step), so this never needs to await the
        # credential-field-mounting branch of ``_goto_step``.
        self.query_one("#step-overview", Vertical).display = True
        self.query_one("#step-connection", Vertical).display = False
        self.query_one("#step-credential", Vertical).display = False
        self.query_one("#step-model", Vertical).display = False
        self.query_one("#overview-add", Button).focus()
        self.query_one("#model-retry", Button).display = False

    @work(exclusive=True, group="credential-mount")
    async def _mount_credential_fields_worker(self, provider: str) -> None:
        await self._mount_credential_fields(provider)

    def _sync_connection_controls(self, runtime: str, provider: str) -> None:
        looplane_runtime = runtime == "looplane-agent"
        self.query_one("#provider-label", Label).display = looplane_runtime
        self.query_one("#provider", Select).display = looplane_runtime
        runtime_model = self.query_one("#runtime-model", Select)
        external_options = self.runtime_models.get(runtime, ())
        select_options = tuple(
            (label, value or _AUTOMATIC_MODEL) for label, value in external_options
        ) or (("Automatic", _AUTOMATIC_MODEL),)
        runtime_model.set_options(select_options)
        selected_runtime_model = (
            self.current.runtime_model
            if self.current.runtime == runtime and self.current.runtime_model is not None
            else _AUTOMATIC_MODEL
        )
        option_values = {value or _AUTOMATIC_MODEL for _, value in external_options}
        runtime_model.value = (
            selected_runtime_model if selected_runtime_model in option_values else _AUTOMATIC_MODEL
        )
        runtime_model.display = not looplane_runtime and bool(external_options)
        self.query_one("#automatic-model", Static).display = (
            not looplane_runtime and not external_options
        )
        adapter = runtime_registry.RUNTIME_REGISTRY.get(runtime)
        if adapter is None:
            hint = ""
        elif looplane_runtime:
            hint = "looplane owns the model loop; API keys remain in environment variables."
        elif adapter.native_session is not None:
            hint = (
                f"Uses the installed {adapter.label.split(' · ')[0]} and its local login. "
                "Local-only and experimental."
            )
        else:
            hint = (
                f"looplane drives the installed {adapter.executable} directly; "
                "your provider keys stay in its config."
            )
        self.query_one("#runtime-hint", Static).update(hint)
        self.query_one("#connection-next", Button).display = looplane_runtime
        self.query_one("#connection-use-once", Button).display = not looplane_runtime
        self.query_one("#connection-save", Button).display = not looplane_runtime

    def _sync_model_controls(self) -> None:
        looplane_runtime = self._active_runtime == "looplane-agent"
        provider = self._active_provider
        use_list = (
            looplane_runtime
            and not self.defer_model
            and provider == "ollama"
            and bool(self.ollama_models)
        )
        use_fetched = (
            looplane_runtime
            and not self.defer_model
            and not use_list
            and bool(self._fetched_models)
        )
        use_input = looplane_runtime and not self.defer_model and not use_list and not use_fetched
        self.query_one("#ollama-model", Select).display = use_list
        self.query_one("#fetched-model", Select).display = use_fetched
        self.query_one("#model-id", Input).display = use_input
        self.query_one("#model-label", Label).display = looplane_runtime and not self.defer_model
        self.query_one("#model-automatic-hint", Static).display = (
            looplane_runtime and self.defer_model
        )
        if use_fetched:
            self._populate_fetched_model_select()

    def _populate_fetched_model_select(self) -> None:
        select = self.query_one("#fetched-model", Select)
        select.set_options(tuple((model, model) for model in self._fetched_models))
        if self._fetched_models:
            select.value = self._fetched_models[0]

    @on(Select.Changed, "#runtime")
    def runtime_changed(self, event: Select.Changed) -> None:
        if event.value is Select.NULL:
            return
        provider_value = self.query_one("#provider", Select).value
        provider = (
            self._initial_provider() if provider_value is Select.NULL else str(provider_value)
        )
        runtime = str(event.value)
        self._active_provider = provider
        self._active_runtime = runtime
        self._sync_connection_controls(runtime, provider)
        self._sync_model_controls()

    @on(Select.Changed, "#provider")
    def provider_changed(self, event: Select.Changed) -> None:
        if event.value is Select.NULL:
            return
        provider = str(event.value)
        if self._active_provider is not None and provider != self._active_provider:
            self.query_one("#model-id", Input).value = ""
            ollama = self.query_one("#ollama-model", Select)
            ollama.value = self.ollama_models[0] if self.ollama_models else Select.NULL
            self._fetched_models = ()
        self._active_provider = provider
        runtime_value = self.query_one("#runtime", Select).value
        runtime = "looplane-agent" if runtime_value is Select.NULL else str(runtime_value)
        self._active_runtime = runtime
        self._sync_connection_controls(runtime, provider)
        self._sync_model_controls()

    async def _goto_step(self, step: str) -> None:
        self._step = step
        self.query_one("#step-overview", Vertical).display = step == "overview"
        self.query_one("#step-connection", Vertical).display = step == "connection"
        self.query_one("#step-credential", Vertical).display = step == "credential"
        self.query_one("#step-model", Vertical).display = step == "model"
        if step == "overview":
            self.query_one("#overview-add", Button).focus()
        elif step == "credential" and self._active_provider:
            await self._mount_credential_fields(self._active_provider)
        elif step == "model":
            self._sync_model_controls()
            self._fetch_models_for_active_provider()

    async def _mount_credential_fields(self, provider: str) -> None:
        container = self.query_one("#credential-fields", Vertical)
        await container.remove_children()
        fields = self._provider_fields(provider)
        for field in fields:
            await container.mount(Label(field.replace("_", " ").title(), classes="field"))
            await container.mount(Input(password=True, id=f"field-{field}"))
        if not self.credential_only:
            self.query_one("#credential-title", Label).update(f"Connect {provider}")
        self.query_one("#credential-result", Static).update("")
        self._set_credential_verifying(False)
        continue_id = "save" if self.credential_only else "credential-continue"
        self.query_one(f"#{continue_id}", Button).disabled = False
        if fields:
            self.query_one(f"#field-{fields[0]}", Input).focus()

    def _set_credential_verifying(self, verifying: bool) -> None:
        spinner = self.query_one("#credential-spinner", RuntimeLoadingIndicator)
        spinner.set_phase(LoadingPhase.VERIFYING if verifying else None)
        continue_id = "save" if self.credential_only else "credential-continue"
        skip_id = "skip-verify" if self.credential_only else "credential-skip"
        self.query_one(f"#{continue_id}", Button).disabled = verifying
        self.query_one(f"#{skip_id}", Button).disabled = verifying

    def _report_model_fetch_issue(self, message: str) -> None:
        """Make a failed/absent listing visible instead of silently degrading
        to the manual model-ID input."""

        self.query_one("#model-fetch-status", Static).update(message)
        retry = self.query_one("#model-retry", Button)
        retry.display = True

    def _clear_model_fetch_issue(self) -> None:
        self.query_one("#model-fetch-status", Static).update("")
        self.query_one("#model-retry", Button).display = False

    def _model_fetch_view_available(self) -> bool:
        return (
            self.is_mounted
            and self.is_current
            and bool(self.query("#model-fetch-status"))
        )

    @on(Button.Pressed, "#model-retry")
    def retry_model_fetch(self, _event: Button.Pressed) -> None:
        self._clear_model_fetch_issue()
        self._fetch_models_for_active_provider()

    @work(exclusive=True, group="model-fetch")
    async def _fetch_models_for_active_provider(self) -> None:
        provider = self._active_provider
        if (
            provider is None
            or provider == "ollama"
            or self.defer_model
            or not self._model_fetch_view_available()
        ):
            return
        self._clear_model_fetch_issue()
        cached = self.verified_providers.get(provider)
        if cached is not None and cached.models:
            self._fetched_models = cached.models
            if self._step == "model":
                self._sync_model_controls()
            return
        # Disk-cached listing from an earlier session/wizard run: show instantly,
        # refresh in the background only once it ages past the TTL.
        import looplane.model_catalog as model_catalog

        snapshot = model_catalog.snapshot(provider)
        if snapshot is not None and snapshot.models:
            self._fetched_models = snapshot.models
            if self._step == "model":
                self._sync_model_controls()
            if not model_catalog.is_stale(snapshot):
                return

        from looplane.native_credentials import NATIVE_CREDENTIAL_FIELDS, resolve_native_field
        from looplane.provider_verification import fetch_models_result

        fields_spec = NATIVE_CREDENTIAL_FIELDS.get(provider)
        if fields_spec is None:
            self._report_model_fetch_issue(
                f"{provider} does not support model discovery; enter the model ID manually."
            )
            return
        values: dict[str, str] = {}
        missing: list[str] = []
        for field in fields_spec:
            value = resolve_native_field(provider, field)
            if value is None:
                missing.append(field)
            else:
                values[field] = value
        if missing:
            self._report_model_fetch_issue(
                f"No credential found (missing: {', '.join(missing)}); "
                "reconnect the provider or enter the model ID manually."
            )
            return

        result = await fetch_models_result(provider, values)
        if (
            not self._model_fetch_view_available()
            or self._active_provider != provider
            or self._step != "model"
        ):
            return  # user moved on while the request was in flight
        if result.ok and result.models:
            model_catalog.store_models(provider, result.models)
            self._fetched_models = result.models
            self._sync_model_controls()
            return
        if result.ok:
            # Connected, but the provider exposes no listing (degraded).
            self._report_model_fetch_issue(f"{result.message} Enter the model ID manually.")
        else:
            self._report_model_fetch_issue(
                f"{result.message} Retry, or enter the model ID manually."
            )

    def _submit_credential(self, *, skip_verification: bool) -> None:
        provider = self._active_provider
        if provider is None:
            return
        fields = self._provider_fields(provider)
        values: dict[str, str] = {}
        for field in fields:
            value = self.query_one(f"#field-{field}", Input).value.strip()
            if not value:
                self.query_one("#credential-result", Static).update("All fields are required.")
                return
            values[field] = value

        from looplane.native_credentials import save_native_credential

        try:
            save_native_credential(provider, values)
        except (OSError, PermissionError, ValueError) as exc:
            self.query_one("#credential-result", Static).update(f"Could not save: {exc}")
            return
        self._verify_and_advance(provider, values, skip_verification=skip_verification)

    @work(exclusive=True, group="verify")
    async def _verify_and_advance(
        self, provider: str, values: dict[str, str], *, skip_verification: bool
    ) -> None:
        if skip_verification:
            from looplane.provider_verification import VerificationResult

            self.verified_providers[provider] = VerificationResult(
                ok=False, message="Verification skipped by user."
            )
        else:
            self._set_credential_verifying(True)
            from looplane.provider_verification import verify_native_credential

            result = await verify_native_credential(provider, values)
            self._set_credential_verifying(False)
            self.verified_providers[provider] = result
            if not result.ok:
                self.query_one("#credential-result", Static).update(f"✗ {result.message}")
                continue_id = "save" if self.credential_only else "credential-continue"
                self.query_one(f"#{continue_id}", Button).disabled = True
                return
            self.query_one("#credential-result", Static).update(f"✓ {result.message}")

        if self.credential_only:
            self.dismiss(TuiConfigurationSelection(config=self.current, persist=False))
            return
        await self._goto_step("model")

    async def _advance_from_connection(self) -> None:
        provider_value = self.query_one("#provider", Select).value
        provider = (
            self._initial_provider() if provider_value is Select.NULL else str(provider_value)
        )
        self._active_provider = provider
        if provider == "ollama" or self.defer_model:
            await self._goto_step("model")
        else:
            await self._goto_step("credential")

    def _dismiss_external_runtime(self, *, persist: bool) -> None:
        runtime_value = self.query_one("#runtime", Select).value
        runtime = "looplane-agent" if runtime_value is Select.NULL else str(runtime_value)
        selected = self.query_one("#runtime-model", Select).value
        runtime_model = (
            None if selected in {Select.NULL, _AUTOMATIC_MODEL} else str(selected).strip()
        )
        configured = self.current.model_copy(
            update={"runtime": runtime, "runtime_model": runtime_model}
        )
        self.dismiss(TuiConfigurationSelection(config=configured, persist=persist))

    def _dismiss_native_runtime(self, *, persist: bool) -> None:
        provider = self._active_provider or self._initial_provider()
        if self.defer_model:
            model = (
                self.current.model
                if self.current.provider == provider
                else (
                    self.ollama_models[0] if provider == "ollama" and self.ollama_models else None
                )
            )
        elif provider == "ollama" and self.ollama_models:
            selected = self.query_one("#ollama-model", Select).value
            model = None if selected is Select.NULL else str(selected).strip()
        elif self._fetched_models:
            selected = self.query_one("#fetched-model", Select).value
            model = None if selected is Select.NULL else str(selected).strip()
        else:
            model = self.query_one("#model-id", Input).value.strip() or None
        if model is not None and (not model.isprintable() or "\x00" in model):
            self.query_one("#setup-error", Static).update("Enter a printable model ID.")
            return
        api_url = self.current.api_url if self.current.provider == provider else None
        self.dismiss(
            TuiConfigurationSelection(
                config=CliConfig(
                    runtime="looplane-agent",
                    runtime_model=None,
                    provider=provider,
                    model=model,
                    api_url=api_url,
                ),
                persist=persist,
            )
        )

    @on(Button.Pressed)
    async def handle_button(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if self.credential_only:
            if button_id == "cancel":
                self.dismiss(None)
            elif button_id in {"save", "skip-verify"}:
                self._submit_credential(skip_verification=button_id == "skip-verify")
            return
        if button_id in {"overview-cancel", "connection-cancel", "credential-cancel", "cancel"}:
            self.dismiss(None)
            return
        if button_id == "overview-add":
            await self._goto_step("connection")
            return
        if button_id.startswith("overview-"):
            self._active_provider = button_id.removeprefix("overview-")
            await self._goto_step("credential")
            return
        if button_id == "connection-next":
            await self._advance_from_connection()
            return
        if button_id in {"connection-use-once", "connection-save"}:
            self._dismiss_external_runtime(persist=button_id == "connection-save")
            return
        if button_id in {"credential-continue", "credential-skip"}:
            self._submit_credential(skip_verification=button_id == "credential-skip")
            return
        if button_id == "credential-later":
            await self._goto_step("model")
            return
        if button_id in {"use-once", "save"}:
            self._dismiss_native_runtime(persist=button_id == "save")
            return

    def action_cancel(self) -> None:
        self.dismiss(None)
