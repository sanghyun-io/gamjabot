//! Private, authenticated desktop boundary. The webview has no desktop invoke.

#[path = "resource_scope.rs"]
mod resource_scope;
use resource_scope::{ResourceScopes, ScopeControl, ScopeProof};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use gamja_platform_helper::protocol::{
    OwnerControl, OwnerOperation, PermissionStatus, SettingsPanel,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs::File;
#[cfg(unix)]
use std::fs::OpenOptions;
use std::io::{Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
#[cfg(test)]
use std::sync::Mutex;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[cfg(target_os = "macos")]
#[path = "macos.rs"]
mod macos;
#[cfg(target_os = "windows")]
#[path = "windows.rs"]
mod windows;

#[allow(dead_code)]
pub(crate) fn screen_recording_permission_status() -> &'static str {
    #[cfg(target_os = "macos")]
    {
        return if macos::screen_recording_preflight() {
            "granted"
        } else {
            "not_granted"
        };
    }
    #[cfg(not(target_os = "macos"))]
    {
        "unsupported"
    }
}

const MAX_REQUEST: usize = 1_100_000;
const MAX_RESPONSE: usize = 15_000_000;

#[derive(Debug, Deserialize, Serialize)]
struct Request {
    version: u8,
    request_id: String,
    deadline_ms: u64,
    operation: String,
    room_id: i64,
    agent_id: i64,
    workflow_id: String,
    lease_id: String,
    action_id: Option<String>,
    grant_id: Option<i64>,
    grant_scope: Option<String>,
    payload_digest: Option<String>,
    family: Option<String>,
    arguments: Option<Value>,
    execution_binding: Option<ScopeProof>,
    scope_deadline_ms: Option<u64>,
}

#[derive(Serialize)]
struct Response {
    state: &'static str,
    text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    code: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    png_base64: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    width: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    height: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    permissions: Option<PermissionStatus>,
}

impl Response {
    fn ok(text: &str) -> Self {
        Self {
            state: "completed",
            text: text.into(),
            code: None,
            png_base64: None,
            width: None,
            height: None,
            permissions: None,
        }
    }
    fn fail(code: &'static str, text: &str) -> Self {
        Self {
            state: "failed",
            text: text.into(),
            code: Some(code),
            png_base64: None,
            width: None,
            height: None,
            permissions: None,
        }
    }
    fn uncertain(code: &'static str, text: &str) -> Self {
        Self {
            state: "uncertain",
            text: text.into(),
            code: Some(code),
            png_base64: None,
            width: None,
            height: None,
            permissions: None,
        }
    }
}

trait Driver {
    fn permission_status(&self) -> PermissionStatus {
        PermissionStatus::unsupported()
    }
    fn open_settings(&self, _: SettingsPanel) -> Response {
        Response::fail(
            "native_settings_unavailable",
            "Native settings are unavailable on this platform.",
        )
    }
    fn execute(&self, request: &Request) -> Response;
}

#[derive(Deserialize)]
struct Envelope {
    body: String,
    mac: String,
}

fn mac(key: &[u8; 32], body: &[u8]) -> [u8; 32] {
    let mut inner = [0x36u8; 64];
    let mut outer = [0x5cu8; 64];
    for (index, byte) in key.iter().enumerate() {
        inner[index] ^= byte;
        outer[index] ^= byte;
    }
    let inner_hash = Sha256::new()
        .chain_update(inner)
        .chain_update(body)
        .finalize();
    Sha256::new()
        .chain_update(outer)
        .chain_update(inner_hash)
        .finalize()
        .into()
}

fn valid_mac(expected: &[u8; 32], supplied: &str) -> bool {
    if supplied.len() != 64 || !supplied.is_ascii() {
        return false;
    }
    let mut difference = 0u8;
    for (index, byte) in expected.iter().enumerate() {
        let digits = &supplied[index * 2..index * 2 + 2];
        let Ok(actual) = u8::from_str_radix(digits, 16) else {
            return false;
        };
        difference |= byte ^ actual;
    }
    difference == 0
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

#[derive(Default)]
struct LeaseState {
    scopes: ResourceScopes,
    execution_binding: Option<ScopeProof>,
    scope_deadline_ms: Option<u64>,
    owner: Option<(i64, i64, String, String)>,
    last_released: Option<(i64, i64, String, String)>,
    effect_lock: Option<File>,
    lock_path: Option<PathBuf>,
    requests: HashSet<String>,
    actions: HashSet<String>,
    on_native_failure: Option<Arc<dyn Fn() + Send + Sync>>,
}

impl LeaseState {
    fn with_lock_path(path: PathBuf) -> Self {
        Self {
            lock_path: Some(path),
            ..Self::default()
        }
    }

    fn scope_allows(&self, request: &Request) -> bool {
        match &request.execution_binding {
            Some(proof) => proof.valid_for(request.room_id, request.agent_id)
                && self.scopes.allows(proof)
                && request.scope_deadline_ms.is_some_and(|deadline|deadline > now_ms()),
            None => request.scope_deadline_ms.is_none(),
        }
    }

    /// Final authenticated operation admission, immediately beside the driver.
    /// This is a start deadline, not an in-flight cancellation timer.
    fn execute_guarded(&self, request: &Request, driver: &dyn Driver) -> Response {
        let owner = (request.room_id, request.agent_id, request.workflow_id.clone(), request.lease_id.clone());
        let opaque = matches!(request.family.as_deref(), Some("pointer_click" | "keyboard_text" | "keyboard_chord"));
        if request.deadline_ms <= now_ms()
            || self.owner.as_ref() != Some(&owner)
            || request.execution_binding != self.execution_binding
            || request.scope_deadline_ms != self.scope_deadline_ms
            || !self.scope_allows(request)
            || (opaque && !matches!(request.grant_scope.as_deref(), Some("once" | "room_session")))
        {
            return Response::fail("review_changed", "Native operation start review expired or lease changed.");
        }
        let response = driver.execute(request);
        if response.state != "completed" {
            if let Some(report) = &self.on_native_failure {
                report();
            }
        }
        response
    }

    fn dispatch(
        &mut self,
        key: &[u8; 32],
        input: &[u8],
        driver: &dyn Driver,
    ) -> Result<Response, String> {
        if input.len() > MAX_REQUEST {
            return Err("Native request too large".into());
        }
        let envelope: Envelope =
            serde_json::from_slice(input).map_err(|_| "Invalid native envelope")?;
        let body = STANDARD
            .decode(envelope.body)
            .map_err(|_| "Invalid native body")?;
        if body.len() > MAX_REQUEST || !valid_mac(&mac(key, &body), &envelope.mac) {
            return Err("Native authentication failed".into());
        }
        let control_operation = serde_json::from_slice::<Value>(&body)
            .ok()
            .and_then(|value| {
                value
                    .get("operation")
                    .and_then(Value::as_str)
                    .map(str::to_owned)
            });
        if matches!(
            control_operation.as_deref(),
            Some("permission_status" | "open_settings")
        ) {
            let control: OwnerControl =
                serde_json::from_slice(&body).map_err(|_| "Invalid native control")?;
            let now = now_ms();
            if control.version != 1
                || control.deadline_ms < now
                || control.deadline_ms > now + 15_000
                || control.request_id.len() != 32
                || !control.request_id.bytes().all(|b| b.is_ascii_hexdigit())
                || (control.operation == OwnerOperation::PermissionStatus
                    && control.panel.is_some())
                || (control.operation == OwnerOperation::OpenSettings && control.panel.is_none())
            {
                return Err("Invalid native control scope".into());
            }
            if !self.requests.insert(control.request_id) {
                return Err("Native request replayed".into());
            }
            return Ok(match control.operation {
                OwnerOperation::PermissionStatus => {
                    let mut response = Response::ok("Native permission status checked.");
                    response.permissions = Some(driver.permission_status());
                    response
                }
                OwnerOperation::OpenSettings => driver.open_settings(control.panel.unwrap()),
            });
        }
        // Control and effects share this serial dispatch. A tombstone is
        // retained even without a lease, so a delayed old signed acquire fails.
        let scope_operation=serde_json::from_slice::<Value>(&body).ok()
            .and_then(|value|value.get("operation").and_then(Value::as_str).map(str::to_owned));
        if matches!(scope_operation.as_deref(),Some("scope_capabilities"|"scope_revoke")) {
            let control:ScopeControl=serde_json::from_slice(&body).map_err(|_|"Invalid native scope control")?;
            if !control.valid(now_ms()) || !self.requests.insert(control.request_id.clone()) {
                return Err("Invalid or replayed native scope control".into());
            }
            let mut released=None;
            if control.operation=="scope_revoke" {
                self.scopes.revoke(&control.revocations);
                if self.execution_binding.as_ref().is_some_and(|proof|!self.scopes.allows(proof)) {
                    released=self.owner.as_ref().map(|owner|owner.3.clone());
                    self.last_released=self.owner.take();
                    self.execution_binding=None;
                    self.scope_deadline_ms=None;
                    if let Some(lock)=&mut self.effect_lock { let _=lock.set_len(0); }
                    self.effect_lock=None;
                }
            }
            let text=serde_json::json!({"capability":resource_scope::CAPABILITY,
                "revocations":control.revocations,"released_lease_id":released}).to_string();
            return Ok(Response::ok(&text));
        }
        let request: Request =
            serde_json::from_slice(&body).map_err(|_| "Invalid native request")?;
        let now = now_ms();
        if request.version != 1
            || request.deadline_ms < now
            || request.deadline_ms > now + 15_000
            || request.request_id.len() != 32
            || !request.request_id.bytes().all(|b| b.is_ascii_hexdigit())
            || request.workflow_id.is_empty()
            || request.workflow_id.len() > 200
            || request.lease_id.len() != 32
            || !request.lease_id.bytes().all(|b| b.is_ascii_hexdigit())
            || request.room_id <= 0
            || request.agent_id <= 0
        {
            return Err("Invalid native request scope".into());
        }
        if !self.requests.insert(request.request_id.clone()) {
            return Err("Native request replayed".into());
        }
        let owner = (
            request.room_id,
            request.agent_id,
            request.workflow_id.clone(),
            request.lease_id.clone(),
        );
        match request.operation.as_str() {
            "lease_acquire" => {
                validate_grant_proof(&request)?;
                if !self.scope_allows(&request) { return Err("Native resource scope revoked or expired".into()); }
                if self.owner.is_some() {
                    return Err("Desktop workflow already owned".into());
                }
                if let Some(path) = &self.lock_path {
                    let Some(mut lock) = try_effect_lock(path)? else {
                        return Ok(Response::fail(
                            "desktop_busy",
                            "Desktop is busy in another app.",
                        ));
                    };
                    lock.set_len(0)
                        .map_err(|_| "Cannot clear desktop lock metadata")?;
                    lock.seek(SeekFrom::Start(0))
                        .map_err(|_| "Cannot position desktop lock metadata")?;
                    let metadata = serde_json::json!({"room_id": request.room_id,
                        "agent_id": request.agent_id, "workflow_id": request.workflow_id});
                    lock.write_all(metadata.to_string().as_bytes())
                        .map_err(|_| "Cannot write desktop lock metadata")?;
                    lock.sync_data()
                        .map_err(|_| "Cannot sync desktop lock metadata")?;
                    self.effect_lock = Some(lock);
                }
                self.execution_binding=request.execution_binding.clone();
                self.scope_deadline_ms=request.scope_deadline_ms;
                self.owner = Some(owner);
                Ok(Response::ok("Desktop workflow lease acquired."))
            }
            "lease_release" => {
                if self.owner.is_none() && self.last_released.as_ref() == Some(&owner) {
                    return Ok(Response::ok("Desktop workflow lease already released."));
                }
                if self.owner.as_ref() != Some(&owner) {
                    return Err("Desktop lease owner mismatch".into());
                }
                if request.execution_binding != self.execution_binding {
                    return Err("Desktop lease scope mismatch".into());
                }
                self.last_released = Some(owner);
                self.execution_binding=None;
                self.scope_deadline_ms=None;
                self.owner = None;
                if let Some(lock) = &mut self.effect_lock {
                    let _ = lock.set_len(0);
                }
                self.effect_lock = None;
                Ok(Response::ok("Desktop workflow lease released."))
            }
            "action" => {
                validate_grant_proof(&request)?;
                if self.owner.as_ref() != Some(&owner) {
                    return Err("Desktop lease owner mismatch".into());
                }
                let action_id = request.action_id.as_ref().unwrap();
                if !self.actions.insert(action_id.clone()) {
                    return Err("Native action replayed".into());
                }
                validate_action(&request)?;
                Ok(self.execute_guarded(&request, driver))
            }
            _ => Err("Unsupported native operation".into()),
        }
    }
}

fn validate_grant_proof(request: &Request) -> Result<(), String> {
    if request
        .action_id
        .as_ref()
        .map_or(true, |value| value.is_empty() || value.len() > 64)
        || request.payload_digest.as_ref().map_or(true, |value| {
            value.len() != 64 || !value.bytes().all(|b| b.is_ascii_hexdigit())
        })
        || !matches!(
            request.grant_scope.as_deref(),
            Some("once" | "room_session" | "broad")
        )
        || (request.grant_scope.as_deref() != Some("broad") && request.grant_id.is_none())
    {
        return Err("Missing broker grant proof".into());
    }
    Ok(())
}

fn validate_action(request: &Request) -> Result<(), String> {
    let args = request
        .arguments
        .as_ref()
        .and_then(Value::as_object)
        .ok_or("Invalid native arguments")?;
    let allowed: &[&str] = match request.family.as_deref() {
        Some("screenshot") => &[],
        Some("pointer_click" | "pointer_move") => &["x", "y"],
        Some("pointer_scroll") => &["x", "y", "delta"],
        Some("keyboard_text") => &["text"],
        Some("keyboard_chord") => &["keys"],
        _ => return Err("Unsupported native family".into()),
    };
    if args.len() != allowed.len() || !allowed.iter().all(|key| args.contains_key(*key)) {
        return Err("Invalid native arguments".into());
    }
    Ok(())
}

#[cfg(unix)]
fn try_effect_lock(path: &Path) -> Result<Option<File>, String> {
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|_| "Cannot create desktop lock directory")?;
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))
            .map_err(|_| "Cannot protect desktop lock directory")?;
    }
    if path
        .symlink_metadata()
        .is_ok_and(|metadata| metadata.file_type().is_symlink())
    {
        return Err("Desktop lock path is a symlink".into());
    }
    let file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|_| "Cannot open desktop effect lock")?;
    file.set_permissions(std::fs::Permissions::from_mode(0o600))
        .map_err(|_| "Cannot protect desktop effect lock")?;
    extern "C" {
        fn flock(fd: i32, operation: i32) -> i32;
    }
    // LOCK_EX | LOCK_NB; dropping File releases it after the synchronous effect.
    if unsafe { flock(file.as_raw_fd(), 2 | 4) } != 0 {
        return Ok(None);
    }
    Ok(Some(file))
}

