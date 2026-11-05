"""Exercise a target-native frozen backend against disposable local fixtures."""

import argparse
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import Request, urlopen


def process_env(root: Path, browsers: Path) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(root), "TMPDIR": str(root),
           "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
           "GAMJA_APP_DATA_DIR": str(root / "app-data"), "GAMJA_PORT": "0",
           "GAMJA_LOCAL_SESSION_TOKEN": "synthetic-local-token-32-characters-long"}
    if os.name == "nt":
        env["USERPROFILE"] = str(root)
        env["TEMP"] = str(root)
        env["TMP"] = str(root)
        for name in ("SystemRoot", "WINDIR"):
            if value := os.environ.get(name):
                env[name] = value
    return env


def cleanup_temporary(temporary) -> None:
    deadline = time.monotonic() + 3
    while True:
        try:
            temporary.cleanup()
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.1)


def stop_backend(backend) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(backend.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        backend.terminate()
    try:
        backend.wait(timeout=5)
    except subprocess.TimeoutExpired:
        backend.kill()
        backend.wait(timeout=5)


def verify_migrations(database: Path) -> None:
    db = sqlite3.connect(database)
    try:
        assert db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 22
    finally:
        db.close()


def health(binary: Path, root: Path, env: dict[str, str]) -> None:
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    env["GAMJA_PORT"] = str(port)
    log_path = root / "backend.log"
    with log_path.open("wb") as log:
        backend = subprocess.Popen([str(binary)], env=env, cwd=root, stdout=log, stderr=log,
                                   start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if backend.poll() is not None:
                    raise RuntimeError(f"frozen backend exited {backend.returncode}: {log_path.read_text()[-3000:]}")
                try:
                    request = Request(f"http://127.0.0.1:{port}/health",
                                      headers={"X-Gamja-Session": env["GAMJA_LOCAL_SESSION_TOKEN"]})
                    with urlopen(request, timeout=.5) as response:
                        assert json.load(response) == {"status": "healthy"}
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError(f"frozen health timed out: {log_path.read_text()[-3000:]}")
            database = root / "app-data" / "gamja_bot.db"
            verify_migrations(database)
            if os.name != "nt":
                assert database.stat().st_mode & 0o077 == 0, "database is not user-private"
            print("frozen /health and 22 SQLite migrations: PASS")
        finally:
            stop_backend(backend)


def guardian(binary: Path, root: Path, env: dict[str, str]) -> None:
    if os.name == "nt":
        print("POSIX frozen guardian: not applicable on Windows")
        return
    control_read, control_write = os.pipe()
    ready_read, ready_write = os.pipe()
    pidfile = root / "descendant.pid"
    marker = root / "escaped"
    descendant = f"import time; time.sleep(2); open({str(marker)!r}, 'w').write('escaped')"
    parent = ("import subprocess,sys,time; "
              f"p=subprocess.Popen([sys.executable,'-c',{descendant!r}]); "
              f"open({str(pidfile)!r},'w').write(str(p.pid)); time.sleep(30)")
    process = subprocess.Popen([str(binary), "--process-guardian", str(control_read), str(ready_write),
                                str(time.monotonic() + 30), sys.executable, "-c", parent],
                               env=env, cwd=root, pass_fds=(control_read, ready_write),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    os.close(control_read)
    os.close(ready_write)
    try:
        assert os.read(ready_read, 1) == b"1", "frozen guardian did not start its child"
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert pidfile.exists(), "guardian child did not launch descendant"
        os.close(control_write)
        control_write = -1
        process.wait(timeout=5)
        time.sleep(2.2)
        assert not marker.exists(), "frozen guardian left descendant running"
        print("frozen guardian re-exec and descendant termination: PASS")
    finally:
        if control_write >= 0:
            os.close(control_write)
        os.close(ready_read)
        aborted = False
        if process.poll() is None:
            try:
                process.wait(timeout=3)  # Give the guardian time to reap its owned group.
            except subprocess.TimeoutExpired:
                aborted = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        if aborted and pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("browsers", type=Path)
    args = parser.parse_args()
    binary, browsers = args.binary.resolve(), args.browsers.resolve()
    assert binary.is_file() and browsers.is_dir()
    temporary = tempfile.TemporaryDirectory(prefix="gamja-frozen-smoke-")
    root = Path(temporary.name)
    try:
        env = process_env(root, browsers)
        health(binary, root, env)
        guardian(binary, root, env)
        browser = subprocess.run([str(binary), "--package-smoke"], env=env, cwd=root,
                                 capture_output=True, text=True, timeout=30)
        assert browser.returncode == 0, browser.stdout + browser.stderr
        assert "Bundled Playwright driver and Chromium passed" in browser.stdout
        assert not list(root.glob("gamja-package-smoke-*")), "browser profile leaked"
        print("frozen Python to Node driver to bundled Chromium local fixture and profile cleanup: PASS")
    finally:
        cleanup_temporary(temporary)


if __name__ == "__main__":
    main()
