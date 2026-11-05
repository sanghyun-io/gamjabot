"""Record the existing Owner GUI runtime-attach decision without secret output.

Used by future real GUI-only installs after their canonical manifest comparison.
This module does not install, sign, stop processes, or change readiness policy.
"""
from pathlib import Path
import datetime
import json
import os
import re
import subprocess
import tempfile


def _public_result(result):
    try:
        value = json.loads(result.stdout)
        json_object = isinstance(value, dict)
    except (ValueError, UnicodeDecodeError):
        value = {}
        json_object = False
    if not json_object:
        value = {}
    public = {'exit': result.returncode, 'json_object': json_object,
              'stderr_present': bool(result.stderr)}

    def copy_fields(source, destination):
        for key in ['status', 'state', 'code', 'native_helper_code']:
            item = source.get(key)
            if isinstance(item, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', item):
                destination[key] = item
        for key in ['core_pid', 'engine_pid', 'pid']:
            item = source.get(key)
            if type(item) is int and 0 < item < 2**31:
                destination[key] = item
        for key in ['ready', 'running', 'engine_ready', 'backend_ready']:
            if type(source.get(key)) is bool:
                destination[key] = source[key]

    copy_fields(value, public)
    if type(value.get('backend_stderr_errors')) is int and value['backend_stderr_errors'] >= 0:
        public['backend_stderr_errors'] = value['backend_stderr_errors']
    for key in ['engine', 'native_helper', 'credential_broker', 'browser']:
        item = value.get(key)
        if isinstance(item, dict):
            public[key] = {}
            copy_fields(item, public[key])
        elif isinstance(item, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', item):
            public[key] = {'status': item}
    return public


def attach_runtime(cli: Path, app_data: Path, receipt: Path):
    """Run status, then the original start --if-needed only if not healthy.

    Returns the original CompletedProcess plus a public decision trace. A zero
    start exit is not a claim that a new process was spawned. No extra status
    probes, retry loops, or additional acceptance gates are introduced.
    """
    receipt = Path(receipt)
    with receipt.open('x', encoding='utf-8') as claim:
        os.fchmod(claim.fileno(), 0o600)
        claim.write('{"phase":"claimed"}\n')
    trace = {'phase': 'status_pending', 'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
             'start_if_needed_requested': False, 'raw_output_retained': False,
             'process_restart_inferred': False}

    def save():
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix='.runtime-attach-', suffix='.tmp', dir=receipt.parent)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(trace, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, receipt)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    save()
    try:
        base = [str(cli), '--app-data', str(app_data)]
        health = subprocess.run([*base, 'status'], stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
        trace['before'] = _public_result(health)
        healthy = health.returncode == 0 and trace['before'].get('status') == 'healthy'
        if healthy:
            trace.update(action='retained_healthy_runtime', phase='complete')
            runtime = health
        else:
            trace.update(action='start_if_needed_requested', phase='start_pending', start_if_needed_requested=True)
            save()  # Keep the actual pre-start decision before the next command.
            runtime = subprocess.run([*base, 'start', '--if-needed'], stdin=subprocess.DEVNULL,
                                     capture_output=True, timeout=90)
            trace['start_result'] = _public_result(runtime)
            before_pid = trace['before'].get('core_pid')
            after_pid = trace['start_result'].get('core_pid')
            trace['observed_pid_comparison'] = ('unavailable' if not before_pid or not after_pid
                                                else 'same' if before_pid == after_pid else 'different')
            trace['phase'] = 'complete' if runtime.returncode == 0 else 'start_failed'
        trace['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save()
        return runtime, trace
    except BaseException as error:
        trace.update(phase='command_exception', exception_type=type(error).__name__)
        save()
        raise
