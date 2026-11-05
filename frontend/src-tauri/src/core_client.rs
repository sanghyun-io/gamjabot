//! Native attach-only client. Credentials never enter the webview except for the UI token.
//! This type owns no runtime handle, lifeline, lock file, or shutdown operation.
use gamja_core_host::{
    control, platform, ControlRequest, EndpointFile, CORE_PROTOCOL_VERSION, ENDPOINT_FILE_VERSION,
    ENGINE_PROTOCOL_VERSION, MAX_CONTROL_BYTES,
};
use serde::Deserialize;
use std::{
    ffi::OsStr,
    fs,
    io::Read,
    os::fd::AsRawFd,
    path::{Path, PathBuf},
    sync::Mutex,
    time::Duration,
};

// The canonical CLI allows 30 seconds for engine startup and four for attachment.
// This bounds only the launcher; the authenticated detached Core owns its lifetime.
const LAUNCHER_TIMEOUT: Duration = Duration::from_secs(40);

fn run_core_launcher(executable: &Path, app_data: &Path, timeout: Duration) -> Result<(), String> {
    use gamja_core_host::macos_spawn::{inherited_environment, OwnedChild, SpawnError};
    let input = fs::File::open("/dev/null").map_err(|_| "core_start_unavailable")?;
    let output = fs::OpenOptions::new()
        .write(true)
        .open("/dev/null")
        .map_err(|_| "core_start_unavailable")?;
    let error = fs::OpenOptions::new()
        .write(true)
        .open("/dev/null")
        .map_err(|_| "core_start_unavailable")?;
    let env = inherited_environment().map_err(|_| "core_start_unavailable")?;
    let args = [
        OsStr::new("start"),
        OsStr::new("--if-needed"),
        OsStr::new("--app-data"),
        app_data.as_os_str(),
    ];
    let mut child = OwnedChild::spawn(
        executable,
        &args,
        env,
        &[
            (input.as_raw_fd(), 0),
            (output.as_raw_fd(), 1),
            (error.as_raw_fd(), 2),
        ],
        timeout,
    )
    .map_err(|_| "core_start_unavailable")?;
    let status = child.wait_timeout(timeout).map_err(|err| match err {
        SpawnError::Timeout => "core_start_timeout",
        _ => "core_start_failed",
    })?;
    if status.success() {
        Ok(())
    } else {
        Err("core_start_failed".into())
    }
}
pub struct CoreClient {
    app_data: PathBuf,
    executable: PathBuf,
    bootstrap_error: Option<&'static str>,
    verify_fixed_runtime: bool,
    attach_gate: Mutex<()>,
}

