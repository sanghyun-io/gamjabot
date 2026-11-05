"""Operator-only bounded signatures from directly owned GUI stderr.

No process lookup, wait, signal or GUI launch. The controller retains its
reviewed PrivateFixture authority, passes the actual exit code after final reap,
and persists this value through verify_cli_core.write_receipt BEFORE closing
the log. Never pass global/Owner logs to this helper.
"""
import json
from pathlib import Path
import re

SCAN_BYTES = 16384
MAX_PANICS = 4
KNOWN_CODES = frozenset({'invalid_gui_arguments', 'app_data_unavailable', 'app_data_requires_attach'})


def _sanitize(value, secret_values):
    for secret in secret_values:
        if isinstance(secret, str) and secret:
            value = value.replace(secret, '[redacted]')
    value = re.sub(r'(?i)\bbearer\s+\S+', 'Bearer [redacted]', value)
    value = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[redacted]', value)
    value = re.sub(r'\b[0-9a-fA-F]{32,}\b', '[redacted]', value)
    value = re.sub(r'(?i)\b(?:token|capability|cap|jwt|password|secret|api_key)\s*[:=]\s*\S+', '[redacted]', value)
    value = re.sub(r'\b[^\s@]+@[^\s@]+\b', '[redacted]', value)
    value = re.sub(r'https?://\S+', '[url]', value)
    value = re.sub(r'(?:[A-Za-z]:\\|/)[^\s]+', '[path]', value)
    value = ''.join(char if char.isprintable() else ' ' for char in value)
    return value[:300]


def snapshot_gui_diagnostics(stream, *, exit_code=None, secret_values=()):
    """Read at most 16KiB head + 16KiB tail; omit all ordinary log contents."""
    stream.flush()
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)
    head = stream.read(SCAN_BYTES)
    tail = b''
    if size > SCAN_BYTES:
        stream.seek(max(SCAN_BYTES, size - SCAN_BYTES))
        tail = stream.read(SCAN_BYTES)
    chunks = [(head + tail).decode('utf-8', errors='replace')] if size <= SCAN_BYTES * 2 else [head.decode('utf-8', errors='replace')]
    if tail and size > SCAN_BYTES * 2:
        # Avoid interpreting a partial tail line as a complete panic message.
        chunks.append(tail.decode('utf-8', errors='replace').split('\n', 1)[-1])
    value = {'format': 1, 'scope': 'direct_owned_gui_stderr_signature',
        'exit_code': exit_code if type(exit_code) is int else None,
        'input_truncated': size > SCAN_BYTES * 2, 'panics': [], 'typed_codes': []}
    for chunk in chunks:
        lines = chunk.splitlines()
        for index, line in enumerate(lines):
            if len(value['panics']) < MAX_PANICS:
                found = re.search(r"panicked at (.+?):(\d+):(\d+):", line)
                if found:
                    message = lines[index + 1] if index + 1 < len(lines) else ''
                    panic = {'file': Path(found[1].replace('\\', '/')).name[:80],
                        'line': int(found[2]), 'column': int(found[3]),
                        'message': _sanitize(message, secret_values)}
                    if panic not in value['panics']:
                        value['panics'].append(panic)
            if line.startswith('{') and len(line) <= 4096:
                try:
                    code = json.loads(line).get('code')
                    if code in KNOWN_CODES and code not in value['typed_codes']:
                        value['typed_codes'].append(code)
                except (ValueError, AttributeError, TypeError):
                    pass
    return value
