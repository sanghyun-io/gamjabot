use crate::{
    capability, control,
    runtime::{Runtime, RuntimeConfig},
    runtime_install::{
        broker_availability, expected_installed, install_runtime, validate_runtime,
        BrokerAvailability, BrokerUnavailableReason, RuntimePins, RuntimeScope, RuntimeStatus,
    },
    ControlRequest, PermissionStatus, Result, SettingsPanel,
};
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
use crate::platform;
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
use std::process::Stdio;
use serde_json::Value;
use std::io::Read;
use std::{
    ffi::OsString,
    path::{Path, PathBuf},
    process::Command,
    sync::OnceLock,
    thread,
    time::{Duration, Instant},
};

static BUSINESS_INTERRUPT_HANDLER: OnceLock<bool> = OnceLock::new();

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
struct NativeStartedCore {
    relay: Option<crate::macos_spawn::OwnedChild>,
    core_pid: u32,
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
impl NativeStartedCore {
    fn id(&self) -> u32 {
        self.core_pid
    }
    fn try_wait(&mut self) -> Result<Option<std::process::ExitStatus>> {
        match &mut self.relay {
            Some(relay) => {
                let status = relay.try_wait().map_err(|_| "core_monitor_failed")?;
                match status {
                    Some(s) if s.success() => {
                        // Relay exited successfully. Check if Core is still in relay's group.
                        let pgid = unsafe { libc::getpgid(self.core_pid as i32) };
                        if pgid >= 0 && pgid as u32 == relay.id() {
                            // Core is still in relay's group, so it's still starting
                            Ok(None)
                        } else {
                            // Core has been adopted or moved, relay exit is final
                            Ok(Some(s))
                        }
                    }
                    other => Ok(other),
                }
            }
            None => Ok(None),
        }
    }
    fn detach(mut self) -> Result<()> {
        if let Some(relay) = self.relay.take() {
            relay
                .release_exited_relay_for_core(self.core_pid, Duration::from_secs(1))
                .map_err(|_| "core_detach_failed")?;
        }
        Ok(())
    }
}

fn preserve_exact_observer_interrupt() -> Result<()> {
    // Install a caught handler before spawning. POSIX exec resets caught signal
    // handlers in the child, so Python still receives SIGINT as KeyboardInterrupt;
    // the native foreground parent stays alive long enough to relay output and reap.
    if *BUSINESS_INTERRUPT_HANDLER.get_or_init(|| ctrlc::set_handler(|| {}).is_ok()) {
        Ok(())
    } else {
        Err("business_signal_handler_failed")
    }
}

pub struct CoreClient {
    pub app_data: PathBuf,
}
impl CoreClient {
    pub fn status(&self) -> Result<Value> {
        control::request(
            &capability::read(&self.app_data)?,
            &ControlRequest::Health {},
        )
    }
    pub fn create_ui_session(&self) -> Result<Value> {
        control::request(
            &capability::read(&self.app_data)?,
            &ControlRequest::CreateUiSession {},
        )
    }
    pub fn slack_credential_status(&self,owner_id:u64)->Result<Value> {
        control::request(&capability::read(&self.app_data)?, &ControlRequest::SlackCredentialStatus{owner_id})
    }
    pub fn slack_credential_store(&self,owner_id:u64,owner_epoch:String,core_epoch:String,account_id:String,expected_revision:u64,operation_id:String,app_token:crate::OwnerSecret,bot_token:crate::OwnerSecret)->Result<Value> {
        control::request(&capability::read(&self.app_data)?, &ControlRequest::SlackCredentialStore{owner_id,owner_epoch,core_epoch,account_id,expected_revision,operation_id,app_token,bot_token})
    }
    pub fn slack_credential_delete(&self,owner_id:u64,owner_epoch:String,core_epoch:String,account_id:String,expected_revision:u64,operation_id:String)->Result<Value> {
        control::request(&capability::read(&self.app_data)?, &ControlRequest::SlackCredentialDelete{owner_id,owner_epoch,core_epoch,account_id,expected_revision,operation_id})
    }
    pub fn store_mcp_secret(&self,server_id:String,expected_revision:u64,operation_id:String,secret:crate::protocol::OwnerSecret)->Result<Value> {
        control::request(&capability::read(&self.app_data)?, &ControlRequest::McpSecretStore{server_id,expected_revision,operation_id,secret})
    }
    pub fn mcp_secret_status(&self,server_id:String,expected_revision:u64,operation_id:String)->Result<Value> {
        control::request(&capability::read(&self.app_data)?, &ControlRequest::McpSecretStatus{server_id,expected_revision,operation_id})
    }
    pub fn mcp_oauth_begin(&self,server_id:String,expected_revision:u64,operation_id:String)->Result<Value> {
        control::request(&capability::read(&self.app_data)?, &ControlRequest::McpOAuthBegin{server_id,expected_revision,operation_id})
    }
    pub fn mcp_oauth_status(&self,server_id:String,expected_revision:u64,operation_id:String)->Result<Value> {
        control::request(&capability::read(&self.app_data)?, &ControlRequest::McpOAuthStatus{server_id,expected_revision,operation_id})
    }
    pub fn mcp_oauth_cancel(&self,server_id:String,expected_revision:u64,operation_id:String)->Result<Value> {
        control::request(&capability::read(&self.app_data)?, &ControlRequest::McpOAuthCancel{server_id,expected_revision,operation_id})
    }
    pub fn native_permission_status(&self) -> Result<PermissionStatus> {
        let response = control::request(
            &capability::read(&self.app_data)?,
            &ControlRequest::NativePermissionStatus {},
        )?;
        serde_json::from_value(response).map_err(|_| "native_control_unconfirmed")
    }
    pub fn open_native_settings(&self, panel: SettingsPanel) -> Result<()> {
        control::request(
            &capability::read(&self.app_data)?,
            &ControlRequest::NativeOpenSettings { panel },
        )
        .map(|_| ())
    }
    pub fn stop(&self) -> Result<Value> {
        let response =
            control::request(&capability::read(&self.app_data)?, &ControlRequest::Stop {})?;
        let deadline = Instant::now() + Duration::from_secs(6);
        while self.app_data.join("owner-cli.json").exists() {
            if Instant::now() >= deadline {
                return Err("core_stop_timeout");
            }
            thread::sleep(Duration::from_millis(25));
        }
        Ok(response)
    }
    pub fn start_if_needed(&self, config: &RuntimeConfig, if_needed: bool) -> Result<Value> {
        if let Ok(health) = self.status() {
            return if if_needed {
                ready_result(health)
            } else {
                Err("runtime_already_running")
            };
        }
        let executable = std::env::current_exe().map_err(|_| "core_location_unavailable")?;
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        let mut child = match native_start_child(&executable, config) {
            Ok(child) => child,
            Err(start_error) => {
                // A concurrent starter can exit before its group handshake while
                // the canonical Core becomes ready. Never adopt that failed PID.
                // Reuse exactly the same protected health path as the initial
                // --if-needed attach, once, without weakening spawn validation.
                if if_needed {
                    if let Ok(health) = self.status() {
                        if let Ok(ready) = ready_result(health) {
                            return Ok(ready);
                        }
                    }
                }
                return Err(start_error);
            }
        };
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        let mut child = {
            let mut command = start_command(&executable, config);
            command
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            platform::configure_detached(&mut command);
            let mut child = command.spawn().map_err(|_| "core_spawn_failed")?;
            child
        };
        let deadline = Instant::now() + config.startup_timeout + Duration::from_secs(4);
        loop {
            if let Ok(health) = self.status() {
                #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
                {
                    if health["core_pid"].as_u64() != Some(child.id() as u64) && !if_needed {
                        return Err("runtime_already_running");
                    }
                    if health["core_pid"].as_u64() == Some(child.id() as u64) {
                        // Preserve the diagnostic-only Core for inspection, while
                        // still reporting an unavailable engine as a failed start.
                        child.detach()?;
                        return ready_result(health);
                    }
                }
                #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
                {
                    if health["core_pid"].as_u64() != Some(child.id() as u64) && !if_needed {
                        let _ = child.wait();
                        return Err("runtime_already_running");
                    }
                }
                // A losing concurrent starter remains owned and is reclaimed.
                return ready_result(health);
            }
            if child
                .try_wait()
                .map_err(|_| "core_monitor_failed")?
                .is_some()
            {
                // The winner of concurrent --if-needed may still be starting.
                if !if_needed || Instant::now() >= deadline {
                    return Err("core_start_failed");
                }
            }
            if Instant::now() >= deadline {
                return Err("core_start_timeout");
            }
            thread::sleep(Duration::from_millis(25));
        }
    }
}

fn start_arguments(operation: &str, config: &RuntimeConfig) -> Vec<OsString> {
    let mut args = vec![OsString::from(operation), OsString::from("--app-data"),
                        config.app_data.clone().into_os_string()];
    // Custom engines intentionally clear compiled credential pins in the child.
    let trusted_installed = config.credential_engine_sha256.is_some()
        && config.credential_engine_requirement.is_some()
        && config.engine_args.is_empty();
    if !trusted_installed {
        args.push(OsString::from("--engine"));
        args.push(config.engine.clone().into_os_string());
        for argument in &config.engine_args {
            args.push(OsString::from("--engine-arg"));
            args.push(argument.clone().into());
        }
    }
    if config.upgrade_remembered_login {
        args.push(OsString::from("--upgrade-remembered-login"));
    }
    if let Some(helper) = &config.helper {
        args.push(OsString::from("--helper"));
        args.push(helper.clone().into_os_string());
    }
    if let Some(browsers) = &config.browsers {
        args.push(OsString::from("--browsers"));
        args.push(browsers.clone().into_os_string());
    }
    args.push(OsString::from("--startup-timeout-ms"));
    args.push(config.startup_timeout.as_millis().to_string().into());
    args
}
#[cfg(any(test, not(all(target_os = "macos", target_arch = "aarch64"))))]
fn start_command(executable: &Path, config: &RuntimeConfig) -> Command {
    let mut command = Command::new(executable);
    command.args(start_arguments("serve", config));
    command
}
fn ready_result(health: Value) -> Result<Value> {
    if health["engine"] == "ready" {
        Ok(health)
    } else {
        Err("engine_unavailable")
    }
}
const HELP: &str = "Gamja core and business CLI
Usage: gamja [--app-data DIR --engine FILE --engine-arg ARG] COMMAND
Core lifecycle: serve | start | status | stop
Authenticated upgrade (Mac ARM): start --upgrade-remembered-login --app-data ISOLATED_SNAPSHOT
  upgrade-login source-proof verifies the current canonical old login without exporting it.
Runtime authority: runtime install|verify|broker-status
Reporting: report list|status|create|update|delete|export|receipt|submit|consent
  gamja report --help describes draft input and explicit submission/consent.
  start --if-needed starts or attaches; status reports the core supervisor.
Business: configure (interactive), register, login, me, logout, restore, setup, workspace, attention, readiness, request,
  provider, agent, bot, project, project-agent, template, room, rooms, members, member-add,
  member-remove, member-pause, member-resume, teammates, events, send, continue, wait, activity,
  preview, decide, grants, grant-revoke, approval-mode, room-settings,
  summary, tool-lease, unlock, unlock-status, diagnostic, api-status.
  Personal Bots: bot primary; bot primary set --json FILE; bot dm AGENT_ID [--json FILE]; bot conversation ROOM_ID.
  Bot collaboration: bot messages ROOM; bot message actions UUID; bot message retry-preview UUID; bot message retry UUID --json FILE --idempotency-key KEY.
  Scoped Stop receipts: bot scope-stops ROOM; bot scope-stop show|confirm UUID (confirm only rechecks the frozen stop).
  Personal memory: bot memory list|show|add|edit|delete AGENT_ID [MEMORY_UUID];
    changes use --json FILE (scope/item revisions) --idempotency-key KEY.
  Project lifecycle: projects active|archived|all; project archive-preview ID;
  project archive ID PLAN_HASH; project restore ID.
  Atomic room launch: room launch --json FILE [--idempotency-key KEY].
  Guided starter: setup connection-add --json FILE; setup test CONNECTION MODEL (text preflight + explicit Test);
  setup preview --json FILE; setup apply --json FILE --idempotency-key KEY; setup last-result.
  Room navigation: rooms [--status STATUS --project ID --query TITLE --limit N --cursor CURSOR];
  room pin ID; room unpin ID. Legacy rooms PROJECT remains available.
  Owner attention: attention list [--limit N --cursor CURSOR].
  Agent workload: agent workload [AGENT_ID] [--limit N --cursor CURSOR].
  Teammates: teammates ROOM (same-room colleague roster and recorded request/reply receipts).
  Context preview: room context-preview ROOM --json FILE.
  Readiness: readiness list; provider test-history ID [--limit 1..20].
  Request inspector: request inspect ROOM TRIGGER_EVENT_ID.
  Account login: login [--remember-device] (opt-in saved login; password stays on hidden input).
  Exact request: gamja run ROOM --stdin --idempotency-key KEY --timeout SECONDS;
  request wait ROOM REQUEST_UUID --timeout SECONDS.
  Targeted retry: request retry-preview ROOM FAILURE_EVENT_ID;
  request retry ROOM FAILURE_EVENT_ID PLAN_HASH [--idempotency-key KEY].
  Explicit final result: request finalize-preview ROOM REQUEST_UUID AGENT_ID;
  request finalize ROOM REQUEST_UUID AGENT_ID PLAN_HASH --idempotency-key KEY.
  Bare gamja opens the same business prompt as configure.
  api-status reports canonical API health; diagnostic shows sanitized details.
Batch: gamja --batch FILE | gamja batch FILE (- reads NDJSON from stdin)
Business input: --json FILE | --file FILE | --stdin; credentials use hidden
  TTY prompts or --secret-stdin only. Do not put credentials in arguments.
The CLI attaches to a running core; start it with gamja start --if-needed.
See docs/desktop-cli-guide.md for canonical command arguments.";

fn option(
    argument: OsString,
    arguments: &mut impl Iterator<Item = OsString>,
    config: &mut RuntimeConfig,
    if_needed: &mut bool,
) -> Result<()> {
    match argument.to_str() {
        Some("--if-needed") => *if_needed = true,
        Some("--upgrade-remembered-login") => {
            if !cfg!(all(target_os = "macos", target_arch = "aarch64")) {
                return Err("upgrade_platform_unsupported");
            }
            config.upgrade_remembered_login = true;
        }
        Some("--app-data") => {
            config.app_data = PathBuf::from(arguments.next().ok_or("argument_required")?)
        }
        Some("--engine") => {
            config.engine = PathBuf::from(arguments.next().ok_or("argument_required")?);
            config.credential_engine_sha256 = None;
            config.credential_engine_requirement = None;
        }
        Some("--engine-arg") => {
            config
                .engine_args
                .push(arguments.next().ok_or("argument_required")?);
            config.credential_engine_sha256 = None;
            config.credential_engine_requirement = None;
        }
        Some("--helper") => {
            config.helper = Some(PathBuf::from(arguments.next().ok_or("argument_required")?))
        }
        Some("--browsers") => {
            config.browsers = Some(PathBuf::from(arguments.next().ok_or("argument_required")?))
        }
        Some("--startup-timeout-ms") => {
            let ms = arguments
                .next()
                .and_then(|v| v.to_str().and_then(|s| s.parse::<u64>().ok()))
                .filter(|ms| (1..=120000).contains(ms))
                .ok_or("invalid_timeout")?;
            config.startup_timeout = Duration::from_millis(ms);
        }
        _ => return Err("unknown_argument"),
    }
    Ok(())
}

fn business(config: &RuntimeConfig, mut words: Vec<OsString>) -> Result<i32> {
    match words.first().and_then(|word| word.to_str()) {
        None => {}
        Some("configure") if words.len() == 1 => words.clear(),
        Some("batch") if words.len() == 2 => words[0] = "--batch".into(),
        Some("api-status") => words[0] = "status".into(),
        Some(
            "--batch" | "register" | "signup" | "login" | "me" | "whoami" | "logout" | "restore"
            | "restore-unlock" | "diagnostic" | "provider" | "providers" | "agent" | "agents" | "bot"
            | "project" | "projects" | "project-agent" | "template" | "setup" | "workspace" | "attention" | "readiness" | "request" | "room" | "rooms" | "members"
            | "member-add" | "member-remove" | "member-pause" | "member-resume" | "teammates" | "events" | "send"
            | "continue" | "run" | "wait" | "activity" | "preview" | "decide" | "grants" | "grant-revoke"
            | "approval-mode" | "room-settings" | "summary" | "tool-lease" | "unlock"
            | "unlock-status",
        ) => {}
        _ => return Err("unknown_command"),
    }
    if words.iter().any(|word| {
        word.to_str().is_some_and(|word| {
            word == "--capability-file"
                || word.starts_with("--capability-file=")
                || word == "--headless-binary"
                || word.starts_with("--headless-binary=")
        })
    }) {
        return Err("unknown_argument");
    }
    let exact_observer = words.first().is_some_and(|word| word == "run")
        || (words.first().is_some_and(|word| word == "request")
            && words.get(1).is_some_and(|word| word == "wait"));
    if exact_observer {
        preserve_exact_observer_interrupt()?;
    }
    // Inherit the foreground console and its pipes. This is a client process,
    // never an OwnedEngine: returning cannot terminate the running supervisor.
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    let status = {
        use crate::macos_spawn::{ForegroundChild, inherited_environment};
        let mut args = config.engine_args.clone();
        args.extend([OsString::from("--cli"), OsString::from("--capability-file"),
            config.app_data.join("owner-cli.json").into_os_string()]);
        args.extend(words.iter().cloned());
        ForegroundChild::spawn(&config.engine, &args,
            inherited_environment().map_err(|_| "business_client_failed")?,
            &[(0,0),(1,1),(2,2)])
            .and_then(|child| child.wait()).map_err(|_| "business_client_failed")?
    };
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    let status = Command::new(&config.engine)
        .args(&config.engine_args)
        .arg("--cli")
        .arg("--capability-file")
        .arg(config.app_data.join("owner-cli.json"))
        .args(words)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .status()
        .map_err(|_| "business_client_failed")?;
    #[cfg(unix)]
    {
        use std::os::unix::process::ExitStatusExt;
        Ok(status
            .code()
            .unwrap_or_else(|| 128 + status.signal().unwrap_or(1)))
    }
    #[cfg(not(unix))]
    {
        Ok(status.code().unwrap_or(1))
    }
}

pub fn run(arguments: impl Iterator<Item = OsString>) -> Result<i32> {
    let mut arguments = arguments.peekable();
    if arguments.peek().is_some_and(|word| word == "upgrade-login") {
        arguments.next();
        if arguments.next().as_deref() != Some(std::ffi::OsStr::new("source-proof"))
            || arguments.next().is_some() { return Err("unknown_argument"); }
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        {
            crate::upgrade_login::VerifiedSource::authenticate(None)?;
            println!("{}",serde_json::json!({"version":1,"source_verified":true}));
            return Ok(0);
        }
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        return Err("upgrade_platform_unsupported");
    }
    if arguments
        .peek()
        .is_some_and(|word| word == "--help" || word == "help")
    {
        arguments.next();
        if arguments.next().is_some() {
            return Err("unknown_argument");
        }
        println!("{HELP}");
        return Ok(0);
    }
    if arguments.peek().is_some_and(|word| word == "runtime") {
        arguments.next();
        return runtime_command(arguments);
    }
    let mut config = RuntimeConfig::installed()?;
    let mut if_needed = false;
    // Adapter location options precede business words, preserving every business
    // argument verbatim; lifecycle commands still accept their existing options.
    while arguments.peek().is_some_and(|word| {
        matches!(
            word.to_str(),
            Some(
                "--app-data"
                    | "--engine"
                    | "--engine-arg"
                    | "--helper"
                    | "--browsers"
                    | "--startup-timeout-ms"
                    | "--if-needed"
                    | "--upgrade-remembered-login"
            )
        )
    }) {
        let argument = arguments.next().unwrap();
        option(argument, &mut arguments, &mut config, &mut if_needed)?;
    }
    let operation = arguments.next();
    let lifecycle = operation.as_ref().is_some_and(|word| {
        matches!(word.to_str(), Some("serve" | "start" | "status" | "stop"))
            || (cfg!(all(target_os = "macos", target_arch = "aarch64"))
                && matches!(word.to_str(), Some("__relay-start" | "__adopted-serve")))
    });
    let words = if lifecycle {
        while let Some(argument) = arguments.next() {
            option(argument, &mut arguments, &mut config, &mut if_needed)?;
        }
        vec![]
    } else {
        operation.iter().cloned().chain(arguments).collect()
    };
    config.app_data = absolute(&config.app_data)?;
    config.engine = absolute(&config.engine)?;
    if config.upgrade_remembered_login
        && (if_needed || !matches!(operation.as_ref().and_then(|w|w.to_str()),
            Some("start" | "serve" | "__relay-start" | "__adopted-serve"))) {
        return Err("upgrade_candidate_invalid");
    }
    if words.first().is_some_and(|word| word == "report") {
        return report(&config, words.into_iter().skip(1));
    }
    if !lifecycle {
        return business(&config, words);
    }
    if let Some(helper) = &mut config.helper {
        *helper = absolute(helper)?;
    }
    if let Some(browsers) = &mut config.browsers {
        *browsers = absolute(browsers)?;
    }
    let client = CoreClient {
        app_data: config.app_data.clone(),
    };
    let response = match operation.as_ref().and_then(|word| word.to_str()) {
        Some("serve") => {
            Runtime::serve(config)?;
            return Ok(0);
        }
        Some("start") => client.start_if_needed(&config, if_needed)?,
        Some("status") => client.status()?,
        Some("stop") => client.stop()?,
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        Some("__relay-start") => {
            return macos_relay_start(&config);
        }
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        Some("__adopted-serve") => {
            return macos_adopted_serve(config);
        }
        _ => return Err("unknown_command"),
    };
    println!("{}", response);
    Ok(0)
}

fn runtime_command(arguments: impl Iterator<Item = OsString>) -> Result<i32> {
    let mut arguments = arguments.peekable();
    let operation = arguments
        .next()
        .and_then(|word| word.into_string().ok())
        .ok_or("runtime_command_required")?;
    if operation == "broker-status" {
        if arguments.next().is_some() {
            return Err("unknown_argument");
        }
        let response = match broker_availability() {
            BrokerAvailability::Available => serde_json::json!({
                "version": 1,
                "operation": "broker-status",
                "status": "available",
            }),
            BrokerAvailability::Unavailable {
                reason: BrokerUnavailableReason::BrokerUnwired,
            } => serde_json::json!({
                "version": 1,
                "operation": "broker-status",
                "status": "unavailable",
                "reason": "broker_unwired",
            }),
        };
        println!("{response}");
        return Ok(0);
    }
    if !matches!(operation.as_str(), "install" | "verify") {
        return Err("unknown_command");
    }

    let mut source = None;
    let mut installed = false;
    let mut manifest = None;
    let mut core = None;
    let mut engine = None;
    while let Some(argument) = arguments.next() {
        match argument.to_str() {
            Some("--source") if source.is_none() && !installed => {
                source = Some(absolute(&PathBuf::from(
                    arguments.next().ok_or("argument_required")?,
                ))?);
            }
            Some("--installed") if source.is_none() && !installed => installed = true,
            Some("--manifest-sha256") if manifest.is_none() => {
                manifest = Some(runtime_pin(arguments.next())?);
            }
            Some("--core-sha256") if core.is_none() => {
                core = Some(runtime_pin(arguments.next())?);
            }
            Some("--engine-sha256") if engine.is_none() => {
                engine = Some(runtime_pin(arguments.next())?);
            }
            _ => return Err("unknown_argument"),
        }
    }
    let pins = RuntimePins::new(
        manifest.ok_or("runtime_pin_required")?,
        core.ok_or("runtime_pin_required")?,
        engine.ok_or("runtime_pin_required")?,
    )?;
    let status = match operation.as_str() {
        "install" => {
            if installed {
                return Err("unknown_argument");
            }
            let source = source.ok_or("runtime_source_required")?;
            install_runtime(&source, &pins, expected_installed()?)?.status
        }
        "verify" => {
            let scope = match (installed, source) {
                (true, None) => RuntimeScope::InstalledForCurrentUser,
                (false, Some(source)) => RuntimeScope::CandidatePrefix(source),
                _ => return Err("runtime_scope_required"),
            };
            validate_runtime(scope, &pins)?.status
        }
        _ => unreachable!(),
    };
    let status = match status {
        RuntimeStatus::Installed => "installed",
        RuntimeStatus::AlreadyValid => "already_valid",
        RuntimeStatus::Valid => "valid",
    };
    println!(
        "{}",
        serde_json::json!({"version": 1, "operation": operation, "status": status})
    );
    Ok(0)
}

fn runtime_pin(value: Option<OsString>) -> Result<String> {
    value
        .and_then(|value| value.into_string().ok())
        .ok_or("core_runtime_pin_invalid")
}

fn absolute(path: &Path) -> Result<PathBuf> {
    if path.is_absolute() {
        Ok(path.to_owned())
    } else {
        Ok(std::env::current_dir()
            .map_err(|_| "path_unavailable")?
            .join(path))
    }
}

#[cfg(test)]
mod tests {
    use super::{business, start_arguments, start_command, HELP};
    use crate::runtime::RuntimeConfig;
    use std::{ffi::OsString, path::Path, path::PathBuf, time::Duration};

