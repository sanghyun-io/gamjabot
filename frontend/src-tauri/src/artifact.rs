use crate::{core_client::CoreClient, local_endpoint::LocalEndpoint, HostRuntime};
use gamja_artifact::{
    read_frame, ActionId, ArtifactListResponse, ArtifactOperation, ArtifactRequest,
    ArtifactResponse, RequestId, ResponseState, MAX_REQUEST_BYTES,
};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    ffi::CString,
    fs::{self, File, Metadata, OpenOptions},
    io::Write,
    os::{
        fd::{AsRawFd, FromRawFd, RawFd},
        unix::{
            ffi::OsStrExt,
            fs::{FileExt, MetadataExt, OpenOptionsExt, PermissionsExt},
            process::ExitStatusExt,
        },
    },
    path::{Path, PathBuf},
    process::ExitStatus,
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use tauri::Manager;

pub(crate) const PARENT_TIMEOUT: Duration = Duration::from_secs(30);
const ARTIFACT_NAME: &str = "gamja-artifact";
const CHILD_APP_DATA_FD: RawFd = 3;

pub(crate) struct ArtifactInvocation {
    pub(crate) request_id: RequestId,
    pub(crate) operation: ArtifactOperation,
    pub(crate) bytes: Vec<u8>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct FileIdentity {
    dev: u64,
    ino: u64,
    len: u64,
    modified_seconds: i64,
    modified_nanoseconds: i64,
    changed_seconds: i64,
    changed_nanoseconds: i64,
}

impl FileIdentity {
    fn from_metadata(metadata: &Metadata) -> Self {
        Self {
            dev: metadata.dev(),
            ino: metadata.ino(),
            len: metadata.len(),
            modified_seconds: metadata.mtime(),
            modified_nanoseconds: metadata.mtime_nsec(),
            changed_seconds: metadata.ctime(),
            changed_nanoseconds: metadata.ctime_nsec(),
        }
    }
}

pub(crate) struct VerifiedExecutable {
    path: PathBuf,
    file: File,
    identity: FileIdentity,
    digest: String,
}

impl VerifiedExecutable {
    pub(crate) fn path(&self) -> &Path {
        &self.path
    }

    pub(crate) fn revalidate_opened(&self) -> Result<(), &'static str> {
        let before =
            FileIdentity::from_metadata(&self.file.metadata().map_err(|_| "artifact_unavailable")?);
        if before != self.identity || hash_opened(&self.file)? != self.digest {
            return Err("artifact_unavailable");
        }
        let after =
            FileIdentity::from_metadata(&self.file.metadata().map_err(|_| "artifact_unavailable")?);
        if after != self.identity {
            return Err("artifact_unavailable");
        }
        Ok(())
    }
}

#[derive(Serialize)]
struct RequestWire<'a> {
    version: u16,
    request_id: &'a RequestId,
    deadline_ms: u64,
    operation: ArtifactOperation,
    room_id: u64,
    action_id: &'a ActionId,
    event_seq: u64,
    auth: AuthWire<'a>,
}

#[derive(Serialize)]
struct ListRequestWire<'a> {
    version: u16,
    request_id: &'a RequestId,
    deadline_ms: u64,
    operation: ArtifactOperation,
    room_id: u64,
    after_seq: u64,
    limit: u16,
    auth: AuthWire<'a>,
}

#[derive(Serialize)]
struct AuthWire<'a> {
    api_url: &'a str,
    session_token: &'a str,
    bearer_token: &'a str,
}

pub(crate) fn build_request(
    operation: ArtifactOperation,
    room_id: u64,
    action_id: &str,
    event_seq: u64,
    bearer_token: &str,
    session: &LocalEndpoint,
    started_epoch_ms: u64,
) -> Result<ArtifactInvocation, &'static str> {
    let deadline_ms = started_epoch_ms
        .checked_add(PARENT_TIMEOUT.as_millis() as u64)
        .ok_or("invalid_request")?;
    let action_id = ActionId::parse(action_id.to_owned()).map_err(|_| "invalid_request")?;
    let request_id = random_request_id()?;
    let wire = RequestWire {
        version: 1,
        request_id: &request_id,
        deadline_ms,
        operation,
        room_id,
        action_id: &action_id,
        event_seq,
        auth: AuthWire {
            api_url: &session.base_url,
            session_token: &session.session_token,
            bearer_token,
        },
    };
    let bytes = serde_json::to_vec(&wire).map_err(|_| "invalid_request")?;
    if bytes.len() > MAX_REQUEST_BYTES
        || ArtifactRequest::decode_json(&bytes, started_epoch_ms).is_err()
    {
        return Err("invalid_request");
    }
    Ok(ArtifactInvocation {
        request_id,
        operation,
        bytes,
    })
}

