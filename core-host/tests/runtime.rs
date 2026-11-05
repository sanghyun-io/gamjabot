#![cfg(unix)]
// Real detached/foreground core processes and local fixture HTTP engine; no GUI or owner data.
use gamja_core_host::EndpointFile;
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Write},
    net::TcpStream,
    path::PathBuf,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};

struct Fixture {
    root: PathBuf,
    core: Option<Child>,
}
impl Fixture {
    fn new() -> Self {
        static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let root = std::env::temp_dir().join(format!(
            "gamja-core-test-{}-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
            NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        fs::write(root.join("engine.py"), r#"
import hashlib, http.server, json, os, signal, socket, subprocess, sys, threading
from pathlib import Path
root = Path(os.environ['GAMJA_APP_DATA_DIR'])
root.joinpath('engine.pid').write_text(str(os.getpid()))
core_pid = os.getppid()
core_ppid = int(subprocess.check_output(['/bin/ps', '-o', 'ppid=', '-p', str(core_pid)], timeout=2).strip())
root.joinpath('engine.core-parent.json').write_text(json.dumps({'engine_pid':os.getpid(), 'engine_ppid':core_pid, 'core_ppid':core_ppid}))
root.joinpath('engine.native.json').write_text(json.dumps({'endpoint_present':bool(os.environ.get('GAMJA_DESKTOP_IPC_ENDPOINT')), 'key_length':len(os.environ.get('GAMJA_DESKTOP_IPC_KEY',''))}))
credential_fd = os.environ.get('GAMJA_CREDENTIAL_FD')
credential_channel = None
credential_valid = False
if credential_fd is not None:
    credential_channel = socket.socket(fileno=int(credential_fd))
    credential_valid = credential_channel.family == socket.AF_UNIX
    os.set_inheritable(int(credential_fd), False)
root.joinpath('engine.credential.json').write_text(json.dumps({'present':credential_fd is not None, 'valid_socket':credential_valid, 'inheritable_after':os.get_inheritable(int(credential_fd)) if credential_fd is not None else None, 'auth_present':os.environ.get('GAMJA_AUTH_BROKER_FD') is not None, 'provider_present':os.environ.get('GAMJA_PROVIDER_FD') is not None}))
# Exercise cleanup of a TERM-resistant descendant in the owned group.
child = subprocess.Popen([sys.executable, '-c', 'import pathlib,signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);pathlib.Path(sys.argv[1]).write_text("ready");time.sleep(120)', str(root / 'descendant.ready')], stdin=subprocess.DEVNULL)
while not root.joinpath('descendant.ready').exists():
    __import__('time').sleep(0.01)
root.joinpath('descendant.pid').write_text(str(child.pid))
def lifeline():
    owner_group = int(sys.stdin.buffer.readline())
    if owner_group != os.getpgrp(): os._exit(7)
    sys.stdin.buffer.read()
    os.killpg(owner_group, signal.SIGKILL)
threading.Thread(target=lifeline, daemon=True).start()
def requested_exit():
    while not root.joinpath('exit-now').exists(): __import__('time').sleep(0.01)
    os._exit(3)
threading.Thread(target=requested_exit, daemon=True).start()
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def do_GET(self):
        valid = self.headers.get('X-Gamja-Session') == os.environ['GAMJA_LOCAL_SESSION_TOKEN']
        body = b'{"status":"healthy"}'
        self.send_response(200 if valid else 401)
        self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *args): pass
http.server.HTTPServer(('127.0.0.1', int(os.environ['GAMJA_PORT'])), Handler).serve_forever()
"#).unwrap();
        Self { root, core: None }
    }
    fn command(&self, operation: &str) -> Command {
        let binary = option_env!("CARGO_BIN_EXE_gamja")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target/debug/gamja")
            });
        let mut command = Command::new(binary);
        command
            .args([operation, "--app-data"])
            .arg(&self.root)
            .arg("--engine")
            .arg(python())
            .arg("--engine-arg")
            .arg(self.root.join("engine.py"))
            .args(["--startup-timeout-ms", "4000"])
            .arg("--helper")
            .arg(self.root.join("missing-helper"));
        command
    }
    fn serve(&mut self) {
        self.core = Some(
            self.command("serve")
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .expect("standalone gamja executable missing"),
        );
    }
    fn endpoint(&self) -> EndpointFile {
        wait_until(|| self.root.join("owner-cli.json").is_file());
        EndpointFile::from_json(&fs::read_to_string(self.root.join("owner-cli.json")).unwrap())
            .unwrap()
    }
    fn ready(&self) -> Value {
        let endpoint = self.endpoint();
        let mut health = Value::Null;
        wait_until(|| {
            health = control(&endpoint, json!({"operation":"health"}), None).1;
            health["engine"] == "ready"
        });
        health
    }
    fn pid(&self, filename: &str) -> i32 {
        fs::read_to_string(self.root.join(filename))
            .unwrap()
            .parse()
            .unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        if let Ok(value) = fs::read_to_string(self.root.join("owner-cli.json")) {
            if let Ok(endpoint) = EndpointFile::from_json(&value) {
                let _ = control(&endpoint, json!({"operation":"stop"}), None);
            }
        }
        if let Some(mut core) = self.core.take() {
            let deadline = Instant::now() + Duration::from_secs(5);
            let mut exited = false;
            while Instant::now() < deadline {
                if core.try_wait().ok().flatten().is_some() {
                    exited = true;
                    break;
                }
                thread::sleep(Duration::from_millis(20));
            }
            if !exited {
                let _ = core.kill();
            }
            let _ = core.wait();
        }
        let _ = fs::remove_dir_all(&self.root);
    }
}
use std::time::{SystemTime, UNIX_EPOCH};

