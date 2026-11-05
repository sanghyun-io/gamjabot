use gamja_artifact::{
    write_frame, ActionId, ArtifactError, ArtifactListItem, ArtifactListResponse,
    ArtifactListState, ArtifactOperation, ArtifactRequest, ArtifactState, ArtifactStore,
    EngineAuth, EngineClient, ResponseHeader, VerifiedArtifact, VerifiedPng, MAX_REQUEST_BYTES,
};
use serde::{Deserialize, Serialize};
use std::{
    ffi::{CStr, CString, OsString},
    fs::File,
    io::{Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd, OwnedFd, RawFd},
        unix::ffi::OsStrExt,
    },
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

const APP_DATA_COMPONENT: &str = "top.gamja.bot";
const CAPABILITY_FILE: &str = "owner-cli.json";
const MAX_CAPABILITY_BYTES: usize = 4096;
const OPERATION_DEADLINE_MS: u64 = 30_000;
// The GUI launcher passes its already-open app-data directory as inherited FD 3.
const IPC_APP_DATA_FD: RawFd = 3;

fn main() {
    let arguments = std::env::args_os().skip(1).collect();
    let mut input = std::io::stdin().lock();
    let mut output = std::io::stdout().lock();
    let mut error = std::io::stderr().lock();
    let status = run(arguments, None, &mut input, &mut output, &mut error);
    if output.flush().is_err() || error.flush().is_err() {
        std::process::exit(1);
    }
    std::process::exit(status);
}

#[derive(Clone, Debug, PartialEq, Eq)]
enum PublicCommand {
    List {
        room_id: u64,
        after_seq: u64,
        limit: u16,
    },
    Get {
        room_id: u64,
        action_id: ActionId,
        event_seq: u64,
        output: PathBuf,
    },
    Delete {
        room_id: u64,
        action_id: ActionId,
        event_seq: u64,
    },
}

enum CommandMode {
    Public(PublicCommand),
    Ipc,
}

#[derive(Clone, Copy)]
struct CliFailure {
    code: &'static str,
    status: i32,
}

impl CliFailure {
    const fn usage(code: &'static str) -> Self {
        Self { code, status: 2 }
    }

    const fn runtime(code: &'static str) -> Self {
        Self { code, status: 1 }
    }
}

fn run(
    arguments: Vec<OsString>,
    root_override: Option<&Path>,
    input: &mut dyn Read,
    output: &mut dyn Write,
    error: &mut dyn Write,
) -> i32 {
    let mode = match parse(arguments) {
        Ok(mode) => mode,
        Err(failure) => return report_failure(error, failure),
    };
    match mode {
        CommandMode::Ipc => match run_ipc(input, output) {
            Ok(()) => 0,
            Err(failure) => report_failure(error, failure),
        },
        CommandMode::Public(command) => {
            let root = match root_override
                .map(Path::to_path_buf)
                .map(Ok)
                .unwrap_or_else(canonical_app_data_root)
            {
                Ok(root) => root,
                Err(code) => return report_failure(error, CliFailure::runtime(code)),
            };
            match run_public(command, &root, output) {
                Ok(()) => 0,
                Err(failure) => report_failure(error, failure),
            }
        }
    }
}

fn report_failure(error: &mut dyn Write, failure: CliFailure) -> i32 {
    let _ = writeln!(error, "{}", failure.code);
    failure.status
}

fn parse(arguments: Vec<OsString>) -> Result<CommandMode, CliFailure> {
    let words: Vec<String> = arguments
        .into_iter()
        .map(|value| {
            value
                .into_string()
                .map_err(|_| CliFailure::usage("invalid_request"))
        })
        .collect::<Result<_, _>>()?;
    if words == ["--ipc"] {
        return Ok(CommandMode::Ipc);
    }
    let Some(command) = words.first().map(String::as_str) else {
        return Err(CliFailure::usage("invalid_request"));
    };
    match command {
        "list" => parse_list(&words[1..]).map(CommandMode::Public),
        "get" => parse_item(&words[1..], true).map(CommandMode::Public),
        "delete" => parse_item(&words[1..], false).map(CommandMode::Public),
        _ => Err(CliFailure::usage("invalid_request")),
    }
}

