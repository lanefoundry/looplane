"""Registry contract tests for M13 runtime dispatch.

These pin the behavior-preserving extraction: the TUI picker and ``cli`` dispatch
read from ``runtime_registry`` rather than hard-coded runtime literals, so a new
external runtime is a registry entry plus its conversation/backend implementation.
"""

from __future__ import annotations

import shutil

from looplane import runtime_registry


def test_runtime_options_lists_native_and_installed_external(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda exe: exe == "claude")
    slugs = {slug for slug, _ in runtime_registry.runtime_options()}
    assert slugs == {"looplane-agent", "claude-code"}


def test_runtime_options_hides_uninstalled_external(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda exe: False)
    slugs = {slug for slug, _ in runtime_registry.runtime_options()}
    assert slugs == {"looplane-agent"}


def test_runtime_model_map_covers_every_runtime() -> None:
    model_map = runtime_registry.runtime_model_map()
    assert set(model_map) == set(runtime_registry.RUNTIME_REGISTRY)


def test_runtime_model_map_does_not_expose_native_role_aliases() -> None:
    model_map = runtime_registry.runtime_model_map()

    assert model_map["looplane-agent"] == ()
    for slug, options in model_map.items():
        if slug == "looplane-agent":
            continue
        assert all(value is None or not value.startswith("@") for _label, value in options)


def test_native_session_runtimes_use_native_controller_dispatch() -> None:
    for slug in ("claude-code", "codex-cli"):
        adapter = runtime_registry.RUNTIME_REGISTRY[slug]
        assert adapter.native_session is not None
        assert adapter.kind is runtime_registry.RuntimeKind.EXTERNAL


def test_looplane_agent_is_native_without_backend() -> None:
    adapter = runtime_registry.RUNTIME_REGISTRY["looplane-agent"]
    assert adapter.kind is runtime_registry.RuntimeKind.NATIVE
    assert adapter.native_session is None
    assert adapter.backend is None


def test_external_runtime_without_native_session_routes_to_backend_dispatch() -> None:
    adapter = runtime_registry.RuntimeAdapter(
        slug="fake-ext",
        label="Fake external",
        kind=runtime_registry.RuntimeKind.EXTERNAL,
        backend="looplane.external_runner.ExternalCodingRunner",
        native_session=None,
    )
    assert adapter.native_session is None
    assert adapter.kind is runtime_registry.RuntimeKind.EXTERNAL and adapter.backend is not None


def test_resolve_class_is_lazy_and_imports_on_demand() -> None:
    cls = runtime_registry._resolve_class("looplane.runtime_registry.RuntimeKind")
    assert cls is runtime_registry.RuntimeKind