    fn config() -> RuntimeConfig {
        RuntimeConfig {
            app_data: PathBuf::from("/tmp/gamja-test-data"),
            engine: PathBuf::from("/tmp/gamja-prefix/libexec/gamja/gamja-engine"),
            engine_args: vec![],
            browsers: None,
            helper: None,
            startup_timeout: Duration::from_secs(30),
            credential_engine_sha256: Some("trusted-engine-hash"),
            credential_engine_requirement: Some("trusted-engine-requirement"),
            upgrade_remembered_login: false,
        }
    }

    #[test]
    fn all_launch_generations_preserve_the_explicit_upgrade_flag() {
        let mut value = config();
        for operation in ["serve", "__relay-start", "__adopted-serve"] {
            assert!(!start_arguments(operation, &value).iter().any(|word| word == "--upgrade-remembered-login"));
            value.upgrade_remembered_login = true;
            let words = start_arguments(operation, &value);
            assert_eq!(words[0], operation);
            assert_eq!(words.iter().filter(|word| *word == "--upgrade-remembered-login").count(), 1);
            assert!(!words.iter().any(|word| word == "--engine"));
            value.upgrade_remembered_login = false;
        }
    }

    fn args(config: &RuntimeConfig) -> Vec<OsString> {
        start_command(Path::new("/tmp/gamja-prefix/bin/gamja"), config)
            .get_args()
            .map(OsString::from)
            .collect()
    }