pub(crate) fn build_list_request(
    room_id: u64,
    after_seq: u64,
    limit: u16,
    bearer_token: &str,
    session: &LocalEndpoint,
    started_epoch_ms: u64,
) -> Result<ArtifactInvocation, &'static str> {
    let deadline_ms = started_epoch_ms
        .checked_add(PARENT_TIMEOUT.as_millis() as u64)
        .ok_or("invalid_request")?;
    let request_id = random_request_id()?;
    let wire = ListRequestWire {
        version: 1,
        request_id: &request_id,
        deadline_ms,
        operation: ArtifactOperation::List,
        room_id,
        after_seq,
        limit,
        auth: AuthWire {
            api_url: &session.base_url,
            session_token: &session.session_token,
            bearer_token,
        },
    };
    let bytes = serde_json::to_vec(&wire).map_err(|_| "invalid_request")?;
    if bytes.len() > MAX_REQUEST_BYTES
        || ArtifactRequest::decode_json(&bytes, started_epoch_ms).is_err()
    {
        return Err("invalid_request");
    }
    Ok(ArtifactInvocation {
        request_id,
        operation: ArtifactOperation::List,
        bytes,
    })
}

fn random_request_id() -> Result<RequestId, &'static str> {
    let mut bytes = [0u8; 16];
    getrandom::fill(&mut bytes).map_err(|_| "artifact_unavailable")?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    RequestId::parse(format!(
        "{:02x}{:02x}{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}-{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
        bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]
    ))
    .map_err(|_| "artifact_unavailable")
}

#[cfg(test)]
pub(crate) fn decode_response(
    bytes: &[u8],
    exit_success: bool,
    request_id: &RequestId,
    operation: ArtifactOperation,
) -> Result<Option<Vec<u8>>, &'static str> {
    if !exit_success {
        return Err("artifact_unavailable");
    }
    let response = read_frame(bytes).map_err(|_| "artifact_unavailable")?;
    validate_response(response, request_id, operation)
}

fn validate_response(
    response: ArtifactResponse,
    request_id: &RequestId,
    operation: ArtifactOperation,
) -> Result<Option<Vec<u8>>, &'static str> {
    if &response.header.request_id != request_id {
        return Err("artifact_unavailable");
    }
    match response.header.state {
        ResponseState::Error => Err(response
            .header
            .error_code
            .map(|error| error.code())
            .unwrap_or("artifact_unavailable")),
        ResponseState::Ok => match operation {
            ArtifactOperation::List if !response.body.is_empty() => Ok(Some(response.body)),
            ArtifactOperation::Get if !response.body.is_empty() => Ok(Some(response.body)),
            ArtifactOperation::Delete if response.body.is_empty() => Ok(None),
            _ => Err("artifact_unavailable"),
        },
    }
}

pub(crate) fn verify_executable_at(
    path: &Path,
    expected: &Path,
    expected_hash: &str,
    verify_signature: &mut dyn FnMut(&Path) -> bool,
) -> Result<VerifiedExecutable, &'static str> {
    if !path.is_absolute()
        || path != expected
        || expected_hash.len() != 64
        || !expected_hash
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err("artifact_unavailable");
    }
    let canonical = fs::canonicalize(path).map_err(|_| "artifact_unavailable")?;
    if canonical != path {
        return Err("artifact_unavailable");
    }
    let path_before = fs::symlink_metadata(path).map_err(|_| "artifact_unavailable")?;
    let current_uid = unsafe { libc::geteuid() };
    if !path_before.is_file()
        || path_before.file_type().is_symlink()
        || !matches!(path_before.uid(), 0) && path_before.uid() != current_uid
        || path_before.nlink() != 1
        || path_before.mode() & 0o022 != 0
        || path_before.permissions().mode() & 0o111 == 0
    {
        return Err("artifact_unavailable");
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(path)
        .map_err(|_| "artifact_unavailable")?;
    let opened_before = file.metadata().map_err(|_| "artifact_unavailable")?;
    if opened_before.dev() != path_before.dev() || opened_before.ino() != path_before.ino() {
        return Err("artifact_unavailable");
    }
    let digest = hash_opened(&file)?;
    let opened_after = file.metadata().map_err(|_| "artifact_unavailable")?;
    let path_after = fs::symlink_metadata(path).map_err(|_| "artifact_unavailable")?;
    if digest != expected_hash
        || opened_before.dev() != opened_after.dev()
        || opened_before.ino() != opened_after.ino()
        || opened_before.len() != opened_after.len()
        || opened_after.dev() != path_after.dev()
        || opened_after.ino() != path_after.ino()
        || !verify_signature(path)
    {
        return Err("artifact_unavailable");
    }
    let verified = VerifiedExecutable {
        path: path.to_path_buf(),
        file,
        identity: FileIdentity::from_metadata(&opened_after),
        digest,
    };
    verified.revalidate_opened()?;
    Ok(verified)
}

