#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
mod artifact;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
mod workspace_export;
#[cfg(all(test, target_os = "macos", target_arch = "aarch64"))]
mod artifact_tests;
mod voice;
mod notifications;
mod call_speech;
mod core_args;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
mod core_client;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
mod core_runtime;
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
mod desktop;
mod local_endpoint;
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
mod reporting;
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
mod sidecar;

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
use std::sync::{
    atomic::{AtomicI32, Ordering},
    Mutex,
};
use tauri::Manager;

#[derive(serde::Serialize)]
struct StartupLoginMode { skip_auto_restore: bool }

#[tauri::command]
fn get_startup_login_mode(window: tauri::Window, mode: tauri::State<'_, StartupLoginMode>) -> Result<StartupLoginMode, String> {
    if window.label() != "main" { return Err("startup_login_mode_unavailable".into()); }
    Ok(StartupLoginMode { skip_auto_restore: mode.skip_auto_restore })
}

// Explicit Owner preview entry: one fixed local history index, no renderer path/URL.
#[tauri::command]
async fn open_release_history(window: tauri::Window, gui_build: Option<String>) -> Result<(), String> {
    if window.label() != "main" || !window.is_focused().unwrap_or(false) {
        return Err("release_history_unavailable".into());
    }
    if let Some(label) = gui_build.as_deref() {
        if !(2..=24).contains(&label.len())
            || !label.as_bytes().first().is_some_and(|first| first.is_ascii_uppercase())
            || !label.bytes().all(|value| value.is_ascii_uppercase() || value.is_ascii_digit() || value == b'-') {
            return Err("release_history_unavailable".into());
        }
    }
    #[cfg(target_os = "macos")]
    {
        let home = window.app_handle().path().home_dir()
            .map_err(|_| "release_history_unavailable".to_string())?;
        return tauri::async_runtime::spawn_blocking(move || {
            use std::os::unix::fs::MetadataExt;
            let fail = || "release_history_unavailable".to_string();
            let home_real = home.canonicalize().map_err(|_| fail())?;
            let home_meta = std::fs::metadata(&home_real).map_err(|_| fail())?;
            let folder = home_real.join("gamja-release-history");
            let index = folder.join("index.html");
            let folder_meta = std::fs::symlink_metadata(&folder).map_err(|_| fail())?;
            let index_meta = std::fs::symlink_metadata(&index).map_err(|_| fail())?;
            if !folder_meta.is_dir() || folder_meta.file_type().is_symlink()
                || !index_meta.is_file() || index_meta.file_type().is_symlink()
                || folder_meta.uid() != home_meta.uid() || index_meta.uid() != home_meta.uid()
                || folder.canonicalize().map_err(|_| fail())?.parent() != Some(home_real.as_path()) {
                return Err(fail());
            }
            let destination: std::ffi::OsString = if let Some(label) = gui_build {
                let mut url = tauri::Url::from_file_path(&index).map_err(|_| fail())?;
                // Only a validated build label enters this fixed local index hash.
                url.set_fragment(Some(&format!("q={label}&stage=installed")));
                url.to_string().into()
            } else { index.into_os_string() };
            let result = std::process::Command::new("/usr/bin/open")
                .arg(destination)
                .stdin(std::process::Stdio::null())
                .stdout(std::process::Stdio::null())
                .stderr(std::process::Stdio::null())
                .status().map_err(|_| fail())?;
            if result.success() { Ok(()) } else { Err(fail()) }
        }).await.map_err(|_| "release_history_unavailable".to_string())?;
    }
    #[cfg(not(target_os = "macos"))]
    Err("release_history_unavailable".into())
}

#[tauri::command]
fn workspace_export_transport(window: tauri::Window) -> Result<&'static str, String> {
    if window.label() != "main" { return Err("export_unavailable".into()); }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    return Ok("native");
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    return Ok("unavailable");
}

// Explicit Owner UI only. These commands read the existing Core helper status
// or open one fixed system panel; they never capture, type or grant permission.
#[tauri::command]
fn local_permissions_transport(window: tauri::Window) -> Result<&'static str, String> {
    if window.label() != "main" { return Err("local_permissions_unavailable".into()); }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    return Ok("native");
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    return Ok("unavailable");
}
#[tauri::command]
async fn read_local_permissions(window: tauri::Window) -> Result<serde_json::Value, String> {
    if window.label() != "main" { return Err("local_permissions_unavailable".into()); }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        let app = window.app_handle().clone();
        return tauri::async_runtime::spawn_blocking(move || {
            match app.state::<HostRuntime>().inner() {
                HostRuntime::Attach(core) => core.native_permission_status()
                    .map(|status| serde_json::json!({"screen_recording":status.screen_recording,"accessibility":status.accessibility}))
                    .map_err(|_| "local_permissions_unavailable".to_owned()),
            }
        }).await.map_err(|_| "local_permissions_unavailable".to_owned())?;
    }
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    Err("local_permissions_unavailable".into())
}
#[tauri::command]
async fn open_local_permission_settings(window: tauri::Window, panel: String) -> Result<(), String> {
    if window.label() != "main" || !window.is_focused().unwrap_or(false)
        || !matches!(panel.as_str(), "screen_recording" | "accessibility") {
        return Err("local_settings_unavailable".into());
    }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        let app = window.app_handle().clone();
        return tauri::async_runtime::spawn_blocking(move || {
            let panel = if panel == "screen_recording" { gamja_core_host::SettingsPanel::ScreenRecording }
                else { gamja_core_host::SettingsPanel::Accessibility };
            match app.state::<HostRuntime>().inner() {
                HostRuntime::Attach(core) => core.open_native_settings(panel)
                    .map_err(|_| "local_settings_unavailable".to_owned()),
            }
        }).await.map_err(|_| "local_settings_unavailable".to_owned())?;
    }
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    Err("local_settings_unavailable".into())
}

#[tauri::command]
fn screenshot_artifact_transport() -> &'static str {
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    return "native";
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    return "http";
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
type ReportingState = Mutex<Option<reporting::ReportStore>>;

// The handler performs only an async-signal-safe write. A normal thread then
// asks the legacy Tauri host to exit and close its sidecar owner lifeline.
#[cfg(all(target_os = "macos", not(target_arch = "aarch64")))]
static HEADLESS_SIGNAL_WRITE: AtomicI32 = AtomicI32::new(-1);

#[cfg(all(target_os = "macos", not(target_arch = "aarch64")))]
extern "C" fn headless_signal_handler(_: libc::c_int) {
    let fd = HEADLESS_SIGNAL_WRITE.load(Ordering::Relaxed);
    if fd >= 0 {
        let byte = [1u8];
        unsafe {
            libc::write(fd, byte.as_ptr().cast(), byte.len());
        }
    }
}

#[cfg(all(target_os = "macos", not(target_arch = "aarch64")))]
fn close_signal_pipe(pipe: [libc::c_int; 2]) {
    unsafe {
        libc::close(pipe[0]);
        libc::close(pipe[1]);
    }
}

#[cfg(all(target_os = "macos", not(target_arch = "aarch64")))]
fn headless_signal_pipe() -> Result<[libc::c_int; 2], String> {
    let mut pipe = [-1; 2];
    if unsafe { libc::pipe(pipe.as_mut_ptr()) } != 0 {
        return Err("Cannot configure headless shutdown".into());
    }
    let configured = unsafe {
        libc::fcntl(pipe[0], libc::F_SETFD, libc::FD_CLOEXEC) == 0
            && libc::fcntl(pipe[1], libc::F_SETFD, libc::FD_CLOEXEC) == 0
            && libc::fcntl(pipe[1], libc::F_SETFL, libc::O_NONBLOCK) == 0
    };
    if !configured {
        close_signal_pipe(pipe);
        return Err("Cannot configure headless shutdown".into());
    }
    Ok(pipe)
}

#[cfg(all(target_os = "macos", not(target_arch = "aarch64")))]
fn install_headless_signal_exit(app: tauri::AppHandle) -> Result<(), String> {
    let pipe = headless_signal_pipe()?;
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    let mut prior_int: libc::sigaction = unsafe { std::mem::zeroed() };
    action.sa_sigaction = headless_signal_handler as *const () as usize;
    // Publish before the first handler is installed, so a signal delivered
    // between SIGINT and SIGTERM registration is still observed. Every setup
    // failure clears the publication before closing descriptors.
    HEADLESS_SIGNAL_WRITE.store(pipe[1], Ordering::Release);
    unsafe {
        libc::sigemptyset(&mut action.sa_mask);
        action.sa_flags = 0;
        if libc::sigaction(libc::SIGINT, &action, &mut prior_int) != 0 {
            HEADLESS_SIGNAL_WRITE.store(-1, Ordering::Release);
            close_signal_pipe(pipe);
            return Err("Cannot configure headless shutdown".into());
        }
        if libc::sigaction(libc::SIGTERM, &action, std::ptr::null_mut()) != 0 {
            libc::sigaction(libc::SIGINT, &prior_int, std::ptr::null_mut());
            HEADLESS_SIGNAL_WRITE.store(-1, Ordering::Release);
            close_signal_pipe(pipe);
            return Err("Cannot configure headless shutdown".into());
        }
    }
    std::thread::spawn(move || {
        let mut byte = [0u8; 1];
        let _ = unsafe { libc::read(pipe[0], byte.as_mut_ptr().cast(), byte.len()) };
        // Disable notification before requesting shutdown. Keep both fds open
        // until process exit: a concurrent handler can then only get EAGAIN,
        // never write to a reused descriptor.
        HEADLESS_SIGNAL_WRITE.swap(-1, Ordering::AcqRel);
        app.exit(0);
    });
    Ok(())
}

