"""Apply a Linux Landlock filesystem policy before executing one command."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import platform
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
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_BPF_LD_W_ABS = 0x20
_BPF_JMP_JEQ_K = 0x15
_BPF_JMP_JA = 0x05
_BPF_RET_K = 0x06
_SECCOMP_NR_OFFSET = 0
_SECCOMP_ARCH_OFFSET = 4
_AUDIT_ARCH_X86_64 = 0xC000003E
_AUDIT_ARCH_AARCH64 = 0xC00000B7

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


class SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class SockFprog(ctypes.Structure):
    _fields_ = [
        ("len", ctypes.c_ushort),
        ("filter", ctypes.POINTER(SockFilter)),
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


def _prctl_seccomp(program: SockFprog) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    prctl = libc.prctl
    prctl.argtypes = [
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
    ]
    prctl.restype = ctypes.c_int
    if prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(program), 0, 0) != 0:
        current_errno = ctypes.get_errno()
        raise OSError(current_errno, os.strerror(current_errno))


def _seccomp_profile_for_machine(machine: str | None = None) -> tuple[int, tuple[int, ...]]:
    value = (machine or platform.machine()).lower()
    if value in {"x86_64", "amd64"}:
        return _AUDIT_ARCH_X86_64, (
            101,  # ptrace
            165,  # mount
            166,  # umount2
            167,  # swapon
            168,  # swapoff
            169,  # reboot
            175,  # init_module
            176,  # delete_module
            246,  # kexec_load
            248,  # add_key
            249,  # request_key
            250,  # keyctl
            272,  # unshare
            298,  # perf_event_open
            300,  # fanotify_init
            304,  # open_by_handle_at
            308,  # setns
            313,  # finit_module
            321,  # bpf
            323,  # userfaultfd
            435,  # clone3
        )
    if value in {"aarch64", "arm64"}:
        return _AUDIT_ARCH_AARCH64, (
            39,  # umount2
            40,  # mount
            97,  # unshare
            104,  # kexec_load
            105,  # init_module
            106,  # delete_module
            117,  # ptrace
            142,  # reboot
            217,  # add_key
            218,  # request_key
            219,  # keyctl
            224,  # swapon
            225,  # swapoff
            241,  # perf_event_open
            262,  # fanotify_init
            265,  # open_by_handle_at
            268,  # setns
            273,  # finit_module
            280,  # bpf
            282,  # userfaultfd
            435,  # clone3
        )
    raise ValueError(f"unsupported seccomp architecture: {value}")


def _bpf_stmt(code: int, k: int) -> SockFilter:
    return SockFilter(code, 0, 0, k)


def _bpf_jump(code: int, k: int, jt: int, jf: int) -> SockFilter:
    return SockFilter(code, jt, jf, k)


def _seccomp_filter(arch: int, denied_syscalls: tuple[int, ...]) -> tuple[SockFilter, ...]:
    instructions: list[SockFilter] = [
        _bpf_stmt(_BPF_LD_W_ABS, _SECCOMP_ARCH_OFFSET),
        _bpf_jump(_BPF_JMP_JEQ_K, arch, 1, 0),
        _bpf_stmt(_BPF_RET_K, _SECCOMP_RET_ERRNO | errno.EPERM),
        _bpf_stmt(_BPF_LD_W_ABS, _SECCOMP_NR_OFFSET),
    ]
    for syscall_number in denied_syscalls:
        instructions.extend(
            (
                _bpf_jump(_BPF_JMP_JEQ_K, syscall_number, 0, 1),
                _bpf_stmt(_BPF_RET_K, _SECCOMP_RET_ERRNO | errno.EPERM),
            )
        )
    instructions.append(_bpf_stmt(_BPF_RET_K, _SECCOMP_RET_ALLOW))
    return tuple(instructions)


def _install_seccomp_filter() -> None:
    arch, denied_syscalls = _seccomp_profile_for_machine()
    filters = _seccomp_filter(arch, denied_syscalls)
    array_type = SockFilter * len(filters)
    filters_array = array_type(*filters)
    program = SockFprog(len=len(filters), filter=filters_array)
    _prctl_seccomp(program)

def landlock_available() -> bool:
    """Return True iff the running Linux kernel actually supports Landlock.

    Older kernels (< 5.13) lack the syscall and some kernels ship with
    ``CONFIG_SECURITY_LANDLOCK=y`` but disabled from ``CONFIG_LSM``, which makes
    the syscall return ``ENOSYS``. Either case means we cannot rely on
    Landlock to wrap a subprocess and the caller should fall back to an
    ``unavailable`` error rather than letting the wrapper crash inside the
    child after ``PR_SET_NO_NEW_PRIVS`` has already restricted filesystem
    access.
    """
    try:
        _landlock_abi()
    except OSError:
        return False
    return True


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
                root == write_root or root.is_relative_to(write_root) for write_root in write_roots
            ):
                continue
            _add_path_rule(ruleset_fd, root, _READ_ACCESS)
        _prctl_no_new_privs()
        _syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
        _install_seccomp_filter()
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