fn hash_opened(file: &File) -> Result<String, &'static str> {
    let mut hash = Sha256::new();
    let mut block = [0u8; 64 * 1024];
    let mut offset = 0;
    loop {
        let read = file
            .read_at(&mut block, offset)
            .map_err(|_| "artifact_unavailable")?;
        if read == 0 {
            break;
        }
        hash.update(&block[..read]);
        offset = offset
            .checked_add(u64::try_from(read).map_err(|_| "artifact_unavailable")?)
            .ok_or("artifact_unavailable")?;
    }
    Ok(format!("{:x}", hash.finalize()))
}

fn bundled_executable() -> Result<VerifiedExecutable, &'static str> {
    let current = std::env::current_exe().map_err(|_| "artifact_unavailable")?;
    let directory = current.parent().ok_or("artifact_unavailable")?;
    let expected = directory.join(ARTIFACT_NAME);
    let pinned = option_env!("GAMJA_ARTIFACT_SHA256").ok_or("artifact_unavailable")?;
    verify_executable_at(&expected, &expected, pinned, &mut verify_signature)
}

fn verify_signature(path: &Path) -> bool {
    run_signature_verifier(Path::new("/usr/bin/codesign"), path, Duration::from_secs(2))
}

fn run_signature_verifier(program: &Path, path: &Path, timeout: Duration) -> bool {
    use gamja_core_host::macos_spawn::OwnedChild as VerifierChild;
    let result = (|| {
        let input = File::open("/dev/null").ok()?;
        let output = OpenOptions::new().write(true).open("/dev/null").ok()?;
        let error = OpenOptions::new().write(true).open("/dev/null").ok()?;
        let args = [
            std::ffi::OsStr::new("--verify"),
            std::ffi::OsStr::new("--strict"),
            path.as_os_str(),
        ];
        // Signature checking retains its empty environment and emits no child output.
        let mut child = VerifierChild::spawn(
            program,
            &args,
            Vec::new(),
            &[
                (input.as_raw_fd(), 0),
                (output.as_raw_fd(), 1),
                (error.as_raw_fd(), 2),
            ],
            timeout,
        )
        .ok()?;
        child.wait_timeout(timeout).ok()
    })();
    result.is_some_and(|status| status.success())
}

#[cfg(test)]
mod signature_verifier_tests {
    use super::*;
    use std::os::{
        fd::OwnedFd,
        unix::{fs::PermissionsExt, net::UnixStream},
    };

    struct TestRoot(PathBuf);
    impl TestRoot {
        fn new() -> Self {
            let mut random = [0u8; 16];
            getrandom::fill(&mut random).unwrap();
            let path = std::env::temp_dir()
                .join(format!("gamja-signature-fd-{:x}", Sha256::digest(random)));
            fs::create_dir(&path).unwrap();
            fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
            Self(path)
        }
    }
    impl Drop for TestRoot {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn signature_verifier_excludes_ambient_read_write_socket_descriptors() {
        let root = TestRoot::new();
        let program = root.0.join("verify");
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
        let path = root.0.join("public.bin");
        fs::write(&program, format!("#!/usr/bin/python3\nimport os,sys,stat\nfor fd in [{numbers}]:\n try: os.fstat(fd); sys.exit(17)\n except OSError as e: assert e.errno==9\nassert sys.argv[1:]==['--verify','--strict',{:?}]\nassert all(stat.S_ISCHR(os.fstat(fd).st_mode) for fd in [0,1,2])\n", path.to_string_lossy())).unwrap();
        fs::set_permissions(&program, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(run_signature_verifier(
            &program,
            &path,
            Duration::from_secs(2)
        ));
    }