    #[test]
    fn trusted_installed_start_rederives_adjacent_engine_and_preserves_compiled_pins() {
        let words = args(&config());
        assert!(!words.iter().any(|word| word == "--engine"));
        assert!(!words.iter().any(|word| word == "--engine-arg"));
        assert_eq!(words.first().and_then(|word| word.to_str()), Some("serve"));
    }

    #[test]
    fn explicit_custom_engine_is_forwarded_and_remains_credential_disabled() {
        let mut custom = config();
        custom.credential_engine_sha256 = None;
        custom.credential_engine_requirement = None;
        custom.engine_args = vec![OsString::from("--fixture")];
        let words = args(&custom);
        assert!(words
            .windows(2)
            .any(|pair| pair[0] == "--engine" && pair[1] == custom.engine.as_os_str()));
        assert!(words
            .windows(2)
            .any(|pair| pair == ["--engine-arg", "--fixture"]));
    }

    #[test]
    fn template_business_family_is_documented_and_forwarded_to_the_engine() {
        assert!(HELP.contains("template"));
        let error = business(
            &config(),
            vec![OsString::from("template"), OsString::from("list"), OsString::from("4")],
        )
        .unwrap_err();
        assert_eq!(error, "business_client_failed");
    }

    #[test]
    fn workspace_business_family_is_documented_and_forwarded_to_the_engine() {
        assert!(HELP.contains("workspace"));
        let error = business(
            &config(),
            vec![OsString::from("workspace"), OsString::from("export")],
        )
        .unwrap_err();
        assert_eq!(error, "business_client_failed");
    }

