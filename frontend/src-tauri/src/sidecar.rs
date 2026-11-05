use crate::local_endpoint::LocalEndpoint;
use crate::reporting::{Code, Component, DiagnosticEvent, ReportDiagnostic, Stage};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::net::{Ipv4Addr, SocketAddrV4, TcpListener, TcpStream};
use std::path::PathBuf;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::thread;
use std::time::{Duration, Instant};
use tauri::Manager;

const DIAGNOSTIC_PREFIX: &[u8] = b"GAMJA_DIAGNOSTIC_V1:";
const DIAGNOSTIC_V2_PREFIX: &[u8] = b"GAMJA_DIAGNOSTIC_V2:";
const MAX_DIAGNOSTIC_LINE: usize = 2304;
type PipeEvent = (Component, Stage, Code, Option<ReportDiagnostic>);
type FailureRecorder = Arc<dyn Fn(Component, Stage, Code, Option<ReportDiagnostic>) + Send + Sync>;

fn app_recorder(app: tauri::AppHandle) -> FailureRecorder {
    Arc::new(move |component, stage, code, diagnostic| {
        let _ =
            crate::reporting::record_diagnostic_failure(&app, component, stage, code, diagnostic);
    })
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct PipeDiagnostic {
    code: Code,
    stage: Stage,
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct PipeDiagnosticV2 {
    code: Code,
    stage: Stage,
    diagnostic: ReportDiagnostic,
}

fn parse_diagnostic_line(line: &[u8]) -> Option<PipeEvent> {
    if line.len() > MAX_DIAGNOSTIC_LINE {
        return None;
    }
    if let Some(payload) = line.strip_prefix(DIAGNOSTIC_V2_PREFIX) {
        let event: PipeDiagnosticV2 = serde_json::from_slice(payload).ok()?;
        if event.code != Code::BackendRuntimeFailed
            || event.stage != Stage::Runtime
            || event.diagnostic.validate().is_err()
        {
            return None;
        }
        return Some((
            Component::Backend,
            event.stage,
            event.code,
            Some(event.diagnostic),
        ));
    }
    let payload = line.strip_prefix(DIAGNOSTIC_PREFIX)?;
    let event: PipeDiagnostic = serde_json::from_slice(payload).ok()?;
    if !matches!(
        (event.code, event.stage),
        (Code::BackendInitFailed, Stage::Startup)
            | (Code::BackendRuntimeFailed, Stage::Runtime)
            | (Code::UnexpectedFailure, Stage::Startup | Stage::Runtime)
    ) {
        return None;
    }
    Some((Component::Backend, event.stage, event.code, None))
}

fn drain_stdout(mut input: impl Read, mut record: impl FnMut(PipeEvent)) {
    let mut chunk = [0u8; 4096];
    let mut line = Vec::with_capacity(MAX_DIAGNOSTIC_LINE);
    let mut oversized = false;
    while let Ok(size) = input.read(&mut chunk) {
        if size == 0 {
            break;
        }
        for &byte in &chunk[..size] {
            if byte == b'\n' {
                if !oversized {
                    if let Some(event) = parse_diagnostic_line(&line) {
                        record(event);
                    }
                }
                line.clear();
                oversized = false;
            } else if !oversized {
                if line.len() < MAX_DIAGNOSTIC_LINE {
                    line.push(byte);
                } else {
                    line.clear();
                    oversized = true;
                }
            }
        }
    }
}

fn monitor_child(child: Arc<Mutex<Child>>, stop: Arc<AtomicBool>, on_exit: impl FnOnce()) {
    while !stop.load(Ordering::SeqCst) {
        if child
            .lock()
            .ok()
            .and_then(|mut owned| owned.try_wait().ok())
            .flatten()
            .is_some()
        {
            if !stop.load(Ordering::SeqCst) {
                on_exit();
            }
            return;
        }
        thread::sleep(Duration::from_millis(100));
    }
}

#[derive(Clone, Copy, Debug)]
enum LaunchFailure {
    Missing,
    Spawn,
    NotReady,
    Exited,
    Other(&'static str),
}

impl LaunchFailure {
    fn message(self) -> &'static str {
        match self {
            Self::Missing => "Bundled service is missing",
            Self::Spawn => "Cannot start bundled service",
            Self::NotReady => "Bundled service did not become ready",
            Self::Exited => "Bundled service exited during startup",
            Self::Other(message) => message,
        }
    }

    fn event(self) -> DiagnosticEvent {
        let code = match self {
            Self::Missing => Code::SidecarMissing,
            Self::Spawn => Code::SidecarSpawnFailed,
            Self::NotReady => Code::BackendNotReady,
            Self::Exited => Code::SidecarExited,
            Self::Other(_) => Code::UnexpectedFailure,
        };
        DiagnosticEvent::new(Component::Host, Stage::Startup, code)
    }
}

#[derive(Serialize)]
struct CliCapabilityFile<'a> {
    base_url: &'a str,
    capability: &'a str,
}

pub struct BackendProcess {
    child: Option<Arc<Mutex<Child>>>,
    owner_lifeline: Option<ChildStdin>,
    desktop: Option<crate::desktop::DesktopServer>,
    drains: Vec<thread::JoinHandle<()>>,
    monitor: Option<thread::JoinHandle<()>>,
    stop_requested: Arc<AtomicBool>,
    endpoint: Result<LocalEndpoint, String>,
    cli_capability_path: Option<PathBuf>,
    host_lock: Option<File>,
}

impl BackendProcess {
    pub fn new() -> Self {
        Self {
            child: None,
            owner_lifeline: None,
            desktop: None,
            drains: Vec::new(),
            monitor: None,
            stop_requested: Arc::new(AtomicBool::new(false)),
            endpoint: Err("Service is starting".into()),
            cli_capability_path: None,
            host_lock: None,
        }
    }

    pub fn endpoint(&self) -> Result<LocalEndpoint, String> {
        self.endpoint.clone()
    }

    pub fn start(&mut self, app: &tauri::AppHandle) -> Result<(), String> {
        self.stop_requested.store(false, Ordering::SeqCst);
        let recorder = app_recorder(app.clone());
        match self.launch(app, recorder.clone()) {
            Ok(endpoint) => {
                self.endpoint = Ok(endpoint);
                Ok(())
            }
            Err(failure) => {
                self.stop();
                Self::record_launch_failure(&recorder, failure);
                let message = failure.message().to_string();
                self.endpoint = Err(message.clone());
                Err(message)
            }
        }
    }

    fn record_launch_failure(recorder: &FailureRecorder, failure: LaunchFailure) {
        let event = failure.event();
        recorder(event.component, event.stage, event.code, None);
    }

    fn launch(
        &mut self,
        app: &tauri::AppHandle,
        recorder: FailureRecorder,
    ) -> Result<LocalEndpoint, LaunchFailure> {
        let executable = std::env::current_exe()
            .map_err(|_| LaunchFailure::Other("Cannot resolve service location"))?
            .parent()
            .ok_or(LaunchFailure::Other("Cannot resolve service location"))?
            .join(if cfg!(windows) {
                "backend.exe"
            } else {
                "backend"
            });
        if !executable.is_file() {
            return Err(LaunchFailure::Missing);
        }
        let app_data = app
            .path()
            .app_data_dir()
            .map_err(|_| LaunchFailure::Other("Cannot resolve application data"))?;
        std::fs::create_dir_all(&app_data)
            .map_err(|_| LaunchFailure::Other("Cannot create application data"))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&app_data, std::fs::Permissions::from_mode(0o700))
                .map_err(|_| LaunchFailure::Other("Cannot secure application data"))?;
        }
        self.acquire_host_lock(&app_data)?;
        let browsers = app
            .path()
            .resource_dir()
            .map_err(|_| LaunchFailure::Other("Cannot resolve bundled browser"))?
            .join("playwright-browsers");
        if !browsers.is_dir() {
            return Err(LaunchFailure::Other("Bundled browser is missing"));
        }
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
            .map_err(|_| LaunchFailure::Other("Cannot reserve local service port"))?;
        let port = listener
            .local_addr()
            .map_err(|_| LaunchFailure::Other("Cannot read local service port"))?
            .port();
        let mut random = [0u8; 32];
        getrandom::fill(&mut random)
            .map_err(|_| LaunchFailure::Other("Cannot create local session"))?;
        let secret: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        getrandom::fill(&mut random)
            .map_err(|_| LaunchFailure::Other("Cannot create CLI capability"))?;
        let cli_capability: String = random.iter().map(|byte| format!("{byte:02x}")).collect();
        let cli_capability_hash = format!("{:x}", Sha256::digest(cli_capability.as_bytes()));
        drop(listener);

        let mut desktop = crate::desktop::DesktopServer::bind()
            .map_err(|_| LaunchFailure::Other("Cannot bind desktop IPC"))?;

        let mut command = Command::new(executable);
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            command.process_group(0);
        }
        let mut spawned = command
            .current_dir(&app_data)
            .env("GAMJA_APP_DATA_DIR", &app_data)
            .env("GAMJA_PORT", port.to_string())
            .env("GAMJA_LOCAL_SESSION_TOKEN", &secret)
            .env("GAMJA_CLI_CAPABILITY_HASH", cli_capability_hash)
            .env("GAMJA_DESKTOP_IPC_ENDPOINT", desktop.endpoint())
            .env("GAMJA_DESKTOP_IPC_KEY", desktop.key_hex())
            .env("GAMJA_DESKTOP_OWNER_STDIN", "1")
            .env("PLAYWRIGHT_BROWSERS_PATH", browsers)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|_| LaunchFailure::Spawn)?;
        self.owner_lifeline = spawned.stdin.take();
        self.start_drains(&mut spawned, recorder.clone());
        let child = Arc::new(Mutex::new(spawned));
        self.child = Some(child.clone());
        let backend_group = child.lock().map_err(|_| LaunchFailure::Spawn)?.id();
        writeln!(
            self.owner_lifeline.as_mut().ok_or(LaunchFailure::Spawn)?,
            "{backend_group}"
        )
        .map_err(|_| LaunchFailure::Exited)?;
        desktop
            .start(child.clone(), app.clone())
            .map_err(|_| LaunchFailure::Other("Cannot start desktop IPC"))?;
        self.desktop = Some(desktop);

        let endpoint = LocalEndpoint {
            base_url: format!("http://127.0.0.1:{port}"),
            session_token: secret,
        };
        let cli_capability_path = app_data.join("owner-cli.json");
        let deadline = Instant::now() + Duration::from_secs(30);
        self.wait_for_ready(child.clone(), deadline, || {
            ready(port, &endpoint.session_token)
        })?;
        if child
            .lock()
            .ok()
            .and_then(|mut owned| owned.try_wait().ok())
            .flatten()
            .is_some()
        {
            return Err(LaunchFailure::Exited);
        }
        self.write_cli_capability(&app_data, &endpoint.base_url, &cli_capability)?;
        self.start_monitor(child, cli_capability_path, recorder);
        Ok(endpoint)
    }

    fn acquire_host_lock(&mut self, app_data: &std::path::Path) -> Result<(), LaunchFailure> {
        #[cfg(target_os = "macos")]
        {
            use std::os::fd::AsRawFd;
            use std::os::unix::fs::OpenOptionsExt;
            let path = app_data.join("gamja-host.lock");
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .mode(0o600)
                .custom_flags(libc::O_NOFOLLOW)
                .open(path)
                .map_err(|_| LaunchFailure::Other("Cannot reserve Gamja Bot runtime"))?;
            if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
                return Err(LaunchFailure::Other("Gamja Bot is already running"));
            }
            self.host_lock = Some(file);
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = app_data;
        }
        Ok(())
    }

    fn write_cli_capability(
        &mut self,
        app_data: &std::path::Path,
        base_url: &str,
        capability: &str,
    ) -> Result<(), LaunchFailure> {
        #[cfg(not(unix))]
        {
            let _ = (app_data, base_url, capability);
            // Do not publish a capability until a Windows owner-only DACL can
            // be applied and verified.  The normal desktop remains available.
            return Ok(());
        }
        #[cfg(unix)]
        {
            let path = app_data.join("owner-cli.json");
            let _ = fs::remove_file(&path);
            let body = serde_json::to_vec(&CliCapabilityFile {
                base_url,
                capability,
            })
            .map_err(|_| LaunchFailure::Other("Cannot encode CLI capability"))?;
            use std::os::unix::fs::OpenOptionsExt;
            let mut suffix = [0u8; 8];
            getrandom::fill(&mut suffix)
                .map_err(|_| LaunchFailure::Other("Cannot create CLI capability"))?;
            let temporary = app_data.join(format!(
                ".owner-cli-{}.tmp",
                suffix
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect::<String>()
            ));
            let mut options = OpenOptions::new();
            options.write(true).create_new(true).mode(0o600);
            let write_result = (|| {
                let mut file = options
                    .open(&temporary)
                    .map_err(|_| LaunchFailure::Other("Cannot create CLI capability"))?;
                file.write_all(&body)
                    .and_then(|_| file.sync_all())
                    .map_err(|_| LaunchFailure::Other("Cannot write CLI capability"))?;
                fs::rename(&temporary, &path)
                    .map_err(|_| LaunchFailure::Other("Cannot publish CLI capability"))
            })();
            if write_result.is_err() {
                let _ = fs::remove_file(&temporary);
            }
            write_result?;
            self.cli_capability_path = Some(path);
            Ok(())
        }
    }

    fn start_drains(&mut self, spawned: &mut Child, recorder: FailureRecorder) {
        if let Some(stdout) = spawned.stdout.take() {
            self.drains.push(thread::spawn(move || {
                drain_stdout(stdout, |(component, stage, code, diagnostic)| {
                    recorder(component, stage, code, diagnostic)
                })
            }));
        }
        if let Some(mut stderr) = spawned.stderr.take() {
            self.drains.push(thread::spawn(move || {
                let _ = std::io::copy(&mut stderr, &mut std::io::sink());
            }));
        }
    }

    fn wait_for_ready(
        &mut self,
        child: Arc<Mutex<Child>>,
        deadline: Instant,
        is_ready: impl Fn() -> bool,
    ) -> Result<(), LaunchFailure> {
        while Instant::now() < deadline {
            if child
                .lock()
                .ok()
                .and_then(|mut owned| owned.try_wait().ok())
                .flatten()
                .is_some()
            {
                return Err(LaunchFailure::Exited);
            }
            if is_ready() {
                return Ok(());
            }
            thread::sleep(Duration::from_millis(100));
        }
        Err(LaunchFailure::NotReady)
    }

    fn start_monitor(
        &mut self,
        child: Arc<Mutex<Child>>,
        cli_capability_path: PathBuf,
        recorder: FailureRecorder,
    ) {
        let stop = self.stop_requested.clone();
        self.monitor = Some(thread::spawn(move || {
            monitor_child(child, stop, || {
                let _ = fs::remove_file(&cli_capability_path);
                recorder(Component::Host, Stage::Runtime, Code::SidecarExited, None);
            })
        }));
    }

    pub fn stop(&mut self) {
        self.stop_requested.store(true, Ordering::SeqCst);
        if let Some(path) = self.cli_capability_path.take() {
            let _ = fs::remove_file(path);
        }
        if let Some(child) = self.child.take() {
            if let Ok(mut owned) = child.lock() {
                #[cfg(unix)]
                stop_process_group(&mut owned);
                #[cfg(not(unix))]
                {
                    let _ = owned.kill();
                    let _ = owned.wait();
                }
            }
        }
        self.owner_lifeline.take();
        if let Some(mut desktop) = self.desktop.take() {
            desktop.stop();
        }
        let drain_deadline = Instant::now() + Duration::from_millis(300);
        for drain in self.drains.drain(..) {
            while !drain.is_finished() && Instant::now() < drain_deadline {
                thread::sleep(Duration::from_millis(10));
            }
            if drain.is_finished() {
                let _ = drain.join();
            } // An escaped descendant can retain its pipe; its bounded drain exits at EOF.
        }
        if let Some(monitor) = self.monitor.take() {
            let _ = monitor.join();
        }
        self.host_lock.take();
    }
}