    #[test]
    fn stalled_signature_verifier_is_bounded_and_reaped() {
        let root = TestRoot::new();
        let program = root.0.join("verify");
        let marker = root.0.join("verifier.pid");
        fs::write(
            &program,
            format!(
                "#!/bin/sh\necho $$ > '{}'\nexec /bin/sleep 30\n",
                marker.display()
            ),
        )
        .unwrap();
        fs::set_permissions(&program, fs::Permissions::from_mode(0o700)).unwrap();
        let started = Instant::now();
        assert!(!run_signature_verifier(
            &program,
            &root.0.join("public.bin"),
            Duration::from_millis(500)
        ));
        assert!(started.elapsed() < Duration::from_secs(3));
        let pid: i32 = fs::read_to_string(marker).unwrap().trim().parse().unwrap();
        assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::ESRCH)
        );
    }
}

unsafe extern "C" {
    fn posix_spawn_file_actions_addchdir_np(
        actions: *mut libc::posix_spawn_file_actions_t,
        path: *const libc::c_char,
    ) -> libc::c_int;
}

struct OwnedChild {
    pid: u32,
}

impl OwnedChild {
    fn try_wait(&mut self) -> std::io::Result<Option<ExitStatus>> {
        let pid =
            i32::try_from(self.pid).map_err(|_| std::io::Error::from_raw_os_error(libc::EINVAL))?;
        let mut status = 0;
        loop {
            let waited = unsafe { libc::waitpid(pid, &mut status, libc::WNOHANG) };
            if waited == pid {
                return Ok(Some(ExitStatus::from_raw(status)));
            }
            if waited == 0 {
                return Ok(None);
            }
            let error = std::io::Error::last_os_error();
            if error.kind() != std::io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }

    fn wait(&mut self) -> std::io::Result<ExitStatus> {
        let pid =
            i32::try_from(self.pid).map_err(|_| std::io::Error::from_raw_os_error(libc::EINVAL))?;
        let mut status = 0;
        loop {
            let waited = unsafe { libc::waitpid(pid, &mut status, 0) };
            if waited == pid {
                return Ok(ExitStatus::from_raw(status));
            }
            let error = std::io::Error::last_os_error();
            if error.kind() != std::io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }
}

struct SuspendedArtifact {
    child: OwnedChild,
    stdin: File,
    stdout: File,
}

fn pipe_cloexec() -> Result<(File, File), &'static str> {
    let mut fds = [-1; 2];
    if unsafe { libc::pipe(fds.as_mut_ptr()) } != 0 {
        return Err("artifact_unavailable");
    }
    let first = unsafe { File::from_raw_fd(fds[0]) };
    let second = unsafe { File::from_raw_fd(fds[1]) };
    for file in [&first, &second] {
        if unsafe { libc::fcntl(file.as_raw_fd(), libc::F_SETFD, libc::FD_CLOEXEC) } != 0 {
            return Err("artifact_unavailable");
        }
    }
    Ok((first, second))
}

fn duplicate_cloexec(file: &File) -> Result<File, &'static str> {
    let fd = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 10) };
    if fd < 0 {
        return Err("artifact_unavailable");
    }
    Ok(unsafe { File::from_raw_fd(fd) })
}

