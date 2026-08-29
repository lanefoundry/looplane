"""Apply a Linux Landlock filesystem policy before executing one command."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_ACCESS_EXECUTE = 1 << 0
_ACCESS_WRITE_FILE = 1 << 1
_ACCESS_READ_FILE = 1 << 2
_ACCESS_READ_DIR = 1 << 3
_ACCESS_REMOVE_DIR = 1 << 4
_ACCESS_REMOVE_FILE = 1 << 5
_ACCESS_MAKE_CHAR = 1 << 6
_ACCESS_MAKE_DIR = 1 << 7
_ACCESS_MAKE_REG = 1 << 8
_ACCESS_MAKE_SOCK = 1 << 9
_ACCESS_MAKE_FIFO = 1 << 10
_ACCESS_MAKE_BLOCK = 1 << 11
_ACCESS_MAKE_SYM = 1 << 12
_ACCESS_REFER = 1 << 13
_ACCESS_TRUNCATE = 1 << 14

_READ_ACCESS = _ACCESS_EXECUTE | _ACCESS_READ_FILE | _ACCESS_READ_DIR
_WRITE_ACCESS = (
    _READ_ACCESS
    | _ACCESS_WRITE_FILE
    | _ACCESS_REMOVE_DIR
    | _ACCESS_REMOVE_FILE
    | _ACCESS_MAKE_CHAR
    | _ACCESS_MAKE_DIR
    | _ACCESS_MAKE_REG
    | _ACCESS_MAKE_SOCK
    | _ACCESS_MAKE_FIFO
    | _ACCESS_MAKE_BLOCK
    | _ACCESS_MAKE_SYM
    | _ACCESS_REFER
    | _ACCESS_TRUNCATE
)
_HANDLED_ACCESS = _WRITE_ACCESS


class LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class LandlockPathBeneathAttr(ctypes.Structure):
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def _die(message: str) -> int:
    print(message, file=sys.stderr)
    return 126


def _syscall(number: int, *args: object) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(number, *args)
    if result < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    return int(result)


def _prctl_no_new_privs() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    if prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))


def _landlock_abi() -> int:
    return _syscall(_SYS_LANDLOCK_CREATE_RULESET, 0, 0, _LANDLOCK_CREATE_RULESET_VERSION)


def _create_ruleset() -> int:
    attr = LandlockRulesetAttr(_HANDLED_ACCESS)
    return _syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(attr),
        ctypes.sizeof(attr),
        0,
    )


def _add_path_rule(ruleset_fd: int, root: Path, access: int) -> None:
    resolved = root.resolve(strict=False)
    if "\x00" in str(resolved):
        raise ValueError("Landlock path contains NUL")
    flags = os.O_PATH | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(resolved, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"Landlock path is not a directory: {resolved}")
        attr = LandlockPathBeneathAttr(access, fd)
        _syscall(
            _SYS_LANDLOCK_ADD_RULE,
            ruleset_fd,
            _LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(attr),
            0,
        )
    finally:
        os.close(fd)


def _paths(values: object) -> tuple[Path, ...]:
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise ValueError("Landlock policy paths must be a list of strings")
    return tuple(dict.fromkeys(Path(item).resolve(strict=False) for item in values))


def _apply_policy(policy: dict[str, Any]) -> None:
    cwd = Path(policy["cwd"]).resolve(strict=False)
    read_roots = _paths(policy.get("read_roots"))
    writable_roots = _paths(policy.get("writable_roots"))
    if not cwd.is_dir():
        raise ValueError("Landlock cwd is not a directory")

    _landlock_abi()
    ruleset_fd = _create_ruleset()
    try:
        for root in (
            Path("/usr"),
            Path("/bin"),
            Path("/lib"),
            Path("/lib64"),
            Path("/etc/ssl"),
            Path("/etc/ca-certificates"),
        ):
            if root.exists():
                _add_path_rule(ruleset_fd, root, _READ_ACCESS)
        write_roots = tuple(dict.fromkeys((cwd, *writable_roots)))
        for root in write_roots:
            _add_path_rule(ruleset_fd, root, _WRITE_ACCESS)
        for root in read_roots:
            if any(
                root == write_root or root.is_relative_to(write_root)
                for write_root in write_roots
            ):
                continue
            _add_path_rule(ruleset_fd, root, _READ_ACCESS)
        _prctl_no_new_privs()
        _syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
    finally:
        os.close(ruleset_fd)
    os.chdir(cwd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy-json", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        return _die("Linux Landlock sandbox command is missing")
    if not sys.platform.startswith("linux"):
        return _die("Linux Landlock sandbox is unavailable on this platform")
    try:
        policy = json.loads(args.policy_json)
        if not isinstance(policy, dict):
            raise ValueError("Landlock policy must be an object")
        _apply_policy(policy)
    except (OSError, ValueError, KeyError) as exc:
        return _die(f"Linux Landlock sandbox is unavailable: {exc}")
    os.execvp(command[0], command)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