#[cfg(target_os = "windows")]
fn try_effect_lock(path: &Path) -> Result<Option<File>, String> {
    windows::try_effect_lock(path)
}

fn effect_lock_path() -> PathBuf {
    #[cfg(target_os = "windows")]
    let root = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    #[cfg(not(target_os = "windows"))]
    let root = std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
        .join("Library/Application Support");
    root.join("Gamja Bot/desktop-workflow.native.lock")
}

#[cfg(unix)]
#[derive(Clone, Copy, Debug)]
enum TransportFailure {
    StreamConfiguration,
    ResponseSerialization,
    ResponseTooLarge,
    ResponseHeaderWrite,
    ResponseBodyWrite,
}

#[cfg(unix)]
fn record_transport_failure(failure: TransportFailure) {
    let message: &[u8] = match failure {
        TransportFailure::StreamConfiguration => b"gamja_native_stream_configuration_failed\0",
        TransportFailure::ResponseSerialization => b"gamja_native_response_serialization_failed\0",
        TransportFailure::ResponseTooLarge => b"gamja_native_response_too_large\0",
        TransportFailure::ResponseHeaderWrite => b"gamja_native_response_header_write_failed\0",
        TransportFailure::ResponseBodyWrite => b"gamja_native_response_body_write_failed\0",
    };
    // Fixed codes only: never place request, response, path, or screen data in logs.
    unsafe { libc::syslog(libc::LOG_ERR, message.as_ptr().cast()) };
}