    #[test]
    fn setup_business_family_is_documented_and_forwarded_to_the_engine() {
        assert!(HELP.contains("setup"));
        assert!(HELP.contains("text preflight + explicit Test"));
        let error = business(
            &config(),
            vec![OsString::from("setup"), OsString::from("last-result")],
        )
        .unwrap_err();
        assert_eq!(error, "business_client_failed");
    }

    #[test]
    fn project_archive_commands_are_documented_and_forwarded_to_the_engine() {
        assert!(HELP.contains("project archive-preview ID"));
        let error = business(
            &config(),
            vec![OsString::from("project"), OsString::from("archive-preview"), OsString::from("4")],
        )
        .unwrap_err();
        assert_eq!(error, "business_client_failed");
    }

    #[test]
    fn atomic_room_launch_is_documented_and_forwarded_to_the_engine() {
        assert!(HELP.contains("room launch --json FILE"));
        let error = business(
            &config(),
            vec![OsString::from("room"), OsString::from("launch")],
        )
        .unwrap_err();
        assert_eq!(error, "business_client_failed");
    }

    #[test]
    fn room_index_and_pins_are_documented_and_forwarded_to_the_engine() {
        assert!(HELP.contains("Room navigation: rooms"));
        assert!(HELP.contains("room pin ID"));
        let error = business(&config(), vec![OsString::from("rooms")]).unwrap_err();
        assert_eq!(error, "business_client_failed");
    }