#[test]
fn backend_stderr_exposes_only_fixed_exception_classes() {
    use gamja_core_host::runtime::{drain_backend_stderr, BackendExceptionClass};
    let input = b"Traceback (most recent call last):\n  File '/private/owner.py', line 9\nModuleNotFoundError: secret-provider-value\napp.services.core_upgrade_login.ChannelError: upgrade_channel_invalid\nAttributeError: private-owner-email\n";
    let mut errors = Vec::new();
    drain_backend_stderr(&input[..], |error| errors.push(error));
    assert_eq!(errors, vec![BackendExceptionClass::ModuleNotFound, BackendExceptionClass::UpgradeChannelInvalid, BackendExceptionClass::Attribute]);
    let projection = serde_json::to_string(&errors).unwrap();
    for private in ["secret-provider-value", "private-owner-email", "/private/owner.py"] {
        assert!(!projection.contains(private));
    }
}

#[test]
fn backend_stderr_rejects_unknown_embedded_and_oversized_lines() {
    use gamja_core_host::runtime::drain_backend_stderr;
    let mut input = b"UnknownError: private-value\nwarning: ModuleNotFoundError: private-value\n".to_vec();
    input.extend(vec![b'x'; 8192]);
    input.extend(b"ModuleNotFoundError: private-value\n\xff\xfe\n");
    let mut errors = Vec::new();
    drain_backend_stderr(&input[..], |error| errors.push(error));
    assert!(errors.is_empty());
}

#[test]
fn backend_stderr_handles_stream_chunks_eof_and_repeated_errors() {
    use gamja_core_host::runtime::{drain_backend_stderr, BackendExceptionClass};
    struct Chunks { input: std::io::Cursor<Vec<u8>> }
    impl Read for Chunks {
        fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
            self.input.read(&mut out[..1])
        }
    }
    let input = b"ImportError: private\nImportError: other\nsqlalchemy.exc.OperationalError: private database".to_vec();
    let mut errors = Vec::new();
    drain_backend_stderr(Chunks { input: std::io::Cursor::new(input) }, |error| errors.push(error));
    assert_eq!(errors, vec![BackendExceptionClass::Import, BackendExceptionClass::Database]);
}

#[test]
fn backend_stderr_bounds_distinct_errors_and_recovers_after_long_line() {
    use gamja_core_host::runtime::{drain_backend_stderr, BackendExceptionClass};
    let mut input = vec![b'x'; 10_000];
    input.extend(b"\nTypeError: a\nValueError: b\nOSError: c\nRuntimeError: d\n");
    let mut errors = Vec::new();
    drain_backend_stderr(&input[..], |error| errors.push(error));
    assert_eq!(errors, vec![BackendExceptionClass::Type, BackendExceptionClass::Value, BackendExceptionClass::Os]);
}

