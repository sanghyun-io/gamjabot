//! One standalone supervisor owns the canonical Python engine, credentials and lifetime.
use crate::{
    capability::{self, PublishedEndpoint},
    control, platform,
    reporting::{self, Code, Component, DiagnosticEvent, ReportDiagnostic, Reporting, Stage},
    ControlRequest, EndpointFile, Result, CORE_PROTOCOL_VERSION, ENDPOINT_FILE_VERSION,
    ENGINE_PROTOCOL_VERSION,
};
use gamja_platform_helper::{CapabilityStatus, PlatformHelper};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    ffi::OsString,
    fs::File,
    io::{Read, Write},
    net::{Ipv4Addr, TcpListener},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
use std::os::unix::net::UnixStream;
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
use std::process::{Child, ChildStdin, Command, Stdio};

#[doc(hidden)]
pub fn default_native_source_paths(home: &Path) -> (PathBuf, PathBuf, PathBuf, PathBuf) {
    (
        home.join(".aws/config"),
        home.join(".aws/credentials"),
        home.join(".aws/sso/cache"),
        home.join("Library/Application Support/gcloud/application_default_credentials.json"),
    )
}

#[derive(Clone)]
pub struct RuntimeConfig {
    pub app_data: PathBuf,
    pub engine: PathBuf,
    pub engine_args: Vec<OsString>,
    pub browsers: Option<PathBuf>,
    pub helper: Option<PathBuf>,
    pub startup_timeout: Duration,
    pub credential_engine_sha256: Option<&'static str>,
    pub credential_engine_requirement: Option<&'static str>,
    pub upgrade_remembered_login: bool,
}
impl RuntimeConfig {
    pub fn installed() -> Result<Self> {
        let executable = std::env::current_exe().map_err(|_| "core_location_unavailable")?;
        let bin = executable.parent().ok_or("core_location_unavailable")?;
        let libexec = bin
            .parent()
            .ok_or("core_location_unavailable")?
            .join("libexec/gamja");
        let browsers = libexec.join("playwright-browsers");
        Ok(Self {
            app_data: platform::app_data_dir()?,
            engine: libexec.join(if cfg!(windows) {
                "gamja-engine.exe"
            } else {
                "gamja-engine"
            }),
            engine_args: vec![],
            browsers: browsers.is_dir().then_some(browsers),
            helper: Some(libexec.join(if cfg!(windows) {
                "gamja-platform-helper.exe"
            } else {
                "gamja-platform-helper"
            })),
            startup_timeout: Duration::from_secs(30),
            upgrade_remembered_login: false,
            credential_engine_sha256: if cfg!(target_os = "macos") {
                option_env!("GAMJA_CREDENTIAL_ENGINE_SHA256")
            } else {
                None
            },
            credential_engine_requirement: if cfg!(target_os = "macos") {
                option_env!("GAMJA_CREDENTIAL_ENGINE_REQUIREMENT")
            } else {
                None
            },
        })
    }
}
#[derive(Debug, PartialEq, Eq)]
pub enum ExitStatus {
    Stopped,
}
pub struct Runtime;

// Runtime::serve is entered by the standalone CLI's process main thread. On
// macOS ARM the speech renderer needs that thread to service Foundation's main
// run loop, even though the renderer itself runs on a separate MCP worker.
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
struct MainRunLoop;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
impl MainRunLoop {
    fn begin() -> Result<Self> {
        unsafe extern "C" { fn gamja_core_mainloop_begin() -> i32; }
        if unsafe { gamja_core_mainloop_begin() } == 1 { Ok(Self) } else { Err("main_runloop_unavailable") }
    }
    fn pump(&self, seconds:f64) -> Result<()> {
        unsafe extern "C" { fn gamja_core_mainloop_pump(seconds:f64) -> i32; }
        if unsafe { gamja_core_mainloop_pump(seconds) } == 1 { Ok(()) } else { Err("main_runloop_unavailable") }
    }
}
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
impl Drop for MainRunLoop {
    fn drop(&mut self) {
        unsafe extern "C" { fn gamja_core_mainloop_end(); }
        unsafe { gamja_core_mainloop_end(); }
    }
}

