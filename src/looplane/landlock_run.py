"""Compatibility entry point; platform enforcement lives in sandbox.landlock_run."""

from looplane.sandbox.landlock_run import (
    _ACCESS_EXECUTE as _ACCESS_EXECUTE,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_MAKE_BLOCK as _ACCESS_MAKE_BLOCK,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_MAKE_CHAR as _ACCESS_MAKE_CHAR,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_MAKE_DIR as _ACCESS_MAKE_DIR,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_MAKE_FIFO as _ACCESS_MAKE_FIFO,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_MAKE_REG as _ACCESS_MAKE_REG,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_MAKE_SOCK as _ACCESS_MAKE_SOCK,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_MAKE_SYM as _ACCESS_MAKE_SYM,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_READ_DIR as _ACCESS_READ_DIR,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_READ_FILE as _ACCESS_READ_FILE,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_REFER as _ACCESS_REFER,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_REMOVE_DIR as _ACCESS_REMOVE_DIR,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_REMOVE_FILE as _ACCESS_REMOVE_FILE,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_TRUNCATE as _ACCESS_TRUNCATE,
)
from looplane.sandbox.landlock_run import (
    _ACCESS_WRITE_FILE as _ACCESS_WRITE_FILE,
)
from looplane.sandbox.landlock_run import (
    _AUDIT_ARCH_AARCH64 as _AUDIT_ARCH_AARCH64,
)
from looplane.sandbox.landlock_run import (
    _AUDIT_ARCH_X86_64 as _AUDIT_ARCH_X86_64,
)
from looplane.sandbox.landlock_run import (
    _BPF_JMP_JA as _BPF_JMP_JA,
)
from looplane.sandbox.landlock_run import (
    _BPF_JMP_JEQ_K as _BPF_JMP_JEQ_K,
)
from looplane.sandbox.landlock_run import (
    _BPF_LD_W_ABS as _BPF_LD_W_ABS,
)
from looplane.sandbox.landlock_run import (
    _BPF_RET_K as _BPF_RET_K,
)
from looplane.sandbox.landlock_run import (
    _DEVICE_FILE_ACCESS as _DEVICE_FILE_ACCESS,
)
from looplane.sandbox.landlock_run import (
    _HANDLED_ACCESS as _HANDLED_ACCESS,
)
from looplane.sandbox.landlock_run import (
    _LANDLOCK_CREATE_RULESET_VERSION as _LANDLOCK_CREATE_RULESET_VERSION,
)
from looplane.sandbox.landlock_run import (
    _LANDLOCK_RULE_PATH_BENEATH as _LANDLOCK_RULE_PATH_BENEATH,
)
from looplane.sandbox.landlock_run import (
    _PR_SET_NO_NEW_PRIVS as _PR_SET_NO_NEW_PRIVS,
)
from looplane.sandbox.landlock_run import (
    _PR_SET_SECCOMP as _PR_SET_SECCOMP,
)
from looplane.sandbox.landlock_run import (
    _READ_ACCESS as _READ_ACCESS,
)
from looplane.sandbox.landlock_run import (
    _SECCOMP_ARCH_OFFSET as _SECCOMP_ARCH_OFFSET,
)
from looplane.sandbox.landlock_run import (
    _SECCOMP_MODE_FILTER as _SECCOMP_MODE_FILTER,
)
from looplane.sandbox.landlock_run import (
    _SECCOMP_NR_OFFSET as _SECCOMP_NR_OFFSET,
)
from looplane.sandbox.landlock_run import (
    _SECCOMP_RET_ALLOW as _SECCOMP_RET_ALLOW,
)
from looplane.sandbox.landlock_run import (
    _SECCOMP_RET_ERRNO as _SECCOMP_RET_ERRNO,
)
from looplane.sandbox.landlock_run import (
    _SYS_LANDLOCK_ADD_RULE as _SYS_LANDLOCK_ADD_RULE,
)
from looplane.sandbox.landlock_run import (
    _SYS_LANDLOCK_CREATE_RULESET as _SYS_LANDLOCK_CREATE_RULESET,
)
from looplane.sandbox.landlock_run import (
    _SYS_LANDLOCK_RESTRICT_SELF as _SYS_LANDLOCK_RESTRICT_SELF,
)
from looplane.sandbox.landlock_run import (
    _WRITE_ACCESS as _WRITE_ACCESS,
)
from looplane.sandbox.landlock_run import (
    LandlockPathBeneathAttr as LandlockPathBeneathAttr,
)
from looplane.sandbox.landlock_run import (
    LandlockRulesetAttr as LandlockRulesetAttr,
)
from looplane.sandbox.landlock_run import (
    SockFilter as SockFilter,
)
from looplane.sandbox.landlock_run import (
    SockFprog as SockFprog,
)
from looplane.sandbox.landlock_run import (
    _add_device_file_rule as _add_device_file_rule,
)
from looplane.sandbox.landlock_run import (
    _add_path_rule as _add_path_rule,
)
from looplane.sandbox.landlock_run import (
    _apply_policy as _apply_policy,
)
from looplane.sandbox.landlock_run import (
    _bpf_jump as _bpf_jump,
)
from looplane.sandbox.landlock_run import (
    _bpf_stmt as _bpf_stmt,
)
from looplane.sandbox.landlock_run import (
    _create_ruleset as _create_ruleset,
)
from looplane.sandbox.landlock_run import (
    _die as _die,
)
from looplane.sandbox.landlock_run import (
    _install_seccomp_filter as _install_seccomp_filter,
)
from looplane.sandbox.landlock_run import (
    _landlock_abi as _landlock_abi,
)
from looplane.sandbox.landlock_run import (
    _paths as _paths,
)
from looplane.sandbox.landlock_run import (
    _prctl_no_new_privs as _prctl_no_new_privs,
)
from looplane.sandbox.landlock_run import (
    _prctl_seccomp as _prctl_seccomp,
)
from looplane.sandbox.landlock_run import (
    _seccomp_filter as _seccomp_filter,
)
from looplane.sandbox.landlock_run import (
    _seccomp_profile_for_machine as _seccomp_profile_for_machine,
)
from looplane.sandbox.landlock_run import (
    _syscall as _syscall,
)
from looplane.sandbox.landlock_run import (
    main as main,
)


def landlock_available() -> bool:
    """Preserve the historical ABI-probe monkeypatch surface."""
    try:
        _landlock_abi()
    except OSError:
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