#[cfg(unix)]
fn configure_stream(stream: &std::os::unix::net::UnixStream) -> Result<(), TransportFailure> {
    // macOS accept(2) inherits O_NONBLOCK from this server's polling listener.
    // A screenshot response exceeds the socket buffer, so blocking framed I/O
    // with bounded timeouts is required after each accepted connection.
    stream
        .set_nonblocking(false)
        .and_then(|_| stream.set_read_timeout(Some(Duration::from_millis(100))))
        .and_then(|_| stream.set_write_timeout(Some(Duration::from_secs(12))))
        .map_err(|_| TransportFailure::StreamConfiguration)
}

#[cfg(unix)]
fn write_response(
    stream: &mut std::os::unix::net::UnixStream,
    response: &Response,
) -> Result<(), TransportFailure> {
    use std::io::Write;
    let bytes =
        serde_json::to_vec(response).map_err(|_| TransportFailure::ResponseSerialization)?;
    if bytes.len() > MAX_RESPONSE {
        return Err(TransportFailure::ResponseTooLarge);
    }
    stream
        .write_all(&(bytes.len() as u32).to_be_bytes())
        .map_err(|_| TransportFailure::ResponseHeaderWrite)?;
    stream
        .write_all(&bytes)
        .map_err(|_| TransportFailure::ResponseBodyWrite)
}