#[cfg(all(target_os = "macos", not(target_arch = "aarch64")))]
fn parse_headless_owner(raw: Option<&str>) -> Result<Option<libc::pid_t>, String> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    let owner = raw
        .parse::<libc::pid_t>()
        .map_err(|_| "Invalid headless owner")?;
    if owner <= 1 {
        return Err("Invalid headless owner".into());
    }
    Ok(Some(owner))
}

#[cfg(all(target_os = "macos", not(target_arch = "aarch64")))]
fn headless_owner_pid() -> Result<Option<libc::pid_t>, String> {
    let raw = std::env::var("GAMJA_HEADLESS_OWNER_PID").ok();
    parse_headless_owner(raw.as_deref())
}

#[cfg(all(target_os = "macos", not(target_arch = "aarch64")))]
fn install_headless_owner_lifeline(app: tauri::AppHandle) -> Result<(), String> {
    let Some(owner) = headless_owner_pid()? else {
        return Ok(());
    };
    std::thread::spawn(move || loop {
        if unsafe { libc::getppid() } != owner {
            app.exit(0);
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(200));
    });
    Ok(())
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
#[tauri::command]
async fn report_control(
    window: tauri::Window,
    request: gamja_core_host::ControlRequest,
) -> Result<serde_json::Value, String> {
    use gamja_core_host::ControlRequest;
    if window.label() != "main" {
        return Err("reporting_unavailable".into());
    }
    request.validate().map_err(|_| "invalid_request")?;
    if !matches!(
        request,
        ControlRequest::ReportList {}
            | ControlRequest::ReportCreate { .. }
            | ControlRequest::ReportUpdate { .. }
            | ControlRequest::ReportDelete { .. }
            | ControlRequest::ReportExport { .. }
            | ControlRequest::ReportConsent { .. }
            | ControlRequest::ReportStatus {}
            | ControlRequest::ReportSubmit { .. }
            | ControlRequest::ReportFrontendFailure {}
    ) {
        return Err("invalid_request".into());
    }
    let app = window.app_handle().clone();
    tauri::async_runtime::spawn_blocking(move || match app.state::<HostRuntime>().inner() {
        HostRuntime::Attach(client) => client.report(request),
    })
    .await
    .map_err(|_| "reporting_unavailable")?
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
#[tauri::command]
async fn report_control(
    window: tauri::Window,
    request: gamja_core_host::ControlRequest,
) -> Result<serde_json::Value, String> {
    use gamja_core_host::ControlRequest;
    if window.label() != "main" {
        return Err("reporting_unavailable".into());
    }
    request.validate().map_err(|_| "invalid_request")?;
    if !matches!(
        request,
        ControlRequest::ReportList {}
            | ControlRequest::ReportCreate { .. }
            | ControlRequest::ReportUpdate { .. }
            | ControlRequest::ReportDelete { .. }
            | ControlRequest::ReportExport { .. }
            | ControlRequest::ReportConsent { .. }
            | ControlRequest::ReportStatus {}
            | ControlRequest::ReportSubmit { .. }
            | ControlRequest::ReportFrontendFailure {}
    ) {
        return Err("invalid_request".into());
    }
    let app = window.app_handle().clone();
    let store = app.state::<ReportingState>();
    let value = match request {
        ControlRequest::ReportList {} => serde_json::to_value(list_reports(window, store)?),
        ControlRequest::ReportCreate {
            event_ids,
            description,
        } => serde_json::to_value(create_manual_draft(window, store, event_ids, description)?),
        ControlRequest::ReportUpdate {
            report_id,
            event_ids,
            description,
        } => serde_json::to_value(update_manual_draft(
            window,
            store,
            report_id,
            event_ids,
            description,
        )?),
        ControlRequest::ReportDelete { report_id } => {
            serde_json::to_value(delete_report(window, store, report_id)?)
        }
        ControlRequest::ReportExport { report_id } => {
            serde_json::to_value(export_report(window, store, report_id)?)
        }
        ControlRequest::ReportConsent { enabled } => {
            serde_json::to_value(set_auto_reporting(window, store, enabled)?)
        }
        ControlRequest::ReportStatus {} => serde_json::to_value(get_report_status(
            window,
            store,
            app.state::<Option<reporting::sender::Sender>>(),
        )?),
        ControlRequest::ReportSubmit { report_id } => serde_json::to_value(
            submit_manual_report(
                window,
                store,
                app.state::<Option<reporting::sender::Sender>>(),
                report_id,
            )
            .await?,
        ),
        ControlRequest::ReportFrontendFailure {} => {
            serde_json::to_value(record_frontend_render_failure(window)?)
        }
        _ => return Err("invalid_request".into()),
    };
    value.map_err(|_| "reporting_unavailable".into())
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
fn with_report_store<T>(
    state: tauri::State<'_, ReportingState>,
    action: impl FnOnce(&mut reporting::ReportStore) -> Result<T, String>,
) -> Result<T, String> {
    let mut guard = state.lock().map_err(|_| "Reporting storage unavailable")?;
    action(guard.as_mut().ok_or("Reporting storage unavailable")?)
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
#[tauri::command]
fn list_reports(
    window: tauri::Window,
    store: tauri::State<'_, ReportingState>,
) -> Result<reporting::ReportSnapshot, String> {
    if window.label() != "main" {
        return Err("Reporting unavailable".into());
    }
    with_report_store(store, |store| Ok(store.list()))
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
#[tauri::command]
fn create_manual_draft(
    window: tauri::Window,
    store: tauri::State<'_, ReportingState>,
    selected_event_ids: Vec<String>,
    description: Option<String>,
) -> Result<reporting::SubmissionDraft, String> {
    if window.label() != "main" {
        return Err("Reporting unavailable".into());
    }
    with_report_store(store, |store| {
        store.create_manual_draft(&selected_event_ids, description)
    })
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
#[tauri::command]
fn update_manual_draft(
    window: tauri::Window,
    store: tauri::State<'_, ReportingState>,
    report_id: String,
    selected_event_ids: Vec<String>,
    description: Option<String>,
) -> Result<reporting::SubmissionDraft, String> {
    if window.label() != "main" {
        return Err("Reporting unavailable".into());
    }
    with_report_store(store, |store| {
        store.update_manual_draft(&report_id, &selected_event_ids, description)
    })
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
#[tauri::command]
fn delete_report(
    window: tauri::Window,
    store: tauri::State<'_, ReportingState>,
    report_id: String,
) -> Result<(), String> {
    if window.label() != "main" {
        return Err("Reporting unavailable".into());
    }
    with_report_store(store, |store| store.delete(&report_id))
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
#[tauri::command]
fn export_report(
    window: tauri::Window,
    store: tauri::State<'_, ReportingState>,
    report_id: String,
) -> Result<String, String> {
    if window.label() != "main" {
        return Err("Reporting unavailable".into());
    }
    with_report_store(store, |store| {
        store
            .export_report(&report_id)
            .map(|path| path.to_string_lossy().into_owned())
    })
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
#[tauri::command]
fn set_auto_reporting(
    window: tauri::Window,
    store: tauri::State<'_, ReportingState>,
    enabled: bool,
) -> Result<(bool, u64), String> {
    if window.label() != "main" {
        return Err("Reporting unavailable".into());
    }
    with_report_store(store, |store| {
        store.set_auto_consent(enabled)?;
        Ok(store.snapshot_consent())
    })
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
#[tauri::command]
fn get_report_status(
    window: tauri::Window,
    store: tauri::State<'_, ReportingState>,
    sender: tauri::State<'_, Option<reporting::sender::Sender>>,
) -> Result<reporting::ReportStatus, String> {
    if window.label() != "main" {
        return Err("Reporting unavailable".into());
    }
    with_report_store(store, |store| {
        let (auto_reporting_enabled, consent_generation) = store.snapshot_consent();
        Ok(reporting::ReportStatus {
            auto_reporting_enabled,
            consent_generation,
            submission_available: sender.is_some(),
        })
    })
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
#[tauri::command]
async fn submit_manual_report(
    window: tauri::Window,
    store: tauri::State<'_, ReportingState>,
    sender: tauri::State<'_, Option<reporting::sender::Sender>>,
    report_id: String,
) -> Result<reporting::Receipt, String> {
    if window.label() != "main" {
        return Err("Reporting unavailable".into());
    }
    let sender = sender.as_ref().ok_or("Submission unavailable")?;
    sender
        .submit_manual(&store, &report_id)
        .await
        .map_err(|error| match error {
            reporting::sender::SendError::Unavailable => "Submission unavailable".into(),
            reporting::sender::SendError::Retryable => "Submission pending".into(),
            reporting::sender::SendError::Rejected => "Report rejected".into(),
        })
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
#[tauri::command]
fn record_frontend_render_failure(window: tauri::Window) -> Result<(), String> {
    if window.label() != "main" {
        return Err("Reporting unavailable".into());
    }
    reporting::record_typed_failure(
        window.app_handle(),
        reporting::Component::Frontend,
        reporting::Stage::Render,
        reporting::Code::FrontendRenderFailed,
    )
}

// Vault secret entry uses a native secure field; the renderer supplies metadata only.
#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn vault_prompt(window:&tauri::Window, cancellation:std::os::unix::net::UnixStream, heading:&str, allow_blank:bool)->Result<gamja_core_host::OwnerSecret,String> {
    use objc2::{class,msg_send,rc::{Allocated,Retained},runtime::AnyObject,encode::{Encode,Encoding}};
    #[repr(C)] #[derive(Clone,Copy)] struct Point { x:f64, y:f64 }
    #[repr(C)] #[derive(Clone,Copy)] struct Size { width:f64, height:f64 }
    #[repr(C)] #[derive(Clone,Copy)] struct Rect { origin:Point, size:Size }
    unsafe impl Encode for Point { const ENCODING:Encoding=Encoding::Struct("CGPoint",&[f64::ENCODING,f64::ENCODING]); }
    unsafe impl Encode for Size { const ENCODING:Encoding=Encoding::Struct("CGSize",&[f64::ENCODING,f64::ENCODING]); }
    unsafe impl Encode for Rect { const ENCODING:Encoding=Encoding::Struct("CGRect",&[Point::ENCODING,Size::ENCODING]); }
    if window.label()!="main" || !window.is_focused().unwrap_or(false) {return Err("vault_unavailable".into());}
    unsafe {
        let alert:Retained<AnyObject>=msg_send![class!(NSAlert),new];
        let title=std::ffi::CString::new(heading).map_err(|_| "vault_unavailable")?;
        let yes=std::ffi::CString::new("저장").map_err(|_| "vault_unavailable")?;
        let no=std::ffi::CString::new("취소").map_err(|_| "vault_unavailable")?;
        let title:Retained<AnyObject>=msg_send![class!(NSString),stringWithUTF8String:title.as_ptr()];
        let yes:Retained<AnyObject>=msg_send![class!(NSString),stringWithUTF8String:yes.as_ptr()];
        let no:Retained<AnyObject>=msg_send![class!(NSString),stringWithUTF8String:no.as_ptr()];
        let _:()=msg_send![&*alert,setMessageText:&*title];
        let _: *mut AnyObject=msg_send![&*alert,addButtonWithTitle:&*yes];
        let _: *mut AnyObject=msg_send![&*alert,addButtonWithTitle:&*no];
        let field:Allocated<AnyObject>=msg_send![class!(NSSecureTextField),alloc];
        let frame=Rect{origin:Point{x:0.0,y:0.0},size:Size{width:320.0,height:26.0}};
        let field:Retained<AnyObject>=msg_send![field,initWithFrame:frame];
        let _:()=msg_send![&*alert,setAccessoryView:&*field];
        let modal:*mut AnyObject=msg_send![&*alert,window];
        let modal=modal as usize;
        let waiting=std::sync::Arc::new(std::sync::atomic::AtomicBool::new(true));
        let running=waiting.clone();let app=window.app_handle().clone();
        let watcher=std::thread::spawn(move || {
            use std::os::fd::AsRawFd;
            while running.load(std::sync::atomic::Ordering::SeqCst) {
                let mut fd=libc::pollfd{fd:cancellation.as_raw_fd(),events:libc::POLLIN,revents:0};
                let n=libc::poll(&mut fd,1,100);
                let mut byte=0u8;
                let closed=n<0 || (n>0 && (fd.revents & (libc::POLLHUP|libc::POLLERR|libc::POLLNVAL)!=0
                    || (fd.revents & libc::POLLIN!=0 && libc::recv(fd.fd,&mut byte as *mut _ as *mut _,1,libc::MSG_PEEK|libc::MSG_DONTWAIT)==0)));
                if closed {
                    let still=running.clone();
                    let _=app.run_on_main_thread(move || {
                        if still.load(std::sync::atomic::Ordering::SeqCst) {
                            let nsapp:Retained<AnyObject>=msg_send![class!(NSApplication),sharedApplication];
                            let current:*mut AnyObject=msg_send![&*nsapp,modalWindow];
                            if current as usize==modal {let _:()=msg_send![&*nsapp,abortModal];}
                        }
                    });
                    break;
                }
            }
        });
        let response:isize=msg_send![&*alert,runModal];
        waiting.store(false,std::sync::atomic::Ordering::SeqCst);
        let _=watcher.join();
        if response!=1000 {return Err("vault_cancelled".into());}
        let string:Retained<AnyObject>=msg_send![&*field,stringValue];
        let ptr:*const std::ffi::c_char=msg_send![&*string,UTF8String];
        if ptr.is_null() {return Err("vault_unavailable".into());}
        let value=std::ffi::CStr::from_ptr(ptr).to_str().map_err(|_| "vault_unavailable")?;
        if (!allow_blank && value.is_empty()) || value.len()>8192 {return Err("vault_unavailable".into());}
        let secret=gamja_core_host::OwnerSecret::new(value.to_owned());
        let empty=std::ffi::CString::new("").map_err(|_| "vault_unavailable")?;
        let empty:Retained<AnyObject>=msg_send![class!(NSString),stringWithUTF8String:empty.as_ptr()];
        let _:()=msg_send![&*field,setStringValue:&*empty];
        Ok(secret)
    }
}

#[tauri::command]
async fn vault_native(window:tauri::Window, operation:String, owner_id:u64, owner_epoch:String, item_id:String, expected_revision:Option<u64>, command_id:Option<String>, website:Option<String>, username:Option<String>) -> Result<serde_json::Value,String> {
    // open_and_fill has no browser effect until vault_finish sends commit.
    let receipt_command=command_id.clone();
    let not_started=||serde_json::json!({"version":1,"status":"unavailable","revision":expected_revision,"command_id":receipt_command.clone()});
    if window.label()!="main" || !window.is_focused().unwrap_or(false) || !matches!(operation.as_str(),"status"|"replace"|"delete"|"open_and_fill"|"close_browser") {
        return if operation=="open_and_fill" {Ok(not_started())} else {Err("vault_unavailable".into())};
    }
    #[cfg(all(target_os="macos",target_arch="aarch64"))] {
        let app=window.app_handle().clone();let begin_app=app.clone();let op=operation.clone();
        let begin=tauri::async_runtime::spawn_blocking(move || match begin_app.state::<HostRuntime>().inner() {
            HostRuntime::Attach(core)=>core.vault_begin(&op,owner_id,&owner_epoch,&item_id,expected_revision,command_id.as_deref(),website.as_deref(),username.as_deref()),
        }).await;
        let exchange=match begin {
            Ok(Ok(exchange))=>exchange,
            _=>return if operation=="open_and_fill" {Ok(not_started())} else {Err("vault_unavailable".into())},
        };
        let secret=if operation=="replace" {
            let cancellation=exchange.cancellation_socket().map_err(|_| "vault_unavailable".to_string())?;
            let prompt_window=window.clone();let (tx,rx)=tokio::sync::oneshot::channel();
            app.run_on_main_thread(move || {let _=tx.send(vault_prompt(&prompt_window,cancellation,"저장할 비밀번호를 입력하세요",false));}).map_err(|_| "vault_unavailable".to_string())?;
            Some(rx.await.map_err(|_| "vault_unavailable".to_string())??)
        } else {None};
        if !window.is_focused().unwrap_or(false) {
            return if operation=="open_and_fill" {Ok(not_started())} else {Err("vault_unavailable".into())};
        }
        // From this point commit may have been sent: any failure stays unknown.
        return tauri::async_runtime::spawn_blocking(move || match app.state::<HostRuntime>().inner() {
            HostRuntime::Attach(core)=>core.vault_finish(exchange,secret),
        }).await.map_err(|_| "vault_unavailable".to_string())?;
    }
    #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
    {let _=(owner_id,owner_epoch,item_id,expected_revision,command_id,website,username);if operation=="open_and_fill" {Ok(not_started())} else {Err("vault_unavailable".into())}}
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn vault_confirm_handoff(window:&tauri::Window,heading:String,cancellation:std::os::unix::net::UnixStream, sender:tokio::sync::oneshot::Sender<Result<(),String>>)->Result<(),String> {
    use objc2::{class,msg_send,rc::Retained,runtime::AnyObject};
    use std::sync::{Arc,Mutex,atomic::{AtomicBool,Ordering}};
    if window.label()!="main" || !window.is_focused().unwrap_or(false) {return Err("vault_unavailable".into());}
    unsafe {
        let parent=window.ns_window().map_err(|_|"vault_unavailable")? as *mut AnyObject;
        if parent.is_null() {return Err("vault_unavailable".into());}
        let attached:*mut AnyObject=msg_send![parent,attachedSheet];
        if !attached.is_null() {return Err("vault_unavailable".into());}
        let alert:Retained<AnyObject>=msg_send![class!(NSAlert),new];
        let heading=std::ffi::CString::new(heading).map_err(|_| "vault_unavailable")?;
        let yes=std::ffi::CString::new("입력").map_err(|_| "vault_unavailable")?;
        let no=std::ffi::CString::new("취소").map_err(|_| "vault_unavailable")?;
        let heading:Retained<AnyObject>=msg_send![class!(NSString),stringWithUTF8String:heading.as_ptr()];
        let yes:Retained<AnyObject>=msg_send![class!(NSString),stringWithUTF8String:yes.as_ptr()];
        let no:Retained<AnyObject>=msg_send![class!(NSString),stringWithUTF8String:no.as_ptr()];
        let _:()=msg_send![&*alert,setMessageText:&*heading];
        let _: *mut AnyObject=msg_send![&*alert,addButtonWithTitle:&*yes];
        let _: *mut AnyObject=msg_send![&*alert,addButtonWithTitle:&*no];
        let sheet:*mut AnyObject=msg_send![&*alert,window];
        if sheet.is_null() {return Err("vault_unavailable".into());}
        let sheet_id=sheet as usize;
        let done=Arc::new(AtomicBool::new(false));let completed=done.clone();
        let sender=Arc::new(Mutex::new(Some(sender)));let result_sender=sender.clone();
        // Keep the alert alive until AppKit has completed its sheet callback.
        let retained_alert=alert.clone();
        let handler=block2::RcBlock::new(move |response:isize| {
            let _keep_alive=&retained_alert;
            if !completed.swap(true,Ordering::SeqCst) {
                if let Ok(mut slot)=result_sender.lock() {if let Some(tx)=slot.take() {let _=tx.send(if response==1000 {Ok(())} else {Err("vault_cancelled".into())});}}
            }
        });
        let _:()=msg_send![&*alert,beginSheetModalForWindow:parent completionHandler:&*handler];
        let app=window.app_handle().clone();let watched_window=window.clone();
        std::thread::spawn(move || {use std::os::fd::AsRawFd;
            while !done.load(Ordering::SeqCst) {
                let mut fd=libc::pollfd{fd:cancellation.as_raw_fd(),events:libc::POLLIN,revents:0};let n=libc::poll(&mut fd,1,100);let mut byte=0u8;
                if n<0 || (n>0 && (fd.revents & (libc::POLLHUP|libc::POLLERR|libc::POLLNVAL)!=0 || (fd.revents & libc::POLLIN!=0 && libc::recv(fd.fd,&mut byte as *mut _ as *mut _,1,libc::MSG_PEEK|libc::MSG_DONTWAIT)==0))) {
                    if !done.swap(true,Ordering::SeqCst) {
                        if let Ok(mut slot)=sender.lock() {if let Some(tx)=slot.take() {let _=tx.send(Err("vault_unavailable".into()));}}
                        let _=app.run_on_main_thread(move || {if let Ok(raw)=watched_window.ns_window() {let parent=raw as *mut AnyObject;if !parent.is_null() {let attached:*mut AnyObject=msg_send![parent,attachedSheet];if attached as usize==sheet_id {let _:()=msg_send![parent,endSheet:attached returnCode:0isize];}}}});
                    }
                    break;
                }
            }
        });
        Ok(())
    }
}

#[tauri::command]
async fn vault_profile_native(window:tauri::Window,operation:String,owner_id:u64,owner_epoch:String,handoff_id:String)->Result<serde_json::Value,String> {
    if window.label()!="main" || !window.is_focused().unwrap_or(false) || !matches!(operation.as_str(),"open_and_fill"|"close"|"park") {return Err("vault_unavailable".into());}
    #[cfg(all(target_os="macos",target_arch="aarch64"))] {
        let app=window.app_handle().clone();let begin_app=app.clone();let op=operation.clone();let begin_id=handoff_id.clone();
        let exchange=tauri::async_runtime::spawn_blocking(move || match begin_app.state::<HostRuntime>().inner() {HostRuntime::Attach(core)=>core.vault_begin_profile(&op,owner_id,&owner_epoch,&begin_id)}).await.map_err(|_|"vault_unavailable".to_string())??;
        if operation=="open_and_fill" {
            let cancellation=exchange.cancellation_socket().map_err(|_|"vault_unavailable".to_string())?;
            let heading=if exchange.ready.account_label.is_empty() {format!("{} · {} · {} 에 로그인 정보 입력",exchange.ready.origin,exchange.ready.username,exchange.ready.profile_label)} else {format!("{} · {} · {} ({}) 에 로그인 정보 입력",exchange.ready.origin,exchange.ready.username,exchange.ready.profile_label,exchange.ready.account_label)};
            let prompt_window=window.clone();let (tx,rx)=tokio::sync::oneshot::channel();
            app.run_on_main_thread(move || {let _=vault_confirm_handoff(&prompt_window,heading,cancellation,tx);}).map_err(|_|"vault_unavailable".to_string())?;
            match rx.await.map_err(|_|"vault_unavailable".to_string())? {
                Ok(())=>{},
                Err(e) if e=="vault_cancelled"=>return Ok(serde_json::json!({"version":1,"status":exchange.ready.profile_status,"handoff_id":handoff_id,"profile_id":exchange.ready.profile_id,"profile_revision":exchange.ready.profile_revision,"fill_status":"cancelled"})),
                Err(e)=>return Err(e),
            }
        }
        if !window.is_focused().unwrap_or(false) {return Err("vault_unavailable".into());}
        return tauri::async_runtime::spawn_blocking(move || match app.state::<HostRuntime>().inner() {HostRuntime::Attach(core)=>core.vault_finish_profile(exchange)}).await.map_err(|_|"vault_unavailable".to_string())?;
    }
    #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
    {let _=(owner_id,owner_epoch,handoff_id);Err("vault_unavailable".into())}
}

#[tauri::command]
async fn vault_card_native(window:tauri::Window,operation:String,owner_id:u64,owner_epoch:String,item_id:String,expected_revision:Option<u64>,command_id:Option<String>,metadata:Option<serde_json::Value>,website:Option<String>)->Result<serde_json::Value,String> {
    let mutating=matches!(operation.as_str(),"create"|"update"|"delete"|"open_and_fill");
    let receipt_command=command_id.clone();
    let not_started=||serde_json::json!({"version":1,"status":"not_started","revision":expected_revision,"command_id":receipt_command.clone(),"last4":null});
    if window.label()!="main" || !window.is_focused().unwrap_or(false) || !matches!(operation.as_str(),"create"|"update"|"status"|"delete"|"open_and_fill") {return if mutating {Ok(not_started())} else {Err("vault_unavailable".into())};}
    #[cfg(all(target_os="macos",target_arch="aarch64"))] {
        let prepared:Result<_,String>=async {
        let metadata:Option<gamja_core_host::vault_card::CardMetadata>=metadata.map(serde_json::from_value).transpose().map_err(|_| "vault_unavailable".to_string())?;
        let target=metadata.as_ref().map(|m|if let Some(last)=&m.last4 {format!("{} · {:02}/{} · 끝자리 {}",m.cardholder,m.expiry_month,m.expiry_year,last)} else {format!("{} · {:02}/{}",m.cardholder,m.expiry_month,m.expiry_year)}).unwrap_or_default();
        let confirm_site=website.clone();
        let app=window.app_handle().clone();let begin_app=app.clone();let op=operation.clone();
        let exchange=tauri::async_runtime::spawn_blocking(move || match begin_app.state::<HostRuntime>().inner() {
            HostRuntime::Attach(core)=>core.vault_begin_card(&op,owner_id,&owner_epoch,&item_id,expected_revision,command_id.as_deref(),metadata,website.as_deref()),
        }).await.map_err(|_| "vault_unavailable".to_string())??;
        if operation=="open_and_fill" {
            let site=confirm_site.ok_or("vault_unavailable".to_string())?;let prompt_window=window.clone();let confirm_target=target.clone();let cancellation=exchange.cancellation_socket().map_err(|_| "vault_unavailable".to_string())?;let (tx,rx)=tokio::sync::oneshot::channel();
            app.run_on_main_thread(move || {let _=vault_confirm_handoff(&prompt_window,format!("{}에 카드 {} 입력",site,confirm_target),cancellation,tx);}).map_err(|_| "vault_unavailable".to_string())?;
            rx.await.map_err(|_| "vault_unavailable".to_string())??;
        }
        let input=if operation=="create" || operation=="update" {
            let pan=if operation=="create" {
                let cancellation=exchange.cancellation_socket().map_err(|_| "vault_unavailable".to_string())?;
                let prompt_window=window.clone();let heading=format!("카드 추가 · {} · 카드 번호",target);let (tx,rx)=tokio::sync::oneshot::channel();
                app.run_on_main_thread(move || {let _=tx.send(vault_prompt(&prompt_window,cancellation,&heading,false));}).map_err(|_| "vault_unavailable".to_string())?;
                rx.await.map_err(|_| "vault_unavailable".to_string())??
            } else {gamja_core_host::OwnerSecret::new(String::new())};
            let cancellation=exchange.cancellation_socket().map_err(|_| "vault_unavailable".to_string())?;
            let prompt_window=window.clone();let heading=if operation=="create" {format!("카드 추가 · {} · 보안 코드 (선택)",target)} else {format!("카드 수정 · {} · 보안 코드 (비워두면 유지)",target)};let (tx,rx)=tokio::sync::oneshot::channel();
            app.run_on_main_thread(move || {let _=tx.send(vault_prompt(&prompt_window,cancellation,&heading,true));}).map_err(|_| "vault_unavailable".to_string())?;
            let cvv=rx.await.map_err(|_| "vault_unavailable".to_string())??;
            Some(gamja_core_host::vault_card::NativeCardSecret::from_parts(pan,cvv))
        } else {None};
        if !window.is_focused().unwrap_or(false) {return Err("vault_unavailable".into());}
        Ok((app,exchange,input))
        }.await;
        let (app,exchange,input)=match prepared {Ok(value)=>value,Err(e)=>return if matches!(operation.as_str(),"create"|"update"|"open_and_fill") && e=="vault_cancelled" {Ok(serde_json::json!({"version":1,"status":"cancelled","revision":expected_revision,"command_id":receipt_command.clone(),"last4":null}))} else if mutating {Ok(not_started())} else {Err(e)}};
        // After finish begins, a frame may have reached Core; errors remain unknown.
        return tauri::async_runtime::spawn_blocking(move || match app.state::<HostRuntime>().inner() {
            HostRuntime::Attach(core)=>core.vault_finish_card(exchange,input),
        }).await.map_err(|_| "vault_unavailable".to_string())?;
    }
    #[cfg(not(all(target_os="macos",target_arch="aarch64")))]
    {let _=(owner_id,owner_epoch,item_id,expected_revision,command_id,metadata,website);if mutating {Ok(not_started())} else {Err("vault_unavailable".into())}}
}

#[cfg(all(target_os="macos",target_arch="aarch64"))]
fn vault_csv_open_panel(window:&tauri::Window)->Result<Option<std::fs::File>,String> {
    use objc2::{class,msg_send,rc::Retained,runtime::{AnyObject,Bool}};
    use std::os::fd::FromRawFd;
    if window.label()!="main" || !window.is_focused().unwrap_or(false) {return Err("vault_unavailable".into());}
    unsafe {
        let panel:Retained<AnyObject>=msg_send![class!(NSOpenPanel),openPanel];
        let _:()=msg_send![&*panel,setCanChooseFiles:Bool::YES];
        let _:()=msg_send![&*panel,setCanChooseDirectories:Bool::NO];
        let _:()=msg_send![&*panel,setAllowsMultipleSelection:Bool::NO];
        let ext=std::ffi::CString::new("csv").map_err(|_| "vault_unavailable")?;
        let ext:Retained<AnyObject>=msg_send![class!(NSString),stringWithUTF8String:ext.as_ptr()];
        let allowed:Retained<AnyObject>=msg_send![class!(NSArray),arrayWithObject:&*ext];
        let _:()=msg_send![&*panel,setAllowedFileTypes:&*allowed];
        let _:()=msg_send![&*panel,setAllowsOtherFileTypes:Bool::NO];
        let result:isize=msg_send![&*panel,runModal];
        if result==0 {return Ok(None);}if result!=1 {return Err("vault_unavailable".into());}
        let url:Option<Retained<AnyObject>>=msg_send![&*panel,URL];let url=url.ok_or("vault_unavailable")?;
        let path:*const std::ffi::c_char=msg_send![&*url,fileSystemRepresentation];
        if path.is_null() {return Err("vault_unavailable".into());}
        let fd=libc::open(path,libc::O_RDONLY|libc::O_CLOEXEC|libc::O_NOFOLLOW);
        if fd<0 {return Err("vault_unavailable".into());}
        Ok(Some(std::fs::File::from_raw_fd(fd)))
    }
}
#[tauri::command]
async fn vault_csv_choose(window:tauri::Window,owner_id:u64,owner_epoch:String)->Result<serde_json::Value,String> {
    if window.label()!="main" || !window.is_focused().unwrap_or(false) {return Err("vault_unavailable".into());}
    #[cfg(all(target_os="macos",target_arch="aarch64"))] {
        let app=window.app_handle().clone();let open_app=app.clone();
        let exchange=tauri::async_runtime::spawn_blocking(move || match open_app.state::<HostRuntime>().inner() {
            HostRuntime::Attach(core)=>core.vault_begin_csv("csv_choose",owner_id,&owner_epoch,None,None,None,None,None,None),
        }).await.map_err(|_| "vault_unavailable".to_string())??;
        let cancel=exchange.cancellation_socket().map_err(|_| "vault_unavailable".to_string())?;
        let panel_window=window.clone();let (tx,rx)=tokio::sync::oneshot::channel();
        app.run_on_main_thread(move || {let _=tx.send(vault_csv_open_panel(&panel_window));}).map_err(|_| "vault_unavailable".to_string())?;
        let mut file=rx.await.map_err(|_| "vault_unavailable".to_string())??.ok_or("cancelled")?;
        if !window.is_focused().unwrap_or(false) {return Err("vault_unavailable".into());}
        return tauri::async_runtime::spawn_blocking(move || {
            let input=gamja_core_host::vault_csv_import::OwnedCsvInput::read(&mut file,&cancel).map_err(|_| "vault_unavailable".to_string())?;
            match app.state::<HostRuntime>().inner(){HostRuntime::Attach(core)=>core.vault_finish_csv(exchange,Some(input))}
        }).await.map_err(|_| "vault_unavailable".to_string())?;
    }
    #[cfg(not(all(target_os="macos",target_arch="aarch64")))] {let _=(owner_id,owner_epoch);Err("vault_unavailable".into())}
}
#[tauri::command]
async fn vault_csv_action(window:tauri::Window,operation:String,owner_id:u64,owner_epoch:String,draft_id:String,row:Option<u64>,offset:Option<u64>,limit:Option<u64>,item_id:Option<String>,command_id:Option<String>)->Result<serde_json::Value,String> {
    if window.label()!="main" || !window.is_focused().unwrap_or(false) || !matches!(operation.as_str(),"csv_page"|"csv_cancel"|"csv_store_row"|"csv_reconcile"|"csv_skip_row") {return Err("vault_unavailable".into());}
    #[cfg(all(target_os="macos",target_arch="aarch64"))] {
        let app=window.app_handle().clone();
        return tauri::async_runtime::spawn_blocking(move || match app.state::<HostRuntime>().inner() {
            HostRuntime::Attach(core)=>{let exchange=core.vault_begin_csv(&operation,owner_id,&owner_epoch,Some(&draft_id),row,offset,limit,item_id.as_deref(),command_id.as_deref())?;core.vault_finish_csv(exchange,None)}
        }).await.map_err(|_| "vault_unavailable".to_string())?;
    }
    #[cfg(not(all(target_os="macos",target_arch="aarch64")))] {let _=(owner_id,owner_epoch,draft_id,row,offset,limit,item_id,command_id);Err("vault_unavailable".into())}
}

// Secret bytes travel only through the Owner Core control channel. Never log input or errors.
#[tauri::command]
async fn slack_credential_status(window:tauri::Window,owner_id:u64)->Result<serde_json::Value,String> {
    if window.label()!="main" {return Err("slack_credential_unavailable".into())}
    #[cfg(all(target_os="macos",target_arch="aarch64"))] {
        let app=window.app_handle().clone();
        return tauri::async_runtime::spawn_blocking(move || match app.state::<HostRuntime>().inner(){HostRuntime::Attach(core)=>core.slack_credential_status(owner_id)}).await.map_err(|_|"slack_credential_unavailable".to_string())?;
    }
    #[cfg(not(all(target_os="macos",target_arch="aarch64")))] {let _=owner_id;Err("slack_credential_unavailable".into())}
}
#[tauri::command]
async fn slack_credential_store(window:tauri::Window,owner_id:u64,owner_epoch:String,core_epoch:String,account_id:String,expected_revision:u64,operation_id:String,app_token:String,bot_token:String)->Result<serde_json::Value,String> {
    let app_token=gamja_core_host::OwnerSecret::new(app_token);let bot_token=gamja_core_host::OwnerSecret::new(bot_token);
    if window.label()!="main" || !window.is_focused().unwrap_or(false) {return Err("slack_credential_unavailable".into())}
    #[cfg(all(target_os="macos",target_arch="aarch64"))] {
        let app=window.app_handle().clone();
        return tauri::async_runtime::spawn_blocking(move || match app.state::<HostRuntime>().inner(){HostRuntime::Attach(core)=>core.slack_credential_store(owner_id,owner_epoch,core_epoch,account_id,expected_revision,operation_id,app_token,bot_token)}).await.map_err(|_|"slack_credential_unavailable".to_string())?;
    }
    #[cfg(not(all(target_os="macos",target_arch="aarch64")))] {let _=(owner_id,owner_epoch,core_epoch,account_id,expected_revision,operation_id,app_token,bot_token);Err("slack_credential_unavailable".into())}
}
#[tauri::command]
async fn slack_credential_delete(window:tauri::Window,owner_id:u64,owner_epoch:String,core_epoch:String,account_id:String,expected_revision:u64,operation_id:String)->Result<serde_json::Value,String> {
    if window.label()!="main" || !window.is_focused().unwrap_or(false) {return Err("slack_credential_unavailable".into())}
    #[cfg(all(target_os="macos",target_arch="aarch64"))] {
        let app=window.app_handle().clone();
        return tauri::async_runtime::spawn_blocking(move || match app.state::<HostRuntime>().inner(){HostRuntime::Attach(core)=>core.slack_credential_delete(owner_id,owner_epoch,core_epoch,account_id,expected_revision,operation_id)}).await.map_err(|_|"slack_credential_unavailable".to_string())?;
    }
    #[cfg(not(all(target_os="macos",target_arch="aarch64")))] {let _=(owner_id,owner_epoch,core_epoch,account_id,expected_revision,operation_id);Err("slack_credential_unavailable".into())}
}

#[tauri::command]
async fn mcp_store_secret(window: tauri::Window, server_id: String, expected_revision: u64, operation_id: String, secret: String) -> Result<serde_json::Value, String> {
    let secret = gamja_core_host::OwnerSecret::new(secret);
    if window.label() != "main" || !window.is_focused().unwrap_or(false) { return Err("mcp_secret_unavailable".into()); }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        let app = window.app_handle().clone();
        return tauri::async_runtime::spawn_blocking(move || match app.state::<HostRuntime>().inner() {
            HostRuntime::Attach(core) => core.store_mcp_secret(server_id, expected_revision, operation_id, secret),
        }).await.map_err(|_| "mcp_secret_unavailable".to_string())?;
    }
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    { let _ = (server_id, expected_revision, operation_id, secret); Err("mcp_secret_unavailable".into()) }
}

#[tauri::command]
async fn mcp_secret_status(window: tauri::Window, server_id: String, expected_revision: u64, operation_id: String) -> Result<serde_json::Value, String> {
    if window.label() != "main" || !window.is_focused().unwrap_or(false) { return Err("mcp_secret_unavailable".into()); }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        let app = window.app_handle().clone();
        return tauri::async_runtime::spawn_blocking(move || match app.state::<HostRuntime>().inner() {
            HostRuntime::Attach(core) => core.mcp_secret_status(server_id, expected_revision, operation_id),
        }).await.map_err(|_| "mcp_secret_unavailable".to_string())?;
    }
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    { let _ = (server_id, expected_revision, operation_id); Err("mcp_secret_unavailable".into()) }
}

// OAuth authorization URLs and protocol secrets remain exclusively in this process.
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn mcp_oauth_public(value: &serde_json::Value, server_id: &str, revision: u64, operation_id: &str) -> Result<serde_json::Value, String> {
    let fail = || "mcp_oauth_unavailable".to_string();
    if value.get("version").and_then(|v|v.as_u64()) != Some(1) || value.get("provider").and_then(|v|v.as_str()) != Some("notion")
        || value.get("server_id").and_then(|v|v.as_str()) != Some(server_id) || value.get("config_revision").and_then(|v|v.as_u64()) != Some(revision)
        || value.get("operation_id").and_then(|v|v.as_str()) != Some(operation_id) { return Err(fail()); }
    let state=value.get("state").and_then(|v|v.as_str()).ok_or_else(fail)?;
    if !matches!(state,"preparing"|"authorizing"|"authorized"|"failed"|"cancelled"|"expired") { return Err(fail()); }
    let mut out=serde_json::json!({"version":1,"provider":"notion","server_id":server_id,"config_revision":revision,"operation_id":operation_id,"state":state});
    if state=="authorized" {
        let credential=value.get("credential_ref").and_then(|v|v.as_str()).ok_or_else(fail)?;
        let suffix=format!(":{}:{}",server_id,operation_id);
        let owner=credential.strip_prefix("mcp-oauth:").and_then(|v|v.strip_suffix(&suffix)).ok_or_else(fail)?;
        if owner.is_empty() || owner.len()>20 || !owner.bytes().all(|v|v.is_ascii_digit()) || owner.starts_with('0') { return Err(fail()); }
        out["credential_ref"]=serde_json::Value::String(credential.to_string());
        if let Some(workspace)=value.get("workspace").filter(|v|!v.is_null()) {
            let id=workspace.get("id").and_then(|v|v.as_str()).ok_or_else(fail)?;
            if id.is_empty() || id.len()>256 || id.chars().any(char::is_control) { return Err(fail()); }
            let name=workspace.get("name").filter(|v|!v.is_null()).map(|v|v.as_str().ok_or_else(fail)).transpose()?;
            if name.is_some_and(|v|v.len()>256 || v.chars().any(char::is_control)) { return Err(fail()); }
            out["workspace"]=serde_json::json!({"id":id,"name":name});
        }
    }
    Ok(out)
}
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
async fn mcp_oauth_call(window: tauri::Window, server_id: String, expected_revision: u64, operation_id: String, method: &'static str) -> Result<serde_json::Value, String> {
    let code=|status_code:&str| if method=="status" {status_code.to_string()} else {"mcp_oauth_unavailable".to_string()};
    if window.label()!="main" || !window.is_focused().unwrap_or(false) {return Err(code("status_focus_initial"))}
    if expected_revision==0 || operation_id.len()!=32 || !operation_id.bytes().all(|v|v.is_ascii_digit()||(b'a'..=b'f').contains(&v)) {return Err(code("status_input_invalid"))}
    let app=window.app_handle().clone();let sid=server_id.clone();let op=operation_id.clone();
    let value=tauri::async_runtime::spawn_blocking(move || match app.state::<HostRuntime>().inner() {
        HostRuntime::Attach(core) => match method {"begin"=>core.mcp_oauth_begin(sid,expected_revision,op),"status"=>core.mcp_oauth_status(sid,expected_revision,op),"cancel"=>core.mcp_oauth_cancel(sid,expected_revision,op),_=>Err("mcp_oauth_unavailable".into())},
    }).await.map_err(|_|code("status_core"))?.map_err(|_|code("status_core"))?;
    if !window.is_focused().unwrap_or(false) {return Err(code("status_focus_final"))}
    mcp_oauth_public(&value,&server_id,expected_revision,&operation_id).map_err(|_|code("status_invalid"))
}
#[tauri::command]
async fn mcp_oauth_begin(window:tauri::Window,server_id:String,expected_revision:u64,operation_id:String)->Result<serde_json::Value,String>{
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))] {return mcp_oauth_call(window,server_id,expected_revision,operation_id,"begin").await;}
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))] {let _=(window,server_id,expected_revision,operation_id);Err("mcp_oauth_unavailable".into())}
}
#[tauri::command]
async fn mcp_oauth_status(window:tauri::Window,server_id:String,expected_revision:u64,operation_id:String)->Result<serde_json::Value,String>{
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))] {return mcp_oauth_call(window,server_id,expected_revision,operation_id,"status").await;}
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))] {let _=(window,server_id,expected_revision,operation_id);Err("mcp_oauth_unavailable".into())}
}
#[tauri::command]
async fn mcp_oauth_cancel(window:tauri::Window,server_id:String,expected_revision:u64,operation_id:String)->Result<serde_json::Value,String>{
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))] {return mcp_oauth_call(window,server_id,expected_revision,operation_id,"cancel").await;}
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))] {let _=(window,server_id,expected_revision,operation_id);Err("mcp_oauth_unavailable".into())}
}
#[tauri::command]
async fn mcp_oauth_open(window:tauri::Window,server_id:String,expected_revision:u64,operation_id:String)->Result<(),String>{
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))] {
        if window.label()!="main" || !window.is_focused().unwrap_or(false) {return Err("open_focus_initial".into())}
        let app=window.app_handle().clone();let sid=server_id.clone();let op=operation_id.clone();
        let value=tauri::async_runtime::spawn_blocking(move || match app.state::<HostRuntime>().inner(){HostRuntime::Attach(core)=>core.mcp_oauth_status(sid,expected_revision,op)}).await.map_err(|_|"core_status_initial".to_string())?.map_err(|_|"core_status_initial".to_string())?;
        let public=mcp_oauth_public(&value,&server_id,expected_revision,&operation_id).map_err(|_|"binding_changed".to_string())?;
        if public["state"]!="authorizing" {return Err("not_authorizing".into())}
        if !window.is_focused().unwrap_or(false) {return Err("open_focus_after_status".into())}
        let raw=value.get("authorization_url").and_then(|v|v.as_str()).ok_or("url_invalid")?;
        if raw.len()>8192 || !(raw=="https://mcp.notion.com/authorize" || raw.starts_with("https://mcp.notion.com/authorize?")) {return Err("url_invalid".into())}
        let url=tauri::Url::parse(raw).map_err(|_|"url_invalid".to_string())?;
        if url.scheme()!="https" || url.host_str()!=Some("mcp.notion.com") || url.port().is_some() || !url.username().is_empty() || url.password().is_some() || url.path()!="/authorize" || url.fragment().is_some() {return Err("url_invalid".into())}
        // Keep the authorization URL (including state and loopback URI) out of argv and logs.
        let value=std::ffi::CString::new(url.as_str()).map_err(|_|"url_invalid".to_string())?;
        let app=window.app_handle().clone();let verified_app=app.clone();let (send,receive)=tokio::sync::oneshot::channel();
        app.run_on_main_thread(move || {
            use objc2::{class,msg_send,rc::Retained,runtime::{AnyObject,Bool}};
            let result=(|| {
                if !window.is_focused().unwrap_or(false) {return Err("main_focus".to_string())}
                // Recheck Owner, epoch and revision at the final explicit open boundary.
                let latest=match verified_app.state::<HostRuntime>().inner(){HostRuntime::Attach(core)=>core.mcp_oauth_status(server_id.clone(),expected_revision,operation_id.clone())}.map_err(|_|"core_status_final".to_string())?;
                let public=mcp_oauth_public(&latest,&server_id,expected_revision,&operation_id).map_err(|_|"binding_changed".to_string())?;
                if public["state"]!="authorizing" || latest.get("authorization_url").and_then(|v|v.as_str()).map(str::as_bytes)!=Some(value.as_bytes()) {return Err("binding_changed".to_string())}
                if !window.is_focused().unwrap_or(false) {return Err("main_focus_final".to_string())}
                unsafe {
                    let string:Option<Retained<AnyObject>>=msg_send![class!(NSString),stringWithUTF8String:value.as_ptr()];
                    let string=string.ok_or("nsworkspace_failed")?;
                    let target:Option<Retained<AnyObject>>=msg_send![class!(NSURL),URLWithString:&*string];
                    let target=target.ok_or("nsworkspace_failed")?;
                    let workspace:Retained<AnyObject>=msg_send![class!(NSWorkspace),sharedWorkspace];
                    let opened:Bool=msg_send![&*workspace,openURL:&*target];
                    if opened.as_bool(){Ok(())}else{Err("nsworkspace_failed".to_string())}
                }
            })();let _=send.send(result);
        }).map_err(|_|"open_dispatch_failed".to_string())?;
        return receive.await.map_err(|_|"open_dispatch_failed".to_string())?
    }
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))] {let _=(window,server_id,expected_revision,operation_id);Err("open_unavailable".into())}
}