fn spawn_suspended(
    executable: &VerifiedExecutable,
    app_data: &File,
) -> Result<SuspendedArtifact, &'static str> {
    let (request_read, request_write) = pipe_cloexec()?;
    let (response_read, response_write) = pipe_cloexec()?;
    let child_stdin = duplicate_cloexec(&request_read)?;
    let child_stdout = duplicate_cloexec(&response_write)?;
    let child_app_data = duplicate_cloexec(app_data)?;
    let child_stderr = OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_CLOEXEC)
        .open("/dev/null")
        .map_err(|_| "artifact_unavailable")?;

    let path = CString::new(executable.path().as_os_str().as_bytes())
        .map_err(|_| "artifact_unavailable")?;
    let ipc = CString::new("--ipc").map_err(|_| "artifact_unavailable")?;
    let root = CString::new("/").map_err(|_| "artifact_unavailable")?;
    let mut argv = [
        path.as_ptr().cast_mut(),
        ipc.as_ptr().cast_mut(),
        std::ptr::null_mut(),
    ];
    let mut envp = [std::ptr::null_mut::<libc::c_char>()];

    let mut attributes: libc::posix_spawnattr_t = std::ptr::null_mut();
    if unsafe { libc::posix_spawnattr_init(&mut attributes) } != 0 {
        return Err("artifact_unavailable");
    }
    let mut actions: libc::posix_spawn_file_actions_t = std::ptr::null_mut();
    if unsafe { libc::posix_spawn_file_actions_init(&mut actions) } != 0 {
        unsafe {
            libc::posix_spawnattr_destroy(&mut attributes);
        }
        return Err("artifact_unavailable");
    }

    let spawn_result = (|| {
        let flags = libc::POSIX_SPAWN_START_SUSPENDED
            | libc::POSIX_SPAWN_SETPGROUP
            | libc::POSIX_SPAWN_CLOEXEC_DEFAULT;
        let flags = libc::c_short::try_from(flags).map_err(|_| "artifact_unavailable")?;
        let mut result = unsafe { libc::posix_spawnattr_setflags(&mut attributes, flags) };
        if result == 0 {
            result = unsafe { libc::posix_spawnattr_setpgroup(&mut attributes, 0) };
        }
        for (source, target) in [
            (child_stdin.as_raw_fd(), libc::STDIN_FILENO),
            (child_stdout.as_raw_fd(), libc::STDOUT_FILENO),
            (child_stderr.as_raw_fd(), libc::STDERR_FILENO),
            (child_app_data.as_raw_fd(), CHILD_APP_DATA_FD),
        ] {
            if result == 0 {
                result =
                    unsafe { libc::posix_spawn_file_actions_adddup2(&mut actions, source, target) };
            }
        }
        if result == 0 {
            result = unsafe { posix_spawn_file_actions_addchdir_np(&mut actions, root.as_ptr()) };
        }
        if result != 0 {
            return Err("artifact_unavailable");
        }
        let mut pid = 0;
        result = unsafe {
            libc::posix_spawn(
                &mut pid,
                path.as_ptr(),
                &actions,
                &attributes,
                argv.as_mut_ptr(),
                envp.as_mut_ptr(),
            )
        };
        if result != 0 || pid <= 0 {
            return Err("artifact_unavailable");
        }
        Ok(SuspendedArtifact {
            child: OwnedChild {
                pid: u32::try_from(pid).map_err(|_| "artifact_unavailable")?,
            },
            stdin: request_write,
            stdout: response_read,
        })
    })();
    unsafe {
        libc::posix_spawn_file_actions_destroy(&mut actions);
        libc::posix_spawnattr_destroy(&mut attributes);
    }
    spawn_result
}

#[repr(C)]
struct ProcRegionInfo {
    protection: u32,
    max_protection: u32,
    inheritance: u32,
    flags: u32,
    offset: u64,
    behavior: u32,
    user_wired_count: u32,
    user_tag: u32,
    pages_resident: u32,
    pages_shared_now_private: u32,
    pages_swapped_out: u32,
    pages_dirtied: u32,
    reference_count: u32,
    shadow_depth: u32,
    share_mode: u32,
    private_pages_resident: u32,
    shared_pages_resident: u32,
    object_id: u32,
    depth: u32,
    address: u64,
    size: u64,
}

#[repr(C)]
struct ProcRegionWithPathInfo {
    region: ProcRegionInfo,
    vnode: libc::vnode_info_path,
}

pub(crate) fn running_executable_matches(pid: u32, executable: &VerifiedExecutable) -> bool {
    const PROC_PIDREGIONPATHINFO: libc::c_int = 8;
    const VM_PROT_EXECUTE: u32 = 4;

    let Ok(pid) = libc::pid_t::try_from(pid) else {
        return false;
    };
    let mut info = std::mem::MaybeUninit::<ProcRegionWithPathInfo>::zeroed();
    let size = std::mem::size_of::<ProcRegionWithPathInfo>();
    let Ok(size_i32) = libc::c_int::try_from(size) else {
        return false;
    };
    let read = unsafe {
        libc::proc_pidinfo(
            pid,
            PROC_PIDREGIONPATHINFO,
            0,
            info.as_mut_ptr().cast(),
            size_i32,
        )
    };
    if read != size_i32 {
        return false;
    }
    let info = unsafe { info.assume_init() };
    info.region.protection & VM_PROT_EXECUTE != 0
        && u64::from(info.vnode.vip_vi.vi_stat.vst_dev) == executable.identity.dev
        && info.vnode.vip_vi.vi_stat.vst_ino == executable.identity.ino
}