#[test]
fn backend_stderr_retries_interrupted_read_without_losing_error() {
    use gamja_core_host::runtime::{drain_backend_stderr, BackendExceptionClass};
    struct Interrupted { first: bool, input: std::io::Cursor<Vec<u8>> }
    impl Read for Interrupted {
        fn read(&mut self, out: &mut [u8]) -> std::io::Result<usize> {
            if self.first {
                self.first = false;
                return Err(std::io::ErrorKind::Interrupted.into());
            }
            self.input.read(out)
        }
    }
    let mut errors = Vec::new();
    drain_backend_stderr(Interrupted { first: true, input: std::io::Cursor::new(b"ValueError: private".to_vec()) }, |error| errors.push(error));
    assert_eq!(errors, vec![BackendExceptionClass::Value]);
}

#[test]
fn diagnostic_only_status_retains_stderr_class_without_raw_output() {
    let mut failed = Fixture::new();
    fs::write(failed.root.join("engine.py"), "import sys\nsys.stderr.write('ModuleNotFoundError: secret-provider-key\\n')\nsys.exit(1)\n").unwrap();
    failed.serve();
    let health = control(&failed.endpoint(), json!({"operation":"health"}), None).1;
    assert_eq!(health["status"], "diagnostic_only");
    assert_eq!(health["backend_stderr_errors"], json!(["module_not_found"]));
    assert!(!health.to_string().contains("secret-provider-key"));
    drop(failed);
    let mut healthy = Fixture::new();
    healthy.serve();
    assert_eq!(healthy.ready()["backend_stderr_errors"], json!([]));
}
fn python() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("backend/.venv/bin/python")
}
fn wait_until(mut predicate: impl FnMut() -> bool) {
    let end = Instant::now() + Duration::from_secs(8);
    while Instant::now() < end {
        if predicate() {
            return;
        }
        thread::sleep(Duration::from_millis(25));
    }
    assert!(predicate(), "bounded lifecycle condition did not occur");
}
fn alive(pid: i32) -> bool {
    unsafe { libc::kill(pid, 0) == 0 }
}
fn control(endpoint: &EndpointFile, value: Value, origin: Option<&str>) -> (u16, Value) {
    let url = url::Url::parse(&endpoint.control_url).unwrap();
    let Ok(mut socket) = TcpStream::connect(("127.0.0.1", url.port().unwrap())) else {
        return (0, Value::Null);
    };
    socket
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let body = value.to_string();
    let origin = origin
        .map(|o| format!("Origin: {o}\r\n"))
        .unwrap_or_default();
    let request=format!("POST /control HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Gamja-CLI-Capability: {}\r\n{origin}Content-Length: {}\r\nConnection: close\r\n\r\n{body}",endpoint.capability,body.len());
    if socket.write_all(request.as_bytes()).is_err() {
        return (0, Value::Null);
    };
    let mut response = String::new();
    let _ = socket.read_to_string(&mut response);
    let status = response
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(0);
    let body = response
        .split_once("\r\n\r\n")
        .map(|(_, b)| b)
        .unwrap_or("");
    (status, serde_json::from_str(body).unwrap_or(Value::Null))
}
#[test]
fn serve_needs_no_tauri_or_frontend_tree() {
    let mut f = Fixture::new();
    f.serve();
    let health = f.ready();
    assert_eq!(health["status"], "healthy");
    assert!(!f.root.join("frontend").exists());
    let result = f.command("status").output().unwrap();
    assert!(result.status.success());
}

