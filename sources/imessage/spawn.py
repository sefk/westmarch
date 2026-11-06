"""Run a program as its own TCC "responsible process".

macOS attributes a file-access check to the app at the top of the process
tree (the terminal, or the launchd job's program), not to the binary doing
the reading. A Full Disk Access grant on the snapshot helper therefore only
counts when the helper is spawned with responsibility disclaimed, which
`subprocess` cannot do; this is posix_spawn through ctypes.
"""

import ctypes
import os
import signal
import subprocess
import tempfile
import time


def run_disclaimed(argv: list[str], timeout: float) -> tuple[int, bytes]:
    """Run argv with an empty environment; return (exit code, stderr).
    Raises subprocess.TimeoutExpired after killing the child."""
    libc = ctypes.CDLL(None, use_errno=True)
    attr, actions = ctypes.c_void_p(), ctypes.c_void_p()
    libc.posix_spawnattr_init(ctypes.byref(attr))
    libc.posix_spawn_file_actions_init(ctypes.byref(actions))
    try:
        if hasattr(libc, "responsibility_spawnattrs_setdisclaim"):
            libc.responsibility_spawnattrs_setdisclaim(ctypes.byref(attr), 1)
        with tempfile.TemporaryFile() as err, open(os.devnull, "r+b") as null:
            libc.posix_spawn_file_actions_adddup2(ctypes.byref(actions), null.fileno(), 0)
            libc.posix_spawn_file_actions_adddup2(ctypes.byref(actions), null.fileno(), 1)
            libc.posix_spawn_file_actions_adddup2(ctypes.byref(actions), err.fileno(), 2)
            args = [os.fsencode(a) for a in argv]
            c_argv = (ctypes.c_char_p * (len(args) + 1))(*args, None)
            c_envp = (ctypes.c_char_p * 1)(None)
            pid = ctypes.c_int()
            rc = libc.posix_spawn(ctypes.byref(pid), args[0], ctypes.byref(actions), ctypes.byref(attr), c_argv, c_envp)
            if rc:
                raise OSError(rc, os.strerror(rc), argv[0])
            deadline = time.monotonic() + timeout
            while True:
                done, status = os.waitpid(pid.value, os.WNOHANG)
                if done:
                    break
                if time.monotonic() > deadline:
                    os.kill(pid.value, signal.SIGKILL)
                    os.waitpid(pid.value, 0)
                    raise subprocess.TimeoutExpired(argv, timeout)
                time.sleep(0.05)
            err.seek(0)
            return os.waitstatus_to_exitcode(status), err.read(4096)
    finally:
        libc.posix_spawn_file_actions_destroy(ctypes.byref(actions))
        libc.posix_spawnattr_destroy(ctypes.byref(attr))
