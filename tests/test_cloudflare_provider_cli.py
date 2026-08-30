from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from looplane import cli
from looplane.cloudflare_provider_setup import ProviderSetupError, ProviderSetupResult


def test_cloudflare_provider_apply_wires_one_batch_command(tmp_path: Path, monkeypatch) -> None:
    manifest = tmp_path / "providers.json"
    secrets = tmp_path / ".env.cloudflare"
    cloudflare_dir = tmp_path / "cloudflare"
    captured: dict[str, object] = {}

    def fake_setup(path: Path, **kwargs: object) -> ProviderSetupResult:
        captured["path"] = path
        captured.update(kwargs)
        return ProviderSetupResult(catalog_json="{}", profile_count=2, dry_run=False)

    monkeypatch.setattr(
        "looplane.cloudflare_provider_setup.setup_cloudflare_providers",
        fake_setup,
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "cloudflare",
            "providers",
            "apply",
            str(manifest),
            "--secrets-env",
            str(secrets),
            "--cloudflare-dir",
            str(cloudflare_dir),
            "--env",
            "production",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Applied 2" in result.output
    assert captured == {
        "path": manifest,
        "cloudflare_dir": cloudflare_dir,
        "secrets_env_file": secrets,
        "allow_custom_endpoint": False,
        "wrangler_env": "production",
        "dry_run": False,
    }


def test_cloudflare_provider_apply_dry_run_and_custom_endpoint_opt_in(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_setup(path: Path, **kwargs: object) -> ProviderSetupResult:
        assert kwargs["dry_run"] is True
        assert kwargs["allow_custom_endpoint"] is True
        return ProviderSetupResult(catalog_json="{}", profile_count=1, dry_run=True)

    monkeypatch.setattr(
        "looplane.cloudflare_provider_setup.setup_cloudflare_providers",
        fake_setup,
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "cloudflare",
            "providers",
            "apply",
            str(tmp_path / "providers.json"),
            "--dry-run",
            "--allow-custom-endpoint",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Validated 1" in result.output


def test_cloudflare_provider_apply_reports_safe_error(monkeypatch) -> None:
    def fail(*args: object, **kwargs: object) -> ProviderSetupResult:
        raise ProviderSetupError("missing provider API key environment variables: GROQ_API_KEY")

    monkeypatch.setattr(
        "looplane.cloudflare_provider_setup.setup_cloudflare_providers",
        fail,
    )

    result = CliRunner().invoke(
        cli.app,
        ["cloudflare", "providers", "apply", "providers.json"],
    )

    assert result.exit_code == 2
    assert "GROQ_API_KEY" in result.output
    assert "Traceback" not in result.output


def test_cloudflare_provider_apply_help_exposes_batch_inputs() -> None:
    result = CliRunner().invoke(
        cli.app,
        ["cloudflare", "providers", "apply", "--help"],
    )

    assert result.exit_code == 0, result.output
    assert "--secrets-env" in result.output
    assert "--dry-run" in result.output
    assert "--allow-custom-endpoint" in result.output