pub struct DesktopServer {
    endpoint: String,
    key: [u8; 32],
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
    #[cfg(unix)]
    listener: Option<std::os::unix::net::UnixListener>,
    #[cfg(unix)]
    directory: PathBuf,
}

impl DesktopServer {
    pub fn bind(endpoint: String, key: [u8; 32]) -> Result<Self, String> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let path = PathBuf::from(&endpoint);
            let directory = path
                .parent()
                .ok_or("Missing private IPC directory")?
                .to_path_buf();
            use std::os::unix::fs::MetadataExt;
            let metadata = std::fs::symlink_metadata(&directory)
                .map_err(|_| "Missing private IPC directory")?;
            if !metadata.is_dir()
                || metadata.uid() != unsafe { libc::geteuid() }
                || metadata.mode() & 0o777 != 0o700
            {
                return Err("Unprotected private IPC directory".into());
            }
            let listener = (|| {
                std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700))
                    .map_err(|_| "Cannot protect private IPC directory")?;
                let listener = std::os::unix::net::UnixListener::bind(&path)
                    .map_err(|_| "Cannot bind private desktop socket")?;
                std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))
                    .map_err(|_| "Cannot protect private desktop socket")?;
                listener
                    .set_nonblocking(true)
                    .map_err(|_| "Cannot configure desktop socket")?;
                Ok::<_, String>(listener)
            })();
            let listener = match listener {
                Ok(listener) => listener,
                Err(error) => {
                    let _ = std::fs::remove_file(&path);
                    let _ = std::fs::remove_dir(&directory);
                    return Err(error);
                }
            };
            Ok(Self {
                endpoint: path.to_string_lossy().into_owned(),
                key,
                stop: Arc::new(AtomicBool::new(false)),
                worker: None,
                listener: Some(listener),
                directory,
            })
        }
        #[cfg(target_os = "windows")]
        {
            Ok(Self {
                endpoint,
                key,
                stop: Arc::new(AtomicBool::new(false)),
                worker: None,
            })
        }
    }

    pub fn endpoint(&self) -> &str {
        &self.endpoint
    }

    pub fn start(&mut self) -> Result<(), String> {
        let stop = self.stop.clone();
        let key = self.key;
        #[cfg(unix)]
        let listener = self
            .listener
            .take()
            .ok_or("Desktop socket already started")?;
        #[cfg(unix)]
        {
            self.worker = Some(thread::spawn(move || {
                let mut state = LeaseState::with_lock_path(effect_lock_path());
                #[cfg(target_os = "macos")]
                let driver = macos::MacDriver;
                #[cfg(not(target_os = "macos"))]
                let driver = UnsupportedDriver;
                while !stop.load(Ordering::SeqCst) {
                    match listener.accept() {
                        Ok((mut stream, _)) => {
                            if let Err(failure) = configure_stream(&stream) {
                                record_transport_failure(failure);
                                continue;
                            }
                            let response = (|| {
                                let mut header = [0u8; 4];
                                read_frame(&mut stream, &mut header, &stop)?;
                                let size = u32::from_be_bytes(header) as usize;
                                if size > MAX_REQUEST {
                                    return Err("Native frame too large".into());
                                }
                                let mut input = vec![0; size];
                                read_frame(&mut stream, &mut input, &stop)?;
                                state.dispatch(&key, &input, &driver)
                            })()
                            .unwrap_or_else(|_| {
                                Response::fail("native_request_denied", "Native request denied.")
                            });
                            if let Err(failure) = write_response(&mut stream, &response) {
                                record_transport_failure(failure);
                            }
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(50))
                        }
                        Err(_) => break,
                    }
                }
                // The driver is synchronous. An in-flight call has finished
                // before this owner lock is dropped after sidecar death.
                drop(state);
            }));
            Ok(())
        }
        #[cfg(target_os = "windows")]
        {
            let endpoint = self.endpoint.clone();
            let (ready_tx, ready_rx) = std::sync::mpsc::channel();
            self.worker = Some(thread::spawn(move || {
                windows::serve(&endpoint, key, stop, ready_tx)
            }));
            if ready_rx.recv_timeout(Duration::from_secs(2)) != Ok(true) {
                self.stop();
                return Err("Cannot start private desktop pipe".into());
            }
            Ok(())
        }
    }

    pub fn stop_signal(&self) -> Arc<AtomicBool> {
        self.stop.clone()
    }

    pub fn stop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
        #[cfg(unix)]
        {
            let _ = std::fs::remove_file(&self.endpoint);
            let _ = std::fs::remove_dir(&self.directory);
        }
    }
}