fn parse_list(words: &[String]) -> Result<PublicCommand, CliFailure> {
    let options = parse_options(words, &["--room", "--after-seq", "--limit"])?;
    let room_id = positive(options.required("--room")?)?;
    let after_seq = options
        .get("--after-seq")
        .map(nonnegative)
        .transpose()?
        .unwrap_or(0);
    let limit = match options.get("--limit") {
        Some(value) => value
            .parse::<u16>()
            .ok()
            .filter(|value| (1..=500).contains(value))
            .ok_or_else(|| CliFailure::usage("invalid_request"))?,
        None => 100,
    };
    Ok(PublicCommand::List {
        room_id,
        after_seq,
        limit,
    })
}

fn parse_item(words: &[String], get: bool) -> Result<PublicCommand, CliFailure> {
    let allowed = if get {
        &["--room", "--action", "--event-seq", "--output"][..]
    } else {
        &["--room", "--action", "--event-seq"][..]
    };
    let options = parse_options(words, allowed)?;
    let room_id = positive(options.required("--room")?)?;
    let event_seq = positive(options.required("--event-seq")?)?;
    let action_id = ActionId::parse(options.required("--action")?.to_owned())
        .map_err(|_| CliFailure::usage("invalid_request"))?;
    if get {
        let output = PathBuf::from(options.required("--output")?);
        validate_output_path(&output)?;
        Ok(PublicCommand::Get {
            room_id,
            action_id,
            event_seq,
            output,
        })
    } else {
        Ok(PublicCommand::Delete {
            room_id,
            action_id,
            event_seq,
        })
    }
}

struct Options<'a>(Vec<(&'a str, &'a str)>);

impl<'a> Options<'a> {
    fn get(&self, name: &str) -> Option<&'a str> {
        self.0
            .iter()
            .find_map(|(key, value)| (*key == name).then_some(*value))
    }

    fn required(&self, name: &str) -> Result<&'a str, CliFailure> {
        self.get(name)
            .ok_or_else(|| CliFailure::usage("invalid_request"))
    }
}

fn parse_options<'a>(words: &'a [String], allowed: &[&str]) -> Result<Options<'a>, CliFailure> {
    if words.len() % 2 != 0 {
        return Err(CliFailure::usage("invalid_request"));
    }
    let mut parsed = Vec::new();
    for pair in words.chunks_exact(2) {
        let key = pair[0].as_str();
        if !allowed.contains(&key) || parsed.iter().any(|(present, _)| *present == key) {
            return Err(CliFailure::usage("invalid_request"));
        }
        parsed.push((key, pair[1].as_str()));
    }
    Ok(Options(parsed))
}

fn positive(value: &str) -> Result<u64, CliFailure> {
    value
        .parse::<u64>()
        .ok()
        .filter(|value| *value > 0)
        .ok_or_else(|| CliFailure::usage("invalid_request"))
}

