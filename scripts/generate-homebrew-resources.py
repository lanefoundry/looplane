#!/usr/bin/env python3
"""Generate Homebrew resource blocks from uv-locked dependencies.

Queries PyPI JSON API for each dependency's sdist URL and SHA256.
Outputs Ruby resource blocks suitable for a Homebrew formula.

Usage: uv run python scripts/generate-homebrew-resources.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request


def get_locked_deps() -> list[tuple[str, str]]:
    result = subprocess.run(
        ["uv", "export", "--no-dev", "--no-emit-project", "--no-hashes"],
        capture_output=True,
        text=True,
        check=True,
    )
    deps = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(" "):
            continue
        # Handle environment markers: "colorama==0.4.6 ; sys_platform == 'win32'"
        spec = line.split(";")[0].strip()
        if "==" not in spec:
            continue
        name, version = spec.split("==", 1)
        deps.append((name.strip(), version.strip()))
    return deps


def get_pypi_sdist(name: str, version: str) -> tuple[str, str] | None:
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        print(f"  # WARNING: could not fetch {url}: {e}", file=sys.stderr)
        return None

    for file_info in data.get("urls", []):
        if file_info.get("packagetype") == "sdist":
            return file_info["url"], file_info["digests"]["sha256"]

    # Some packages only have wheels
    print(f"  # WARNING: no sdist for {name}=={version}", file=sys.stderr)
    return None


def main():
    deps = get_locked_deps()
    print(f"  # {len(deps)} dependencies from uv.lock\n")

    for name, version in sorted(deps, key=lambda d: d[0].lower()):
        result = get_pypi_sdist(name, version)
        if result is None:
            print(f"  # SKIP: {name}=={version} (no sdist on PyPI)")
            print()
            continue
        url, sha256 = result
        print(f'  resource "{name}" do')
        print(f'    url "{url}"')
        print(f'    sha256 "{sha256}"')
        print("  end")
        print()


if __name__ == "__main__":
    main()