struct OwnedEngine {
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    slack_owner: Option<Arc<dyn crate::auth_broker::SlackOwner>>,
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    vault_listener: Option<crate::vault_control::Listener>,
    #[cfg(unix)]
    mcp_owner: Option<Arc<dyn crate::mcp_transport::private_channel::SecretOwner>>,
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    child: Child,
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    child: crate::macos_spawn::OwnedChild,
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    lifeline: Option<ChildStdin>,
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    lifeline: Option<UnixStream>,
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    broker: Option<crate::broker_runtime::BrokerSession>,
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    mcp_secret_broker: Option<crate::broker_runtime::McpBrokerSession>,
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    slack_secret_broker: Option<crate::broker_runtime::SlackBrokerSession>,
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    publisher_secret_broker: Option<crate::broker_runtime::PublisherBrokerSession>,
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    channel_closers: Vec<UnixStream>,
    child_reaped: bool,
    drains: Vec<thread::JoinHandle<()>>,
    #[cfg(windows)]
    _job: platform::windows::EngineJob,
}
impl Drop for OwnedEngine {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}
impl OwnedEngine {
    fn id(&self) -> u32 {
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        {
            self.child.pid()
        }
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        {
            self.child.id()
        }
    }
    fn poll_failure(&mut self) -> Option<&'static str> {
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        {
            if self.child.try_wait().map_or(true, |s| s.is_some()) {
                return Some("engine_exited");
            }
            if self
                .broker
                .as_mut()
                .is_some_and(|b| !b.available().unwrap_or(false))
            {
                return Some("credential_broker_exited");
            }
        }
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        {
            if platform::child_exited(&mut self.child).unwrap_or(true) {
                return Some("engine_exited");
            }
        }
        None
    }
    fn broker_available(&self) -> bool {
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        {
            self.broker.is_some() && !self.child_reaped
        }
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        {
            false
        }
    }
    fn shutdown(&mut self) -> Result<()> {
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        { self.vault_listener.take(); }
        let mut uncertain = false;
        if !self.child_reaped {
            #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
            {
                // Preserve the existing TERM grace without reaping the leader.
                // The retained child pins its PGID until the final group KILL.
                if self.child.terminate_group().is_ok() {
                    thread::sleep(Duration::from_secs(2));
                }
                if self.child.shutdown(Duration::from_secs(2)).is_err() {
                    uncertain = true;
                } else {
                    self.child_reaped = true;
                }
            }
            #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
            {
                platform::stop_child(&mut self.child);
                self.child_reaped = true;
            }
        }
        self.lifeline.take();
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        {
            for s in self.channel_closers.drain(..) {
                let _ = s.shutdown(std::net::Shutdown::Both);
            }
            if let Some(b) = self.publisher_secret_broker.as_mut() {
                if b.shutdown().is_err() { uncertain = true; }
            }
            if let Some(b) = self.slack_secret_broker.as_mut() { if b.shutdown().is_err() { uncertain = true; } }
            if let Some(b) = self.mcp_secret_broker.as_mut() {
                if b.shutdown().is_err() { uncertain = true; }
            }
            if let Some(b) = self.broker.as_mut() {
                if b.shutdown().is_err() {
                    uncertain = true;
                }
            }
        }
        let deadline = Instant::now() + Duration::from_millis(300);
        for drain in self.drains.drain(..) {
            while !drain.is_finished() && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(10));
            }
            if drain.is_finished() {
                let _ = drain.join();
            }
        }
        if uncertain {
            Err("child_cleanup_uncertain")
        } else {
            Ok(())
        }
    }
    fn spawn(
        config: &RuntimeConfig,
        port: u16,
        session: &str,
        capability: &str,
        native: Option<(String, String)>,
        reports: Arc<reporting::ReportingState>,
        backend_errors: Arc<Mutex<Vec<BackendExceptionClass>>>,
    ) -> Result<Self> {
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        {
            Self::spawn_arm(config, port, session, capability, native, reports, backend_errors)
        }
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        {
            Self::spawn_legacy(config, port, session, capability, native, reports, backend_errors)
        }
    }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    fn spawn_arm(
        config: &RuntimeConfig,
        port: u16,
        session: &str,
        capability: &str,
        native: Option<(String, String)>,
        reports: Arc<reporting::ReportingState>,
        backend_errors: Arc<Mutex<Vec<BackendExceptionClass>>>,
    ) -> Result<Self> {
        use std::os::fd::AsRawFd;
        crate::credential::require_library_constraints_os()?;
        let upgrade = if config.upgrade_remembered_login {
            if config.credential_engine_sha256.is_none()
                || config.credential_engine_requirement.is_none() || !config.engine_args.is_empty() {
                return Err("upgrade_credential_unavailable");
            }
            Some(crate::upgrade_login::VerifiedSource::authenticate(Some(&config.app_data))?)
        } else { None };
        let verified_hash=match (config.credential_engine_sha256,config.credential_engine_requirement) {
            (Some(hash),Some(_)) if config.engine_args.is_empty()=>Some(hash),
            (None,None)=>None,
            _=>return Err("credential_engine_untrusted"),
        };
        // Start the optional new helper before v1, so its bootstrap latency does
        // not consume the legacy broker's bounded Engine-bind window.
        let mut mcp_secret_broker=if let Some(hash)=verified_hash {
            match crate::broker_runtime::McpBrokerSession::start(
                &std::env::current_exe().map_err(|_| "core_location_unavailable")?,&config.engine,hash,
            ) {
                Ok(session)=>Some(session),
                Err("mcp_broker_cleanup_uncertain")=>return Err("mcp_broker_cleanup_uncertain"),
                Err(_)=>None,
            }
        }else{None};
        // Independent Slack broker; failure never routes Slack through legacy.
        let mut slack_secret_broker=if let Some(hash)=verified_hash {
            match crate::broker_runtime::SlackBrokerSession::start(&std::env::current_exe().map_err(|_| "core_location_unavailable")?,&config.engine,hash) {
                Ok(session)=>Some(session), Err("slack_broker_cleanup_uncertain")=>return Err("slack_broker_cleanup_uncertain"), Err(_)=>None,
            }
        }else{None};
        // Publisher has a separate optional broker; never replace the legacy Engine FD.
        let mut publisher_secret_broker=if let Some(hash)=verified_hash {
            match crate::broker_runtime::PublisherBrokerSession::start(
                &std::env::current_exe().map_err(|_| "core_location_unavailable")?,&config.engine,hash,
            ) {
                Ok(session)=>Some(session),
                Err("publisher_broker_cleanup_uncertain")=>return Err("publisher_broker_cleanup_uncertain"),
                Err(_)=>None,
            }
        }else{None};
        let mut broker=if let Some(hash)=verified_hash {
            match crate::broker_runtime::BrokerSession::start(
                &std::env::current_exe().map_err(|_| "core_location_unavailable")?,&config.engine,hash,
            ) {
                Ok(session)=>Some(session),
                Err(code)=>{
                    if let Some(slack)=slack_secret_broker.as_mut(){slack.shutdown()?;}
                    if let Some(publisher)=publisher_secret_broker.as_mut(){publisher.shutdown()?;}
                    if let Some(mcp)=mcp_secret_broker.as_mut(){mcp.shutdown()?;}
                    return Err(code);
                }
            }
        }else{None};
        // Every setup error explicitly closes the broker; custom engines receive
        // no credential, auth, or provider capability.
        let setup = (|| {
            let (life, child_stdin) =
                UnixStream::pair().map_err(|_| "engine_channel_unavailable")?;
            let (stdout, child_stdout) =
                UnixStream::pair().map_err(|_| "engine_channel_unavailable")?;
            let (stderr, child_stderr) =
                UnixStream::pair().map_err(|_| "engine_channel_unavailable")?;
            let mut env = std::collections::BTreeMap::<String, String>::new();
            for name in [
                "HOME",
                "PATH",
                "TMPDIR",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "TZ",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
            ] {
                if let Some(value) = std::env::var_os(name) {
                    env.insert(
                        name.into(),
                        value
                            .into_string()
                            .map_err(|_| "engine_environment_invalid")?,
                    );
                }
            }
            let app_data = config
                .app_data
                .to_str()
                .ok_or("engine_environment_invalid")?;
            env.insert("GAMJA_APP_DATA_DIR".into(), app_data.into());
            env.insert("GAMJA_PORT".into(), port.to_string());
            env.insert("GAMJA_LOCAL_SESSION_TOKEN".into(), session.into());
            env.insert(
                "GAMJA_CLI_CAPABILITY_HASH".into(),
                format!("{:x}", Sha256::digest(capability.as_bytes())),
            );
            env.insert("GAMJA_DESKTOP_OWNER_STDIN".into(), "1".into());
            if let Some((endpoint, key)) = native {
                env.insert("GAMJA_DESKTOP_IPC_ENDPOINT".into(), endpoint);
                env.insert("GAMJA_DESKTOP_IPC_KEY".into(), key);
            }
            if let Some(browsers) = &config.browsers {
                env.insert(
                    "PLAYWRIGHT_BROWSERS_PATH".into(),
                    browsers
                        .to_str()
                        .ok_or("engine_environment_invalid")?
                        .into(),
                );
            }
            let mut mappings = vec![
                (child_stdin.as_raw_fd(), 0),
                (child_stdout.as_raw_fd(), 1),
                (child_stderr.as_raw_fd(), 2),
            ];
            let mut credential_child = None;
            let mut upgrade_channel = None;
            let mut publisher_channel = None;
            let mut auth = None;
            let mut closers = Vec::new();
            if let Some(b) = broker.as_mut() {
                let data = b.take_engine_data()?;
                let legacy_backend = crate::stable_broker::StableBrokerBackend::new(
                    b.take_internal()?, Duration::from_secs(5),
                ).map_err(|_| "credential_channel_unavailable")?;
                let mcp_backend = match mcp_secret_broker.as_mut() {
                    Some(session) => {
                        let candidate=session.take_internal().and_then(|stream|
                            crate::stable_broker::StableBrokerBackend::new_mcp(stream,Duration::from_secs(5))
                                .map_err(|_| "mcp_broker_channel_unavailable"));
                        match candidate {
                            Ok(backend)=>Some(backend),
                            Err(_)=>{
                                // This optional channel must not interrupt legacy login.
                                // Unconfirmed child cleanup still fails closed.
                                session.shutdown()?;
                                mcp_secret_broker=None;
                                None
                            }
                        }
                    }
                    None => None,
                };
                let backend=crate::stable_broker::SplitCredentialBackend::new(legacy_backend,mcp_backend);
                let slack_backend=match slack_secret_broker.as_mut() {
                    Some(session)=>match session.take_internal().and_then(|stream|crate::stable_broker::StableBrokerBackend::new_slack(stream,Duration::from_secs(5)).map_err(|_|"slack_broker_channel_unavailable")) {
                        Ok(backend)=>Some(backend),Err(_)=>{session.shutdown()?;slack_secret_broker=None;None}
                    },None=>None,
                };
                let backend=crate::stable_broker::SlackSplitCredentialBackend::new(backend,slack_backend);
                let (auth_child, mut auth_server) =
                    UnixStream::pair().map_err(|_| "auth_broker_unavailable")?;
                let (provider_child, provider_server) =
                    UnixStream::pair().map_err(|_| "provider_channel_unavailable")?;
                let (mcp_child,mcp_server)=UnixStream::pair().map_err(|_| "mcp_channel_unavailable")?;
                let home = std::env::var_os("HOME")
                    .map(PathBuf::from)
                    .ok_or("native_source_unavailable")?;
                let azure_cli = [
                    PathBuf::from("/opt/homebrew/bin/az"),
                    PathBuf::from("/usr/local/bin/az"),
                ]
                .into_iter()
                .find(|p| p.is_file())
                .unwrap_or_else(|| PathBuf::from("/opt/homebrew/bin/az"));
                let (aws_config, aws_credentials, aws_sso_cache, vertex_adc) =
                    default_native_source_paths(&home);
                let transport =
                    crate::provider_transport::NativeChainEnrollmentTransport::from_paths(
                        azure_cli,
                        aws_config,
                        aws_credentials,
                        aws_sso_cache,
                        vertex_adc,
                    );
                let auth_broker = Arc::new(Mutex::new(crate::auth_broker::AuthBroker::new(
                    backend, transport,
                )));
                // Initial generation is needed at engine import. This attachment
                // performs no get/put/delete; workers start only after bind.
                let (mcp_state,mcp_owner)=crate::mcp_transport::private_channel::new_owner_at(auth_broker.clone(),Some(config.app_data.clone()));
                let generation = auth_broker
                    .lock()
                    .map_err(|_| "auth_broker_unavailable")?
                    .attach_backend(&mut auth_server)?;
                closers.push(
                    auth_server
                        .try_clone()
                        .map_err(|_| "auth_broker_unavailable")?,
                );
                closers.push(
                    provider_server
                        .try_clone()
                        .map_err(|_| "provider_channel_unavailable")?,
                );
                closers.push(mcp_server.try_clone().map_err(|_| "mcp_channel_unavailable")?);
                mappings.extend([
                    (mcp_child.as_raw_fd(), 7),
                    (data.as_raw_fd(), 3),
                    (auth_child.as_raw_fd(), 4),
                    (provider_child.as_raw_fd(), 5),
                ]);
                env.insert("GAMJA_CREDENTIAL_FD".into(), "3".into());
                env.insert("GAMJA_AUTH_BROKER_FD".into(), "4".into());
                env.insert("GAMJA_PROVIDER_FD".into(), "5".into());
                env.insert("GAMJA_MCP_FD".into(), "7".into());
                credential_child = Some(data);
                if let Some(session)=publisher_secret_broker.as_mut() {
                    let prepared=(|| {
                        let backend=crate::stable_broker::StableBrokerBackend::new_publisher(
                            session.take_internal()?,Duration::from_secs(5))
                            .map_err(|_| "publisher_broker_channel_unavailable")?;
                        let backend=crate::stable_broker::PublisherRelayBackend::new(backend,auth_broker.clone());
                        let (parent,child)=UnixStream::pair().map_err(|_| "publisher_broker_channel_unavailable")?;
                        let closer=parent.try_clone().map_err(|_| "publisher_broker_channel_unavailable")?;
                        Ok::<_, &'static str>((parent,child,backend,closer))
                    })();
                    if let Ok((parent,child,backend,closer))=prepared {
                        mappings.push((child.as_raw_fd(),8));
                        env.insert("GAMJA_TEMPLATE_PUBLISHER_FD".into(),"8".into());
                        closers.push(closer);
                        publisher_channel=Some((parent,child,backend));
                    }
                }
                auth = Some((
                    auth_child,
                    auth_server,
                    provider_child,
                    provider_server,
                    auth_broker,
                    generation,
                    mcp_child,mcp_server,mcp_state,mcp_owner,
                ));
                if upgrade.is_some() {
                    let (child, server) = UnixStream::pair().map_err(|_| "upgrade_channel_unavailable")?;
                    mappings.push((child.as_raw_fd(), 6));
                    env.insert("GAMJA_UPGRADE_FD".into(), "6".into());
                    upgrade_channel = Some((child, server));
                }
            }
            let child = crate::macos_spawn::OwnedChild::spawn(
                &config.engine,
                &config.engine_args,
                env.into_iter().collect(),
                &mappings,
                config.startup_timeout,
            )
            .map_err(|_| "engine_spawn_failed")?;
            drop((child_stdin, child_stdout, child_stderr, credential_child));
            Ok((child, life, stdout, stderr, auth, closers, upgrade_channel, publisher_channel))
        })();
        let (child, life, stdout, stderr, auth, closers, upgrade_channel, mut publisher_channel) = match setup {
            Ok(v) => v,
            Err(code) => {
                if let Some(b)=slack_secret_broker.as_mut() {b.shutdown()?;}
                if let Some(b)=publisher_secret_broker.as_mut() {b.shutdown()?;}
                if let Some(b)=mcp_secret_broker.as_mut() {b.shutdown()?;}
                if let Some(b) = broker.as_mut() {b.shutdown()?;}
                return Err(code);
            }
        };
        let mut owned = Self {
            child,
            lifeline: Some(life),
            broker,
            mcp_secret_broker,
            slack_secret_broker,
            publisher_secret_broker,
            vault_listener: None,
            slack_owner: None,
            channel_closers: closers,
            mcp_owner: None,
            child_reaped: false,
            drains: Vec::new(),
        };
        // A startup handshake must never leave the child's output pipes unread.
        // Keep typed stdout diagnostics and classify only fixed stderr symbols.
        owned.drains.push(thread::spawn(move || {
            drain_backend_diagnostics(stdout, |value| {
                if let Ok(event) = serde_json::from_value::<DiagnosticEvent>(value) {
                    reporting::service::record(&reports, event);
                }
            });
        }));
        owned.drains.push(thread::spawn(move || {
            drain_backend_stderr(stderr, |error| {
                if let Ok(mut errors) = backend_errors.lock() {
                    errors.push(error);
                }
            });
        }));
        let bind = (|| {
            if let Some(requirement) = config.credential_engine_requirement {
                crate::credential::verify_live_engine(owned.id(), requirement)?;
            }
            if let Some(b) = owned.broker.as_mut() {
                b.bind(owned.child.pid())?;
            }
            if let Some(b)=owned.mcp_secret_broker.as_mut() {
                if b.bind(owned.child.pid()).is_err() {
                    // Closing the private socket leaves MCP secret access unavailable;
                    // legacy auth must not depend on this optional helper.
                    b.shutdown()?;
                    owned.mcp_secret_broker=None;
                }
            }
            if let Some(b)=owned.slack_secret_broker.as_mut() {
                if b.bind(owned.child.pid()).is_err() {b.shutdown()?;owned.slack_secret_broker=None;}
            }
            if let Some(b)=owned.publisher_secret_broker.as_mut() {
                if b.bind(owned.child.pid()).is_err() {
                    // No worker is started; closing this one relay leaves legacy intact.
                    publisher_channel.take();
                    if b.shutdown().is_ok() {owned.publisher_secret_broker=None;}
                }
            }
            let pid = owned.id();
            writeln!(owned.lifeline.as_mut().ok_or("engine_exited")?, "{pid}")
                .map_err(|_| "engine_exited")?;
            Ok(())
        })();
        if let Err(code) = bind {
            owned.shutdown()?;
            return Err(code);
        }
        if let Some((publisher_server,publisher_child,publisher_backend))=publisher_channel {
            drop(publisher_child);
            owned.drains.push(thread::spawn(move || {
                let _=crate::credential::serve_channel(publisher_server,publisher_backend);
            }));
        }
        if let Some((
            auth_child,
            auth_server,
            provider_child,
            provider_server,
            auth_broker,
            generation,
            mcp_child,mcp_server,mcp_state,mcp_owner,
        )) = auth
        {
            drop((auth_child, provider_child,mcp_child));
            owned.mcp_owner=Some(mcp_owner);
            owned.slack_owner=Some(crate::auth_broker::slack_owner(auth_broker.clone(),config.app_data.clone()));
            auth_broker.lock().map_err(|_| "auth_broker_unavailable")?.allow_vault_attestation();
            // Vault is optional and never changes the existing auth/provider paths.
            if let (Some(hash),Ok(core))=(verified_hash,std::env::current_exe()) {
                let directory=config.app_data.join("vault-realm-leases");
                if platform::secure_directory(&directory).is_ok() {
                    let vconfig=crate::vault::VaultConfig{core,engine:config.engine.clone(),engine_hash:hash.to_string(),engine_pid:owned.id(),directory};
                    auth_broker.lock().map_err(|_| "auth_broker_unavailable")?.set_vault_config(vconfig.clone());
                    owned.vault_listener=crate::vault_control::Listener::start(&config.app_data,auth_broker.clone(),vconfig).ok();
                }
            }
            let mcp_broker=auth_broker.clone();
            let mcp_generation=generation.clone();
            owned.drains.push(thread::spawn(move || { let _=crate::mcp_transport::private_channel::serve(mcp_server,mcp_broker,mcp_generation,mcp_state); }));
            let upgrade_generation = generation.fingerprint().to_owned();
            let provider_broker = auth_broker.clone();
            #[cfg(all(target_os="macos",target_arch="aarch64"))]
            let auth_monitor=auth_server.try_clone().ok();
            owned.drains.push(thread::spawn(move || {
                #[cfg(all(target_os="macos",target_arch="aarch64"))]
                let _ = crate::auth_broker::serve_attached_auth_channel_monitored(auth_server, auth_broker, auth_monitor);
                #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
                let _ = crate::auth_broker::serve_attached_auth_channel(auth_server, auth_broker);
            }));
            owned.drains.push(thread::spawn(move || {
                let _ = crate::provider_transport::serve_provider_channel(
                    provider_server,
                    provider_broker,
                    generation,
                );
            }));
            if let (Some(source), Some((child, server))) = (upgrade, upgrade_channel) {
                drop(child);
                if let Err(code) = source.serve(server, &upgrade_generation, config.startup_timeout) {
                    owned.shutdown()?;
                    return Err(code);
                }
            }
        }
        Ok(owned)
    }
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    fn spawn_legacy(
        config: &RuntimeConfig,
        port: u16,
        session: &str,
        capability: &str,
        native: Option<(String, String)>,
        reports: Arc<reporting::ReportingState>,
        backend_errors: Arc<Mutex<Vec<BackendExceptionClass>>>,
    ) -> Result<Self> {
        #[cfg(target_os = "macos")]
        crate::credential::require_library_constraints_os()?;
        let mut command = Command::new(&config.engine);
        command
            .env_remove("GAMJA_CREDENTIAL_FD")
            .env_remove("GAMJA_UPGRADE_FD")
            .env_remove("GAMJA_AUTH_BROKER_FD")
            .env_remove("GAMJA_PROVIDER_FD")
            .env_remove("GAMJA_MCP_FD")
            .env_remove("GAMJA_TEMPLATE_PUBLISHER_FD");
        #[cfg(target_os = "macos")]
        let credential = if let (Some(expected), Some(requirement)) = (
            config.credential_engine_sha256,
            config.credential_engine_requirement,
        ) {
            use std::os::fd::AsRawFd;
            use std::os::unix::net::UnixStream;
            crate::credential::verify_engine(&config.engine, expected)?;
            let (child, server) =
                UnixStream::pair().map_err(|_| "credential_channel_unavailable")?;
            let descriptor = child.as_raw_fd();
            crate::credential::inherit_descriptor_in_child(&mut command, descriptor)?;
            let backend = crate::credential::MacKeychainBackend::new()
                .map_err(|_| "credential_store_unavailable")?;
            command.env("GAMJA_CREDENTIAL_FD", descriptor.to_string());
            // The auth channel is always inherited with an unbound broker.
            // Only an authenticated owner_bind control frame can publish a
            // Core-generated owner epoch after the engine has started.
            let (auth_child, mut auth_server) =
                UnixStream::pair().map_err(|_| "auth_broker_unavailable")?;
            let auth_descriptor = auth_child.as_raw_fd();
            crate::credential::inherit_descriptor_in_child(&mut command, auth_descriptor)?;
            let auth_backend = crate::credential::MacKeychainBackend::new()
                .map_err(|_| "credential_store_unavailable")?;
            command.env("GAMJA_AUTH_BROKER_FD", auth_descriptor.to_string());
            let (provider_child, provider_server) =
                UnixStream::pair().map_err(|_| "provider_channel_unavailable")?;
            let provider_descriptor = provider_child.as_raw_fd();
            crate::credential::inherit_descriptor_in_child(&mut command, provider_descriptor)?;
            command.env("GAMJA_PROVIDER_FD", provider_descriptor.to_string());
            let (mcp_child,mcp_server)=UnixStream::pair().map_err(|_| "mcp_channel_unavailable")?;
            let mcp_descriptor=mcp_child.as_raw_fd();
            crate::credential::inherit_descriptor_in_child(&mut command,mcp_descriptor)?;
            command.env("GAMJA_MCP_FD",mcp_descriptor.to_string());
            let home = std::env::var_os("HOME").map(PathBuf::from)
                .ok_or("native_source_unavailable")?;
            let azure_cli = [PathBuf::from("/opt/homebrew/bin/az"), PathBuf::from("/usr/local/bin/az")]
                .into_iter().find(|path| path.is_file())
                .unwrap_or_else(|| PathBuf::from("/opt/homebrew/bin/az"));
            let (aws_config, aws_credentials, aws_sso_cache, vertex_adc) =
                default_native_source_paths(&home);
            let native_transport = crate::provider_transport::NativeChainEnrollmentTransport::from_paths(
                azure_cli,
                aws_config,
                aws_credentials,
                aws_sso_cache,
                vertex_adc,
            );
            let broker = Arc::new(Mutex::new(crate::auth_broker::AuthBroker::new(
                auth_backend,
                native_transport,
            )));
            let (mcp_state,mcp_owner)=crate::mcp_transport::private_channel::new_owner(broker.clone());
            let generation = broker.lock().map_err(|_| "auth_broker_unavailable")?
                .attach_backend(&mut auth_server)?;
            let auth = (
                auth_child,
                auth_server,
                provider_child,
                provider_server,
                broker,
                generation,
                mcp_child,mcp_server,mcp_state,mcp_owner,
            );
            Some((child, server, backend, auth, requirement))
        } else if config.credential_engine_sha256.is_some()
            || config.credential_engine_requirement.is_some()
        {
            return Err("credential_engine_untrusted");
        } else {
            None
        };
        platform::configure_engine(&mut command);
        for (name, _) in std::env::vars_os() {
            let text = name.to_string_lossy();
            if text.starts_with("DYLD_")
                || text.starts_with("PYTHON_")
                || matches!(
                    text.as_ref(),
                    "PYTHONPATH" | "PYTHONHOME" | "LD_PRELOAD" | "LD_LIBRARY_PATH"
                )
            {
                command.env_remove(name);
            }
        }
        command
            .args(&config.engine_args)
            .current_dir(&config.app_data)
            .env_remove("SECRET_KEY")
            .env_remove("DATABASE_URL")
            .env_remove("GAMJA_DESKTOP_IPC_ENDPOINT")
            .env_remove("GAMJA_DESKTOP_IPC_KEY")
            .env("GAMJA_APP_DATA_DIR", &config.app_data)
            .env("GAMJA_PORT", port.to_string())
            .env("GAMJA_LOCAL_SESSION_TOKEN", session)
            .env(
                "GAMJA_CLI_CAPABILITY_HASH",
                format!("{:x}", Sha256::digest(capability.as_bytes())),
            )
            .env("GAMJA_DESKTOP_OWNER_STDIN", "1")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if let Some((endpoint, key)) = native {
            command
                .env("GAMJA_DESKTOP_IPC_ENDPOINT", endpoint)
                .env("GAMJA_DESKTOP_IPC_KEY", key);
        }
        if let Some(browsers) = &config.browsers {
            command.env("PLAYWRIGHT_BROWSERS_PATH", browsers);
        } else {
            command.env_remove("PLAYWRIGHT_BROWSERS_PATH");
        }
        let mut child = command.spawn().map_err(|_| "engine_spawn_failed")?;
        #[cfg(target_os = "macos")]
        if let Some((_, _, _, _, requirement)) = credential.as_ref() {
            if crate::credential::verify_live_engine(child.id(), requirement).is_err() {
                platform::stop_child(&mut child);
                return Err("credential_engine_untrusted");
            }
        }
        #[cfg(windows)]
        let job = match platform::windows::EngineJob::attach(&child) {
            Ok(job) => job,
            Err(code) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(code);
            }
        };
        let mut lifeline = child.stdin.take();
        if lifeline
            .as_mut()
            .is_none_or(|pipe| writeln!(pipe, "{}", child.id()).is_err())
        {
            platform::stop_child(&mut child);
            return Err("engine_exited");
        }
        let mut drains = Vec::new();
        #[cfg(unix)] let mut mcp_owner=None;
        #[cfg(target_os = "macos")]
        if let Some((credential_child, credential_server, credential_backend, auth, _)) = credential {
            drop(credential_child);
            drains.push(thread::spawn(move || {
                let _ = crate::credential::serve_channel(credential_server, credential_backend);
            }));
            let (auth_child, auth_server, provider_child, provider_server, broker, generation,mcp_child,mcp_server,mcp_state,owner) = auth;
            drop(mcp_child);mcp_owner=Some(owner);
            let mcp_broker=broker.clone();let mcp_generation=generation.clone();
            drains.push(thread::spawn(move || {let _=crate::mcp_transport::private_channel::serve(mcp_server,mcp_broker,mcp_generation,mcp_state);}));
            drop(auth_child);
            drop(provider_child);
            let provider_broker = broker.clone();
            drains.push(thread::spawn(move || {
                let _ = crate::auth_broker::serve_attached_auth_channel(auth_server, broker);
            }));
            drains.push(thread::spawn(move || {
                let _ = crate::provider_transport::serve_provider_channel(
                    provider_server,
                    provider_broker,
                    generation,
                );
            }));
        }
        // Drain bounded diagnostic lines; do not forward raw stdout/stderr to clients/logs.
        if let Some(stdout) = child.stdout.take() {
            drains.push(thread::spawn(move || {
                drain_backend_diagnostics(stdout, |value| {
                    if let Ok(event) = serde_json::from_value::<DiagnosticEvent>(value) {
                        reporting::service::record(&reports, event);
                    }
                })
            }));
        }
        if let Some(stderr) = child.stderr.take() {
            drains.push(thread::spawn(move || {
                drain_backend_stderr(stderr, |error| {
                    if let Ok(mut errors) = backend_errors.lock() {
                        errors.push(error);
                    }
                });
            }));
        }
        Ok(Self {
            child,
            lifeline,
            #[cfg(unix)] mcp_owner,
            child_reaped: false,
            drains,
            #[cfg(windows)]
            _job: job,
        })
    }
}
fn shutdown_engine(engine: &mut Option<OwnedEngine>) -> Result<()> {
    if let Some(mut owned) = engine.take() {
        owned.shutdown()?;
    }
    Ok(())
}
/// Content-free observations from the owned engine's stderr, not inferred causes.
#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BackendExceptionClass {
    ModuleNotFound,
    Import,
    Attribute,
    Type,
    Value,
    Os,
    Database,
    Runtime,
    Eof,
    UpgradeChannelInvalid,
    UpgradeChannelUnavailable,
    UpgradeStateChanged,
    UpgradeNativeFailed,
    UpgradeAckUnconfirmed,
}