fn nonnegative(value: &str) -> Result<u64, CliFailure> {
    value
        .parse::<u64>()
        .map_err(|_| CliFailure::usage("invalid_request"))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EndpointFile {
    version: u16,
    api_url: String,
    control_url: String,
    capability: String,
    core_version: u16,
    engine_version: u16,
}

fn run_public(
    command: PublicCommand,
    root_path: &Path,
    output: &mut dyn Write,
) -> Result<(), CliFailure> {
    let root = open_owner_directory(root_path)
        .map_err(|_| CliFailure::runtime("owner_capability_unavailable"))?;
    let deadline_ms = now_ms()
        .and_then(|now| now.checked_add(OPERATION_DEADLINE_MS))
        .ok_or_else(|| CliFailure::runtime("deadline_expired"))?;
    let endpoint = read_endpoint(root.as_raw_fd(), deadline_ms)?;
    validate_endpoint(&endpoint)?;
    let client = EngineClient::restore_cli(&endpoint.api_url, endpoint.capability, deadline_ms)
        .map_err(restore_failure)?;
    let store = ArtifactStore::from_app_data_fd(duplicate_fd(root.as_raw_fd())?, deadline_ms)
        .map_err(artifact_failure)?;
    match command {
        PublicCommand::List {
            room_id,
            after_seq,
            limit,
        } => {
            let artifacts = client
                .list_screenshots(room_id, after_seq, limit, deadline_ms)
                .map_err(artifact_failure)?;
            let records = artifacts
                .iter()
                .map(|artifact| {
                    let state = store.inspect(artifact).map_err(artifact_failure)?;
                    Ok(ListRecord::new(artifact, state))
                })
                .collect::<Result<Vec<_>, CliFailure>>()?;
            serde_json::to_writer(&mut *output, &records)
                .map_err(|_| CliFailure::runtime("output_unavailable"))?;
            writeln!(output).map_err(|_| CliFailure::runtime("output_unavailable"))?;
        }
        PublicCommand::Get {
            room_id,
            action_id,
            event_seq,
            output: output_path,
        } => {
            let first = client
                .verify_screenshot(room_id, action_id.clone(), event_seq, deadline_ms)
                .map_err(artifact_failure)?;
            let image = store.read(&first).map_err(artifact_failure)?;
            let output_parent = OutputParent::open(&output_path)?;
            let second = client
                .verify_screenshot(room_id, action_id, event_seq, deadline_ms)
                .map_err(artifact_failure)?;
            if first != second {
                return Err(CliFailure::runtime("artifact_conflict"));
            }
            output_parent.create(&image.bytes)?;
            output
                .write_all(b"{\"state\":\"ok\"}\n")
                .map_err(|_| CliFailure::runtime("output_unavailable"))?;
        }
        PublicCommand::Delete {
            room_id,
            action_id,
            event_seq,
        } => {
            let artifact = client
                .verify_screenshot(room_id, action_id, event_seq, deadline_ms)
                .map_err(artifact_failure)?;
            store.delete(&artifact).map_err(artifact_failure)?;
            output
                .write_all(b"{\"state\":\"ok\"}\n")
                .map_err(|_| CliFailure::runtime("output_unavailable"))?;
        }
    }
    Ok(())
}

fn restore_failure(error: ArtifactError) -> CliFailure {
    if error == ArtifactError::AccessDenied {
        CliFailure::runtime("remembered_login_unavailable")
    } else {
        artifact_failure(error)
    }
}

fn artifact_failure(error: ArtifactError) -> CliFailure {
    CliFailure::runtime(error.code())
}

#[derive(Serialize)]
struct ListRecord<'a> {
    event_seq: u64,
    action_id: &'a str,
    created_at: &'a str,
    width: u32,
    height: u32,
    state: &'static str,
}

impl<'a> ListRecord<'a> {
    fn new(artifact: &'a VerifiedArtifact, state: ArtifactState) -> Self {
        Self {
            event_seq: artifact.event_seq,
            action_id: artifact.action_id.as_str(),
            created_at: &artifact.created_at,
            width: artifact.width,
            height: artifact.height,
            state: match state {
                ArtifactState::Available => "available",
                ArtifactState::Removed => "removed",
                ArtifactState::Unavailable => "unavailable",
            },
        }
    }
}

fn read_endpoint(root: RawFd, deadline_ms: u64) -> Result<EndpointFile, CliFailure> {
    read_endpoint_with_hooks(root, deadline_ms, || {}, || {})
}