#[tauri::command]
async fn get_local_endpoint(
    window: tauri::Window,
) -> Result<local_endpoint::LocalEndpoint, String> {
    if window.label() != "main" {
        return Err("Local endpoint unavailable".into());
    }
    let app = window.app_handle().clone();
    tauri::async_runtime::spawn_blocking(move || app.state::<HostRuntime>().endpoint())
        .await
        .map_err(|_| "core_attach_unavailable")?
}

#[tauri::command]
fn open_screen_recording_settings(window: tauri::Window) -> Result<(), String> {
    if window.label() != "main" {
        return Err("System Settings unavailable".into());
    }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        return match window.state::<HostRuntime>().inner() {
            HostRuntime::Attach(core) => {
                core.open_native_settings(gamja_core_host::SettingsPanel::ScreenRecording)
            }
        };
    }
    #[cfg(all(target_os = "macos", not(target_arch = "aarch64")))]
    {
        let status = std::process::Command::new("/usr/bin/open")
            .arg("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")
            .status()
            .map_err(|_| "Could not open System Settings")?;
        if status.success() {
            Ok(())
        } else {
            Err("Could not open System Settings".into())
        }
    }
    #[cfg(not(target_os = "macos"))]
    {
        Err("Screen Recording settings are available on macOS only".into())
    }
}