fn classify_backend_stderr(line: &[u8]) -> Option<BackendExceptionClass> {
    use BackendExceptionClass::*;
    // Match complete, fixed exception symbols. Never retain the message suffix.
    for (symbol, kind) in [
        (b"ModuleNotFoundError: ".as_slice(), ModuleNotFound),
        (b"ImportError: ".as_slice(), Import),
        (b"AttributeError: ".as_slice(), Attribute),
        (b"TypeError: ".as_slice(), Type),
        (b"ValueError: ".as_slice(), Value),
        (b"OSError: ".as_slice(), Os),
        (b"PermissionError: ".as_slice(), Os),
        (b"FileNotFoundError: ".as_slice(), Os),
        (b"sqlite3.OperationalError: ".as_slice(), Database),
        (b"sqlalchemy.exc.OperationalError: ".as_slice(), Database),
        (b"RuntimeError: ".as_slice(), Runtime),
        (b"EOFError: ".as_slice(), Eof),
    ] {
        if line.starts_with(symbol) {
            return Some(kind);
        }
    }
    match line.strip_prefix(b"app.services.core_upgrade_login.ChannelError: ")? {
        b"upgrade_channel_invalid" => Some(UpgradeChannelInvalid),
        b"upgrade_channel_unavailable" => Some(UpgradeChannelUnavailable),
        b"upgrade_state_changed" => Some(UpgradeStateChanged),
        b"upgrade_native_failed" => Some(UpgradeNativeFailed),
        b"upgrade_ack_unconfirmed" => Some(UpgradeAckUnconfirmed),
        _ => None,
    }
}