impl Drop for DesktopServer {
    fn drop(&mut self) {
        self.stop();
    }
}

#[cfg(test)]
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(test)]
struct FakeDriver;
#[cfg(test)]
impl Driver for FakeDriver {
    fn execute(&self, _: &Request) -> Response {
        Response::ok("synthetic")
    }
}
#[cfg(test)]
fn test_request(operation: &str, room_id: i64, action_id: &str, lease_id: &str) -> Request {
    Request {
        version: 1,
        request_id: hex(&rand_test_id()),
        deadline_ms: now_ms() + 10_000,
        operation: operation.into(),
        room_id,
        agent_id: 2,
        workflow_id: "workflow".into(),
        lease_id: lease_id.into(),
        action_id: Some(action_id.into()),
        grant_id: Some(1),
        grant_scope: Some("once".into()),
        payload_digest: Some("a".repeat(64)),
        family: Some("screenshot".into()),
        arguments: Some(serde_json::json!({})),
        execution_binding: None,
        scope_deadline_ms: None,
    }
}
#[cfg(test)]
fn rand_test_id() -> [u8; 16] {
    let mut id = [0; 16];
    getrandom::fill(&mut id).unwrap();
    id
}
#[cfg(test)]
fn sign_for_test(key: &[u8; 32], request: &Request) -> Vec<u8> {
    let body = serde_json::to_vec(request).unwrap();
    serde_json::to_vec(
        &serde_json::json!({"body": STANDARD.encode(&body), "mac": hex(&mac(key, &body))}),
    )
    .unwrap()
}