#[tauri::command]
fn get_screen_recording_permission_status(window: tauri::Window) -> Result<&'static str, String> {
    if window.label() != "main" {
        return Err("Screen Recording status unavailable".into());
    }
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    {
        return match window.state::<HostRuntime>().inner() {
            HostRuntime::Attach(core) => {
                Ok(match core.native_permission_status()?.screen_recording {
                    gamja_core_host::PermissionState::Granted => "granted",
                    gamja_core_host::PermissionState::NotGranted => "not_granted",
                    gamja_core_host::PermissionState::Unsupported => "unsupported",
                })
            }
        };
    }
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    {
        Ok(desktop::screen_recording_permission_status())
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum RuntimeMode {
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    Legacy,
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    Attach,
}

impl RuntimeMode {
    fn selected() -> Self {
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        return Self::Attach;
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        return Self::Legacy;
    }
}

enum HostRuntime {
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    Legacy(Box<Mutex<sidecar::BackendProcess>>),
    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    Attach(core_client::CoreClient),
}

impl HostRuntime {
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    fn mode(&self) -> RuntimeMode {
        match self {
            Self::Legacy(_) => RuntimeMode::Legacy,
        }
    }
    fn endpoint(&self) -> Result<local_endpoint::LocalEndpoint, String> {
        match self {
            #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
            Self::Legacy(backend) => backend
                .lock()
                .map_err(|_| "Service state unavailable")?
                .endpoint(),
            #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
            Self::Attach(core) => core.endpoint(),
        }
    }
    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    fn stop_legacy(&self) {
        let Self::Legacy(backend) = self;
        if let Ok(mut backend) = backend.lock() {
            backend.stop();
        }
    }
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
fn stop_backend_on_exit(mode: RuntimeMode, event: &tauri::RunEvent, stop: impl FnOnce()) {
    if mode == RuntimeMode::Legacy && matches!(event, tauri::RunEvent::Exit) {
        stop();
    }
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
fn handle_runtime_event<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    event: &tauri::RunEvent,
    stop: impl FnOnce(&HostRuntime),
) {
    if matches!(event, tauri::RunEvent::Exit) {
        if let Some(runtime) = app.try_state::<HostRuntime>() {
            stop_backend_on_exit(runtime.mode(), event, || stop(runtime.inner()));
        }
    }
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
fn handle_window_event<R: tauri::Runtime>(window: &tauri::Window<R>, event: &tauri::WindowEvent) {
    if let tauri::WindowEvent::Destroyed = event {
        if window.label() != "main" {
            return;
        }
        if let Some(runtime) = window.try_state::<HostRuntime>() {
            runtime.stop_legacy();
        }
    }
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
fn headless_mode(arguments: impl IntoIterator<Item = String>) -> bool {
    arguments
        .into_iter()
        .any(|argument| argument == "--headless")
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let startup_arguments: Vec<_> = std::env::args_os().skip(1).collect();
    let app_data_override = match core_args::app_data_override(startup_arguments.clone().into_iter(), true) {
        Ok(path) => path,
        Err(code) => {
            eprintln!("{}", serde_json::json!({"code": code}));
            return;
        }
    };
    let skip_auto_restore = core_args::skip_auto_restore(&startup_arguments);
    tauri::Builder::default()
        .manage(StartupLoginMode { skip_auto_restore })
        .plugin(tauri_plugin_deep_link::init())
        .manage(artifact::ArtifactChildren::default())
        .manage(workspace_export::WorkspaceExportState::default())
        .invoke_handler(tauri::generate_handler![
            get_startup_login_mode,
            call_speech::call_speech,
            notifications::local_notification,
            voice::voice_transport,
            voice::voice_action,
            voice::voice_snapshot,
            get_local_endpoint,
            slack_credential_status,
            slack_credential_store,
            slack_credential_delete,
            mcp_store_secret,
            vault_native,
            vault_profile_native,
            vault_card_native,
            vault_csv_choose,
            vault_csv_action,
            mcp_secret_status,
            mcp_oauth_begin,
            mcp_oauth_status,
            mcp_oauth_cancel,
            mcp_oauth_open,
            report_control,
            open_screen_recording_settings,
            open_release_history,
            get_screen_recording_permission_status,
            screenshot_artifact_transport,
            workspace_export_transport,
            local_permissions_transport,
            read_local_permissions,
            open_local_permission_settings,
            workspace_export::choose_workspace_export,
            workspace_export::choose_transcript_export,
            workspace_export::choose_generated_file_export,
            workspace_export::choose_binary_file_export,
            workspace_export::save_transcript_export,
            workspace_export::save_workspace_export,
            workspace_export::save_generated_file_export,
            workspace_export::save_binary_file_export,
            workspace_export::discard_workspace_export,
            workspace_export::reveal_workspace_export,
            artifact::list_screenshot_artifacts,
            artifact::read_screenshot_artifact,
            artifact::delete_screenshot_artifact
        ])
        .setup(move |app| {
            notifications::initialize();
            let data = match &app_data_override {
                Some(path) => path.clone(),
                None => app.path().app_data_dir()?,
            };
            let resources = app.path().resource_dir()?;
            app.manage(HostRuntime::Attach(core_client::CoreClient::bootstrap(
                data, &resources,
            )));
            // Attach failure is surfaced by get_local_endpoint; the shell stays
            // open so later core reporting/error clients can remain usable.
            Ok(())
        })
        .on_window_event(|window, event| {
            artifact::handle_window_event(window, event);
            workspace_export::handle_window_event(window, event);
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(artifact::handle_run_event);
}

#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app_data_override = match core_args::app_data_override(std::env::args_os().skip(1), false) {
        Ok(path) => path,
        Err(code) => {
            eprintln!("{}", serde_json::json!({"code": code}));
            return;
        }
    };
    debug_assert!(app_data_override.is_none());
    let headless = headless_mode(std::env::args_os().filter_map(|word| word.into_string().ok()));
    let mut context = tauri::generate_context!();
    if headless {
        // Keep the same AppHandle and sidecar/native IPC bootstrap, but do not
        // create the webview window for an owner-started CLI engine.
        context.config_mut().app.windows.clear();
    }
    let builder = tauri::Builder::default();
    #[cfg(any(target_os = "windows", target_os = "linux"))]
    let builder = builder.plugin(tauri_plugin_single_instance::init(|_app, _argv, _cwd| {}));
    builder
        .manage(StartupLoginMode { skip_auto_restore: false })
        .plugin(tauri_plugin_deep_link::init())
        .manage(Mutex::new(None::<reporting::ReportStore>))
        .manage(reporting::sender::Sender::production())
        .invoke_handler(tauri::generate_handler![
            get_startup_login_mode,
            call_speech::call_speech,
            notifications::local_notification,
            voice::voice_transport,
            voice::voice_action,
            voice::voice_snapshot,
            get_local_endpoint,
            slack_credential_status,
            slack_credential_store,
            slack_credential_delete,
            mcp_store_secret,
            vault_native,
            vault_profile_native,
            vault_card_native,
            vault_csv_choose,
            vault_csv_action,
            mcp_secret_status,
            mcp_oauth_begin,
            mcp_oauth_status,
            mcp_oauth_cancel,
            mcp_oauth_open,
            report_control,
            open_screen_recording_settings,
            open_release_history,
            get_screen_recording_permission_status,
            screenshot_artifact_transport,
            workspace_export_transport,
            local_permissions_transport,
            read_local_permissions,
            open_local_permission_settings,
            list_reports,
            create_manual_draft,
            update_manual_draft,
            delete_report,
            export_report,
            set_auto_reporting,
            get_report_status,
            submit_manual_report,
            record_frontend_render_failure
        ])
        .setup(move |app| {
            notifications::initialize();
            app.manage(HostRuntime::Legacy(Box::new(Mutex::new(
                sidecar::BackendProcess::new(),
            ))));
            #[cfg(all(target_os = "macos", not(target_arch = "aarch64")))]
            if headless {
                app.handle()
                    .set_activation_policy(tauri::ActivationPolicy::Accessory)
                    .map_err(|_| "Cannot configure headless application")?;
                app.handle()
                    .set_dock_visibility(false)
                    .map_err(|_| "Cannot hide headless application dock icon")?;
                install_headless_signal_exit(app.handle().clone())?;
                install_headless_owner_lifeline(app.handle().clone())?;
            }
            if let Ok(path) = app.path().app_data_dir() {
                if let Ok(store) = reporting::ReportStore::open(&path) {
                    if let Ok(mut slot) = app.state::<ReportingState>().lock() {
                        *slot = Some(store);
                    }
                }
            }
            if app.state::<Option<reporting::sender::Sender>>().is_some() {
                let handle = app.handle().clone();
                tauri::async_runtime::spawn(async move {
                    loop {
                        let sender = handle.state::<Option<reporting::sender::Sender>>();
                        let store = handle.state::<ReportingState>();
                        if let Some(sender) = sender.as_ref() {
                            sender
                                .run_auto_due(&store, chrono::Utc::now().timestamp())
                                .await;
                        }
                        tokio::time::sleep(std::time::Duration::from_secs(30)).await;
                    }
                });
            }
            let HostRuntime::Legacy(backend) = app.state::<HostRuntime>().inner();
            if let Ok(mut backend) = backend.lock() {
                if let Err(error) = backend.start(app.handle()) {
                    if headless {
                        return Err(std::io::Error::new(std::io::ErrorKind::Other, error).into());
                    }
                }
            }
            Ok(())
        })
        .on_window_event(handle_window_event)
        .build(context)
        .expect("error while building tauri application")
        .run(|app, event| handle_runtime_event(app, &event, HostRuntime::stop_legacy));
}

#[cfg(test)]
mod tests {
    use tauri::Manager;

    #[test]
    fn runtime_mode_is_fixed_by_the_native_target() {
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        assert!(matches!(
            super::RuntimeMode::selected(),
            super::RuntimeMode::Attach
        ));
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        assert!(matches!(
            super::RuntimeMode::selected(),
            super::RuntimeMode::Legacy
        ));
    }

    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    #[test]
    fn gui_lifecycle_unmanaged_runtime_events_do_not_panic() {
        let app = tauri::test::mock_app();
        for event in [
            tauri::RunEvent::Resumed,
            tauri::RunEvent::Ready,
            tauri::RunEvent::Exit,
        ] {
            super::handle_runtime_event(app.handle(), &event, |_| {
                panic!("uninitialized shell has no owned runtime")
            });
        }
        assert!(app.try_state::<super::HostRuntime>().is_none());
    }

    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    #[test]
    fn gui_lifecycle_unmanaged_destroyed_window_does_not_panic() {
        let app = tauri::test::mock_app();
        let window = tauri::WebviewWindowBuilder::new(&app, "main", tauri::WebviewUrl::default())
            .build()
            .unwrap();
        super::handle_window_event(&window.as_ref().window(), &tauri::WindowEvent::Destroyed);
        assert!(app.try_state::<super::HostRuntime>().is_none());
    }

    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    #[test]
    fn gui_lifecycle_registered_legacy_exit_keeps_existing_shutdown_contract() {
        let app = tauri::test::mock_app();
        app.manage(super::HostRuntime::Legacy(Box::new(std::sync::Mutex::new(
            super::sidecar::BackendProcess::new(),
        ))));
        let mut stops = 0;
        super::handle_runtime_event(app.handle(), &tauri::RunEvent::Ready, |_| stops += 1);
        assert_eq!(stops, 0);
        super::handle_runtime_event(app.handle(), &tauri::RunEvent::Exit, |_| stops += 1);
        assert_eq!(stops, 1);
    }

    #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
    #[test]
    fn gui_lifecycle_registered_attach_exit_does_not_shutdown() {
        let app = tauri::test::mock_app();
        app.manage(super::HostRuntime::Attach(
            super::core_client::CoreClient::new(
                std::path::PathBuf::from("/unused-fixture"),
                std::path::PathBuf::from("/unused-fixture/gamja"),
            ),
        ));
        let window = tauri::WebviewWindowBuilder::new(&app, "main", tauri::WebviewUrl::default())
            .build()
            .unwrap();
        drop(window);
        assert!(matches!(
            app.state::<super::HostRuntime>().inner(),
            super::HostRuntime::Attach(_)
        ));
    }

    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    #[test]
    fn app_exit_stops_backend_but_ready_does_not() {
        let mut stops = 0;
        super::stop_backend_on_exit(super::RuntimeMode::Legacy, &tauri::RunEvent::Ready, || {
            stops += 1
        });
        assert_eq!(stops, 0);
        super::stop_backend_on_exit(super::RuntimeMode::Legacy, &tauri::RunEvent::Exit, || {
            stops += 1
        });
        assert_eq!(stops, 1);
    }

    #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
    #[test]
    fn headless_mode_requires_the_exact_flag() {
        assert!(super::headless_mode([
            "gamja-bot".into(),
            "--headless".into()
        ]));
        assert!(!super::headless_mode([
            "gamja-bot".into(),
            "--headless=true".into()
        ]));
    }

    #[cfg(all(target_os = "macos", not(target_arch = "aarch64")))]
    #[test]
    fn headless_signal_pipe_is_private_to_host_and_disables_once() {
        use std::sync::atomic::Ordering;
        let pipe = super::headless_signal_pipe().unwrap();
        unsafe {
            assert_ne!(libc::fcntl(pipe[0], libc::F_GETFD) & libc::FD_CLOEXEC, 0);
            assert_ne!(libc::fcntl(pipe[1], libc::F_GETFD) & libc::FD_CLOEXEC, 0);
            assert_ne!(libc::fcntl(pipe[1], libc::F_GETFL) & libc::O_NONBLOCK, 0);
        }
        super::HEADLESS_SIGNAL_WRITE.store(pipe[1], Ordering::Release);
        assert_eq!(
            super::HEADLESS_SIGNAL_WRITE.swap(-1, Ordering::AcqRel),
            pipe[1]
        );
        assert_eq!(super::HEADLESS_SIGNAL_WRITE.load(Ordering::Acquire), -1);
        super::close_signal_pipe(pipe);
    }

    #[cfg(all(target_os = "macos", not(target_arch = "aarch64")))]
    #[test]
    fn headless_owner_lifeline_requires_a_real_parent_pid() {
        assert_eq!(super::parse_headless_owner(None).unwrap(), None);
        assert_eq!(super::parse_headless_owner(Some("42")).unwrap(), Some(42));
        assert!(super::parse_headless_owner(Some("0")).is_err());
        assert!(super::parse_headless_owner(Some("parent")).is_err());
    }
}
