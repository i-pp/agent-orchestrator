"""Linux parent-death guard before exec, avoiding unsafe Popen preexec_fn hooks."""

import ctypes
import os
import signal
import sys


def main():
    expected_parent = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "Could not install parent-death guard")
    if os.getppid() != expected_parent:
        raise SystemExit("Parent exited before guard installation")
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)


if __name__ == "__main__":
    main()