enum EndpointState {
    Missing,
    Current(EndpointFile),
    LegacyStale,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LegacyEndpoint {
    base_url: String,
    capability: String,
}

impl CoreClient {
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub fn vault_begin(&self, operation:&str, owner:u64, epoch:&str, item:&str, revision:Option<u64>, command:Option<&str>, website:Option<&str>, username:Option<&str>) -> Result<gamja_core_host::vault_control::NativeExchange,String> {
        if !self.verify_fixed_runtime || self.bootstrap_error.is_some() {return Err("vault_unavailable".into());}
        crate::core_runtime::verify_executable(&self.executable).map_err(|_| "vault_unavailable".to_string())?;
        gamja_core_host::vault_control::begin_native(&self.app_data,&self.executable,operation,owner,epoch,item,revision,command,website,username).map_err(|_| "vault_unavailable".to_string())
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub fn vault_finish(&self, exchange:gamja_core_host::vault_control::NativeExchange, secret:Option<gamja_core_host::OwnerSecret>) -> Result<serde_json::Value,String> {
        if !self.verify_fixed_runtime || self.bootstrap_error.is_some() {return Err("vault_unavailable".into());}
        crate::core_runtime::verify_executable(&self.executable).map_err(|_| "vault_unavailable".to_string())?;
        let status=exchange.finish_native(secret).map_err(|_| "vault_unavailable".to_string())?;
        serde_json::to_value(status).map_err(|_| "vault_unavailable".to_string())
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub fn vault_begin_card(&self,operation:&str,owner:u64,epoch:&str,item:&str,revision:Option<u64>,command:Option<&str>,metadata:Option<gamja_core_host::vault_card::CardMetadata>,website:Option<&str>)->Result<gamja_core_host::vault_control::NativeExchange,String> {
        if !self.verify_fixed_runtime || self.bootstrap_error.is_some(){return Err("vault_unavailable".into());}
        crate::core_runtime::verify_executable(&self.executable).map_err(|_| "vault_unavailable".to_string())?;
        gamja_core_host::vault_control::begin_card(&self.app_data,&self.executable,operation,owner,epoch,item,revision,command,metadata,website).map_err(|_| "vault_unavailable".to_string())
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub fn vault_finish_card(&self,exchange:gamja_core_host::vault_control::NativeExchange,input:Option<gamja_core_host::vault_card::NativeCardSecret>)->Result<serde_json::Value,String> {
        if !self.verify_fixed_runtime || self.bootstrap_error.is_some(){return Err("vault_unavailable".into());}
        crate::core_runtime::verify_executable(&self.executable).map_err(|_| "vault_unavailable".to_string())?;
        exchange.finish_card(input).map_err(|_| "vault_unavailable".to_string())
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub fn vault_begin_profile(&self,operation:&str,owner:u64,epoch:&str,handoff:&str)->Result<gamja_core_host::vault_control::ProfileExchange,String> {
        if !self.verify_fixed_runtime || self.bootstrap_error.is_some(){return Err("vault_unavailable".into());}
        crate::core_runtime::verify_executable(&self.executable).map_err(|_|"vault_unavailable".to_string())?;
        gamja_core_host::vault_control::begin_profile(&self.app_data,&self.executable,operation,owner,epoch,handoff).map_err(|_|"vault_unavailable".to_string())
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub fn vault_finish_profile(&self,exchange:gamja_core_host::vault_control::ProfileExchange)->Result<serde_json::Value,String> {
        if !self.verify_fixed_runtime || self.bootstrap_error.is_some(){return Err("vault_unavailable".into());}
        crate::core_runtime::verify_executable(&self.executable).map_err(|_|"vault_unavailable".to_string())?;
        exchange.finish().map_err(|_|"vault_unavailable".to_string())
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    #[allow(clippy::too_many_arguments)]
    pub fn vault_begin_csv(&self,operation:&str,owner:u64,epoch:&str,draft:Option<&str>,row:Option<u64>,offset:Option<u64>,limit:Option<u64>,item:Option<&str>,command:Option<&str>)->Result<gamja_core_host::vault_control::NativeExchange,String> {
        if !self.verify_fixed_runtime || self.bootstrap_error.is_some(){return Err("vault_unavailable".into());}
        crate::core_runtime::verify_executable(&self.executable).map_err(|_| "vault_unavailable".to_string())?;
        gamja_core_host::vault_control::begin_csv(&self.app_data,&self.executable,operation,owner,epoch,draft,row,offset,limit,item,command).map_err(|_| "vault_unavailable".to_string())
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))]
    pub fn vault_finish_csv(&self,exchange:gamja_core_host::vault_control::NativeExchange,input:Option<gamja_core_host::vault_csv_import::OwnedCsvInput>)->Result<serde_json::Value,String> {
        if !self.verify_fixed_runtime || self.bootstrap_error.is_some(){return Err("vault_unavailable".into());}
        crate::core_runtime::verify_executable(&self.executable).map_err(|_| "vault_unavailable".to_string())?;
        exchange.finish_csv(input).map_err(|_| "vault_unavailable".to_string())
    }
    pub fn new(app_data: PathBuf, executable: PathBuf) -> Self {
        Self {
            app_data,
            executable,
            bootstrap_error: None,
            verify_fixed_runtime: false,
            attach_gate: Mutex::new(()),
        }
    }

    pub fn bootstrap(app_data: PathBuf, resource_dir: &Path) -> Self {
        let embedded = resource_dir.join("gamja-core");
        match crate::core_runtime::ensure(&embedded) {
            Ok(executable) => Self {
                app_data,
                executable,
                bootstrap_error: None,
                verify_fixed_runtime: true,
                attach_gate: Mutex::new(()),
            },
            Err(error) => Self {
                app_data,
                executable: PathBuf::new(),
                bootstrap_error: Some(error),
                verify_fixed_runtime: true,
                attach_gate: Mutex::new(()),
            },
        }
    }

    fn runtime_ready(&self) -> Result<(), String> {
        if let Some(error) = self.bootstrap_error {
            return Err(error.into());
        }
        if self.verify_fixed_runtime {
            crate::core_runtime::verify_executable(&self.executable).map_err(str::to_owned)?;
        }
        Ok(())
    }
    pub fn endpoint(&self) -> Result<crate::local_endpoint::LocalEndpoint, String> {
        self.runtime_ready()?;
        let _guard = self
            .attach_gate
            .lock()
            .map_err(|_| "core_attach_unavailable")?;
        self.start_if_needed()?;
        self.create_ui_session()
    }

    pub fn report(&self, request: ControlRequest) -> Result<serde_json::Value, String> {
        self.runtime_ready()?;
        request.validate()?;
        let _guard = self
            .attach_gate
            .lock()
            .map_err(|_| "core_attach_unavailable")?;
        // A diagnostic-only host is still the canonical reporting owner.
        if let Err(error) = self.start_if_needed() {
            if error != "engine_unavailable" && error != "core_start_failed" {
                return Err(error);
            }
        }
        let endpoint = self.read_endpoint()?.ok_or("endpoint_unavailable")?;
        let health =
            control::request(&endpoint, &ControlRequest::Health {}).map_err(str::to_owned)?;
        let core = health["core_version"]
            .as_u64()
            .and_then(|v| u16::try_from(v).ok())
            .ok_or("invalid_core_health")?;
        let engine = health["engine_version"]
            .as_u64()
            .and_then(|v| u16::try_from(v).ok())
            .ok_or("invalid_core_health")?;
        validate_versions(core, engine)?;
        control::request(&endpoint, &request).map_err(str::to_owned)
    }

    fn slack_error(error: &'static str) -> String {
        match error { "slack_credential_changed" | "slack_credential_invalid" => error, _ => "slack_credential_unavailable" }.to_owned()
    }
    pub fn slack_credential_status(&self, owner_id:u64) -> Result<serde_json::Value,String> {
        self.runtime_ready().map_err(|_| "slack_credential_unavailable".to_string())?;
        gamja_core_host::cli::CoreClient { app_data:self.app_data.clone() }.slack_credential_status(owner_id).map_err(Self::slack_error)
    }
    pub fn slack_credential_store(&self, owner_id:u64, owner_epoch:String, core_epoch:String, account_id:String, expected_revision:u64, operation_id:String, app_token:gamja_core_host::OwnerSecret, bot_token:gamja_core_host::OwnerSecret) -> Result<serde_json::Value,String> {
        self.runtime_ready().map_err(|_| "slack_credential_unavailable".to_string())?;
        gamja_core_host::cli::CoreClient { app_data:self.app_data.clone() }.slack_credential_store(owner_id,owner_epoch,core_epoch,account_id,expected_revision,operation_id,app_token,bot_token).map_err(Self::slack_error)
    }
    pub fn slack_credential_delete(&self, owner_id:u64, owner_epoch:String, core_epoch:String, account_id:String, expected_revision:u64, operation_id:String) -> Result<serde_json::Value,String> {
        self.runtime_ready().map_err(|_| "slack_credential_unavailable".to_string())?;
        gamja_core_host::cli::CoreClient { app_data:self.app_data.clone() }.slack_credential_delete(owner_id,owner_epoch,core_epoch,account_id,expected_revision,operation_id).map_err(Self::slack_error)
    }

    pub fn store_mcp_secret(&self, server_id: String, expected_revision: u64, operation_id: String, secret: gamja_core_host::OwnerSecret) -> Result<serde_json::Value, String> {
        self.runtime_ready()?;
        gamja_core_host::cli::CoreClient { app_data: self.app_data.clone() }
            .store_mcp_secret(server_id, expected_revision, operation_id, secret)
            .map_err(|_| "mcp_secret_unavailable".to_string())
    }

    pub fn mcp_secret_status(&self, server_id: String, expected_revision: u64, operation_id: String) -> Result<serde_json::Value, String> {
        self.runtime_ready()?;
        gamja_core_host::cli::CoreClient { app_data: self.app_data.clone() }
            .mcp_secret_status(server_id, expected_revision, operation_id)
            .map_err(|_| "mcp_secret_unavailable".to_string())
    }

    pub fn mcp_oauth_begin(&self, server_id: String, expected_revision: u64, operation_id: String) -> Result<serde_json::Value, String> {
        self.runtime_ready()?;
        gamja_core_host::cli::CoreClient { app_data: self.app_data.clone() }.mcp_oauth_begin(server_id, expected_revision, operation_id).map_err(|_| "mcp_oauth_unavailable".to_string())
    }
    pub fn mcp_oauth_status(&self, server_id: String, expected_revision: u64, operation_id: String) -> Result<serde_json::Value, String> {
        self.runtime_ready()?;
        gamja_core_host::cli::CoreClient { app_data: self.app_data.clone() }.mcp_oauth_status(server_id, expected_revision, operation_id).map_err(|_| "mcp_oauth_unavailable".to_string())
    }
    pub fn mcp_oauth_cancel(&self, server_id: String, expected_revision: u64, operation_id: String) -> Result<serde_json::Value, String> {
        self.runtime_ready()?;
        gamja_core_host::cli::CoreClient { app_data: self.app_data.clone() }.mcp_oauth_cancel(server_id, expected_revision, operation_id).map_err(|_| "mcp_oauth_unavailable".to_string())
    }

    pub fn native_permission_status(&self) -> Result<gamja_core_host::PermissionStatus, String> {
        self.runtime_ready()?;
        gamja_core_host::cli::CoreClient {
            app_data: self.app_data.clone(),
        }
        .native_permission_status()
        .map_err(str::to_owned)
    }

    pub fn open_native_settings(
        &self,
        panel: gamja_core_host::SettingsPanel,
    ) -> Result<(), String> {
        self.runtime_ready()?;
        gamja_core_host::cli::CoreClient {
            app_data: self.app_data.clone(),
        }
        .open_native_settings(panel)
        .map_err(str::to_owned)
    }

    fn read_endpoint(&self) -> Result<Option<EndpointFile>, String> {
        match self.read_endpoint_state()? {
            EndpointState::Missing => Ok(None),
            EndpointState::Current(endpoint) => Ok(Some(endpoint)),
            EndpointState::LegacyStale => Err("invalid_endpoint".into()),
        }
    }

    fn read_endpoint_state(&self) -> Result<EndpointState, String> {
        match fs::symlink_metadata(self.app_data.join("owner-cli.json")) {
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(EndpointState::Missing)
            }
            Err(_) => return Err("endpoint_unavailable".into()),
        }
        platform::verify_directory(&self.app_data).map_err(str::to_owned)?;
        let mut file =
            platform::open_private(&self.app_data.join("owner-cli.json")).map_err(str::to_owned)?;
        let mut bytes = Vec::new();
        Read::by_ref(&mut file)
            .take((MAX_CONTROL_BYTES + 1) as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| "endpoint_unavailable")?;
        if bytes.len() > MAX_CONTROL_BYTES {
            return Err("invalid_endpoint".into());
        }
        let text = std::str::from_utf8(&bytes).map_err(|_| "invalid_endpoint")?;
        if let Ok(endpoint) = EndpointFile::from_json(text) {
            return Ok(EndpointState::Current(endpoint));
        }
        let legacy: LegacyEndpoint = serde_json::from_str(text).map_err(|_| "invalid_endpoint")?;
        EndpointFile {
            version: ENDPOINT_FILE_VERSION,
            api_url: legacy.base_url.clone(),
            control_url: legacy.base_url,
            capability: legacy.capability,
            core_version: CORE_PROTOCOL_VERSION,
            engine_version: ENGINE_PROTOCOL_VERSION,
        }
        .validate()
        .map_err(|_| "invalid_endpoint")?;
        Ok(EndpointState::LegacyStale)
    }

    pub fn start_if_needed(&self) -> Result<(), String> {
        self.runtime_ready()?;
        match self.read_endpoint_state()? {
            EndpointState::Current(endpoint) => {
                match control::request(&endpoint, &ControlRequest::Health {}) {
                    Ok(health) => return validate_health(health),
                    Err("core_unavailable") => {}
                    Err(error) => return Err(error.into()),
                }
            }
            EndpointState::Missing | EndpointState::LegacyStale => {}
        }
        // Revalidate the fixed package after endpoint probing and immediately before
        // the only process spawn in this client.
        self.runtime_ready()?;
        // This waits for the short-lived launcher only. The canonical CLI detaches its
        // core host; no child, stdin, or kill callback is retained by the GUI.
        run_core_launcher(&self.executable, &self.app_data, LAUNCHER_TIMEOUT)?;
        let endpoint = self.read_endpoint()?.ok_or("endpoint_unavailable")?;
        validate_health(
            control::request(&endpoint, &ControlRequest::Health {}).map_err(str::to_owned)?,
        )
    }

    pub fn create_ui_session(&self) -> Result<crate::local_endpoint::LocalEndpoint, String> {
        let endpoint = self.read_endpoint()?.ok_or("endpoint_unavailable")?;
        let response = control::request(&endpoint, &ControlRequest::CreateUiSession {})
            .map_err(str::to_owned)?;
        let session: UiSession =
            serde_json::from_value(response).map_err(|_| "invalid_ui_session")?;
        validate_versions(session.core_version, session.engine_version)?;
        if session.api_url != endpoint.api_url
            || session.session_token.is_empty()
            || session.session_token.len() > 4096
            || !session
                .session_token
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'='))
        {
            return Err("invalid_ui_session".into());
        }
        Ok(crate::local_endpoint::LocalEndpoint {
            base_url: session.api_url,
            session_token: session.session_token,
        })
    }

    pub(crate) fn open_app_data_dir(&self) -> Result<std::fs::File, String> {
        gamja_core_host::platform::verify_directory(&self.app_data)
            .map_err(|_| "artifact_unavailable")?;
        let mut options = std::fs::OpenOptions::new();
        options.read(true);
        std::os::unix::fs::OpenOptionsExt::custom_flags(
            &mut options,
            libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        );
        let directory = options
            .open(&self.app_data)
            .map_err(|_| "artifact_unavailable")?;
        let opened = directory.metadata().map_err(|_| "artifact_unavailable")?;
        let current =
            std::fs::symlink_metadata(&self.app_data).map_err(|_| "artifact_unavailable")?;
        if !opened.is_dir()
            || std::os::unix::fs::MetadataExt::dev(&opened)
                != std::os::unix::fs::MetadataExt::dev(&current)
            || std::os::unix::fs::MetadataExt::ino(&opened)
                != std::os::unix::fs::MetadataExt::ino(&current)
            || unsafe { libc::fcntl(std::os::fd::AsRawFd::as_raw_fd(&directory), libc::F_GETFD) }
                & libc::FD_CLOEXEC
                == 0
        {
            return Err("artifact_unavailable".into());
        }
        Ok(directory)
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UiSession {
    api_url: String,
    session_token: String,
    core_version: u16,
    engine_version: u16,
}

fn validate_versions(core: u16, engine: u16) -> Result<(), String> {
    if core != CORE_PROTOCOL_VERSION || engine != ENGINE_PROTOCOL_VERSION {
        Err("core_version_mismatch".into())
    } else {
        Ok(())
    }
}

fn validate_health(health: serde_json::Value) -> Result<(), String> {
    let core = health["core_version"]
        .as_u64()
        .and_then(|value| u16::try_from(value).ok())
        .ok_or("invalid_core_health")?;
    let engine = health["engine_version"]
        .as_u64()
        .and_then(|value| u16::try_from(value).ok())
        .ok_or("invalid_core_health")?;
    validate_versions(core, engine)?;
    if health["engine"] == "ready" {
        Ok(())
    } else {
        Err("engine_unavailable".into())
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use gamja_core_host::{capability, control, platform, ControlRequest, EndpointFile};
    use serde_json::json;
    use std::{
        fs,
        io::Write,
        net::TcpListener,
        os::unix::fs::PermissionsExt,
        sync::{
            atomic::{AtomicBool, AtomicUsize, Ordering},
            Arc,
        },
        thread,
        time::Duration,
    };

    static NEXT: AtomicUsize = AtomicUsize::new(0);
    struct Fixture {
        root: PathBuf,
        data: PathBuf,
        executable: PathBuf,
        endpoint: EndpointFile,
        stop: Arc<AtomicBool>,
        stops: Arc<AtomicUsize>,
        native_available: Arc<AtomicBool>,
        engine_ready: Arc<AtomicBool>,
        server: Option<thread::JoinHandle<()>>,
    }
    impl Fixture {
        fn new(published: bool, version: u16) -> Self {
            let root = std::env::temp_dir().join(format!(
                "gamja-gui-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&root).unwrap();
            let data = root.join("data");
            platform::secure_directory(&data).unwrap();
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            listener.set_nonblocking(true).unwrap();
            let endpoint = EndpointFile {
                version: 1,
                api_url: "http://127.0.0.1:41001".into(),
                control_url: format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port()),
                capability: "ab".repeat(32),
                core_version: version,
                engine_version: 1,
            };
            let source = root.join("source.json");
            fs::write(&source, serde_json::to_vec(&endpoint).unwrap()).unwrap();
            if published {
                write_endpoint(&data, &endpoint);
            }
            let executable = root.join("gamja");
            let mut script = fs::File::create(&executable).unwrap();
            writeln!(script, "#!/bin/sh\n[ \"$1\" = start ] && [ \"$2\" = --if-needed ] && [ \"$3\" = --app-data ] && [ \"$4\" = '{}' ] || exit 9\nprintf x >> '{}'\ncp '{}' '{}/owner-cli.json'\nchmod 600 '{}/owner-cli.json'", data.display(), root.join("launches").display(), source.display(), data.display(), data.display()).unwrap();
            fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).unwrap();
            let stop = Arc::new(AtomicBool::new(false));
            let stops = Arc::new(AtomicUsize::new(0));
            let native_available = Arc::new(AtomicBool::new(true));
            let native = native_available.clone();
            let engine_ready = Arc::new(AtomicBool::new(true));
            let ready = engine_ready.clone();
            let halt = stop.clone();
            let stop_count = stops.clone();
            let cap = endpoint.capability.clone();
            let server = thread::spawn(move || {
                while !halt.load(Ordering::Acquire) {
                    match listener.accept() {
                        Ok((socket, _)) => {
                            control::serve_connection(socket, &cap, |request| match request {
                                ControlRequest::Health {} => (
                                    200,
                                    json!({"status":if ready.load(Ordering::Acquire){"healthy"}else{"diagnostic_only"}, "engine":if ready.load(Ordering::Acquire){"ready"}else{"unavailable"}, "core_version":version, "engine_version":1}),
                                ),
                                ControlRequest::CreateUiSession {} => (
                                    200,
                                    json!({"api_url":"http://127.0.0.1:41001", "session_token":"engine-lifetime-session", "core_version":version, "engine_version":1}),
                                ),
                                ControlRequest::Stop {} => {
                                    stop_count.fetch_add(1, Ordering::Relaxed);
                                    (200, json!({"status":"stopping"}))
                                }
                                ControlRequest::NativePermissionStatus {} => {
                                    if native.load(Ordering::Acquire) {
                                        (
                                            200,
                                            json!({"screen_recording":"not_granted", "accessibility":"granted"}),
                                        )
                                    } else {
                                        (503, json!({"code":"native_capability_unavailable"}))
                                    }
                                }
                                ControlRequest::NativeOpenSettings {
                                    panel: gamja_core_host::SettingsPanel::ScreenRecording,
                                } => (200, json!({"status":"opened"})),
                                ControlRequest::ReportStatus {} => (
                                    200,
                                    json!({"auto_reporting_enabled":false,"consent_generation":4,"submission_available":false}),
                                ),
                                ControlRequest::ReportFrontendFailure {} => {
                                    (200, serde_json::Value::Null)
                                }
                                _ => (503, json!({"code":"reporting_unavailable"})),
                            })
                        }
                        Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                            thread::sleep(Duration::from_millis(2))
                        }
                        Err(error) => panic!("fixture server failed: {error}"),
                    }
                }
            });
            Self {
                root,
                data,
                executable,
                endpoint,
                stop,
                stops,
                native_available,
                engine_ready,
                server: Some(server),
            }
        }
        fn client(&self) -> CoreClient {
            CoreClient::new(self.data.clone(), self.executable.clone())
        }
        fn launches(&self) -> usize {
            fs::read(self.root.join("launches"))
                .unwrap_or_default()
                .len()
        }
    }
    fn write_endpoint(data: &std::path::Path, endpoint: &EndpointFile) {
        let mut file = platform::create_private(&data.join("owner-cli.json")).unwrap();
        file.write_all(&serde_json::to_vec(endpoint).unwrap())
            .unwrap();
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            self.stop.store(true, Ordering::Release);
            self.server.take().unwrap().join().unwrap();
            fs::remove_dir_all(&self.root).unwrap();
        }
    }

    #[test]
    fn core_client_reporting_remains_available_without_engine_or_ui_session() {
        let fixture = Fixture::new(true, 1);
        fixture.engine_ready.store(false, Ordering::Release);
        let client = fixture.client();
        assert_eq!(client.endpoint().err().unwrap(), "engine_unavailable");
        let status = client.report(ControlRequest::ReportStatus {}).unwrap();
        assert_eq!(status["consent_generation"], 4);
        assert_eq!(
            client
                .report(ControlRequest::ReportFrontendFailure {})
                .unwrap(),
            serde_json::Value::Null
        );
        assert_eq!(fixture.launches(), 0);
        assert_eq!(fixture.stops.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn gui_attaches_existing_core() {
        let fixture = Fixture::new(true, 1);
        let session = fixture.client().endpoint().unwrap();
        assert_eq!(session.base_url, "http://127.0.0.1:41001");
        assert_eq!(session.session_token, "engine-lifetime-session");
        assert_eq!(fixture.launches(), 0);
        assert_eq!(
            serde_json::to_value(session)
                .unwrap()
                .as_object()
                .unwrap()
                .len(),
            2
        );
    }
    #[test]
    fn gui_starts_missing_core_once() {
        let fixture = Fixture::new(false, 1);
        let client = fixture.client();
        assert_eq!(
            client.endpoint().unwrap().base_url,
            "http://127.0.0.1:41001"
        );
        assert_eq!(
            client.endpoint().unwrap().base_url,
            "http://127.0.0.1:41001"
        );
        assert_eq!(fixture.launches(), 1);
    }

    #[test]
    fn gui_launcher_excludes_ambient_read_write_socket_descriptors() {
        use std::os::{
            fd::{AsRawFd, FromRawFd, OwnedFd},
            unix::net::UnixStream,
        };
        let fixture = Fixture::new(false, 1);
        let mut pipe = [-1; 2];
        assert_eq!(unsafe { libc::pipe(pipe.as_mut_ptr()) }, 0);
        let reader = unsafe { OwnedFd::from_raw_fd(pipe[0]) };
        let writer = unsafe { OwnedFd::from_raw_fd(pipe[1]) };
        let (socket, _peer) = UnixStream::pair().unwrap();
        let extras: Vec<OwnedFd> = [reader.as_raw_fd(), writer.as_raw_fd(), socket.as_raw_fd()]
            .into_iter()
            .map(|fd| {
                let copy = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 200) };
                assert!(copy >= 200);
                let held = unsafe { OwnedFd::from_raw_fd(copy) };
                assert_eq!(unsafe { libc::fcntl(copy, libc::F_SETFD, 0) }, 0);
                held
            })
            .collect();
        let numbers = extras
            .iter()
            .map(|f| f.as_raw_fd().to_string())
            .collect::<Vec<_>>()
            .join(",");
        let script=format!("#!/usr/bin/python3\nimport os,sys,shutil\nfor fd in [{numbers}]:\n try: os.fstat(fd); sys.exit(17)\n except OSError as e: assert e.errno==9\nassert sys.argv[1:]==['start','--if-needed','--app-data',{:?}]\nwith open({:?},'ab') as f: f.write(b'x')\nshutil.copyfile({:?},{:?})\nos.chmod({:?},0o600)\n",fixture.data.to_string_lossy(),fixture.root.join("launches").to_string_lossy(),fixture.root.join("source.json").to_string_lossy(),fixture.data.join("owner-cli.json").to_string_lossy(),fixture.data.join("owner-cli.json").to_string_lossy());
        fs::write(&fixture.executable, script).unwrap();
        let client = fixture.client();
        assert!(client.start_if_needed().is_ok());
        assert_eq!(fixture.launches(), 1);
        assert_eq!(fixture.stops.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn stalled_gui_launcher_is_bounded_and_reaped() {
        let fixture = Fixture::new(false, 1);
        let marker = fixture.root.join("launcher.pid");
        fs::write(
            &fixture.executable,
            format!(
                "#!/bin/sh\necho $$ > '{}'\nexec /bin/sleep 30\n",
                marker.display()
            ),
        )
        .unwrap();
        let started = std::time::Instant::now();
        assert_eq!(
            run_core_launcher(
                &fixture.executable,
                &fixture.data,
                Duration::from_millis(500)
            ),
            Err("core_start_timeout".into())
        );
        assert!(started.elapsed() < Duration::from_secs(3));
        let pid: i32 = fs::read_to_string(marker).unwrap().trim().parse().unwrap();
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::ESRCH)
        );
        assert_eq!(fixture.stops.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn gui_replaces_only_a_private_well_formed_legacy_endpoint() {
        let fixture = Fixture::new(false, 1);
        write_legacy_endpoint(
            &fixture.data,
            &json!({
                "base_url": "http://127.0.0.1:41001",
                "capability": "cd".repeat(32),
            }),
        );
        let client = fixture.client();
        assert_eq!(
            client.endpoint().unwrap().base_url,
            "http://127.0.0.1:41001"
        );
        assert_eq!(fixture.launches(), 1);
        assert_eq!(capability::read(&fixture.data).unwrap(), fixture.endpoint);
    }

    #[test]
    fn gui_does_not_replace_malformed_legacy_endpoints() {
        for value in [
            json!({"base_url":"https://127.0.0.1:41001","capability":"cd".repeat(32)}),
            json!({"base_url":"http://127.0.0.1:41001","capability":"not-private"}),
            json!({"base_url":"http://127.0.0.1:41001","capability":"cd".repeat(32),"extra":true}),
        ] {
            let fixture = Fixture::new(false, 1);
            write_legacy_endpoint(&fixture.data, &value);
            assert_eq!(fixture.client().endpoint().unwrap_err(), "invalid_endpoint");
            assert_eq!(fixture.launches(), 0);
        }
    }
    #[test]
    fn window_destroy_does_not_stop_core() {
        let fixture = Fixture::new(true, 1);
        let runtime = crate::HostRuntime::Attach(fixture.client());
        runtime.endpoint().unwrap();
        drop(runtime);
        let health = control::request(&fixture.endpoint, &ControlRequest::Health {}).unwrap();
        assert_eq!(health["engine"], "ready");
        assert_eq!(fixture.stops.load(Ordering::Relaxed), 0);
        assert!(fixture.data.join("owner-cli.json").exists());
    }
    #[test]
    fn version_mismatch_does_not_launch_second_core() {
        let fixture = Fixture::new(true, 2);
        assert_eq!(
            fixture.client().endpoint().err().unwrap(),
            "invalid_endpoint"
        );
        assert_eq!(fixture.launches(), 0);
        assert_eq!(fixture.stops.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn core_client_health_version_mismatch_does_not_launch_second_core() {
        let fixture = Fixture::new(true, 2);
        let mut endpoint = fixture.endpoint.clone();
        endpoint.core_version = 1;
        fs::write(
            fixture.data.join("owner-cli.json"),
            serde_json::to_vec(&endpoint).unwrap(),
        )
        .unwrap();
        assert_eq!(
            fixture.client().endpoint().err().unwrap(),
            "core_version_mismatch"
        );
        assert_eq!(fixture.launches(), 0);
    }

    #[test]
    fn core_client_insecure_capability_is_not_repaired_or_replaced_by_gui() {
        let fixture = Fixture::new(true, 1);
        let path = fixture.data.join("owner-cli.json");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert_eq!(
            fixture.client().endpoint().err().unwrap(),
            "private_file_required"
        );
        assert_eq!(
            fs::metadata(path).unwrap().permissions().mode() & 0o777,
            0o644
        );
        assert_eq!(fixture.launches(), 0);
    }

    #[test]
    fn core_client_symlinked_legacy_endpoint_is_not_repaired_or_replaced_by_gui() {
        let fixture = Fixture::new(false, 1);
        let target = fixture.root.join("legacy.json");
        fs::write(
            &target,
            serde_json::to_vec(&json!({
                "base_url":"http://127.0.0.1:41001",
                "capability":"cd".repeat(32),
            }))
            .unwrap(),
        )
        .unwrap();
        std::os::unix::fs::symlink(&target, fixture.data.join("owner-cli.json")).unwrap();
        assert_eq!(
            fixture.client().endpoint().unwrap_err(),
            "private_file_required"
        );
        assert_eq!(fixture.launches(), 0);
    }

    #[test]
    fn core_client_concurrent_endpoint_calls_launch_once() {
        let fixture = Fixture::new(false, 1);
        let client = Arc::new(fixture.client());
        let other = client.clone();
        let first = thread::spawn(move || other.endpoint().unwrap());
        let second = client.endpoint().unwrap();
        assert_eq!(first.join().unwrap().base_url, second.base_url);
        assert_eq!(fixture.launches(), 1);
    }

    #[test]
    fn core_client_permissions_and_settings_use_private_helper_control() {
        let fixture = Fixture::new(true, 1);
        let client = fixture.client();
        let status = client.native_permission_status().unwrap();
        assert_eq!(
            status.screen_recording,
            gamja_core_host::PermissionState::NotGranted
        );
        assert_eq!(
            status.accessibility,
            gamja_core_host::PermissionState::Granted
        );
        client
            .open_native_settings(gamja_core_host::SettingsPanel::ScreenRecording)
            .unwrap();
        assert_eq!(fixture.launches(), 0);
    }

    fn write_legacy_endpoint(data: &Path, value: &serde_json::Value) {
        let mut file = platform::create_private(&data.join("owner-cli.json")).unwrap();
        file.write_all(&serde_json::to_vec(value).unwrap()).unwrap();
    }

    #[test]
    fn core_client_missing_helper_returns_typed_unavailable_and_keeps_core_healthy() {
        let fixture = Fixture::new(true, 1);
        fixture.native_available.store(false, Ordering::Release);
        assert_eq!(
            fixture.client().native_permission_status().err().unwrap(),
            "native_capability_unavailable"
        );
        assert_eq!(
            control::request(&fixture.endpoint, &ControlRequest::Health {}).unwrap()["engine"],
            "ready"
        );
        assert_eq!(fixture.launches(), 0);
        assert_eq!(fixture.stops.load(Ordering::Relaxed), 0);
    }
}
