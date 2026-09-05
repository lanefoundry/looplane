"""Check release archive bounds, paths, and inclusion of production modules."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

FORBIDDEN = {
    ".git", ".venv", ".work", ".research", ".artifacts", ".pytest_cache",
    ".ruff_cache", "__pycache__", "node_modules", "build-baseline",
}
MAX_ARCHIVE_BYTES = 5 * 1024 * 1024
MAX_CONTENT_BYTES = 25 * 1024 * 1024
MAX_MEMBERS = 2000


def check_archives(directory: Path, source: Path) -> None:
    expected = {p.relative_to(source).as_posix() for p in (source / "looplane").rglob("*.py")}
    sdists = sorted(directory.glob("looplane-*.tar.gz"))
    wheels = sorted(directory.glob("looplane-*.whl"))
    if len(sdists) != 1 or len(wheels) != 1:
        raise ValueError("Expected exactly one looplane sdist and one wheel")
    for archive in [*sdists, *wheels]:
        if archive.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError(f"Archive exceeds 5 MiB: {archive.name}")
        if archive.suffix == ".whl":
            with zipfile.ZipFile(archive) as wheel:
                names = wheel.namelist()
                content_size = sum(info.file_size for info in wheel.infolist())
            included = set(names)
        else:
            root = archive.name.removesuffix(".tar.gz")
            with tarfile.open(archive) as sdist:
                members = sdist.getmembers()
                names = [member.name for member in members]
                content_size = sum(member.size for member in members)
                if any(member.issym() or member.islnk() for member in members):
                    raise ValueError("Source archive contains links")
            if any(PurePosixPath(name).parts[0] != root for name in names):
                raise ValueError("Source archive has an unexpected root")
            included = {name.removeprefix(f"{root}/src/") for name in names}
        if len(names) > MAX_MEMBERS or content_size > MAX_CONTENT_BYTES:
            raise ValueError(f"Archive content exceeds bounds: {archive.name}")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or FORBIDDEN.intersection(path.parts):
                raise ValueError(f"Unwanted archive path: {name}")
            if name.endswith((".pyc", ".whl", ".tar.gz")):
                raise ValueError(f"Generated artifact in archive: {name}")
        missing = expected - included
        if missing:
            raise ValueError(f"Missing production modules in {archive.name}: {sorted(missing)}")
        print(
            f"{archive.name}: {archive.stat().st_size} bytes; "
            f"{len(names)} members; all {len(expected)} production modules present"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    check_archives(args.directory, Path(__file__).resolve().parents[1] / "src")