fn read_endpoint_with_hooks(
    root: RawFd,
    deadline_ms: u64,
    before_open: impl FnOnce(),
    after_open: impl FnOnce(),
) -> Result<EndpointFile, CliFailure> {
    check_operation_deadline(deadline_ms)?;
    let name = CString::new(CAPABILITY_FILE).unwrap();
    let before = stat_at_nofollow(root, &name)?;
    validate_capability_metadata(&before)?;
    before_open();
    check_operation_deadline(deadline_ms)?;
    let raw = unsafe {
        libc::openat(
            root,
            name.as_ptr(),
            libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW | libc::O_NONBLOCK,
        )
    };
    if raw < 0 {
        return Err(CliFailure::runtime("owner_capability_unavailable"));
    }
    let fd = unsafe { OwnedFd::from_raw_fd(raw) };
    let opened =
        fstat(fd.as_raw_fd()).map_err(|_| CliFailure::runtime("owner_capability_unavailable"))?;
    validate_capability_metadata(&opened)?;
    after_open();
    let current = stat_at_nofollow(root, &name)?;
    validate_capability_metadata(&current)?;
    if !same_file_identity(&before, &opened) || !same_file_identity(&opened, &current) {
        return Err(CliFailure::runtime("owner_capability_unavailable"));
    }
    check_operation_deadline(deadline_ms)?;
    let mut bytes = Vec::new();
    let mut file = File::from(fd);
    Read::by_ref(&mut file)
        .take((MAX_CAPABILITY_BYTES + 1) as u64)
        .read_to_end(&mut bytes)
        .map_err(|_| CliFailure::runtime("owner_capability_unavailable"))?;
    let after_fd =
        fstat(file.as_raw_fd()).map_err(|_| CliFailure::runtime("owner_capability_unavailable"))?;
    let after_path = stat_at_nofollow(root, &name)?;
    validate_capability_metadata(&after_fd)?;
    validate_capability_metadata(&after_path)?;
    if bytes.len() > MAX_CAPABILITY_BYTES
        || bytes.len() as i64 != opened.st_size
        || !same_file_identity(&opened, &after_fd)
        || !same_file_identity(&after_fd, &after_path)
    {
        return Err(CliFailure::runtime("owner_capability_unavailable"));
    }
    check_operation_deadline(deadline_ms)?;
    serde_json::from_slice(&bytes).map_err(|_| CliFailure::runtime("owner_capability_unavailable"))
}