    #[test]
    fn attention_inbox_is_documented_and_forwarded_to_the_engine() {
        assert!(HELP.contains("attention list [--limit N --cursor CURSOR]"));
        let error = business(
            &config(),
            vec![OsString::from("attention"), OsString::from("list")],
        )
        .unwrap_err();
        assert_eq!(error, "business_client_failed");
    }

    #[test]
    fn agent_workload_is_documented_and_forwarded_to_the_engine() {
        assert!(HELP.contains("agent workload [AGENT_ID] [--limit N --cursor CURSOR]"));
        let error = business(
            &config(),
            vec![OsString::from("agent"), OsString::from("workload")],
        )
        .unwrap_err();
        assert_eq!(error, "business_client_failed");
    }

    #[test]
    fn room_context_preview_is_documented_and_forwarded_to_the_engine() {
        assert!(HELP.contains("room context-preview ROOM --json FILE"));
        let error = business(
            &config(),
            vec![OsString::from("room"), OsString::from("context-preview"), OsString::from("1")],
        )
        .unwrap_err();
        assert_eq!(error, "business_client_failed");
    }

    #[test]
    fn readiness_commands_are_documented_and_forwarded_to_the_engine() {
        assert!(HELP.contains("readiness list"));
        assert!(HELP.contains("provider test-history ID"));
        let error = business(
            &config(),
            vec![OsString::from("readiness"), OsString::from("list")],
        )
        .unwrap_err();
        assert_eq!(error, "business_client_failed");
    }

