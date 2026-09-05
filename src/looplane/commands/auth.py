"""Auth command services."""

from __future__ import annotations

import asyncio
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from looplane.commands.ports import CommandServices

if TYPE_CHECKING:
    from looplane.codex_oauth import CodexOAuthClient


def login_codex(
    timeout_seconds: float = 300.0, manual: bool = False, *, services: CommandServices
) -> None:
    """Create this application's own experimental ChatGPT/Codex OAuth grant."""

    from looplane.codex_oauth import CodexCredentialStore, CodexOAuthClient
    from looplane.models import ProviderError
    from looplane.oauth_login import parse_codex_callback, wait_for_codex_callback

    oauth = CodexOAuthClient()
    exchange_started = False
    try:
        authorization = oauth.begin_login(originator="looplane")
        typer.echo("This creates a separate grant; it does not read the Codex CLI credential.")
        if manual:
            typer.echo(authorization.url)
            callback_url = typer.prompt(
                "Paste the final localhost callback URL",
                hide_input=True,
            )
            code = parse_codex_callback(callback_url, expected_state=authorization.state)
        else:

            def open_browser(_: tuple[str, int]) -> None:
                typer.echo("Opening the ChatGPT/Codex authorization page in your browser.")
                if not webbrowser.open(authorization.url):
                    typer.echo("The browser did not open. Open this URL manually:")
                    typer.echo(authorization.url)

            code = wait_for_codex_callback(
                authorization,
                timeout_seconds=timeout_seconds,
                on_listening=open_browser,
            )
        exchange_started = True
        credentials = asyncio.run(_exchange_codex_code(oauth, code, authorization.verifier))
        CodexCredentialStore(services.credential_path()).save(credentials)
    except (OSError, ProviderError, TimeoutError, ValueError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    finally:
        if not exchange_started:
            asyncio.run(oauth.aclose())
    typer.echo("ChatGPT/Codex authorization saved for looplane.")


def status_codex(*, services: CommandServices) -> None:
    """Report redacted status for this application's Codex grant."""

    from looplane.codex_oauth import CodexCredentialStore

    try:
        credentials = CodexCredentialStore(services.credential_path()).load()
    except (OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: Codex authorization is unreadable: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if credentials is None:
        typer.echo("ChatGPT/Codex authorization: not configured")
        raise typer.Exit(code=1)
    expiry = "expired" if credentials.expires_at <= time.time() else "valid"
    typer.echo(f"ChatGPT/Codex authorization: configured ({expiry})")


def logout_codex(*, services: CommandServices) -> None:
    """Delete this application's Codex grant without touching another CLI."""

    from looplane.codex_oauth import CodexCredentialStore

    path = services.credential_path()
    store = CodexCredentialStore(path)
    try:
        credentials = store.load()
        if credentials is None:
            typer.echo("ChatGPT/Codex authorization was not configured.")
            return
        path.unlink()
    except (OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: Codex authorization could not be removed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo("ChatGPT/Codex authorization removed from looplane.")


def login_mcp(server: str, repository: Path | None = None, manual: bool = False) -> None:
    """Create this application's own MCP OAuth authorization-code grant."""

    from looplane.mcp_client import (
        McpError,
        McpOAuthClient,
        McpOAuthCredentialStore,
        load_native_mcp_server_configs,
        mcp_oauth_credential_path,
        parse_mcp_oauth_callback,
    )

    try:
        repository = repository or Path.cwd()
        configs = load_native_mcp_server_configs(repository, allowlist=(server,))
        if len(configs) != 1 or configs[0].oauth is None:
            raise typer.BadParameter("MCP server must exist and define oauth metadata")
        oauth = McpOAuthClient()
        try:
            authorization = oauth.begin_login(configs[0].oauth)
            typer.echo("This creates a looplane-owned MCP grant; no other client store is read.")
            if manual:
                typer.echo(authorization.url)
            elif not webbrowser.open(authorization.url):
                typer.echo("The browser did not open. Open this URL manually:")
                typer.echo(authorization.url)
            callback_url = typer.prompt("Paste the final OAuth callback URL", hide_input=True)
            code = parse_mcp_oauth_callback(callback_url, expected_state=authorization.state)
            credential = oauth.exchange_code(
                configs[0].oauth,
                code=code,
                verifier=authorization.verifier,
            )
        finally:
            oauth.close()
        path = mcp_oauth_credential_path(server)
        McpOAuthCredentialStore(path).save(credential)
    except (McpError, OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: MCP authorization failed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"MCP authorization saved for {server!r}.")


def status_mcp(server: str) -> None:
    """Report redacted status for one looplane-owned MCP OAuth grant."""

    from looplane.mcp_client import McpOAuthCredentialStore, mcp_oauth_credential_path

    try:
        credential = McpOAuthCredentialStore(mcp_oauth_credential_path(server)).load()
    except (OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: MCP authorization is unreadable: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if credential is None:
        typer.echo(f"MCP authorization for {server!r}: not configured")
        raise typer.Exit(code=1)
    expiry = (
        "unknown"
        if credential.expires_at is None
        else ("expired" if credential.expires_at <= time.time() else "valid")
    )
    typer.echo(f"MCP authorization for {server!r}: configured ({expiry})")


def logout_mcp(server: str) -> None:
    """Delete this application's MCP OAuth grant for one server."""

    from looplane.mcp_client import McpOAuthCredentialStore, mcp_oauth_credential_path

    try:
        cleared = McpOAuthCredentialStore(mcp_oauth_credential_path(server)).clear()
    except (OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: MCP authorization could not be removed: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if cleared:
        typer.echo(f"MCP authorization for {server!r} removed from looplane.")
    else:
        typer.echo(f"MCP authorization for {server!r} was not configured.")


def auth_set_key(provider: str, *, services: CommandServices) -> None:
    """Store an API key/secret for a looplane-agent provider, local to this application only."""

    from looplane.native_credentials import NATIVE_CREDENTIAL_FIELDS, save_native_credential

    fields = NATIVE_CREDENTIAL_FIELDS.get(provider)
    if fields is None:
        choices = ", ".join(sorted(NATIVE_CREDENTIAL_FIELDS))
        raise typer.BadParameter(f"provider must be one of: {choices}")
    if not services.stdin_is_tty():
        raise typer.BadParameter("looplane auth set-key requires a TTY")
    values = {
        field: typer.prompt(field.replace("_", " ").title(), hide_input=True) for field in fields
    }
    try:
        path = save_native_credential(provider, values)
    except (OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: could not save {provider} credentials: {exc}", err=True)
        raise typer.Exit(code=2) from exc

    from looplane.provider_verification import verify_native_credential

    result = asyncio.run(verify_native_credential(provider, values))
    if result.skipped:
        typer.echo(f"Saved {provider} credentials for looplane-agent at {path}")
    elif result.ok:
        typer.secho(f"✓ Saved and verified {provider} credentials at {path}", fg=typer.colors.GREEN)
    else:
        # Verification failed, but the credential is already saved -- don't lock the user
        # out of a key they may just not be able to verify from here (offline, provider
        # outage). They can re-run `looplane auth set-key` or `looplane auth list --verify`.
        typer.secho(
            f"⚠ Saved {provider} credentials at {path}, but verification failed: {result.message}",
            fg=typer.colors.YELLOW,
        )


def auth_clear_key(provider: str) -> None:
    """Delete a stored looplane-agent provider credential."""

    from looplane.native_credentials import NATIVE_CREDENTIAL_FIELDS, clear_native_credential

    if provider not in NATIVE_CREDENTIAL_FIELDS:
        choices = ", ".join(sorted(NATIVE_CREDENTIAL_FIELDS))
        raise typer.BadParameter(f"provider must be one of: {choices}")
    try:
        cleared = clear_native_credential(provider)
    except (OSError, PermissionError, ValueError) as exc:
        typer.echo(f"error: could not clear {provider} credentials: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    if cleared:
        typer.echo(f"Cleared stored {provider} credentials for looplane-agent.")
    else:
        typer.echo(f"No stored {provider} credentials were found.")


def auth_list(verify: bool = False) -> None:
    """Show which looplane-agent providers have stored credentials, and their status.

    Without --verify this only reads local state (env vars / the credential store) and
    never touches the network, so it stays fast. --verify calls each already-configured
    provider's API once, the same connection check `auth set-key` runs after saving.
    """

    from looplane.native_credentials import NATIVE_CREDENTIAL_FIELDS, missing_native_fields

    for provider in sorted(NATIVE_CREDENTIAL_FIELDS):
        if missing_native_fields(provider):
            typer.echo(f"· {provider:<18} not set · run `looplane auth set-key {provider}`")
            continue
        if not verify:
            typer.secho(
                f"⚠ {provider:<18} saved, not verified this run · re-run with --verify",
                fg=typer.colors.YELLOW,
            )
            continue

        from looplane.native_credentials import resolve_native_field
        from looplane.provider_verification import verify_native_credential

        fields: dict[str, str] = {}
        for field in NATIVE_CREDENTIAL_FIELDS[provider]:
            value = resolve_native_field(provider, field)
            assert value is not None  # guaranteed non-missing by the check above
            fields[field] = value
        result = asyncio.run(verify_native_credential(provider, fields))
        if result.ok:
            typer.secho(f"✓ {provider:<18} verified", fg=typer.colors.GREEN)
        else:
            typer.secho(f"✗ {provider:<18} invalid · {result.message}", fg=typer.colors.RED)


async def _exchange_codex_code(oauth: CodexOAuthClient, code: str, verifier: str):
    try:
        return await oauth.exchange_code(code=code, verifier=verifier)
    finally:
        await oauth.aclose()
