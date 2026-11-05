"""Source-only GUI gate: identity-bound polite Quit and shielded final cleanup."""
from contextlib import contextmanager
import ctypes
from pathlib import Path
import signal
import struct
import sys
import time

from gui_gate_checks import accept_normal_close, process_inventory


def kernel_start(h, pid):
    h.require(sys.platform == 'darwin' and type(pid) is int and 0 < pid < 2**31, 'gui_identity_unproven')
    # macOS SDK sys/proc_info.h: PROC_PIDTBSDINFO=3, proc_bsdinfo=136,
    # pbi_start_tvsec/tvusec are the final two uint64 fields (offset 120).
    library = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    function = library.proc_pidinfo
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    data = ctypes.create_string_buffer(136)
    h.require(function(pid, 3, 0, data, 136) == 136, 'gui_identity_unproven')
    h.require(struct.unpack_from('=I', data.raw, 12)[0] == pid, 'gui_identity_unproven')
    seconds, micros = struct.unpack_from('=QQ', data.raw, 120)
    h.require(seconds > 0 and micros < 1_000_000, 'gui_identity_unproven')
    return seconds, micros


def request_polite_quit(h, fixture, child, identity, helper):
    h.require(child in fixture.children and child.pid not in fixture.finalized and
              not h.child_exited(child), 'gui_identity_unproven')
    rows = process_inventory(h)
    h.require(any(row['pid'] == child.pid and row['started'] == identity['started'] and
                  row['executable'] == identity['executable'] for row in rows), 'gui_identity_unproven')
    h.require(kernel_start(h, child.pid) == identity['kernel_start'], 'gui_identity_unproven')
    executable = Path(identity['executable'])
    h.require(executable.name == 'gamja-bot' and executable.parent.name == 'MacOS' and
              executable.parent.parent.name == 'Contents', 'gui_identity_unproven')
    before = time.monotonic_ns()
    result = fixture.command(['/usr/bin/swift', str(helper), str(child.pid),
        *(str(value) for value in identity['kernel_start']),
        str(executable.parents[2]), str(executable)], timeout=8)
    after = time.monotonic_ns()
    h.require(len(result) == 1 and type(result[0]) is dict, 'gui_quit_request_failed')
    value = result[0]
    h.require(set(value) == {'requested', 'request_monotonic_ns', 'launch_date_available'} and
              value['requested'] is True and type(value['launch_date_available']) is bool and
              type(value['request_monotonic_ns']) is int and
              before <= value['request_monotonic_ns'] <= after, 'gui_quit_request_failed')
    return value


def polite_close(h, fixture, child, log, identity, helper, *, phase, exit_seconds, attest, secret_values=()):
    h.require((phase, exit_seconds) in {('gui-closed', 18), ('reopened-closed', 30)}, 'invalid_close_phase')
    attest(phase + '_before_native_quit')
    value = request_polite_quit(h, fixture, child, identity, helper)
    deadline = value['request_monotonic_ns'] + exit_seconds * 1_000_000_000
    while True:
        exited = h.child_exited(child)  # WNOWAIT: sole reap remains PrivateFixture.
        now = time.monotonic_ns()
        h.require(now < deadline, 'gui_normal_exit_timeout')
        attest(phase + '_waiting_normal_exit')
        h.require(time.monotonic_ns() < deadline, 'gui_normal_exit_timeout')
        if exited:
            break
        time.sleep(.05)
    signature = accept_normal_close(h, fixture, child, log, phase=phase, secret_values=secret_values)
    return {'phase': phase, 'requested': True, 'exit_budget_seconds': exit_seconds,
        'observed_exit_elapsed_ms': round((now - value['request_monotonic_ns']) / 1_000_000),
        'launch_date_available': value['launch_date_available'], 'exit_code': signature['exit_code']}


@contextmanager
def cleanup_signal_shield(receipt):
    """Defer normal cancellation only during already bounded finally cleanup."""
    signals = (signal.SIGINT, signal.SIGTERM)
    previous = {number: signal.getsignal(number) for number in signals}
    committed = False
    def deferred(_number, _frame):
        if committed:
            return  # The saved receipt is authoritative after final commit.
        receipt['cleanup_interrupts'] = min(32, receipt.get('cleanup_interrupts', 0) + 1)
        receipt['failure'] = receipt.get('failure') or 'operator_interrupted'
        receipt['status'] = 'failed'
    def commit_receipt(h, path):
        nonlocal committed
        h.require(not committed, 'receipt_already_committed')
        # Serialize/write/close and the commit marker form one cancellation
        # boundary. Requests queued here run only after the outcome is final.
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
        try:
            h.write_receipt(path, receipt)  # Existing exclusive 0600 writer.
            committed = True
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
    try:
        for number in signals:
            signal.signal(number, deferred)
        yield commit_receipt
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