#[test]
fn native_source_paths_include_only_the_exact_selected_aws_cache_root() {
    let home = PathBuf::from("/fixture-home");
    let (config, credentials, sso_cache, adc) =
        gamja_core_host::runtime::default_native_source_paths(&home);
    assert_eq!(config, home.join(".aws/config"));
    assert_eq!(credentials, home.join(".aws/credentials"));
    assert_eq!(sso_cache, home.join(".aws/sso/cache"));
    assert_eq!(
        adc,
        home.join("Library/Application Support/gcloud/application_default_credentials.json")
    );
}
#[cfg(target_os = "macos")]
#[test]
fn mac_custom_engine_never_receives_the_production_credential_descriptor() {
    let mut f = Fixture::new();
    f.core = Some(
        f.command("serve")
            .env("GAMJA_AUTH_BROKER_FD", "999")
            .env("GAMJA_PROVIDER_FD", "998")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
    f.ready();
    let observed: Value =
        serde_json::from_str(&fs::read_to_string(f.root.join("engine.credential.json")).unwrap())
            .unwrap();
    assert_eq!(
        observed,
        json!({"present":false,"valid_socket":false,"inheritable_after":null,
            "auth_present":false,"provider_present":false})
    );
}
#[test]
fn start_is_single_instance() {
    let f = Fixture::new();
    let first = f.command("start").output().unwrap();
    assert!(
        first.status.success(),
        "{}",
        String::from_utf8_lossy(&first.stderr)
    );
    let identity = f.ready()["core_pid"].clone();
    let second = f.command("start").arg("--if-needed").output().unwrap();
    assert!(second.status.success());
    assert_eq!(f.ready()["core_pid"], identity);
    let third = f.command("start").output().unwrap();
    assert!(!third.status.success());
}
#[test]
fn legacy_live_backend_blocks_start() {
    use std::os::fd::AsRawFd;
    let f = Fixture::new();
    let lock = fs::File::create(f.root.join("gamja_bot.db.backend.lock")).unwrap();
    assert_eq!(
        unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) },
        0
    );
    let result = f.command("start").output().unwrap();
    assert!(!result.status.success());
    assert!(!f.root.join("engine.pid").exists());
    assert!(!f.root.join("owner-cli.json").exists());
}
#[test]
fn client_exit_does_not_stop_core() {
    let f = Fixture::new();
    assert!(f.command("start").output().unwrap().status.success());
    let health = f.ready();
    let pid = health["engine_pid"].as_i64().unwrap() as i32;
    assert!(f.command("status").output().unwrap().status.success());
    thread::sleep(Duration::from_millis(150));
    assert!(alive(pid));
    assert_eq!(f.ready()["engine_pid"], health["engine_pid"]);
}
#[test]
fn stop_removes_endpoint_and_owned_children() {
    let mut f = Fixture::new();
    f.serve();
    f.ready();
    let engine = f.pid("engine.pid");
    let child = f.pid("descendant.pid");
    assert!(f.command("stop").output().unwrap().status.success());
    wait_until(|| !f.root.join("owner-cli.json").exists());
    wait_until(|| !alive(engine) && !alive(child));
}
#[test]
fn core_death_closes_engine_lifeline() {
    let mut f = Fixture::new();
    f.serve();
    f.ready();
    let engine = f.pid("engine.pid");
    let child = f.pid("descendant.pid");
    f.core.as_mut().unwrap().kill().unwrap();
    f.core.as_mut().unwrap().wait().unwrap();
    wait_until(|| !alive(engine) && !alive(child));
}
#[test]
fn control_rejects_origin_unknown_and_wrong_capability() {
    let mut f = Fixture::new();
    f.serve();
    f.ready();
    let mut endpoint = f.endpoint();
    assert_eq!(
        control(
            &endpoint,
            json!({"operation":"health"}),
            Some("http://localhost:3000")
        )
        .0,
        401
    );
    assert_eq!(
        control(&endpoint, json!({"operation":"health","extra":true}), None).0,
        400
    );
    endpoint.capability = "00".repeat(32);
    assert_eq!(control(&endpoint, json!({"operation":"stop"}), None).0, 401);
    assert_eq!(f.ready()["engine"], "ready");
}
#[test]
fn reporting_before_engine_readiness_retains_diagnostic_only_control() {
    let mut f = Fixture::new();
    fs::write(f.root.join("engine.py"), "raise SystemExit(3)\n").unwrap();
    f.serve();
    let endpoint = f.endpoint();
    let mut health = Value::Null;
    wait_until(|| {
        health = control(&endpoint, json!({"operation":"health"}), None).1;
        health["engine"] == "unavailable"
    });
    assert_eq!(health["status"], "diagnostic_only");
    assert_eq!(
        control(&endpoint, json!({"operation":"create_ui_session"}), None).0,
        503
    );
    let (status, reports) = control(&endpoint, json!({"operation":"report_list"}), None);
    assert_eq!(status, 200);
    assert!(reports["events"]
        .as_array()
        .unwrap()
        .iter()
        .any(|event| event["code"] == "sidecar_exited"));
    assert_eq!(reports["auto_reporting_enabled"], false);
    assert!(f.command("stop").output().unwrap().status.success());
}
#[test]
fn private_endpoint_rejects_symlink_and_public_mode() {
    use std::os::unix::fs::{symlink, PermissionsExt};
    let mut f = Fixture::new();
    f.serve();
    f.ready();
    let path = f.root.join("owner-cli.json");
    assert_eq!(
        fs::metadata(&path).unwrap().permissions().mode() & 0o777,
        0o600
    );
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(!f.command("status").output().unwrap().status.success());
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let moved = f.root.join("moved.json");
    fs::rename(&path, &moved).unwrap();
    symlink(&moved, &path).unwrap();
    assert!(!f.command("status").output().unwrap().status.success());
    fs::remove_file(&path).unwrap();
    fs::rename(&moved, &path).unwrap();
}