#[cfg(test)]
#[path = "desktop_tests.rs"]
mod tests;

#[cfg(test)]
mod diagnostic_tests {
    use super::*;

    struct FailingDriver;
    impl Driver for FailingDriver {
        fn execute(&self, _: &Request) -> Response {
            Response::fail("driver_error", "secret /Users/alice/private")
        }
    }

    #[test]
    fn native_driver_failure_uses_typed_callback_only() {
        let key = [13u8; 32];
        let count = Arc::new(Mutex::new(0));
        let notified = count.clone();
        let mut state = LeaseState::default();
        state.on_native_failure = Some(Arc::new(move || {
            *notified.lock().unwrap() += 1;
        }));
        let lease = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
        let acquire = sign_for_test(&key, &test_request("lease_acquire", 1, "a1", lease));
        state.dispatch(&key, &acquire, &FailingDriver).unwrap();
        let action = sign_for_test(&key, &test_request("action", 1, "a2", lease));
        assert_eq!(
            state.dispatch(&key, &action, &FailingDriver).unwrap().state,
            "failed"
        );
        assert_eq!(*count.lock().unwrap(), 1);
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn accepted_stream_delivers_maximum_screenshot_frame() {
        use std::io::Read;
        use std::os::unix::{fs::PermissionsExt, net::UnixStream};

        struct Fixture(PathBuf);
        impl Drop for Fixture {
            fn drop(&mut self) {
                let _ = std::fs::remove_file(self.0.join("desktop.sock"));
                let _ = std::fs::remove_dir(&self.0);
            }
        }

        // Keep the Unix socket path below macOS's SUN_LEN even when the
        // process temp directory expands to a long /var/folders path.
        let root = PathBuf::from("/tmp").join(format!(
            "gjlf-{}-{}",
            std::process::id(),
            &hex(&rand_test_id())[..8]
        ));
        std::fs::create_dir(&root).unwrap();
        std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
        let fixture = Fixture(root);
        let endpoint = fixture.0.join("desktop.sock");
        let listener = std::os::unix::net::UnixListener::bind(&endpoint).unwrap();
        listener.set_nonblocking(true).unwrap();
        let client = std::thread::spawn(move || {
            let mut stream = UnixStream::connect(endpoint).unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(15)))
                .unwrap();
            let mut header = [0u8; 4];
            stream.read_exact(&mut header).unwrap();
            let length = u32::from_be_bytes(header) as usize;
            assert!(length <= MAX_RESPONSE);
            let mut body = vec![0; length];
            stream.read_exact(&mut body).unwrap();
            serde_json::from_slice::<serde_json::Value>(&body).unwrap()
        });
        let (mut stream, _) = loop {
            match listener.accept() {
                Ok(accepted) => break accepted,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::yield_now()
                }
                Err(error) => panic!("accept failed: {error}"),
            }
        };
        configure_stream(&stream).unwrap();
        let response = Response {
            state: "completed",
            text: "synthetic".into(),
            code: None,
            png_base64: Some("a".repeat(13_333_336)),
            width: Some(1),
            height: Some(1),
            permissions: None,
        };
        write_response(&mut stream, &response).unwrap();
        drop(stream);
        let value = client.join().unwrap();
        assert_eq!(value["state"], "completed");
        assert_eq!(value["png_base64"].as_str().unwrap().len(), 13_333_336);
    }
}

