//! Owner-triggered workspace JSON / transcript Markdown export. Selection never writes; commit consumes one
//! main-window selection. This is not a model tool or a general path-write API.
use objc2::{
    class, msg_send,
    rc::Retained,
    runtime::{AnyObject, Bool},
    MainThreadMarker,
};
use serde::Serialize;
use std::{
    collections::VecDeque,
    ffi::{CStr, CString},
    fs::{File, OpenOptions},
    io::Write,
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{
            ffi::OsStrExt,
            fs::{MetadataExt, OpenOptionsExt},
        },
    },
    path::PathBuf,
    sync::{Arc, Mutex},
};
use tauri::Manager;
use base64::{engine::general_purpose::STANDARD, Engine as _};
use sha2::{Digest, Sha256};
const MAX_BYTES: usize = 2 * 1024 * 1024;
const MAX_GENERATED_BYTES: usize = 4_000_000;
const MAX_UPLOAD_BYTES: usize = 20 * 1024 * 1024;
const MAX_UPLOAD_BASE64_BYTES: usize = 4 * ((MAX_UPLOAD_BYTES + 2) / 3);
#[derive(Clone, Copy, PartialEq, Eq)]
enum ExportKind { WorkspaceJson, TranscriptMarkdown, GeneratedUtf8, BinaryFile }
impl ExportKind {
    fn extension(self) -> Option<&'static str> { match self { Self::WorkspaceJson => Some("json"), Self::TranscriptMarkdown => Some("md"), Self::GeneratedUtf8 | Self::BinaryFile => None } }
    fn filename(self) -> &'static str { match self { Self::WorkspaceJson => "gamja-workspace-blueprint.json", Self::TranscriptMarkdown => "gamja-room-transcript.md", Self::GeneratedUtf8 => "generated.txt", Self::BinaryFile => "file" } }
    fn extension_error(self) -> &'static str { match self { Self::WorkspaceJson => "export_json_required", Self::TranscriptMarkdown => "export_markdown_required", Self::GeneratedUtf8 | Self::BinaryFile => "export_destination_unavailable" } }
}
#[derive(Clone, Default)]
pub struct WorkspaceExportState {
    inner: Arc<Mutex<Phase>>,
    recent: Arc<Mutex<VecDeque<RevealTarget>>>,
}
#[derive(Default)]
enum Phase {
    #[default]
    Idle,
    Choosing(String),
    Selected(Selection),
    Writing(String),
}
#[derive(Serialize)]
pub struct Destination {
    selection_id: String,
    filename: String,
    replacing: bool,
}
#[derive(Serialize)]
pub struct SaveReceipt {
    path: String,
    bytes: usize,
    replaced: bool,
    durability_unconfirmed: bool,
    reveal_id: Option<String>,
}
#[derive(Clone, PartialEq, Eq)]
struct Identity {
    dev: u64,
    ino: u64,
    len: u64,
    mtime: i64,
    mnsec: i64,
    ctime: i64,
    cnsec: i64,
    mode: u32,
}
#[derive(Clone)]
struct RevealTarget {
    id: String,
    context: String,
    export_id: String,
    parent: PathBuf,
    parent_dev: u64,
    parent_ino: u64,
    path: PathBuf,
    identity: Identity,
}
fn identity_of(m: &std::fs::Metadata) -> Identity {
    Identity { dev: m.dev(), ino: m.ino(), len: m.len(), mtime: m.mtime(),
        mnsec: m.mtime_nsec(), ctime: m.ctime(), cnsec: m.ctime_nsec(), mode: m.mode() }
}
struct Selection {
    kind: ExportKind,
    id: String,
    context: String,
    export_id: String,
    dir: File,
    parent: PathBuf,
    name: CString,
    path: PathBuf,
    existing: Option<Identity>,
}
fn random_id() -> Result<String, String> {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).map_err(|_| "export_unavailable")?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
fn valid_context(value: &str) -> bool {
    value.len() == 36 && value.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-')
}
fn target_identity(dir: &File, name: &CStr) -> Result<Option<Identity>, String> {
    let fd = unsafe {
        libc::openat(
            dir.as_raw_fd(),
            name.as_ptr(),
            libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return if std::io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) {
            Ok(None)
        } else {
            Err("export_destination_unavailable".into())
        };
    }
    let file = unsafe { File::from_raw_fd(fd) };
    let m = file
        .metadata()
        .map_err(|_| "export_destination_unavailable")?;
    if !m.is_file() || m.uid() != unsafe { libc::geteuid() } || m.nlink() != 1 {
        return Err("export_destination_unavailable".into());
    }
    Ok(Some(identity_of(&m)))
}
impl Selection {
    fn prepare(path: PathBuf, context: String, export_id: String, kind: ExportKind) -> Result<Self, String> {
        if let Some(extension) = kind.extension() {
            if !path.extension().is_some_and(|ext| ext.eq_ignore_ascii_case(extension)) {
                return Err(kind.extension_error().into());
            }
        }
        let parent = path
            .parent()
            .ok_or("export_destination_unavailable")?
            .canonicalize()
            .map_err(|_| "export_destination_unavailable")?;
        let filename = path.file_name().ok_or("export_destination_unavailable")?;
        let name =
            CString::new(filename.as_bytes()).map_err(|_| "export_destination_unavailable")?;
        let dir = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&parent)
            .map_err(|_| "export_destination_unavailable")?;
        let existing = target_identity(&dir, &name)?;
        let path = parent.join(filename);
        Ok(Self {
            kind,
            id: random_id()?,
            context,
            export_id,
            dir,
            parent,
            name,
            path,
            existing,
        })
    }
    fn parent_current(&self) -> Result<(), String> {
        let current = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(&self.parent)
            .map_err(|_| "export_destination_changed")?;
        let held = self
            .dir
            .metadata()
            .map_err(|_| "export_destination_changed")?;
        let now = current
            .metadata()
            .map_err(|_| "export_destination_changed")?;
        if held.dev() != now.dev() || held.ino() != now.ino() {
            return Err("export_destination_changed".into());
        }
        Ok(())
    }
    fn write(self, content: &[u8]) -> Result<(SaveReceipt, Option<RevealTarget>), String> {
        self.parent_current()?;
        if target_identity(&self.dir, &self.name)? != self.existing {
            return Err("export_destination_changed".into());
        }
        let mut temporary = Temporary::new(&self.dir)?;
        temporary
            .file
            .write_all(content)
            .map_err(|_| "export_save_failed")?;
        temporary
            .file
            .sync_all()
            .map_err(|_| "export_save_failed")?;
        self.parent_current()?;
        if target_identity(&self.dir, &self.name)? != self.existing {
            return Err("export_destination_changed".into());
        }
        // EXCL protects a previously absent name atomically. For a confirmed replace,
        // compare the selected identity again then atomically rename. This is not CAS
        // against arbitrary external writers in the final compare/rename interval.
        let flags = if self.existing.is_none() {
            libc::RENAME_EXCL
        } else {
            0
        };
        if unsafe {
            libc::renameatx_np(
                self.dir.as_raw_fd(),
                temporary.name.as_ptr(),
                self.dir.as_raw_fd(),
                self.name.as_ptr(),
                flags,
            )
        } != 0
        {
            return Err(
                if std::io::Error::last_os_error().raw_os_error() == Some(libc::EEXIST) {
                    "export_destination_changed"
                } else {
                    "export_save_failed"
                }
                .into(),
            );
        }
        temporary.moved = true;
        let durability_unconfirmed = self.dir.sync_all().is_err();
        // Failure to register a location must never turn a completed save into
        // a failed/unknown write. No open file handles are retained by this list.
        let reveal = (|| {
            let parent = self.dir.metadata().map_err(|_| "export_reveal_unavailable")?;
            let saved = temporary.file.metadata().map_err(|_| "export_reveal_unavailable")?;
            Ok::<_, String>(RevealTarget {
                id: random_id()?, context: self.context.clone(), export_id: self.export_id.clone(),
                parent: self.parent.clone(), parent_dev: parent.dev(), parent_ino: parent.ino(),
                path: self.path.clone(), identity: identity_of(&saved),
            })
        })().ok();
        Ok((SaveReceipt {
            path: self.path.to_string_lossy().into_owned(),
            bytes: content.len(),
            replaced: self.existing.is_some(),
            durability_unconfirmed,
            reveal_id: None,
        }, reveal))
    }
}
struct Temporary<'a> {
    dir: &'a File,
    name: CString,
    file: File,
    moved: bool,
}
impl<'a> Temporary<'a> {
    fn new(dir: &'a File) -> Result<Self, String> {
        let name = CString::new(format!(".gamja-workspace-{}.tmp", random_id()?))
            .map_err(|_| "export_save_failed")?;
        let fd = unsafe {
            libc::openat(
                dir.as_raw_fd(),
                name.as_ptr(),
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600,
            )
        };
        if fd < 0 {
            return Err("export_save_failed".into());
        }
        Ok(Self {
            dir,
            name,
            file: unsafe { File::from_raw_fd(fd) },
            moved: false,
        })
    }
}
impl Drop for Temporary<'_> {
    fn drop(&mut self) {
        if self.moved {
            return;
        }
        let fd = unsafe {
            libc::openat(
                self.dir.as_raw_fd(),
                self.name.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_NONBLOCK | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return;
        }
        let current = unsafe { File::from_raw_fd(fd) };
        if let (Ok(current), Ok(owned)) = (current.metadata(), self.file.metadata()) {
            if current.dev() == owned.dev() && current.ino() == owned.ino() {
                unsafe {
                    libc::unlinkat(self.dir.as_raw_fd(), self.name.as_ptr(), 0);
                }
            }
        }
    }
}
unsafe fn string(value: &str) -> Result<Retained<AnyObject>, String> {
    let value = CString::new(value).map_err(|_| "export_unavailable")?;
    let result: Option<Retained<AnyObject>> =
        unsafe { msg_send![class!(NSString),stringWithUTF8String:value.as_ptr()] };
    result.ok_or_else(|| "export_unavailable".into())
}
fn choose(title: &str, kind: ExportKind, default_filename: &str) -> Result<Option<PathBuf>, String> {
    if MainThreadMarker::new().is_none() {
        return Err("export_unavailable".into());
    }
    // AppKit's standard Save panel supplies the destination and its Replace
    // confirmation. No renderer-supplied filesystem path is ever accepted.
    unsafe {
        let panel: Retained<AnyObject> = msg_send![class!(NSSavePanel), savePanel];
        let title = string(title)?;
        let filename = string(default_filename)?;
        let _: () = msg_send![&*panel,setTitle:&*title];
        let _: () = msg_send![&*panel,setNameFieldStringValue:&*filename];
        if let Some(extension) = kind.extension() {
            let extension = string(extension)?;
            let types: Retained<AnyObject> = msg_send![class!(NSArray),arrayWithObject:&*extension];
            let _: () = msg_send![&*panel,setAllowedFileTypes:&*types];
            let _: () = msg_send![&*panel,setAllowsOtherFileTypes:Bool::NO];
        }
        let _: () = msg_send![&*panel,setCanCreateDirectories:Bool::YES];
        let _: () = msg_send![&*panel,setExtensionHidden:Bool::NO];
        let response: isize = msg_send![&*panel, runModal];
        if response == 0 {
            return Ok(None);
        }
        if response != 1 {
            return Err("export_unavailable".into());
        }
        let url: Option<Retained<AnyObject>> = msg_send![&*panel, URL];
        let url = url.ok_or("export_destination_unavailable")?;
        let path: *const std::ffi::c_char = msg_send![&*url, fileSystemRepresentation];
        if path.is_null() {
            return Err("export_destination_unavailable".into());
        }
        Ok(Some(PathBuf::from(std::ffi::OsStr::from_bytes(
            CStr::from_ptr(path).to_bytes(),
        ))))
    }
}
#[tauri::command]
pub async fn choose_workspace_export(
    window: tauri::Window,
    app: tauri::AppHandle,
    state: tauri::State<'_, WorkspaceExportState>,
    context: String,
    export_id: String,
    title: String,
) -> Result<Option<Destination>, String> {
    choose_export(window, app, state, context, export_id, title, ExportKind::WorkspaceJson, ExportKind::WorkspaceJson.filename().to_owned()).await
}
#[tauri::command]
pub async fn choose_transcript_export(
    window: tauri::Window,
    app: tauri::AppHandle,
    state: tauri::State<'_, WorkspaceExportState>,
    context: String,
    export_id: String,
    title: String,
    filename: Option<String>,
) -> Result<Option<Destination>, String> {
    let filename = filename.unwrap_or_else(|| ExportKind::TranscriptMarkdown.filename().to_owned());
    // This is only the suggested leaf name in AppKit, never a destination path.
    if filename.len() > 255 || filename.chars().any(char::is_control)
        || filename.contains('/') || filename.contains('\\')
        || !PathBuf::from(&filename).extension().is_some_and(|ext| ext.eq_ignore_ascii_case("md")) {
        return Err("export_markdown_required".into());
    }
    choose_export(window, app, state, context, export_id, title, ExportKind::TranscriptMarkdown, filename).await
}
#[tauri::command]
pub async fn choose_generated_file_export(
    window: tauri::Window,
    app: tauri::AppHandle,
    state: tauri::State<'_, WorkspaceExportState>,
    context: String,
    export_id: String,
    title: String,
    filename: String,
) -> Result<Option<Destination>, String> {
    // Only a suggested leaf name; the panel supplies the actual destination.
    if !valid_leaf(&filename) {
        return Err("export_destination_unavailable".into());
    }
    choose_export(window, app, state, context, export_id, title, ExportKind::GeneratedUtf8, filename).await
}
fn valid_leaf(filename: &str) -> bool {
    !filename.is_empty() && filename != "." && filename != ".." && filename.len() <= 255
        && !filename.chars().any(char::is_control) && !filename.contains('/') && !filename.contains('\\')
}
#[tauri::command]
pub async fn choose_binary_file_export(
    window: tauri::Window,
    app: tauri::AppHandle,
    state: tauri::State<'_, WorkspaceExportState>,
    context: String,
    export_id: String,
    title: String,
    filename: String,
) -> Result<Option<Destination>, String> {
    if !valid_leaf(&filename) { return Err("export_destination_unavailable".into()); }
    choose_export(window, app, state, context, export_id, title, ExportKind::BinaryFile, filename).await
}
async fn choose_export(
    window: tauri::Window,
    app: tauri::AppHandle,
    state: tauri::State<'_, WorkspaceExportState>,
    context: String,
    export_id: String,
    title: String,
    kind: ExportKind,
    default_filename: String,
) -> Result<Option<Destination>, String> {
    if window.label() != "main"
        || !valid_context(&context)
        || !valid_context(&export_id)
        || title.len() > 256
        || title.contains('\0')
    {
        return Err("export_unavailable".into());
    }
    let inner = state.inner.clone();
    {
        let mut phase = inner.lock().map_err(|_| "export_unavailable")?;
        if matches!(*phase, Phase::Choosing(_) | Phase::Writing(_)) {
            return Err("export_busy".into());
        }
        // A previous unused selection owns no disk output and may be retired here.
        *phase = Phase::Choosing(context.clone());
    }
    let (send, receive) = tokio::sync::oneshot::channel();
    let closure_inner = inner.clone();
    let closure_context = context.clone();
    let queued = app.run_on_main_thread(move || {
        let result = if !window.is_focused().unwrap_or(false) {
            Err("export_canceled".into())
        } else {
            choose(&title, kind, &default_filename).and_then(|path| {
                path.map(|path| Selection::prepare(path, closure_context.clone(), export_id, kind))
                    .transpose()
            })
        };
        let reply = (|| {
            let mut phase = closure_inner.lock().map_err(|_| "export_unavailable")?;
            if !matches!(&*phase,Phase::Choosing(active)if active==&closure_context) {
                return Err("export_canceled".into());
            }
            match result {
                Ok(Some(selection)) => {
                    let reply = Destination {
                        selection_id: selection.id.clone(),
                        filename: selection
                            .path
                            .file_name()
                            .unwrap_or_default()
                            .to_string_lossy()
                            .into_owned(),
                        replacing: selection.existing.is_some(),
                    };
                    *phase = Phase::Selected(selection);
                    Ok(Some(reply))
                }
                Ok(None) => {
                    *phase = Phase::Idle;
                    Ok(None)
                }
                Err(error) => {
                    *phase = Phase::Idle;
                    Err(error)
                }
            }
        })();
        if send.send(reply).is_err() {
            if let Ok(mut phase) = closure_inner.lock() {
                if matches!(&*phase,Phase::Selected(selection)if selection.context==closure_context)
                {
                    *phase = Phase::Idle
                }
            }
        }
    });
    if queued.is_err() {
        if let Ok(mut phase) = inner.lock() {
            if matches!(&*phase,Phase::Choosing(active)if active==&context) {
                *phase = Phase::Idle
            }
        }
        return Err("export_unavailable".into());
    }
    receive.await.map_err(|_| "export_unavailable".to_owned())?
}
fn valid_payload(content: &str) -> bool {
    if content.is_empty() || content.len() > MAX_BYTES {
        return false;
    }
    let Ok(value) = serde_json::from_str::<serde_json::Value>(content) else {
        return false;
    };
    let Some(object) = value.as_object() else {
        return false;
    };
    matches!(object.get("version").and_then(|v| v.as_u64()), Some(1 | 2))
        && object.len() == 4
        && ["connections", "agents", "projects"]
            .iter()
            .all(|key| object.get(*key).is_some_and(|v| v.is_array()))
}
#[tauri::command]
pub async fn save_workspace_export(
    window: tauri::Window,
    state: tauri::State<'_, WorkspaceExportState>,
    selection_id: String,
    context: String,
    export_id: String,
    content: String,
) -> Result<SaveReceipt, String> {
    save_export(window, state, selection_id, context, export_id, content, ExportKind::WorkspaceJson).await
}
#[tauri::command]
pub async fn save_transcript_export(
    window: tauri::Window,
    state: tauri::State<'_, WorkspaceExportState>,
    selection_id: String,
    context: String,
    export_id: String,
    content: String,
) -> Result<SaveReceipt, String> {
    save_export(window, state, selection_id, context, export_id, content, ExportKind::TranscriptMarkdown).await
}
#[tauri::command]
pub async fn save_generated_file_export(
    window: tauri::Window,
    state: tauri::State<'_, WorkspaceExportState>,
    selection_id: String,
    context: String,
    export_id: String,
    content: String,
) -> Result<SaveReceipt, String> {
    save_export(window, state, selection_id, context, export_id, content, ExportKind::GeneratedUtf8).await
}
#[tauri::command]
pub async fn save_binary_file_export(
    window: tauri::Window,
    state: tauri::State<'_, WorkspaceExportState>,
    selection_id: String,
    context: String,
    export_id: String,
    content_base64: String,
    expected_sha256: String,
) -> Result<SaveReceipt, String> {
    if content_base64.len() > MAX_UPLOAD_BASE64_BYTES || expected_sha256.len() != 64
        || !expected_sha256.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)) {
        return Err("export_invalid_payload".into());
    }
    let content = STANDARD.decode(&content_base64).map_err(|_| "export_invalid_payload")?;
    if content.len() > MAX_UPLOAD_BYTES || STANDARD.encode(&content) != content_base64
        || format!("{:x}", Sha256::digest(&content)) != expected_sha256 {
        return Err("export_invalid_payload".into());
    }
    commit_export(window, state, selection_id, context, export_id, content, ExportKind::BinaryFile).await
}
async fn save_export(
    window: tauri::Window,
    state: tauri::State<'_, WorkspaceExportState>,
    selection_id: String,
    context: String,
    export_id: String,
    content: String,
    kind: ExportKind,
) -> Result<SaveReceipt, String> {
    let payload_valid = match kind {
        ExportKind::WorkspaceJson => valid_payload(&content),
        ExportKind::TranscriptMarkdown => !content.is_empty() && content.len() <= MAX_BYTES && !content.contains('\0'),
        ExportKind::GeneratedUtf8 => content.len() <= MAX_GENERATED_BYTES,
        ExportKind::BinaryFile => false,
    };
    if window.label() != "main" || !payload_valid {
        return Err("export_invalid_payload".into());
    }
    commit_export(window, state, selection_id, context, export_id, content.into_bytes(), kind).await
}
async fn commit_export(
    window: tauri::Window,
    state: tauri::State<'_, WorkspaceExportState>,
    selection_id: String,
    context: String,
    export_id: String,
    content: Vec<u8>,
    kind: ExportKind,
) -> Result<SaveReceipt, String> {
    if window.label() != "main" { return Err("export_invalid_payload".into()); }
    let inner = state.inner.clone();
    let selection = {
        let mut phase = inner.lock().map_err(|_| "export_unavailable")?;
        if !matches!(&*phase,Phase::Selected(selection)if selection.id==selection_id&&selection.context==context&&selection.export_id==export_id&&selection.kind==kind)
        {
            return Err("export_selection_expired".into());
        }
        let Phase::Selected(selection) =
            std::mem::replace(&mut *phase, Phase::Writing(context.clone()))
        else {
            unreachable!()
        };
        selection
    };
    let join = tauri::async_runtime::spawn_blocking(move || selection.write(&content)).await;
    if let Ok(mut phase) = inner.lock() {
        if matches!(&*phase,Phase::Writing(active)if active==&context) {
            *phase = Phase::Idle
        }
    }
    let (mut receipt, reveal) = join.map_err(|_| "export_transport_unknown".to_owned())??;
    if let Some(reveal) = reveal {
        if let Ok(mut recent) = state.recent.lock() {
            receipt.reveal_id = Some(reveal.id.clone());
            recent.push_back(reveal);
            while recent.len() > 64 { recent.pop_front(); }
        }
    }
    Ok(receipt)
}
#[tauri::command]
pub fn discard_workspace_export(
    window: tauri::Window,
    state: tauri::State<'_, WorkspaceExportState>,
    selection_id: String,
    context: String,
) -> Result<(), String> {
    if window.label() != "main" {
        return Err("export_unavailable".into());
    }
    let mut phase = state.inner.lock().map_err(|_| "export_unavailable")?;
    if matches!(&*phase,Phase::Selected(selection)if selection.id==selection_id&&selection.context==context)
    {
        *phase = Phase::Idle
    }
    Ok(())
}
pub fn handle_window_event<R: tauri::Runtime>(
    window: &tauri::Window<R>,
    event: &tauri::WindowEvent,
) {
    if window.label() == "main" && matches!(event, tauri::WindowEvent::Destroyed) {
        if let Some(state) = window.try_state::<WorkspaceExportState>() {
            if let Ok(mut recent) = state.recent.lock() { recent.clear(); }
            if let Ok(mut phase) = state.inner.lock() {
                if !matches!(*phase, Phase::Writing(_)) {
                    *phase = Phase::Idle
                }
            }
        }
    }
}

