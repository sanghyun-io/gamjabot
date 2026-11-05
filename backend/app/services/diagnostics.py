"""Code-only diagnostics for the host's bounded sidecar pipe."""

import os
import stat
import json
import socket


_PREFIX = b"GAMJA_DIAGNOSTIC_V1:"
_DETAIL_PREFIX = b"GAMJA_DIAGNOSTIC_V2:"
_ALLOWED = {
    ("backend_init_failed", "startup"),
    ("backend_runtime_failed", "runtime"),
    ("unexpected_failure", "startup"),
    ("unexpected_failure", "runtime"),
}


def _private_output() -> bool:
    """The host uses a pipe on legacy platforms and a Unix stream on ARM Mac."""
    try:
        mode = os.fstat(1).st_mode
        if stat.S_ISFIFO(mode):
            return True
        if not stat.S_ISSOCK(mode):
            return False
        descriptor = os.dup(1)
        try:
            channel = socket.socket(fileno=descriptor)
        except OSError:
            os.close(descriptor)
            return False
        with channel:
            return (channel.family == socket.AF_UNIX
                    and channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE)
                    == socket.SOCK_STREAM)
    except (OSError, ValueError):
        return False


def emit_diagnostic_failure(code: str, stage: str) -> None:
    if (code, stage) not in _ALLOWED:
        return
    try:
        if not _private_output():
            return
        # Both values are fixed allowlist members; never serialize an exception.
        os.write(1, _PREFIX + b'{"code":"' + code.encode("ascii") + b'","stage":"' + stage.encode("ascii") + b'"}\n')
    except (OSError, ValueError):
        pass  # Reporting cannot interrupt startup or shutdown.


def emit_detailed_diagnostic(record: dict) -> None:
    """Send the already-sanitized committed record to the private host pipe."""
    try:
        if not _private_output():
            return
        payload = json.dumps({"code": "backend_runtime_failed", "stage": "runtime",
                              "diagnostic": record}, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        line = _DETAIL_PREFIX + payload + b"\n"
        if len(line) <= 2304:
            os.write(1, line)
    except (OSError, TypeError, ValueError):
        pass  # Reporting never interrupts the committed failure path.
