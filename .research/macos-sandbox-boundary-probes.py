"""Local-only OS-action and denial probes, no production policy changes."""

import json
import subprocess
import sys
from pathlib import Path

from looplane.execution.environment import sanitized_subprocess_env
from looplane.sandbox.macos import _macos_sandbox_profile
from looplane.sandbox.policy import python_runtime_read_roots

out = Path(".research/macos-sandbox-evidence").resolve()
workspace = out / "workspace"
secret = out / "synthetic-private-file"
secret.write_text("diagnostic fixture, not real private data")
if not (workspace / "escape-link").is_symlink():
    (workspace / "escape-link").symlink_to(secret)
base = _macos_sandbox_profile(workspace, read_roots=python_runtime_read_roots(), writable_roots=())
setup = """import ctypes,os,json,socket,errno
lib=ctypes.CDLL('/usr/lib/libsandbox.dylib', use_errno=True)
lib.sandbox_init.argtypes=[ctypes.c_char_p,ctypes.c_uint64,ctypes.POINTER(ctypes.c_char_p)]
lib.sandbox_init.restype=ctypes.c_int
error=ctypes.c_char_p()
"""
results = []
for name, addition in [("original", ""), ("root_data", '\n(allow file-read-data (literal "/"))')]:
    # Apply after Python/dyld initialization, isolating the OS open action from startup.
    script = (
        setup
        + f"""assert lib.sandbox_init({(base + addition).encode()!r},0,ctypes.byref(error)) == 0
try:
 fd=os.open('/',0x20100000)
 os.close(fd)
 print(json.dumps({{"open_root":"allowed"}}))
except OSError as exc:
 print(json.dumps({{"open_root":"denied","errno":exc.errno}}))
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=workspace,
        env=sanitized_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=5,
    )
    row = {
        "case": name + "_inprocess_root_open",
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    print(json.dumps(row))
    results.append(row)

script = (
    setup
    + f"""
results={{}}
def attempt(name,action):
 try:
  action()
  results[name]="allowed"
 except OSError as exc:
  results[name]={{"denied_errno":exc.errno}}
def read(path):
 with open(path) as handle: handle.read(1)
def write(path):
 with open(path,'w') as handle: handle.write('diagnostic')
attempt('workspace_write',lambda:write('allowed'))
attempt('workspace_read',lambda:read('allowed'))
attempt('outside_read',lambda:read({str(secret)!r}))
attempt('outside_write',lambda:write({str(out / "must-not-exist")!r}))
attempt('symlink_escape_read',lambda:read('escape-link'))
attempt('root_directory_listing',lambda:os.listdir('/'))
rootfd=os.open('/',0x20100000)
def relative_read():
 fd=os.open({str(secret).lstrip("/")!r},os.O_RDONLY,dir_fd=rootfd)
 os.close(fd)
attempt('root_fd_openat_escape',relative_read)
os.close(rootfd)
# This queries policy only: no connection, listener, DNS, packet or remote service.
lib.sandbox_check.restype=ctypes.c_int
for operation in ('network-outbound','network-inbound'):
 results[operation+'_policy']=lib.sandbox_check(os.getpid(),operation.encode(),0)
print(json.dumps(results,sort_keys=True))
"""
)
argv = ["/usr/bin/sandbox-exec", "-f", str(out / "root_data.sb"), sys.executable, "-c", script]
result = subprocess.run(
    argv, cwd=workspace, env=sanitized_subprocess_env(), capture_output=True, text=True, timeout=5
)
row = {
    "case": "candidate_boundary_checks",
    "returncode": result.returncode,
    "stdout": result.stdout,
    "stderr": result.stderr,
}
print(json.dumps(row))
results.append(row)
(out / "boundary-probes.json").write_text(json.dumps(results, indent=2) + "\n")