#[test]
fn concurrent_start_if_needed_has_one_engine() {
    let f = Fixture::new();
    let mut first = f
        .command("start")
        .arg("--if-needed")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut second = f
        .command("start")
        .arg("--if-needed")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    assert!(first.wait().unwrap().success());
    assert!(second.wait().unwrap().success());
    let health = f.ready();
    assert_eq!(
        health["engine_pid"].as_i64().unwrap(),
        f.pid("engine.pid") as i64
    );
}

#[test]
fn real_engine_uses_typed_config_without_gui_and_optional_helper() {
    let mut f = Fixture::new();
    let backend = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .unwrap()
        .join("backend");
    let wrapper = format!(
        "import sys,runpy\nsys.path.insert(0,{:?})\nrunpy.run_path({:?},run_name='__main__')\n",
        backend.to_string_lossy(),
        backend.join("run.py").to_string_lossy()
    );
    fs::write(f.root.join("engine.py"), wrapper).unwrap();
    f.serve();
    let health = f.ready();
    assert_eq!(health["native_helper"], "unavailable");
    assert!(f.root.join("gamja_bot.db").is_file());
    let endpoint = f.endpoint();
    let (status, session) = control(&endpoint, json!({"operation":"create_ui_session"}), None);
    assert_eq!(status, 200);
    assert!(session["session_token"].as_str().unwrap().len() >= 32);
    assert!(!health.to_string().contains(&endpoint.capability));
    let port = url::Url::parse(&endpoint.api_url).unwrap().port().unwrap();
    let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
    write!(stream,"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Gamja-CLI-Capability: {}\r\nConnection: close\r\n\r\n",endpoint.capability).unwrap();
    let mut response = String::new();
    stream.read_to_string(&mut response).unwrap();
    assert!(response.starts_with("HTTP/1.1 200"));
    assert!(f.command("stop").output().unwrap().status.success());
    wait_until(|| f.core.as_mut().unwrap().try_wait().unwrap().is_some());
}

#[test]
fn control_bounds_oversized_and_slow_requests() {
    let mut f = Fixture::new();
    f.serve();
    f.ready();
    let endpoint = f.endpoint();
    let port = url::Url::parse(&endpoint.control_url)
        .unwrap()
        .port()
        .unwrap();
    let mut socket = TcpStream::connect(("127.0.0.1", port)).unwrap();
    write!(socket,"POST /control HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Gamja-CLI-Capability: {}\r\nContent-Length: 16385\r\n\r\n",endpoint.capability).unwrap();
    let mut response = String::new();
    socket.read_to_string(&mut response).unwrap();
    assert!(response.starts_with("HTTP/1.1 413"));
    let mut slow = TcpStream::connect(("127.0.0.1", port)).unwrap();
    slow.set_nodelay(true).unwrap();
    let start = Instant::now();
    // Keep individual reads below the socket timeout; the whole request still needs a deadline.
    for _ in 0..20 {
        if slow.write_all(b"P").is_err() {
            break;
        }
        thread::sleep(Duration::from_millis(80));
    }
    let mut response = String::new();
    let _ = slow.read_to_string(&mut response);
    assert!(
        start.elapsed() < Duration::from_millis(800),
        "trickled headers bypassed the total request deadline"
    );
    assert!(response.starts_with("HTTP/1.1 400"));
    assert_eq!(f.ready()["engine"], "ready");
}

#[test]
fn control_refuses_rebound_host_and_invalid_header_names() {
    let mut f = Fixture::new();
    f.serve();
    f.ready();
    let endpoint = f.endpoint();
    let port = url::Url::parse(&endpoint.control_url)
        .unwrap()
        .port()
        .unwrap();
    for (host, extra) in [
        ("127.0.0.1.attacker.invalid", ""),
        ("127.0.0.1", "Origin : http://localhost:3000\r\n"),
    ] {
        let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
        let body = r#"{"operation":"health"}"#;
        write!(stream,"POST /control HTTP/1.1\r\nHost: {host}\r\n{extra}X-Gamja-CLI-Capability: {}\r\nContent-Length: {}\r\n\r\n{body}",endpoint.capability,body.len()).unwrap();
        let mut response = String::new();
        stream.read_to_string(&mut response).unwrap();
        assert!(response.starts_with("HTTP/1.1 400"));
    }
}