    #[test]
    fn request_inspector_is_documented_and_forwarded_to_the_engine() {
        assert!(HELP.contains("request inspect ROOM TRIGGER_EVENT_ID"));
        let error = business(
            &config(),
            vec![
                OsString::from("request"),
                OsString::from("inspect"),
                OsString::from("1"),
                OsString::from("550e8400-e29b-41d4-a716-446655440000"),
            ],
        )
        .unwrap_err();
        assert_eq!(error, "business_client_failed");
    }

    #[test]
    fn exact_request_run_and_wait_are_documented_and_forwarded_to_the_engine() {
        assert!(HELP.contains("gamja run ROOM --stdin --idempotency-key KEY --timeout SECONDS"));
        assert!(HELP.contains("request wait ROOM REQUEST_UUID --timeout SECONDS"));
        for words in [
            vec![OsString::from("run"), OsString::from("1")],
            vec![OsString::from("request"), OsString::from("wait"), OsString::from("1")],
        ] {
            let error = business(&config(), words).unwrap_err();
            assert_eq!(error, "business_client_failed");
        }
    }

    #[test]
    fn targeted_retry_is_documented_and_forwarded_to_the_engine() {
        assert!(HELP.contains("request retry-preview ROOM FAILURE_EVENT_ID"));
        assert!(HELP.contains("request retry ROOM FAILURE_EVENT_ID PLAN_HASH"));
        let error = business(
            &config(),
            vec![
                OsString::from("request"),
                OsString::from("retry-preview"),
                OsString::from("1"),
                OsString::from("550e8400-e29b-41d4-a716-446655440000"),
            ],
        )
        .unwrap_err();
        assert_eq!(error, "business_client_failed");
    }
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct ReportDraftInput {
    event_ids: Vec<String>,
    description: Option<String>,
}

fn report(config: &RuntimeConfig, words: impl Iterator<Item = OsString>) -> Result<i32> {
    let mut words = words.peekable();
    let operation = words.next().ok_or("report_command_required")?;
    if operation == "--help" || operation == "help" {
        if words.next().is_some() {
            return Err("unknown_argument");
        }
        println!("gamja report list|status|create|update ID|delete ID|export ID|receipt ID|submit ID|consent on|off\ncreate/update --json reads {{event_ids, description}} from bounded stdin; output is JSON. submit sends the reviewed manual draft; consent on enables automatic reporting until core restart.");
        return Ok(0);
    }
    let id = if matches!(
        operation.to_str(),
        Some("update" | "delete" | "export" | "receipt" | "submit")
    ) {
        Some(
            words
                .next()
                .and_then(|word| word.into_string().ok())
                .ok_or("report_id_required")?,
        )
    } else {
        None
    };
    let consent = if operation == "consent" {
        Some(match words.next().as_ref().and_then(|word| word.to_str()) {
            Some("on" | "true") => true,
            Some("off" | "false") => false,
            _ => return Err("invalid_consent"),
        })
    } else {
        None
    };
    let mut json_input = false;
    let mut data = config.app_data.clone();
    while let Some(word) = words.next() {
        match word.to_str() {
            Some("--json") if !json_input => json_input = true,
            Some("--app-data") => {
                data = absolute(&PathBuf::from(words.next().ok_or("app_data_required")?))?
            }
            _ => return Err("unknown_argument"),
        }
    }
    let draft = if json_input && matches!(operation.to_str(), Some("create" | "update")) {
        let mut body = Vec::new();
        std::io::stdin()
            .take((crate::MAX_REPORT_CONTROL_BYTES + 1) as u64)
            .read_to_end(&mut body)
            .map_err(|_| "invalid_request")?;
        if body.len() > crate::MAX_REPORT_CONTROL_BYTES {
            return Err("invalid_request");
        }
        Some(serde_json::from_slice::<ReportDraftInput>(&body).map_err(|_| "invalid_request")?)
    } else {
        None
    };
    let request = match operation.to_str() {
        Some("list") => ControlRequest::ReportList {},
        Some("status") => ControlRequest::ReportStatus {},
        Some("create") => {
            let input = draft.unwrap_or(ReportDraftInput {
                event_ids: vec![],
                description: None,
            });
            ControlRequest::ReportCreate {
                event_ids: input.event_ids,
                description: input.description,
            }
        }
        Some("update") => {
            let input = draft.ok_or("report_json_required")?;
            ControlRequest::ReportUpdate {
                report_id: id.unwrap(),
                event_ids: input.event_ids,
                description: input.description,
            }
        }
        Some("delete") => ControlRequest::ReportDelete {
            report_id: id.unwrap(),
        },
        Some("export") => ControlRequest::ReportExport {
            report_id: id.unwrap(),
        },
        Some("receipt") => ControlRequest::ReportReceipt {
            report_id: id.unwrap(),
        },
        Some("submit") => ControlRequest::ReportSubmit {
            report_id: id.unwrap(),
        },
        Some("consent") => ControlRequest::ReportConsent {
            enabled: consent.unwrap(),
        },
        _ => return Err("unknown_command"),
    };
    request.validate().map_err(|_| "invalid_request")?;
    let response = control::request(&capability::read(&data)?, &request)?;
    println!("{response}");
    Ok(0)
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn native_start_child(executable: &Path, config: &RuntimeConfig) -> Result<NativeStartedCore> {
    use crate::macos_spawn::{OwnedChild, cloexec_pipe, inherited_environment};
    use std::{fs::File, io::Read, os::fd::AsRawFd};
    
    // Create handshake pipe for Core PID
    let (mut reader, writer) = cloexec_pipe().map_err(|_| "core_spawn_failed")?;
    
    // Build relay command arguments
    let args = start_arguments("__relay-start", config);

    let input = File::open("/dev/null").map_err(|_| "core_spawn_failed")?;
    let output = File::options().write(true).open("/dev/null").map_err(|_| "core_spawn_failed")?;
    let error = File::options().write(true).open("/dev/null").map_err(|_| "core_spawn_failed")?;
    
    let relay = OwnedChild::spawn_detached(
        executable,
        &args,
        inherited_environment().map_err(|_| "core_spawn_failed")?,
        &[
            (input.as_raw_fd(), 0),
            (output.as_raw_fd(), 1),
            (error.as_raw_fd(), 2),
            (writer.as_raw_fd(), 3),
        ],
        config.startup_timeout,
    )
    .map_err(|_| "core_spawn_failed")?;
    
    // Parent closes writer end
    drop(writer);
    
    // Set reader to non-blocking
    let flags = unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_GETFL) };
    if flags < 0 {
        return Err("core_handshake_failed");
    }
    if unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err("core_handshake_failed");
    }
    