fn stat_at_nofollow(root: RawFd, name: &CStr) -> Result<libc::stat, CliFailure> {
    let mut metadata = std::mem::MaybeUninit::<libc::stat>::uninit();
    if unsafe {
        libc::fstatat(
            root,
            name.as_ptr(),
            metadata.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    } != 0
    {
        Err(CliFailure::runtime("owner_capability_unavailable"))
    } else {
        Ok(unsafe { metadata.assume_init() })
    }
}

fn validate_capability_metadata(metadata: &libc::stat) -> Result<(), CliFailure> {
    let mode = metadata.st_mode as libc::mode_t;
    if mode & libc::S_IFMT != libc::S_IFREG
        || metadata.st_uid != unsafe { libc::geteuid() }
        || mode & 0o777 != 0o600
        || metadata.st_nlink != 1
        || metadata.st_size <= 0
        || metadata.st_size as usize > MAX_CAPABILITY_BYTES
    {
        Err(CliFailure::runtime("owner_capability_unavailable"))
    } else {
        Ok(())
    }
}

fn same_file_identity(left: &libc::stat, right: &libc::stat) -> bool {
    left.st_dev == right.st_dev && left.st_ino == right.st_ino && left.st_size == right.st_size
}

fn check_operation_deadline(deadline_ms: u64) -> Result<(), CliFailure> {
    match now_ms() {
        Some(now) if now < deadline_ms => Ok(()),
        _ => Err(CliFailure::runtime("deadline_expired")),
    }
}

fn validate_endpoint(endpoint: &EndpointFile) -> Result<(), CliFailure> {
    if endpoint.version != 1
        || endpoint.core_version != 1
        || endpoint.engine_version != 1
        || !lower_hex(&endpoint.capability, 64)
        || !bare_loopback(&endpoint.api_url)
        || !bare_loopback(&endpoint.control_url)
    {
        return Err(CliFailure::runtime("owner_capability_unavailable"));
    }
    Ok(())
}

fn bare_loopback(value: &str) -> bool {
    let Ok(url) = reqwest::Url::parse(value) else {
        return false;
    };
    let Some(port) = url.port() else {
        return false;
    };
    value == format!("http://127.0.0.1:{port}")
        && url.scheme() == "http"
        && url.host_str() == Some("127.0.0.1")
        && url.username().is_empty()
        && url.password().is_none()
        && url.path() == "/"
        && url.query().is_none()
        && url.fragment().is_none()
}

fn lower_hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

struct OutputParent {
    directory: OwnedFd,
    name: CString,
}

impl OutputParent {
    fn open(path: &Path) -> Result<Self, CliFailure> {
        validate_output_path(path)?;
        let parent = path
            .parent()
            .ok_or_else(|| CliFailure::usage("invalid_output"))?;
        let name = CString::new(
            path.file_name()
                .ok_or_else(|| CliFailure::usage("invalid_output"))?
                .as_bytes(),
        )
        .map_err(|_| CliFailure::usage("invalid_output"))?;
        let directory =
            open_owner_directory(parent).map_err(|_| CliFailure::usage("invalid_output"))?;
        let mut metadata = std::mem::MaybeUninit::<libc::stat>::uninit();
        let result = unsafe {
            libc::fstatat(
                directory.as_raw_fd(),
                name.as_ptr(),
                metadata.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        };
        if result == 0 || std::io::Error::last_os_error().raw_os_error() != Some(libc::ENOENT) {
            return Err(CliFailure::usage("invalid_output"));
        }
        Ok(Self { directory, name })
    }

    fn create(self, bytes: &[u8]) -> Result<(), CliFailure> {
        let raw = unsafe {
            libc::openat(
                self.directory.as_raw_fd(),
                self.name.as_ptr(),
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600,
            )
        };
        if raw < 0 {
            return Err(CliFailure::runtime("output_unavailable"));
        }
        let fd = unsafe { OwnedFd::from_raw_fd(raw) };
        let result = (|| {
            let metadata =
                fstat(fd.as_raw_fd()).map_err(|_| CliFailure::runtime("output_unavailable"))?;
            if metadata.st_mode as libc::mode_t & libc::S_IFMT != libc::S_IFREG
                || metadata.st_uid != unsafe { libc::geteuid() }
                || metadata.st_mode as libc::mode_t & 0o777 != 0o600
                || metadata.st_nlink != 1
            {
                return Err(CliFailure::runtime("output_unavailable"));
            }
            let mut file = File::from(fd);
            file.write_all(bytes)
                .and_then(|_| file.sync_all())
                .map_err(|_| CliFailure::runtime("output_unavailable"))?;
            sync_fd(self.directory.as_raw_fd())
                .map_err(|_| CliFailure::runtime("output_unavailable"))
        })();
        if result.is_err() {
            unsafe {
                libc::unlinkat(self.directory.as_raw_fd(), self.name.as_ptr(), 0);
                libc::fsync(self.directory.as_raw_fd());
            }
        }
        result
    }
}

fn validate_output_path(path: &Path) -> Result<(), CliFailure> {
    if !path.is_absolute() || path.file_name().is_none() {
        return Err(CliFailure::usage("invalid_output"));
    }
    Ok(())
}

fn run_ipc(input: &mut dyn Read, output: &mut dyn Write) -> Result<(), CliFailure> {
    let stdin = fstat(libc::STDIN_FILENO).map_err(|_| CliFailure::runtime("invalid_request"))?;
    if stdin.st_mode as libc::mode_t & libc::S_IFMT != libc::S_IFIFO {
        return Err(CliFailure::runtime("invalid_request"));
    }
    let mut request_bytes = Vec::new();
    input
        .take((MAX_REQUEST_BYTES + 1) as u64)
        .read_to_end(&mut request_bytes)
        .map_err(|_| CliFailure::runtime("invalid_request"))?;
    if request_bytes.len() > MAX_REQUEST_BYTES {
        return Err(CliFailure::runtime("invalid_request"));
    }
    let request = ArtifactRequest::decode_json(
        &request_bytes,
        now_ms().ok_or_else(|| CliFailure::runtime("deadline_expired"))?,
    )
    .map_err(artifact_failure)?;
    let root =
        duplicate_fd(IPC_APP_DATA_FD).map_err(|_| CliFailure::runtime("artifact_unavailable"))?;
    let request_id = request.request_id.clone();
    let response = execute_ipc(request, root);
    let (header, body) = match response {
        Ok(IpcOutput::Image(image)) => (
            ResponseHeader::png(request_id, image.width, image.height, &image.bytes)
                .map_err(artifact_failure)?,
            image.bytes,
        ),
        Ok(IpcOutput::List(body)) => (
            ResponseHeader::json(request_id, &body).map_err(artifact_failure)?,
            body,
        ),
        Ok(IpcOutput::Empty) => (ResponseHeader::success(request_id), Vec::new()),
        Err(error) => (ResponseHeader::error(request_id, error), Vec::new()),
    };
    write_frame(output, &header, &body).map_err(artifact_failure)
}

fn execute_ipc(
    request: ArtifactRequest,
    root: OwnedFd,
) -> Result<IpcOutput, ArtifactError> {
    let client = EngineClient::new(
        &request.auth.api_url,
        EngineAuth::LocalSession {
            session_token: request.auth.session_token,
            bearer_token: request.auth.bearer_token,
        },
    )?;
    match request.operation {
        ArtifactOperation::List => {
            let after_seq = request.after_seq.ok_or(ArtifactError::InvalidRequest)?;
            let limit = request.limit.ok_or(ArtifactError::InvalidRequest)?;
            let page = client.list_screenshot_page(
                request.room_id,
                after_seq,
                limit,
                request.deadline_ms,
            )?;
            let store = ArtifactStore::from_app_data_fd(root, request.deadline_ms)?;
            let items = page
                .artifacts
                .into_iter()
                .map(|artifact| {
                    let state = match store.inspect(&artifact)? {
                        ArtifactState::Available => ArtifactListState::Available,
                        ArtifactState::Removed => ArtifactListState::Removed,
                        ArtifactState::Unavailable => ArtifactListState::Unavailable,
                    };
                    Ok(ArtifactListItem {
                        room_id: artifact.room_id,
                        event_seq: artifact.event_seq,
                        action_id: artifact.action_id,
                        created_at: artifact.created_at,
                        width: artifact.width,
                        height: artifact.height,
                        state,
                    })
                })
                .collect::<Result<Vec<_>, ArtifactError>>()?;
            let body = ArtifactListResponse {
                version: 1,
                room_id: page.room_id,
                after_seq: page.after_seq,
                scanned_through_seq: page.scanned_through_seq,
                next_after_seq: page.next_after_seq,
                items,
            }
            .encode_json()?;
            Ok(IpcOutput::List(body))
        }
        ArtifactOperation::Get | ArtifactOperation::Delete => {
            let artifact = client.verify_screenshot(
                request.room_id,
                request.action_id.ok_or(ArtifactError::InvalidRequest)?,
                request.event_seq.ok_or(ArtifactError::InvalidRequest)?,
                request.deadline_ms,
            )?;
            let store = ArtifactStore::from_app_data_fd(root, request.deadline_ms)?;
            match request.operation {
                ArtifactOperation::Get => store.read(&artifact).map(IpcOutput::Image),
                ArtifactOperation::Delete => store.delete(&artifact).map(|_| IpcOutput::Empty),
                ArtifactOperation::List => unreachable!(),
            }
        }
    }
}

enum IpcOutput {
    Image(VerifiedPng),
    List(Vec<u8>),
    Empty,
}

fn canonical_app_data_root() -> Result<PathBuf, &'static str> {
    let home = unsafe {
        let record = libc::getpwuid(libc::geteuid());
        if record.is_null() || (*record).pw_dir.is_null() {
            return Err("owner_capability_unavailable");
        }
        CStr::from_ptr((*record).pw_dir)
            .to_str()
            .map_err(|_| "owner_capability_unavailable")?
    };
    #[cfg(target_os = "macos")]
    let path = PathBuf::from(home)
        .join("Library/Application Support")
        .join(APP_DATA_COMPONENT);
    #[cfg(not(target_os = "macos"))]
    let path = PathBuf::from(home)
        .join(".local/share")
        .join(APP_DATA_COMPONENT);
    Ok(path)
}

fn open_owner_directory(path: &Path) -> Result<OwnedFd, ()> {
    let encoded = CString::new(path.as_os_str().as_bytes()).map_err(|_| ())?;
    let raw = unsafe {
        libc::open(
            encoded.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
        )
    };
    if raw < 0 {
        return Err(());
    }
    let fd = unsafe { OwnedFd::from_raw_fd(raw) };
    let metadata = fstat(fd.as_raw_fd()).map_err(|_| ())?;
    if metadata.st_mode as libc::mode_t & libc::S_IFMT != libc::S_IFDIR
        || metadata.st_uid != unsafe { libc::geteuid() }
    {
        return Err(());
    }
    Ok(fd)
}

fn duplicate_fd(fd: RawFd) -> Result<OwnedFd, CliFailure> {
    let raw = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 0) };
    if raw < 0 {
        Err(CliFailure::runtime("artifact_unavailable"))
    } else {
        Ok(unsafe { OwnedFd::from_raw_fd(raw) })
    }
}

