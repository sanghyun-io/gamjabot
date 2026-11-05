"""Read-only instance attestation and close acceptance for the GUI operator.

This module never signals a process. Only the reviewed PrivateFixture may reap
and finish groups it directly spawned; unexpected instances fail isolation.
"""
from pathlib import Path
import subprocess
import tempfile

from gui_gate_diagnostics import snapshot_gui_diagnostics


def process_inventory(h):
    # comm is the executable path, not argv: no credentials or Owner data read.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(['/bin/ps', '-axo', 'pid=,lstart=,comm='],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL, timeout=2)
            h.require(result.returncode == 0, 'process_isolation_unproven')
            output.seek(0)
            raw = output.read(262145)
            h.require(len(raw) <= 262144, 'process_isolation_unproven')
            rows = []
            for line in raw.decode('utf-8', errors='strict').splitlines():
                parts = line.split(maxsplit=6)
                h.require(len(parts) == 7 and parts[0].isdigit(), 'process_isolation_unproven')
                executable = parts[6]
                kind = ('gui' if executable.endswith('/Contents/MacOS/gamja-bot') else
                    'core' if Path(executable).name == 'gamja' else None)
                if kind:
                    rows.append({'pid': int(parts[0]), 'started': ' '.join(parts[1:6]),
                        'kind': kind, 'executable': str(Path(executable).resolve())})
            h.require(len(rows) <= 32, 'process_isolation_unproven')
            return rows
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            raise h.GateFailure('process_isolation_unproven') from None


def identity(row):
    return (row['pid'], row['started'], row['kind'], row['executable'])


def validate_inventory(h, baseline, owned, observed):
    initial = {identity(row) for row in baseline}
    permitted = initial | {identity(row) for row in owned}
    current = {identity(row) for row in observed}
    h.require(len(current) == len(observed), 'process_isolation_unproven')
    h.require(not current - permitted, 'unexpected_gui_or_core_instance')
    # Owner/preexisting instances are observation-only and must stay unchanged.
    h.require(initial <= current, 'baseline_gui_or_core_changed')


def accept_normal_close(h, fixture, child, log, *, phase, secret_values=()):
    h.require(phase in {'gui-closed', 'reopened-closed'}, 'invalid_close_phase')
    h.require(h.child_exited(child), 'gui_not_exited')
    fixture.reap(child)  # Existing sole final group cleanup/reap authority.
    signature = snapshot_gui_diagnostics(log, exit_code=child.returncode, secret_values=secret_values)
    h.require(child.returncode == 0 and not signature['panics'] and not signature['typed_codes'] and not signature['input_truncated'],
        'gui_abnormal_close')
    return signature
