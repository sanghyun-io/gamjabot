"""POSIX child guardian: parent death or deadline ends its owned process group."""

import errno
import os
import select
import signal
import subprocess
import sys
import time

if __package__:
    from .launch_review import read_review, checked_targets, recheck
else:
    from launch_review import read_review, checked_targets, recheck


def main():
    control_fd = int(sys.argv[1])
    ready_fd = int(sys.argv[2])
    review_fd = int(sys.argv[3])
    deadline = float(sys.argv[4])
    command = sys.argv[5:]
    child = None
    launch_attempted = False
    try:
        # The backend may have timed out while the frozen executable unpacked.
        # No command may start once its owner has closed the control pipe.
        if select.select([control_fd], [], [], 0)[0]:
            return 1
        try:
            review = read_review(review_fd, command)
            with checked_targets(review) as leaves:
                # The cwd is the opened reviewed directory, not a second
                # unverified lookup passed to the child. Retain both FDs.
                os.fchdir(leaves[1])
                recheck(review, leaves)
                if select.select([control_fd], [], [], 0)[0] or time.monotonic() >= deadline:
                    return 1
                launch_attempted = True
                child = subprocess.Popen(command, stdin=subprocess.DEVNULL, close_fds=True,
                                         start_new_session=True)
        except (ValueError, TypeError, KeyError, UnicodeError):
            try:
                os.write(ready_fd, b"R")
            except BrokenPipeError:
                pass
            return 1
        except OSError as error:
            category = (b"R" if not launch_attempted else b"N" if error.errno == errno.ENOENT else
                        b"P" if error.errno in (errno.EACCES, errno.EPERM) else b"0")
            try:
                os.write(ready_fd, category)
            except BrokenPipeError:
                pass
            return 1
        try:
            os.write(ready_fd, b"1")
        except BrokenPipeError:
            return 1
        os.close(ready_fd)
        ready_fd = None
        while child.poll() is None:
            ready, _, _ = select.select([control_fd], [], [], max(0, min(.2, deadline - time.monotonic())))
            if ready or time.monotonic() >= deadline:
                break
        code = child.poll()
        return code or 0
    finally:
        if ready_fd is not None:
            os.close(ready_fd)
        os.close(review_fd)
        # The child owns a separate session; the guardian stays alive after
        # the root child exits so descendants cannot outlive its cleanup.
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            time.sleep(.2)
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        os.close(control_fd)


if __name__ == "__main__":
    sys.exit(main())