fn wait_for_suspended(pid: u32, deadline: Instant) -> bool {
    let Ok(pid) = libc::pid_t::try_from(pid) else {
        return false;
    };
    let mut status = 0;
    loop {
        let waited = unsafe { libc::waitpid(pid, &mut status, libc::WUNTRACED | libc::WNOHANG) };
        if waited == pid {
            return libc::WIFSTOPPED(status);
        }
        if waited < 0 && std::io::Error::last_os_error().kind() == std::io::ErrorKind::Interrupted {
            continue;
        }
        if waited < 0 || Instant::now() >= deadline {
            return false;
        }
        thread::sleep(Duration::from_millis(1));
    }
}

#[derive(Default)]
pub(crate) struct ArtifactChildren {
    children: Mutex<HashMap<u32, OwnedChild>>,
    shutting_down: AtomicBool,
}

enum Poll {
    Running,
    Exited(ExitStatus),
    Missing,
}

impl ArtifactChildren {
    pub(crate) fn run_verified(
        &self,
        executable: &VerifiedExecutable,
        app_data: File,
        invocation: ArtifactInvocation,
        timeout: Duration,
    ) -> Result<Option<Vec<u8>>, &'static str> {
        self.run_verified_inner(
            executable,
            app_data,
            invocation,
            timeout,
            running_executable_matches,
        )
    }

    #[cfg(test)]
    pub(crate) fn run_test_child(
        &self,
        path: &Path,
        app_data: File,
        invocation: ArtifactInvocation,
        timeout: Duration,
    ) -> Result<Option<Vec<u8>>, &'static str> {
        let file = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC)
            .open(path)
            .map_err(|_| "artifact_unavailable")?;
        let metadata = file.metadata().map_err(|_| "artifact_unavailable")?;
        let executable = VerifiedExecutable {
            path: path.to_path_buf(),
            digest: hash_opened(&file)?,
            identity: FileIdentity::from_metadata(&metadata),
            file,
        };
        self.run_verified_inner(&executable, app_data, invocation, timeout, |_, _| true)
    }

    #[cfg(test)]
    pub(crate) fn run_test_verified(
        &self,
        executable: &VerifiedExecutable,
        app_data: File,
        invocation: ArtifactInvocation,
        timeout: Duration,
    ) -> Result<Option<Vec<u8>>, &'static str> {
        self.run_verified_inner(executable, app_data, invocation, timeout, |_, _| true)
    }

    fn run_verified_inner(
        &self,
        executable: &VerifiedExecutable,
        app_data: File,
        invocation: ArtifactInvocation,
        timeout: Duration,
        verify_running: fn(u32, &VerifiedExecutable) -> bool,
    ) -> Result<Option<Vec<u8>>, &'static str> {
        if self.shutting_down.load(Ordering::Acquire)
            || timeout.is_zero()
            || timeout > PARENT_TIMEOUT
        {
            return Err("artifact_unavailable");
        }
        let deadline = Instant::now()
            .checked_add(timeout)
            .ok_or("artifact_unavailable")?;
        let SuspendedArtifact {
            mut child,
            mut stdin,
            mut stdout,
        } = spawn_suspended(executable, &app_data)?;
        let pid = child.pid;
        if !wait_for_suspended(pid, deadline)
            || !verify_running(pid, executable)
            || executable.revalidate_opened().is_err()
        {
            kill_group(pid);
            let _ = child.wait();
            return Err(if Instant::now() >= deadline {
                "deadline_expired"
            } else {
                "artifact_unavailable"
            });
        }
        if Instant::now() >= deadline {
            kill_group(pid);
            let _ = child.wait();
            return Err("deadline_expired");
        }
        if self.register(child).is_err() {
            return Err("artifact_unavailable");
        }
        let Ok(pid_i32) = i32::try_from(pid) else {
            self.kill_and_reap(pid);
            return Err("artifact_unavailable");
        };
        if unsafe { libc::kill(pid_i32, libc::SIGCONT) } != 0 {
            self.kill_and_reap(pid);
            return Err("artifact_unavailable");
        }
        drop(app_data);
        let request = invocation.bytes;
        let writer = thread::spawn(move || stdin.write_all(&request).is_ok());
        let reader = thread::spawn(move || read_frame(&mut stdout));
        let status = loop {
            if self.shutting_down.load(Ordering::Acquire) {
                self.kill_and_reap(pid);
                break None;
            }
            if Instant::now() >= deadline {
                self.kill_and_reap(pid);
                let _ = writer.join();
                let _ = reader.join();
                return Err("deadline_expired");
            }
            match self.poll(pid) {
                Poll::Running => thread::sleep(Duration::from_millis(5)),
                Poll::Exited(status) => break Some(status),
                Poll::Missing => break None,
            }
        };
        let wrote = writer.join().unwrap_or(false);
        let response = reader
            .join()
            .map_err(|_| "artifact_unavailable")?
            .map_err(|_| "artifact_unavailable")?;
        let status = status.ok_or("artifact_unavailable")?;
        if !wrote || !status.success() {
            return Err("artifact_unavailable");
        }
        validate_response(response, &invocation.request_id, invocation.operation)
    }

    fn register(&self, mut child: OwnedChild) -> Result<(), ()> {
        let pid = child.pid;
        let mut children = self.children.lock().map_err(|_| ())?;
        if self.shutting_down.load(Ordering::Acquire) {
            kill_group(pid);
            let _ = child.wait();
            return Err(());
        }
        children.insert(pid, child);
        Ok(())
    }

    fn poll(&self, pid: u32) -> Poll {
        let Ok(mut children) = self.children.lock() else {
            return Poll::Missing;
        };
        let Some(child) = children.get_mut(&pid) else {
            return Poll::Missing;
        };
        match child.try_wait() {
            Ok(Some(status)) => {
                children.remove(&pid);
                Poll::Exited(status)
            }
            Ok(None) => Poll::Running,
            Err(_) => {
                kill_group(pid);
                if let Some(mut child) = children.remove(&pid) {
                    let _ = child.wait();
                }
                Poll::Missing
            }
        }
    }

    fn kill_and_reap(&self, pid: u32) {
        if let Ok(mut children) = self.children.lock() {
            if let Some(mut child) = children.remove(&pid) {
                kill_group(pid);
                let _ = child.wait();
            }
        }
    }

    pub(crate) fn shutdown(&self) {
        self.shutting_down.store(true, Ordering::Release);
        if let Ok(mut children) = self.children.lock() {
            for (pid, child) in children.iter_mut() {
                kill_group(*pid);
                let _ = child.wait();
            }
            children.clear();
        }
    }

    #[cfg(test)]
    pub(crate) fn active_count(&self) -> usize {
        self.children
            .lock()
            .map(|children| children.len())
            .unwrap_or(0)
    }
}