// Read-only observation of the process's kernel parent/status. This deliberately
// does not simulate PID reuse or authorize cleanup with a PID-file value.
fn parent_and_state(pid: i32) -> Option<(u32, String)> {
    let output = Command::new("ps")
        .args(["-p", &pid.to_string(), "-o", "ppid=", "-o", "stat="])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8(output.stdout).ok()?;
    let mut fields = text.split_whitespace();
    Some((fields.next()?.parse().ok()?, fields.next()?.to_owned()))
}

#[test]
fn exited_engine_leader_stays_bound_until_resistant_descendant_cleanup() {
    let mut f = Fixture::new();
    f.serve();
    let health = f.ready();
    let engine = f.pid("engine.pid");
    let descendant = f.pid("descendant.pid");
    let core = health["core_pid"].as_u64().unwrap() as u32;
    assert_eq!(parent_and_state(engine).unwrap().0, core);
    assert!(f.root.join("descendant.ready").is_file());
    fs::write(f.root.join("exit-now"), "exit").unwrap();
    // The dead leader must remain a waitable child throughout the TERM grace,
    // pinning its PID while its known resistant descendant still occupies the group.
    wait_until(|| {
        parent_and_state(engine)
            .is_some_and(|(parent, state)| parent == core && state.starts_with('Z'))
    });
    thread::sleep(Duration::from_millis(250));
    assert!(
        parent_and_state(engine)
            .is_some_and(|(parent, state)| parent == core && state.starts_with('Z')),
        "core reaped the leader before the final owned-group signal"
    );
    assert!(
        parent_and_state(descendant).is_some_and(|(_, state)| !state.starts_with('Z')),
        "fixture descendant did not resist TERM during group cleanup"
    );
    let endpoint = f.endpoint();
    let mut diagnostic = Value::Null;
    wait_until(|| {
        diagnostic = control(&endpoint, json!({"operation":"health"}), None).1;
        diagnostic["status"] == "diagnostic_only"
    });
    assert_eq!(diagnostic["code"], "engine_exited");
    wait_until(|| parent_and_state(engine).is_none() && parent_and_state(descendant).is_none());
}

#[test]
fn absent_helper_is_typed_without_degrading_core_or_engine() {
    let mut f = Fixture::new();
    f.serve();
    let health = f.ready();
    assert_eq!(health["status"], "healthy");
    assert_eq!(health["native_helper"], "unavailable");
    assert_eq!(
        health["native_helper_code"],
        "native_capability_unavailable"
    );
    let endpoint = f.endpoint();
    assert_eq!(
        control(
            &endpoint,
            json!({"operation":"native_permission_status"}),
            None
        ),
        (503, json!({"code":"native_capability_unavailable"}))
    );
    assert_eq!(
        control(
            &endpoint,
            json!({"operation":"native_open_settings","panel":"screen_recording"}),
            None
        ),
        (503, json!({"code":"native_capability_unavailable"}))
    );
    assert_eq!(
        control(&endpoint, json!({"operation":"create_ui_session"}), None).0,
        200
    );
    assert_eq!(
        control(&endpoint, json!({"operation":"health"}), None).1["engine"],
        "ready"
    );
}