/// Drain all stderr while retaining at most three distinct fixed exception classes.
/// Tracebacks, paths, arguments, exception messages and oversized lines are discarded.
pub fn drain_backend_stderr(mut input: impl Read, mut record: impl FnMut(BackendExceptionClass)) {
    const MAX_LINE: usize = 512;
    let mut chunk = [0u8; 4096];
    let mut line = Vec::with_capacity(MAX_LINE);
    let mut oversized = false;
    let mut seen = Vec::with_capacity(3);
    let mut classify = |line: &[u8]| {
        if seen.len() < 3 {
            if let Some(kind) = classify_backend_stderr(line) {
                if !seen.contains(&kind) {
                    seen.push(kind);
                    record(kind);
                }
            }
        }
    };
    loop {
        let size = match input.read(&mut chunk) {
            Ok(0) => {
                if !oversized { classify(&line); }
                break;
            }
            Ok(size) => size,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => break,
        };
        for &byte in &chunk[..size] {
            if byte == b'\n' {
                if !oversized { classify(&line); }
                line.fill(0);
                line.clear();
                oversized = false;
            } else if !oversized {
                if line.len() < MAX_LINE {
                    line.push(byte);
                } else {
                    line.fill(0);
                    line.clear();
                    oversized = true;
                }
            }
        }
        chunk.fill(0);
    }
    chunk.fill(0);
    line.fill(0);
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

/// Only fixed typed frames enter reporting; raw, unknown and oversized output is discarded.
pub fn drain_backend_diagnostics(mut input: impl Read, mut record: impl FnMut(Value)) {
    const MAX: usize = 2304;
    let mut chunk = [0u8; 4096];
    let mut line = Vec::with_capacity(MAX);
    let mut oversized = false;
    while let Ok(size) = input.read(&mut chunk) {
        if size == 0 {
            break;
        }
        for &byte in &chunk[..size] {
            if byte == b'\n' {
                if !oversized {
                    let event = if let Some(payload) = line.strip_prefix(b"GAMJA_DIAGNOSTIC_V2:") {
                        serde_json::from_slice::<PipeDiagnosticV2>(payload)
                            .ok()
                            .filter(|event| {
                                event.code == Code::BackendRuntimeFailed
                                    && event.stage == Stage::Runtime
                                    && event.diagnostic.validate().is_ok()
                            })
                            .map(|event| {
                                let mut diagnostic = DiagnosticEvent::new(
                                    Component::Backend,
                                    event.stage,
                                    event.code,
                                );
                                diagnostic.diagnostic = Some(event.diagnostic);
                                diagnostic
                            })
                    } else if let Some(payload) = line.strip_prefix(b"GAMJA_DIAGNOSTIC_V1:") {
                        serde_json::from_slice::<PipeDiagnostic>(payload)
                            .ok()
                            .filter(|event| {
                                matches!(
                                    (event.code, event.stage),
                                    (Code::BackendInitFailed, Stage::Startup)
                                        | (Code::BackendRuntimeFailed, Stage::Runtime)
                                        | (
                                            Code::UnexpectedFailure,
                                            Stage::Startup | Stage::Runtime
                                        )
                                )
                            })
                            .map(|event| {
                                DiagnosticEvent::new(Component::Backend, event.stage, event.code)
                            })
                    } else {
                        None
                    };
                    if let Some(event) = event {
                        record(json!(event));
                    }
                }
                line.clear();
                oversized = false;
            } else if !oversized {
                if line.len() < MAX {
                    line.push(byte);
                } else {
                    line.clear();
                    oversized = true;
                }
            }
        }
    }
}
impl Runtime {
    pub fn serve(config: RuntimeConfig) -> Result<ExitStatus> {
        if config.upgrade_remembered_login && !cfg!(all(target_os = "macos", target_arch = "aarch64")) {
            return Err("upgrade_platform_unsupported");
        }
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        let main_run_loop = MainRunLoop::begin()?;
        platform::secure_directory(&config.app_data)?;
        let _host_lock: File = platform::acquire_lock(&config.app_data.join("gamja-host.lock"))?;
        // Canonical backend holds this lock during lifespan, including legacy Windows hosts.
        let probe = platform::acquire_lock(&config.app_data.join("gamja_bot.db.backend.lock"))
            .map_err(|code| {
                if code == "runtime_already_running" {
                    "legacy_backend_running"
                } else {
                    code
                }
            })?;
        drop(probe);
        let listener =
            TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).map_err(|_| "control_bind_failed")?;
        listener
            .set_nonblocking(true)
            .map_err(|_| "control_bind_failed")?;
        let reserve =
            TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).map_err(|_| "engine_port_unavailable")?;
        let port = reserve
            .local_addr()
            .map_err(|_| "engine_port_unavailable")?
            .port();
        let session = capability::random_hex()?;
        let endpoint = EndpointFile {
            version: ENDPOINT_FILE_VERSION,
            api_url: format!("http://127.0.0.1:{port}"),
            control_url: format!(
                "http://127.0.0.1:{}",
                listener
                    .local_addr()
                    .map_err(|_| "control_bind_failed")?
                    .port()
            ),
            capability: capability::random_hex()?,
            core_version: CORE_PROTOCOL_VERSION,
            engine_version: ENGINE_PROTOCOL_VERSION,
        };
        let stop = Arc::new(AtomicBool::new(false));
        let stop_signal = stop.clone();
        ctrlc::set_handler(move || stop_signal.store(true, Ordering::SeqCst))
            .map_err(|_| "signal_handler_unavailable")?;
        drop(reserve);
        let reports = Reporting::open(&config.app_data);
        let mut helper = PlatformHelper::start(config.helper.as_deref());
        let mut failure = None;
        // Each launch owns a fresh, bounded projection; no raw output is persisted.
        let backend_errors = Arc::new(Mutex::new(Vec::new()));
        let mut engine = match OwnedEngine::spawn(
            &config,
            port,
            &session,
            &endpoint.capability,
            helper.credentials(),
            reports.state(),
            backend_errors.clone(),
        ) {
            Ok(engine) => Some(engine),
            Err(code) => {
                failure = Some(code);
                None
            }
        };
        let deadline = Instant::now() + config.startup_timeout;
        let mut ready = false;
        while !stop.load(Ordering::SeqCst) && failure.is_none() {
            let child = engine.as_mut().ok_or("engine_spawn_failed")?;
            if let Some(code) = child.poll_failure() {
                failure = Some(code);
                break;
            }
            if control::engine_ready(port, &session) {
                ready = true;
                break;
            }
            if Instant::now() >= deadline {
                failure = Some("engine_not_ready");
                break;
            }
            #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
            main_run_loop.pump(0.025)?;
            #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
            thread::sleep(Duration::from_millis(25));
        }
        if stop.load(Ordering::SeqCst) {
            shutdown_engine(&mut engine)?;
            return Ok(ExitStatus::Stopped);
        }
        if !ready {
            if let Err(code) = shutdown_engine(&mut engine) {
                failure = Some(code);
            }
        }
        if let Some(code) = failure {
            reports.record(
                Component::Host,
                Stage::Startup,
                match code {
                    "engine_spawn_failed" => {
                        if config.engine.exists() {
                            Code::SidecarSpawnFailed
                        } else {
                            Code::SidecarMissing
                        }
                    }
                    "engine_not_ready" => Code::BackendNotReady,
                    _ => Code::SidecarExited,
                },
            );
        }
        // Publish once a known healthy or diagnostic-only state is ready to answer control.
        let published = PublishedEndpoint::publish(&config.app_data, &endpoint)?;
        while !stop.load(Ordering::SeqCst) {
            #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
            main_run_loop.pump(0.001)?;
            if ready {
                if let Some(code) = engine.as_mut().and_then(OwnedEngine::poll_failure) {
                    ready = false;
                    failure = Some(code);
                    reports.record(Component::Host, Stage::Runtime, Code::SidecarExited);
                    if let Err(code) = shutdown_engine(&mut engine) {
                        failure = Some(code);
                    }
                }
            }
            let native_status = helper.capability_status();
            let (native_available, native_code) = match native_status {
                CapabilityStatus::Available => (true, None),
                CapabilityStatus::Unavailable(code) => (false, Some(code)),
            };
            match listener.accept() {
                Ok((socket, _)) => {
                    control::serve_connection(socket, &endpoint.capability, |request| match request
                    {
                        ControlRequest::Health {} => (
                            200,
                            json!({"status":if ready{"healthy"}else{"diagnostic_only"},"engine":if ready{"ready"}else{"unavailable"},"core_pid":std::process::id(),"engine_pid":engine.as_ref().map(OwnedEngine::id),"code":failure,"backend_stderr_errors":backend_errors.lock().map(|errors| errors.clone()).unwrap_or_default(),"core_version":CORE_PROTOCOL_VERSION,"engine_version":ENGINE_PROTOCOL_VERSION,"credential_broker":if ready && engine.as_ref().is_some_and(OwnedEngine::broker_available){"available"}else{"unavailable"},"native_helper":if native_available{"available"}else{"unavailable"},"native_helper_code":native_code,"browser":if config.browsers.is_some(){"available"}else{"unavailable"}}),
                        ),
                        ControlRequest::CreateUiSession {} => {
                            if ready {
                                (
                                    200,
                                    json!({"api_url":endpoint.api_url,"session_token":session,"core_version":CORE_PROTOCOL_VERSION,"engine_version":ENGINE_PROTOCOL_VERSION}),
                                )
                            } else {
                                (503, json!({"code":"engine_unavailable"}))
                            }
                        }
                        ControlRequest::SlackCredentialStatus {owner_id} => {
                            #[cfg(all(target_os="macos",target_arch="aarch64"))]
                            {match engine.as_ref().and_then(|e|e.slack_owner.as_ref()).ok_or("slack_credential_unavailable").and_then(|o|o.status(owner_id)) {Ok(v)=>(200,v),Err(code)=>(503,json!({"code":code}))}}
                            #[cfg(not(all(target_os="macos",target_arch="aarch64")))] {let _=owner_id;(503,json!({"code":"slack_credential_unavailable"}))}
                        }
                        ControlRequest::SlackCredentialStore {owner_id,owner_epoch,core_epoch,account_id,expected_revision,operation_id,app_token,bot_token} => {
                            #[cfg(all(target_os="macos",target_arch="aarch64"))]
                            {match engine.as_ref().and_then(|e|e.slack_owner.as_ref()).ok_or("slack_credential_unavailable").and_then(|o|o.change(owner_id,&owner_epoch,&core_epoch,&account_id,expected_revision,&operation_id,Some((app_token,bot_token)))) {Ok(v)=>(200,v),Err(code)=>(503,json!({"code":code}))}}
                            #[cfg(not(all(target_os="macos",target_arch="aarch64")))] {let _=(owner_id,owner_epoch,core_epoch,account_id,expected_revision,operation_id,app_token,bot_token);(503,json!({"code":"slack_credential_unavailable"}))}
                        }
                        ControlRequest::SlackCredentialDelete {owner_id,owner_epoch,core_epoch,account_id,expected_revision,operation_id} => {
                            #[cfg(all(target_os="macos",target_arch="aarch64"))]
                            {match engine.as_ref().and_then(|e|e.slack_owner.as_ref()).ok_or("slack_credential_unavailable").and_then(|o|o.change(owner_id,&owner_epoch,&core_epoch,&account_id,expected_revision,&operation_id,None)) {Ok(v)=>(200,v),Err(code)=>(503,json!({"code":code}))}}
                            #[cfg(not(all(target_os="macos",target_arch="aarch64")))] {let _=(owner_id,owner_epoch,core_epoch,account_id,expected_revision,operation_id);(503,json!({"code":"slack_credential_unavailable"}))}
                        }
                        ControlRequest::McpSecretStore {server_id,expected_revision,operation_id,secret} => {
                            #[cfg(unix)]
                            {match engine.as_ref().and_then(|e|e.mcp_owner.as_ref()) {
                                Some(owner)=>match owner.store(&server_id,expected_revision,&operation_id,zeroize::Zeroizing::new(secret.as_str().as_bytes().to_vec())) {
                                    Ok(reference)=>(200,json!({"state":"stored","credential_ref":reference})),
                                    Err(code)=>(503,json!({"code":code})),
                                },None=>(503,json!({"code":"mcp_unavailable"})),
                            }}
                            #[cfg(not(unix))] {let _=(server_id,expected_revision,operation_id,secret);(503,json!({"code":"mcp_unavailable"}))}
                        }
                        ControlRequest::McpSecretStatus {server_id,expected_revision,operation_id} => {
                            #[cfg(unix)]
                            {match engine.as_ref().and_then(|e|e.mcp_owner.as_ref()) {
                                Some(owner)=>match owner.status(&server_id,expected_revision,&operation_id) {
                                    Ok(Some(reference))=>(200,json!({"state":"present","credential_ref":reference})),
                                    Ok(None)=>(200,json!({"state":"missing"})),
                                    Err(code)=>(503,json!({"code":code})),
                                },None=>(503,json!({"code":"mcp_unavailable"})),
                            }}
                            #[cfg(not(unix))] {let _=(server_id,expected_revision,operation_id);(503,json!({"code":"mcp_unavailable"}))}
                        }
                        ControlRequest::McpOAuthBegin {server_id,expected_revision,operation_id} => {
                            #[cfg(unix)]
                            {match engine.as_ref().and_then(|e|e.mcp_owner.as_ref()) {
                                Some(owner)=>match owner.oauth_begin(&server_id,expected_revision,&operation_id) { Ok(value)=>(200,value), Err(code)=>(503,json!({"code":code})) },
                                None=>(503,json!({"code":"mcp_unavailable"})),
                            }}
                            #[cfg(not(unix))] {let _=(server_id,expected_revision,operation_id);(503,json!({"code":"mcp_unavailable"}))}
                        }
                        ControlRequest::McpOAuthStatus {server_id,expected_revision,operation_id} => {
                            #[cfg(unix)]
                            {match engine.as_ref().and_then(|e|e.mcp_owner.as_ref()) {
                                Some(owner)=>match owner.oauth_status(&server_id,expected_revision,&operation_id) { Ok(value)=>(200,value), Err(code)=>(503,json!({"code":code})) },
                                None=>(503,json!({"code":"mcp_unavailable"})),
                            }}
                            #[cfg(not(unix))] {let _=(server_id,expected_revision,operation_id);(503,json!({"code":"mcp_unavailable"}))}
                        }
                        ControlRequest::McpOAuthCancel {server_id,expected_revision,operation_id} => {
                            #[cfg(unix)]
                            {match engine.as_ref().and_then(|e|e.mcp_owner.as_ref()) {
                                Some(owner)=>match owner.oauth_cancel(&server_id,expected_revision,&operation_id) { Ok(value)=>(200,value), Err(code)=>(503,json!({"code":code})) },
                                None=>(503,json!({"code":"mcp_unavailable"})),
                            }}
                            #[cfg(not(unix))] {let _=(server_id,expected_revision,operation_id);(503,json!({"code":"mcp_unavailable"}))}
                        }
                        ControlRequest::NativePermissionStatus {} => {
                            match helper.native_permission_status() {
                                Ok(status) => (200, serde_json::to_value(status).unwrap()),
                                Err(code) => (503, json!({"code":code})),
                            }
                        }
                        ControlRequest::NativeOpenSettings { panel } => {
                            match helper.open_native_settings(panel) {
                                Ok(()) => (200, json!({"status":"completed"})),
                                Err(code) => (503, json!({"code":code})),
                            }
                        }
                        ControlRequest::Stop {} => {
                            stop.store(true, Ordering::SeqCst);
                            (200, json!({"status":"stopping"}))
                        }
                        request => reports.handle(request),
                    })
                }
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
                    main_run_loop.pump(0.010)?;
                    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
                    thread::sleep(Duration::from_millis(10))
                }
                Err(_) => {
                    shutdown_engine(&mut engine)?;
                    return Err("control_unavailable");
                }
            }
        }
        drop(listener);
        let cleanup = shutdown_engine(&mut engine);
        helper.stop();
        drop(published);
        cleanup?;
        Ok(ExitStatus::Stopped)
    }
}