#[cfg(unix)]
fn stop_process_group(child: &mut Child) {
    unsafe extern "C" {
        fn kill(pid: i32, signal: i32) -> i32;
    }
    let group = -(child.id() as i32);
    unsafe { kill(group, 15) }; // TERM lets the frozen bootloader clean its extraction directory.
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline && unsafe { kill(group, 0) } == 0 {
        thread::sleep(Duration::from_millis(50));
    }
    if unsafe { kill(group, 0) } == 0 {
        unsafe { kill(group, 9) }; // Force only the owned group if it did not stop.
    }
    let _ = child.wait();
}

impl Drop for BackendProcess {
    fn drop(&mut self) {
        self.stop();
    }
}

fn ready(port: u16, secret: &str) -> bool {
    let addr = SocketAddrV4::new(Ipv4Addr::LOCALHOST, port);
    let Ok(mut stream) = TcpStream::connect_timeout(&addr.into(), Duration::from_millis(200))
    else {
        return false;
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(200)));
    let request = format!("GET /health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-Gamja-Session: {secret}\r\nConnection: close\r\n\r\n");
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = [0u8; 32];
    stream
        .read(&mut response)
        .is_ok_and(|size| response[..size].starts_with(b"HTTP/1.1 200"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn protocol_accepts_only_exact_fixed_backend_fields() {
        use crate::reporting::{Code, Component, Stage};
        assert_eq!(
            parse_diagnostic_line(
                b"GAMJA_DIAGNOSTIC_V1:{\"code\":\"backend_init_failed\",\"stage\":\"startup\"}"
            ),
            Some((
                Component::Backend,
                Stage::Startup,
                Code::BackendInitFailed,
                None
            ))
        );
        for line in [
            b"/Users/alice/secret traceback".as_slice(),
            b"GAMJA_DIAGNOSTIC_V1:{\"code\":\"backend_init_failed\",\"stage\":\"startup\",\"secret\":\"token\"}",
            b"GAMJA_DIAGNOSTIC_V1:{\"code\":\"backend_init_failed\",\"stage\":\"tool\"}",
            b"GAMJA_DIAGNOSTIC_V1:{\"code\":\"backend_init_failed\",\"stage\":\"startup\"} trailing",
        ] {
            assert_eq!(parse_diagnostic_line(line), None);
        }
    }

    #[test]
    fn version_two_pipe_accepts_only_bounded_sanitized_diagnostic() {
        use crate::reporting::{Code, Component, Stage};
        let detail = serde_json::json!({
            "version": 1, "correlation_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "occurred_at": "2026-09-30T01:00:00Z", "stage": "tool_execution",
            "failure_code": "provider_unavailable", "reason": "unexpected_error", "error_class": "value_error",
            "room_id": 4, "agent_id": 7, "connection_id": 3,
            "trigger_event_id": null, "action_id": null,
            "exception_chain": [{"module": "builtins", "class": "ValueError"}],
            "frames": [{"file": "app/services/scheduler.py", "function": "_run", "line": 382}]
        });
        let line = format!(
            "GAMJA_DIAGNOSTIC_V2:{}",
            serde_json::json!({
                "code": "backend_runtime_failed", "stage": "runtime", "diagnostic": detail
            })
        );
        let parsed = parse_diagnostic_line(line.as_bytes()).unwrap();
        assert_eq!(
            (parsed.0, parsed.1, parsed.2),
            (
                Component::Backend,
                Stage::Runtime,
                Code::BackendRuntimeFailed
            )
        );
        assert_eq!(parsed.3.unwrap().correlation_id, "a".repeat(32));
        let maximal_module = line.replace("builtins", &format!("app.{}", "a".repeat(76)));
        assert!(parse_diagnostic_line(maximal_module.as_bytes()).is_some());
        let native_code = line.replace("provider_unavailable", "accessibility_denied");
        assert!(parse_diagnostic_line(native_code.as_bytes()).is_some());
        let leaked = line.replace("app/services/scheduler.py", "/Users/alice/private.py");
        assert!(parse_diagnostic_line(leaked.as_bytes()).is_none());
        let extra = line.replace("\"version\":1", "\"version\":1,\"raw_prompt\":\"secret\"");
        assert!(parse_diagnostic_line(extra.as_bytes()).is_none());
    }

    #[test]
    fn producer_fixture_flows_through_pipe_into_manual_and_auto_report_drafts() {
        use crate::reporting::{DiagnosticEvent, Mode, ReportStore};
        struct Temp(std::path::PathBuf);
        impl Drop for Temp {
            fn drop(&mut self) {
                let _ = std::fs::remove_dir_all(&self.0);
            }
        }
        let temp = Temp(
            std::env::temp_dir().join(format!("gamja-report-pipe-{}", crate::reporting::new_id())),
        );
        std::fs::create_dir(&temp.0).unwrap();
        let line = include_bytes!("../../../reporting/fixtures/diagnostic-v2-line.txt");
        assert!(line.len() <= MAX_DIAGNOSTIC_LINE);
        let mut parsed = Vec::new();
        drain_stdout(line.as_slice(), |event| parsed.push(event));
        assert_eq!(parsed.len(), 1);
        let (component, stage, code, detail) = parsed.remove(0);
        let detail = detail.unwrap();
        assert!(!detail.frames.is_empty());
        assert_eq!(detail.exception_chain[0].class_name, "ValueError");
        let mut store = ReportStore::open(&temp.0).unwrap();
        let mut event = DiagnosticEvent::new(component, stage, code);
        event.diagnostic = Some(detail.clone());
        let id = event.event_id.clone();
        store.record(event).unwrap();
        let manual = store.create_manual_draft(&[id], None).unwrap();
        assert_eq!(manual.envelope.diagnostics, vec![detail.clone()]);
        assert!(manual.body().contains("app/services/synthetic_error.py"));
        assert!(!manual.body().contains("synthetic-private-value"));
        store.set_auto_consent(true).unwrap();
        let mut another = DiagnosticEvent::new(component, stage, code);
        another.diagnostic = Some(detail.clone());
        store.record(another).unwrap();
        let snapshot = store.list();
        let automatic = snapshot
            .reports
            .iter()
            .find(|report| report.draft.envelope.mode == Mode::Automatic)
            .unwrap();
        assert_eq!(automatic.draft.envelope.diagnostics, vec![detail]);
    }

    #[test]
    fn drain_drops_raw_and_oversized_lines_but_recovers_next_frame() {
        use crate::reporting::{Code, Component, Stage};
        let mut input = b"/Users/alice/private SECRET\n".to_vec();
        input.extend(std::iter::repeat_n(b'x', 50_000));
        input.extend_from_slice(
            b"\nGAMJA_DIAGNOSTIC_V1:{\"code\":\"backend_runtime_failed\",\"stage\":\"runtime\"}\n",
        );
        let mut events = Vec::new();
        drain_stdout(input.as_slice(), |event| events.push(event));
        assert_eq!(
            events,
            vec![(
                Component::Backend,
                Stage::Runtime,
                Code::BackendRuntimeFailed,
                None
            )]
        );
    }

    #[cfg(unix)]
    #[test]
    fn concurrent_pipe_drains_allow_chatty_child_to_exit() {
        let mut child = Command::new("/bin/sh")
            .args([
                "-c",
                "head -c 200000 /dev/zero; head -c 200000 /dev/zero >&2",
            ])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let mut stderr = child.stderr.take().unwrap();
        let first =
            thread::spawn(move || drain_stdout(stdout, |_| panic!("raw output became event")));
        let second = thread::spawn(move || {
            let _ = std::io::copy(&mut stderr, &mut std::io::sink());
        });
        assert!(child.wait().unwrap().success());
        first.join().unwrap();
        second.join().unwrap();
    }

    #[test]
    fn startup_failures_record_fixed_codes() {
        use crate::reporting::{Code, Component, ReportStore, Stage};
        let dir =
            std::env::temp_dir().join(format!("gamja-capture-test-{}", crate::reporting::new_id()));
        let mut store = ReportStore::open(&dir).unwrap();
        for failure in [
            LaunchFailure::Missing,
            LaunchFailure::Spawn,
            LaunchFailure::NotReady,
            LaunchFailure::Exited,
        ] {
            store.record(failure.event()).unwrap();
        }
        let entries = store.list().events;
        assert_eq!(
            entries
                .iter()
                .map(|event| (event.component, event.stage, event.code))
                .collect::<Vec<_>>(),
            vec![
                (Component::Host, Stage::Startup, Code::SidecarMissing),
                (Component::Host, Stage::Startup, Code::SidecarSpawnFailed),
                (Component::Host, Stage::Startup, Code::BackendNotReady),
                (Component::Host, Stage::Startup, Code::SidecarExited),
            ]
        );
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn ready_then_exit_records_host_runtime_event_but_startup_exit_does_not() {
        use crate::reporting::{Code, Component, ReportStore, Stage};
        let dir =
            std::env::temp_dir().join(format!("gamja-ready-test-{}", crate::reporting::new_id()));
        let store = Arc::new(Mutex::new(ReportStore::open(&dir).unwrap()));
        let recorded = store.clone();
        let recorder: FailureRecorder = Arc::new(move |component, stage, code, diagnostic| {
            let mut event = DiagnosticEvent::new(component, stage, code);
            event.diagnostic = diagnostic;
            recorded.lock().unwrap().record(event).unwrap();
        });
        let mut backend = BackendProcess::new();
        let early = Arc::new(Mutex::new(
            Command::new("/bin/sh")
                .args(["-c", "exit 0"])
                .spawn()
                .unwrap(),
        ));
        early.lock().unwrap().wait().unwrap();
        let failure = backend
            .wait_for_ready(early, Instant::now() + Duration::from_secs(1), || false)
            .unwrap_err();
        BackendProcess::record_launch_failure(&recorder, failure);

        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let port = listener.local_addr().unwrap().port();
        let health = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = [0u8; 512];
            stream.read(&mut request).unwrap();
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                .unwrap();
        });
        let ready_child = Arc::new(Mutex::new(
            Command::new("/bin/sh")
                .args(["-c", "sleep 1"])
                .spawn()
                .unwrap(),
        ));
        backend
            .wait_for_ready(
                ready_child.clone(),
                Instant::now() + Duration::from_secs(2),
                || ready(port, "fixture-token"),
            )
            .unwrap();
        backend.start_monitor(ready_child, dir.join("owner-cli.json"), recorder);
        health.join().unwrap();
        if let Some(monitor) = backend.monitor.take() {
            monitor.join().unwrap();
        }
        drop(store);
        let mut reopened = ReportStore::open(&dir).unwrap();
        let tuples: Vec<_> = reopened
            .list()
            .events
            .iter()
            .map(|e| (e.component, e.stage, e.code))
            .collect();
        assert_eq!(
            tuples,
            vec![
                (Component::Host, Stage::Startup, Code::SidecarExited),
                (Component::Host, Stage::Runtime, Code::SidecarExited),
            ]
        );
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn stop_is_bounded_when_detached_descendant_keeps_pipes_open() {
        use std::os::unix::process::CommandExt;
        unsafe extern "C" {
            fn kill(pid: i32, signal: i32) -> i32;
        }
        let pid_file =
            std::env::temp_dir().join(format!("gamja-pipe-pid-{}", crate::reporting::new_id()));
        let python =
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../backend/.venv/bin/python");
        let script = "import os,sys,time\np=os.fork()\nif p:\n for _ in range(200):\n  if os.path.exists(sys.argv[1]): break\n  time.sleep(.01)\n sys.exit(0)\nos.setsid()\nwith open(sys.argv[1]+'.tmp','w') as output: output.write(str(os.getpid()))\nos.replace(sys.argv[1]+'.tmp',sys.argv[1])\ntime.sleep(3)";
        let mut child = Command::new(python)
            .args(["-c", script])
            .arg(&pid_file)
            .process_group(0)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .unwrap();
        let mut backend = BackendProcess::new();
        backend.start_drains(&mut child, Arc::new(|_, _, _, _| {}));
        let owned = Arc::new(Mutex::new(child));
        backend.child = Some(owned.clone());
        let deadline = Instant::now() + Duration::from_secs(2);
        while !pid_file.exists() && Instant::now() < deadline {
            thread::sleep(Duration::from_millis(10));
        }
        assert!(pid_file.exists());
        let escaped: i32 = std::fs::read_to_string(&pid_file).unwrap().parse().unwrap();
        owned.lock().unwrap().wait().unwrap();
        let started = Instant::now();
        backend.stop();
        let elapsed = started.elapsed();
        unsafe {
            kill(escaped, 9);
        }
        std::fs::remove_file(pid_file).unwrap();
        assert!(
            elapsed < Duration::from_millis(800),
            "stop waited for inherited pipe: {elapsed:?}"
        );
    }

    #[cfg(unix)]
    #[test]
    fn stop_reaps_a_sidecars_child_process() {
        use std::os::unix::process::CommandExt;

        let mut launched = Command::new("/bin/sh")
            .args(["-c", "(sleep 1; printf escaped) & printf ready; wait"])
            .process_group(0)
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut output = launched.stdout.take().unwrap();
        let mut greeting = [0; 5];
        output.read_exact(&mut greeting).unwrap();
        assert_eq!(&greeting, b"ready");

        let mut backend = BackendProcess::new();
        backend.child = Some(Arc::new(Mutex::new(launched)));
        backend.stop();

        let mut remainder = Vec::new();
        output.read_to_end(&mut remainder).unwrap();
        assert!(
            remainder.is_empty(),
            "sidecar descendant outlived its owner"
        );
    }

    #[test]
    fn unused_port_is_loopback() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        assert!(listener.local_addr().unwrap().ip().is_loopback());
    }

    #[test]
    fn readiness_rejects_closed_port() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        assert!(!ready(
            listener.local_addr().unwrap().port(),
            "not-a-service"
        ));
    }
}