fn fixture_helper(f: &Fixture, mismatch: bool) -> PathBuf {
    use std::os::unix::fs::PermissionsExt;
    let path = f.root.join("helper");
    let script = r#"
import base64, hashlib, hmac, json, os, socket, struct, sys, threading, time
from pathlib import Path
root = Path(__file__).parent
bootstrap = json.loads(sys.stdin.readline())
root.joinpath('helper.pid').write_text(str(os.getpid()))
s = socket.socket(socket.AF_UNIX); s.bind(bootstrap['endpoint']); os.chmod(bootstrap['endpoint'], 0o600); s.listen()
print(json.dumps({'version':HELPER_VERSION, 'endpoint':bootstrap['endpoint'], 'ready':True}), flush=True)
def die_on_eof():
    sys.stdin.read()
    try: os.unlink(bootstrap['endpoint']); os.rmdir(Path(bootstrap['endpoint']).parent)
    except OSError: pass
    os._exit(0)
threading.Thread(target=die_on_eof,daemon=True).start()
def crash():
    while not root.joinpath('helper-exit-now').exists(): time.sleep(.01)
    os._exit(4)
threading.Thread(target=crash,daemon=True).start()
def exact(stream, count):
    data=b''
    while len(data)<count:
        part=stream.recv(count-len(data))
        if not part: raise ValueError('incomplete fixture frame')
        data += part
    return data
while True:
    with s.accept()[0] as stream:
        try:
            envelope=json.loads(exact(stream,struct.unpack('>I',exact(stream,4))[0]))
            body=base64.b64decode(envelope['body']); request=json.loads(body)
            valid=hmac.compare_digest(envelope['mac'],hmac.new(bytes.fromhex(bootstrap['key']),body,hashlib.sha256).hexdigest())
            if not valid: response={'state':'failed','code':'native_request_denied'}
            elif request['operation']=='permission_status': response={'state':'completed','permissions':{'screen_recording':'granted','accessibility':'not_granted'}}
            elif request['operation']=='open_settings' and request['panel'] in ('screen_recording','accessibility'):
                root.joinpath('settings.panel').write_text(request['panel'])
                response={'state':'completed'}
            else: response={'state':'failed','code':'native_request_denied'}
            root.joinpath('helper.control.json').write_text(json.dumps({'valid_mac':valid,'permission_request':request['operation']=='permission_status'}))
            data=json.dumps(response).encode(); stream.sendall(struct.pack('>I',len(data))+data)
        except (ValueError, OSError): pass
"#;
    fs::write(
        &path,
        format!(
            "#!{}\n{}",
            python().display(),
            script.replace("HELPER_VERSION", if mismatch { "99" } else { "1" })
        ),
    )
    .unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
    path
}
fn serve_with_helper(f: &mut Fixture, helper: &std::path::Path) {
    f.core = Some(
        f.command("serve")
            .arg("--helper")
            .arg(helper)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap(),
    );
}
#[test]
fn helper_authentication_permissions_and_crash_do_not_stop_engine() {
    use gamja_core_host::{cli::CoreClient, PermissionState, SettingsPanel};
    let mut f = Fixture::new();
    let helper = fixture_helper(&f, false);
    serve_with_helper(&mut f, &helper);
    let health = f.ready();
    assert_eq!(health["native_helper"], "available");
    assert!(health["native_helper_code"].is_null());
    let injected: Value =
        serde_json::from_str(&fs::read_to_string(f.root.join("engine.native.json")).unwrap())
            .unwrap();
    assert_eq!(injected, json!({"endpoint_present":true,"key_length":64}));
    let client = CoreClient {
        app_data: f.root.clone(),
    };
    let raw_control = control(
        &f.endpoint(),
        json!({"operation":"native_permission_status"}),
        None,
    );
    assert_eq!(
        raw_control.0, 200,
        "raw permission control: {:?}",
        raw_control
    );
    let permission_result = client.native_permission_status();
    assert!(
        permission_result.is_ok(),
        "permission result: {:?}, fixture trace: {:?}",
        permission_result,
        fs::read_to_string(f.root.join("helper.control.json"))
    );
    let permission = permission_result.unwrap();
    assert_eq!(permission.screen_recording, PermissionState::Granted);
    assert_eq!(permission.accessibility, PermissionState::NotGranted);
    client
        .open_native_settings(SettingsPanel::ScreenRecording)
        .unwrap();
    assert_eq!(
        fs::read_to_string(f.root.join("settings.panel")).unwrap(),
        "screen_recording"
    );
    let endpoint = f.endpoint();
    assert_eq!(
        control(
            &endpoint,
            json!({"operation":"native_open_settings","panel":"arbitrary_url"}),
            None
        )
        .0,
        400
    );
    assert_eq!(
        control(
            &endpoint,
            json!({"operation":"native_permission_status","url":"file:///private"}),
            None
        )
        .0,
        400
    );
    fs::write(f.root.join("helper-exit-now"), "exit").unwrap();
    wait_until(|| {
        control(&endpoint, json!({"operation":"health"}), None).1["native_helper_code"]
            == "native_helper_stopped"
    });
    let healthy = control(&endpoint, json!({"operation":"health"}), None).1;
    assert_eq!(healthy["status"], "healthy");
    assert_eq!(healthy["engine"], "ready");
    assert_eq!(
        client.native_permission_status().unwrap_err(),
        "native_capability_unavailable"
    );
    assert_eq!(client.create_ui_session().unwrap()["engine_version"], 1);
}
#[test]
fn incompatible_helper_never_injects_native_credentials() {
    let mut f = Fixture::new();
    let helper = fixture_helper(&f, true);
    serve_with_helper(&mut f, &helper);
    let health = f.ready();
    assert_eq!(health["status"], "healthy");
    assert_eq!(
        health["native_helper_code"],
        "native_helper_version_mismatch"
    );
    let injected: Value =
        serde_json::from_str(&fs::read_to_string(f.root.join("engine.native.json")).unwrap())
            .unwrap();
    assert_eq!(injected, json!({"endpoint_present":false,"key_length":0}));
    wait_until(|| !alive(f.pid("helper.pid")));
}
#[test]
fn explicit_core_stop_and_core_death_clean_up_owned_helper() {
    for crash in [false, true] {
        let mut f = Fixture::new();
        let helper = fixture_helper(&f, false);
        serve_with_helper(&mut f, &helper);
        assert_eq!(f.ready()["native_helper"], "available");
        let pid = f.pid("helper.pid");
        let endpoint = f.endpoint();
        if crash {
            f.core.as_mut().unwrap().kill().unwrap();
            f.core.as_mut().unwrap().wait().unwrap();
        } else {
            assert_eq!(control(&endpoint, json!({"operation":"stop"}), None).0, 200);
        }
        wait_until(|| !alive(pid));
    }
}