    // Read exactly 4 bytes followed by EOF. FD3 must not remain in Core.
    let mut buf = [0u8; 4];
    let deadline = Instant::now() + Duration::from_secs(2);
    let mut total = 0;
    while total < 4 {
        if Instant::now() >= deadline {
            return Err("core_handshake_timeout");
        }
        match reader.read(&mut buf[total..]) {
            Ok(0) => return Err("core_handshake_eof"),
            Ok(n) => total += n,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(10));
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return Err("core_handshake_failed"),
        }
    }
    let mut extra = [0u8; 1];
    loop {
        if Instant::now() >= deadline {
            return Err("core_handshake_timeout");
        }
        match reader.read(&mut extra) {
            Ok(0) => break,
            Ok(_) => return Err("core_handshake_invalid"),
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(10));
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return Err("core_handshake_failed"),
        }
    }
    
    let core_pid = u32::from_be_bytes(buf);
    if core_pid <= 1 || core_pid == relay.id() {
        return Err("core_handshake_invalid");
    }
    
    // Verify getpgid(Core) == relay
    let pgid = unsafe { libc::getpgid(core_pid as i32) };
    if pgid < 0 || pgid as u32 != relay.id() {
        return Err("core_handshake_group_mismatch");
    }
    
    Ok(NativeStartedCore {
        relay: Some(relay),
        core_pid,
    })
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn macos_relay_start(config: &RuntimeConfig) -> Result<i32> {
    use std::os::fd::AsRawFd;
    
    // Only the detached session/group leader may delegate Core startup.
    let pid = unsafe { libc::getpid() };
    let pgrp = unsafe { libc::getpgrp() };
    if pid != pgrp || unsafe { libc::getsid(0) } != pid {
        return Err("relay_group_invalid");
    }
    
    // FD3 is a private writable pipe, never a regular file or ambient descriptor.
    let fd3 = 3;
    let mut stat: libc::stat = unsafe { std::mem::zeroed() };
    let flags = unsafe { libc::fcntl(fd3, libc::F_GETFL) };
    if unsafe { libc::fstat(fd3, &mut stat) } < 0
        || stat.st_mode & libc::S_IFMT != libc::S_IFIFO
        || flags < 0
        || flags & libc::O_ACCMODE != libc::O_WRONLY
    {
        return Err("relay_fd3_invalid");
    }
    
    // Spawn Core with __adopted-serve command
    let executable = std::env::current_exe().map_err(|_| "core_location_unavailable")?;
    let args = start_arguments("__adopted-serve", config);

    let input = std::fs::File::open("/dev/null").map_err(|_| "core_spawn_failed")?;
    let output = std::fs::File::options().write(true).open("/dev/null").map_err(|_| "core_spawn_failed")?;
    let error = std::fs::File::options().write(true).open("/dev/null").map_err(|_| "core_spawn_failed")?;
    
    let core = crate::macos_spawn::OwnedChild::spawn_inherited_group(
        &executable,
        &args,
        crate::macos_spawn::inherited_environment().map_err(|_| "core_spawn_failed")?,
        &[
            (input.as_raw_fd(), 0),
            (output.as_raw_fd(), 1),
            (error.as_raw_fd(), 2),
        ],
        config.startup_timeout,
    )
    .map_err(|_| "core_spawn_failed")?;
    
    // Write Core PID to FD3 as 4-byte big-endian
    let core_pid = core.id();
    let bytes = core_pid.to_be_bytes();
    let written = unsafe { libc::write(fd3, bytes.as_ptr() as *const libc::c_void, 4) };
    if written != 4 {
        return Err("relay_handshake_write_failed");
    }
    
    // Close FD3
    unsafe { libc::close(fd3) };
    
    // Forget Core to avoid Drop killing it
    std::mem::forget(core);
    
    // Relay exits immediately
    Ok(0)
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn macos_adopted_serve(config: RuntimeConfig) -> Result<i32> {
    // Wait for PPID to become 1 (adopted by init)
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        let ppid = unsafe { libc::getppid() };
        if ppid == 1 {
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    
    // Verify PPID is 1
    let ppid = unsafe { libc::getppid() };
    if ppid != 1 {
        return Err("core_adoption_timeout");
    }
    
    // Start runtime
    Runtime::serve(config)?;
    Ok(0)
}