impl Drop for ArtifactChildren {
    fn drop(&mut self) {
        self.shutdown();
    }
}

fn kill_group(pid: u32) {
    if let Ok(pid) = i32::try_from(pid) {
        unsafe {
            libc::kill(-pid, libc::SIGKILL);
        }
    }
}

fn epoch_ms() -> Result<u64, &'static str> {
    let elapsed = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| "artifact_unavailable")?;
    u64::try_from(elapsed.as_millis()).map_err(|_| "artifact_unavailable")
}

fn execute(
    client: &CoreClient,
    children: &ArtifactChildren,
    operation: ArtifactOperation,
    room_id: u64,
    action_id: &str,
    event_seq: u64,
    bearer_token: &str,
    started: Instant,
    started_epoch_ms: u64,
) -> Result<Option<Vec<u8>>, &'static str> {
    let app_data = client
        .open_app_data_dir()
        .map_err(|_| "artifact_unavailable")?;
    let session = client
        .create_ui_session()
        .map_err(|_| "artifact_unavailable")?;
    let invocation = build_request(
        operation,
        room_id,
        action_id,
        event_seq,
        bearer_token,
        &session,
        started_epoch_ms,
    )?;
    let executable = bundled_executable()?;
    let remaining = PARENT_TIMEOUT
        .checked_sub(started.elapsed())
        .ok_or("deadline_expired")?;
    children.run_verified(&executable, app_data, invocation, remaining)
}