// Reveal only a recent successful save captured by native code. The renderer
// supplies receipt identifiers, never a path. This does not read file contents,
// open the saved document in an application, or confer any model-tool permissions.
#[tauri::command]
pub async fn reveal_workspace_export(
    window: tauri::Window,
    app: tauri::AppHandle,
    state: tauri::State<'_, WorkspaceExportState>,
    context: String,
    export_id: String,
    reveal_id: String,
) -> Result<(), String> {
    if window.label() != "main" || !valid_context(&context) || !valid_context(&export_id)
        || reveal_id.len() != 32 || !reveal_id.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err("export_reveal_unavailable".into());
    }
    let target = state.recent.lock().map_err(|_| "export_reveal_unavailable")?
        .iter().find(|item| item.id == reveal_id && item.context == context && item.export_id == export_id)
        .cloned().ok_or("export_reveal_expired")?;
    let (send, receive) = tokio::sync::oneshot::channel();
    app.run_on_main_thread(move || {
        let result = (|| {
            if !window.is_focused().unwrap_or(false) { return Err("export_reveal_unavailable".into()); }
            let dir = OpenOptions::new().read(true)
                .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(&target.parent).map_err(|_| "export_reveal_changed")?;
            let parent = dir.metadata().map_err(|_| "export_reveal_changed")?;
            let name = CString::new(target.path.file_name().ok_or("export_reveal_changed")?.as_bytes())
                .map_err(|_| "export_reveal_changed")?;
            if parent.dev() != target.parent_dev || parent.ino() != target.parent_ino
                || target_identity(&dir, &name).map_err(|_| "export_reveal_changed")? != Some(target.identity) {
                return Err("export_reveal_changed".into());
            }
            // Fingerprint check is an observation, not a filesystem lock/CAS.
            // Finder may observe a later external change after this check.
            unsafe {
                let path = string(target.path.to_str().ok_or("export_reveal_unavailable")?)?;
                let url: Option<Retained<AnyObject>> = msg_send![class!(NSURL), fileURLWithPath:&*path];
                let url = url.ok_or("export_reveal_unavailable")?;
                let urls: Retained<AnyObject> = msg_send![class!(NSArray), arrayWithObject:&*url];
                let workspace: Retained<AnyObject> = msg_send![class!(NSWorkspace), sharedWorkspace];
                let _: () = msg_send![&*workspace, activateFileViewerSelectingURLs:&*urls];
            }
            Ok::<(), String>(())
        })();
        let _ = send.send(result);
    }).map_err(|_| "export_reveal_unavailable")?;
    receive.await.map_err(|_| "export_reveal_unavailable".to_owned())?
}