#[cfg(unix)]
fn read_frame(
    stream: &mut std::os::unix::net::UnixStream,
    output: &mut [u8],
    stop: &AtomicBool,
) -> Result<(), String> {
    use std::io::Read;
    let deadline = std::time::Instant::now() + Duration::from_secs(12);
    let mut offset = 0;
    while offset < output.len() {
        if stop.load(Ordering::SeqCst) || std::time::Instant::now() >= deadline {
            return Err("Native frame stopped or expired".into());
        }
        match stream.read(&mut output[offset..]) {
            Ok(0) => return Err("Incomplete native frame".into()),
            Ok(count) => offset += count,
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::TimedOut
                        | std::io::ErrorKind::WouldBlock
                        | std::io::ErrorKind::Interrupted
                ) => {}
            Err(_) => return Err("Invalid native frame".into()),
        }
    }
    Ok(())
}
#[cfg(all(unix, not(target_os = "macos")))]
struct UnsupportedDriver;
#[cfg(all(unix, not(target_os = "macos")))]
impl Driver for UnsupportedDriver {
    fn execute(&self, _: &Request) -> Response {
        Response::fail(
            "native_capability_unavailable",
            "Native desktop is unavailable on this platform.",
        )
    }
}

#[cfg(test)]
mod root_scope_dispatch_tests {
    use super::*;
    #[test]
    fn revoked_ancestor_blocks_delayed_acquire_and_action_but_keeps_sibling() {
        let key=[31u8;32];
        let root="10000000-0000-4000-8000-000000000000";
        let b="20000000-0000-4000-8000-000000000000";
        let c="30000000-0000-4000-8000-000000000000";
        let d="40000000-0000-4000-8000-000000000000";
        let proof=|ids:Vec<&str>| serde_json::from_value::<ScopeProof>(serde_json::json!({
            "version":1,"root_scope_id":root,"scope_id":ids.last().unwrap(),"owner_id":1,"root_room_id":1,
            "root_request_event_id":root,"execution_room_id":2,"agent_id":2,"run_generation":1,
            "job_id":c,"phase":"response","context_mode":"delegated","deadline_at":"2035-01-01T00:00:00+00:00",
            "lineage":ids.iter().map(|id|serde_json::json!({"scope_id":id,"epoch":0})).collect::<Vec<_>>() })).unwrap();
        let sign=|value:serde_json::Value| { let body=serde_json::to_vec(&value).unwrap();
            serde_json::to_vec(&serde_json::json!({"body":STANDARD.encode(&body),"mac":hex(&mac(&key,&body))})).unwrap() };
        let mut state=LeaseState::default();
        // No lease is present: the tombstone must still be retained.
        let revoke=sign(serde_json::json!({"version":1,"request_id":hex(&rand_test_id()),"deadline_ms":now_ms()+10_000,
            "operation":"scope_revoke","revocations":[{"scope_id":b,"epoch":1}]}));
        assert_eq!(state.dispatch(&key,&revoke,&FakeDriver).unwrap().state,"completed");
        let mut late=test_request("lease_acquire",2,"late","aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
        late.execution_binding=Some(proof(vec![root,b,c]));late.scope_deadline_ms=Some(now_ms()+10_000);
        assert!(state.dispatch(&key,&sign_for_test(&key,&late),&FakeDriver).is_err());
        let mut sibling=test_request("lease_acquire",2,"sibling","bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
        sibling.execution_binding=Some(proof(vec![root,d]));sibling.scope_deadline_ms=Some(now_ms()+10_000);
        assert_eq!(state.dispatch(&key,&sign_for_test(&key,&sibling),&FakeDriver).unwrap().state,"completed");
        let mut changed=test_request("action",2,"changed","bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");
        changed.execution_binding=sibling.execution_binding.clone();changed.execution_binding.as_mut().unwrap().run_generation=2;
        changed.scope_deadline_ms=sibling.scope_deadline_ms;
        assert_eq!(state.dispatch(&key,&sign_for_test(&key,&changed),&FakeDriver).unwrap().state,"failed");
    }
}