fn fstat(fd: RawFd) -> Result<libc::stat, ()> {
    let mut metadata = std::mem::MaybeUninit::<libc::stat>::uninit();
    if unsafe { libc::fstat(fd, metadata.as_mut_ptr()) } != 0 {
        Err(())
    } else {
        Ok(unsafe { metadata.assume_init() })
    }
}

fn sync_fd(fd: RawFd) -> Result<(), ()> {
    (unsafe { libc::fsync(fd) } == 0).then_some(()).ok_or(())
}

fn now_ms() -> Option<u64> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()?
        .as_millis()
        .try_into()
        .ok()
}

#[cfg(test)]
#[allow(dead_code)]
#[derive(Clone, Copy)]
pub(crate) enum CapabilitySwapPoint {
    BeforeOpen,
    AfterOpen,
}

#[cfg(test)]
#[allow(dead_code)]
pub(crate) fn capability_swap_rejected_for_test(
    root: &Path,
    replacement: &Path,
    point: CapabilitySwapPoint,
) -> bool {
    let Ok(root_fd) = open_owner_directory(root) else {
        return false;
    };
    let Some(deadline_ms) = now_ms().and_then(|now| now.checked_add(10_000)) else {
        return false;
    };
    let target = root.join(CAPABILITY_FILE);
    let swap = || std::fs::rename(replacement, &target).unwrap();
    let result = match point {
        CapabilitySwapPoint::BeforeOpen => {
            read_endpoint_with_hooks(root_fd.as_raw_fd(), deadline_ms, swap, || {})
        }
        CapabilitySwapPoint::AfterOpen => {
            read_endpoint_with_hooks(root_fd.as_raw_fd(), deadline_ms, || {}, swap)
        }
    };
    matches!(result, Err(failure) if failure.code == "owner_capability_unavailable")
}

#[cfg(test)]
#[allow(dead_code)]
pub(crate) fn run_with_root_for_test(
    arguments: Vec<OsString>,
    root: &Path,
    input: &mut dyn Read,
    output: &mut dyn Write,
    error: &mut dyn Write,
) -> i32 {
    run(arguments, Some(root), input, output, error)
}

#[cfg(test)]
#[allow(dead_code)]
pub(crate) fn canonical_app_data_root_for_test() -> Result<PathBuf, &'static str> {
    canonical_app_data_root()
}