async fn invoke_native(
    window: tauri::Window,
    operation: ArtifactOperation,
    room_id: u64,
    action_id: String,
    event_seq: u64,
    bearer_token: String,
) -> Result<Option<Vec<u8>>, String> {
    if window.label() != "main" {
        return Err("artifact_unavailable".into());
    }
    let started = Instant::now();
    let started_epoch_ms = epoch_ms().map_err(str::to_owned)?;
    let app = window.app_handle().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let runtime = app.state::<HostRuntime>();
        let children = app.state::<ArtifactChildren>();
        match runtime.inner() {
            HostRuntime::Attach(client) => execute(
                client,
                children.inner(),
                operation,
                room_id,
                &action_id,
                event_seq,
                &bearer_token,
                started,
                started_epoch_ms,
            ),
        }
    })
    .await
    .map_err(|_| "artifact_unavailable".to_owned())?
    .map_err(str::to_owned)
}

async fn invoke_native_list(
    window: tauri::Window,
    room_id: u64,
    after_seq: u64,
    limit: u16,
    bearer_token: String,
) -> Result<ArtifactListResponse, String> {
    if window.label() != "main" {
        return Err("artifact_unavailable".into());
    }
    let started = Instant::now();
    let started_epoch_ms = epoch_ms().map_err(str::to_owned)?;
    let app = window.app_handle().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let runtime = app.state::<HostRuntime>();
        let children = app.state::<ArtifactChildren>();
        let HostRuntime::Attach(client) = runtime.inner();
        let app_data = client
            .open_app_data_dir()
            .map_err(|_| "artifact_unavailable")?;
        let session = client
            .create_ui_session()
            .map_err(|_| "artifact_unavailable")?;
        let invocation = build_list_request(
            room_id,
            after_seq,
            limit,
            &bearer_token,
            &session,
            started_epoch_ms,
        )?;
        let executable = bundled_executable()?;
        let remaining = PARENT_TIMEOUT
            .checked_sub(started.elapsed())
            .ok_or("deadline_expired")?;
        let body = children
            .run_verified(&executable, app_data, invocation, remaining)?
            .ok_or("artifact_unavailable")?;
        ArtifactListResponse::decode_json(&body, room_id, after_seq, limit)
            .map_err(|_| "artifact_unavailable")
    })
    .await
    .map_err(|_| "artifact_unavailable".to_owned())?
    .map_err(str::to_owned)
}

#[tauri::command]
pub(crate) async fn list_screenshot_artifacts(
    window: tauri::Window,
    room_id: u64,
    after_seq: u64,
    limit: u16,
    bearer_token: String,
) -> Result<ArtifactListResponse, String> {
    invoke_native_list(window, room_id, after_seq, limit, bearer_token).await
}

#[tauri::command]
pub(crate) async fn read_screenshot_artifact(
    window: tauri::Window,
    room_id: u64,
    action_id: String,
    event_seq: u64,
    bearer_token: String,
) -> Result<tauri::ipc::Response, String> {
    let bytes = invoke_native(
        window,
        ArtifactOperation::Get,
        room_id,
        action_id,
        event_seq,
        bearer_token,
    )
    .await?
    .ok_or_else(|| "artifact_unavailable".to_owned())?;
    Ok(tauri::ipc::Response::new(bytes))
}

#[tauri::command]
pub(crate) async fn delete_screenshot_artifact(
    window: tauri::Window,
    room_id: u64,
    action_id: String,
    event_seq: u64,
    bearer_token: String,
) -> Result<(), String> {
    match invoke_native(
        window,
        ArtifactOperation::Delete,
        room_id,
        action_id,
        event_seq,
        bearer_token,
    )
    .await?
    {
        None => Ok(()),
        Some(_) => Err("artifact_unavailable".into()),
    }
}

pub(crate) fn handle_run_event<R: tauri::Runtime>(
    app: &tauri::AppHandle<R>,
    event: tauri::RunEvent,
) {
    if matches!(event, tauri::RunEvent::Exit) {
        if let Some(children) = app.try_state::<ArtifactChildren>() {
            children.shutdown();
        }
    }
}

pub(crate) fn handle_window_event<R: tauri::Runtime>(
    window: &tauri::Window<R>,
    event: &tauri::WindowEvent,
) {
    if window.label() == "main" && matches!(event, tauri::WindowEvent::Destroyed) {
        if let Some(children) = window.try_state::<ArtifactChildren>() {
            children.shutdown();
        }
    }
}