#[test]
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn core_ppid_is_1_before_engine_bootstrap_and_stays_healthy_after_cli_exit() {
    let f = Fixture::new();
    let result = f.command("start").arg("--if-needed").output().unwrap();
    assert!(result.status.success(), "start failed: {}", String::from_utf8_lossy(&result.stderr));
    let health = f.ready();
    let core_pid = health["core_pid"].as_u64().unwrap() as i32;
    let engine_pid = f.pid("engine.pid");
    let parent: Value = serde_json::from_slice(&fs::read(f.root.join("engine.core-parent.json")).unwrap()).unwrap();
    assert!(alive(core_pid), "core not alive");
    assert!(alive(engine_pid), "engine not alive");
    assert_eq!(parent["engine_pid"], engine_pid);
    assert_eq!(parent["engine_ppid"], core_pid, "engine must remain a direct child of Core");
    assert_eq!(parent["core_ppid"], 1, "Core must be adopted before engine bootstrap");
    // Verify core is still healthy after CLI exit
    thread::sleep(Duration::from_secs(12));
    assert!(alive(core_pid), "core died after CLI exit");
    assert!(alive(engine_pid), "engine died after CLI exit");
    let health2 = f.ready();
    assert_eq!(health2["engine"], "ready");
}

#[test]
fn reporting_cli_works_in_diagnostic_only_core_and_preserves_manual_draft() {
    let mut f = Fixture::new();
    fs::write(f.root.join("engine.py"), "raise SystemExit(3)\n").unwrap();
    f.serve();
    f.endpoint();
    let binary = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target/debug/gamja");
    let report = |words: &[&str]| {
        Command::new(&binary)
            .arg("--app-data")
            .arg(&f.root)
            .arg("report")
            .args(words)
            .output()
            .unwrap()
    };
    let created = report(&["create"]);
    assert!(
        created.status.success(),
        "report create failed: {}",
        String::from_utf8_lossy(&created.stderr)
    );
    let draft: Value = serde_json::from_slice(&created.stdout).unwrap();
    let id = draft["envelope"]["report_id"].as_str().unwrap();
    assert_eq!(draft["envelope"]["mode"], "manual");
    let listed = report(&["list"]);
    assert!(listed.status.success());
    assert_eq!(
        serde_json::from_slice::<Value>(&listed.stdout).unwrap()["reports"][0]["draft"],
        draft
    );
    let exported = report(&["export", id]);
    assert!(exported.status.success());
    let path: String = serde_json::from_slice(&exported.stdout).unwrap();
    assert_eq!(
        fs::read_to_string(path).unwrap(),
        draft["body"].as_str().unwrap()
    );
    let denied = report(&["create", "--description", "never-echo-this-fixture"]);
    assert!(!denied.status.success());
    assert!(!String::from_utf8_lossy(&denied.stderr).contains("never-echo-this-fixture"));
    assert!(report(&["delete", id]).status.success());
}
