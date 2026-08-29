from __future__ import annotations

from rivumi.secret_scan import scan_patch_for_secrets


def test_scan_patch_for_secrets_reports_added_secret_without_value() -> None:
    patch = """\
diff --git a/config.py b/config.py
--- a/config.py
+++ b/config.py
@@ -1,2 +1,3 @@
 DEBUG = False
+API_KEY = "sk-test_abcdefghijklmnopqrstuvwxyz123456"
 NAME = "demo"
"""

    findings = scan_patch_for_secrets(patch)

    assert len(findings) == 1
    assert findings[0].path == "config.py"
    assert findings[0].line == 2
    assert findings[0].pattern == "openai-api-key"
    assert "sk-test" not in findings[0].label()


def test_scan_patch_for_secrets_ignores_removed_and_short_placeholder_values() -> None:
    patch = """\
diff --git a/config.py b/config.py
--- a/config.py
+++ b/config.py
@@ -1,3 +1,3 @@
-AUTH_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz123456"
+AUTH_TOKEN = "replace-me"
+TOKEN_NAME = "public configuration label"
"""

    assert scan_patch_for_secrets(patch) == ()
