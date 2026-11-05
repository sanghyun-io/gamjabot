//! Private, authenticated desktop boundary. The webview has no desktop invoke.

#[path = "../../../../platform-helper/src/resource_scope.rs"]
mod resource_scope;
use resource_scope::{ResourceScopes, ScopeControl, ScopeProof};

use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs::{File, OpenOptions};
use std::io::{Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::process::Child;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::thread::{self, JoinHandle};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[cfg(target_os = "macos")]
mod macos;
#[cfg(target_os = "windows")]
mod windows;

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
        }
    }
}

trait Driver {
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
    if supplied.len() != 64 {
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
    use std::os::unix::fs::PermissionsExt;
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
        .open(path)
        .map_err(|_| "Cannot open desktop effect lock")?;
    std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600))
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
    pub fn bind() -> Result<Self, String> {
        let mut key = [0u8; 32];
        getrandom::fill(&mut key).map_err(|_| "Cannot create desktop IPC nonce")?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let mut suffix = [0u8; 16];
            getrandom::fill(&mut suffix).map_err(|_| "Cannot create private IPC path")?;
            let directory = PathBuf::from("/tmp").join(format!("gamja-desktop-{}", hex(&suffix)));
            std::fs::create_dir(&directory).map_err(|_| "Cannot create private IPC directory")?;
            let path = directory.join("desktop.sock");
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
                endpoint: format!(r"\\.\pipe\gamja-desktop-{}", hex(&key[..16])),
                key,
                stop: Arc::new(AtomicBool::new(false)),
                worker: None,
            })
        }
    }

    pub fn endpoint(&self) -> &str {
        &self.endpoint
    }
    pub fn key_hex(&self) -> String {
        hex(&self.key)
    }

    pub fn start(&mut self, child: Arc<Mutex<Child>>, app: tauri::AppHandle) -> Result<(), String> {
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
                use std::io::{Read, Write};
                let mut state = LeaseState::with_lock_path(effect_lock_path());
                state.on_native_failure = Some(Arc::new(move || {
                    let _ = crate::reporting::record_typed_failure(
                        &app,
                        crate::reporting::Component::Native,
                        crate::reporting::Stage::Tool,
                        crate::reporting::Code::NativeActionFailed,
                    );
                }));
                let driver = macos::MacDriver;
                while !stop.load(Ordering::SeqCst) {
                    if child
                        .lock()
                        .is_ok_and(|mut owned| owned.try_wait().ok().flatten().is_some())
                    {
                        break;
                    }
                    match listener.accept() {
                        Ok((mut stream, _)) => {
                            let _ = stream.set_read_timeout(Some(Duration::from_secs(12)));
                            let _ = stream.set_write_timeout(Some(Duration::from_secs(12)));
                            let response = (|| {
                                let mut header = [0u8; 4];
                                stream
                                    .read_exact(&mut header)
                                    .map_err(|_| "Invalid native frame")?;
                                let size = u32::from_be_bytes(header) as usize;
                                if size > MAX_REQUEST {
                                    return Err("Native frame too large".into());
                                }
                                let mut input = vec![0; size];
                                stream
                                    .read_exact(&mut input)
                                    .map_err(|_| "Incomplete native frame")?;
                                if child.lock().is_ok_and(|mut owned| {
                                    owned.try_wait().ok().flatten().is_some()
                                }) {
                                    return Ok(Response::fail(
                                        "backend_stopped",
                                        "Owned backend stopped before desktop dispatch.",
                                    ));
                                }
                                state.dispatch(&key, &input, &driver)
                            })()
                            .unwrap_or_else(|_| {
                                Response::fail("native_request_denied", "Native request denied.")
                            });
                            if let Ok(bytes) = serde_json::to_vec(&response) {
                                if bytes.len() <= MAX_RESPONSE {
                                    let _ = stream.write_all(&(bytes.len() as u32).to_be_bytes());
                                    let _ = stream.write_all(&bytes);
                                }
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
            self.worker = Some(thread::spawn(move || {
                windows::serve(&endpoint, key, stop, child, app)
            }));
            Ok(())
        }
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
