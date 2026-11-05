//! Lightweight optional helper supervision; this library has no native driver.
mod control;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
pub mod macos_spawn;
pub mod protocol;
#[cfg(windows)]
mod supervision_windows;
use protocol::{Bootstrap, Ready, VERSION};
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
use std::io::{BufRead, BufReader};
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
use std::process::{Child, ChildStdin, Command, Stdio};
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
type HelperChild = macos_spawn::OwnedChild;
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
type HelperChild = Child;
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
type HelperInput = std::fs::File;
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
type HelperInput = ChildStdin;
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, Debug, PartialEq, Eq, serde::Serialize)]
#[serde(tag = "status", content = "code", rename_all = "snake_case")]
pub enum CapabilityStatus {
    Available,
    Unavailable(&'static str),
}

pub struct PlatformHelper {
    path: Option<PathBuf>,
    child: Option<HelperChild>,
    input: Option<HelperInput>,
    credentials: Option<(String, String)>,
    status: CapabilityStatus,
    directory: Option<PathBuf>,
    #[cfg(windows)]
    job: Option<supervision_windows::HelperJob>,
}
impl PlatformHelper {
    pub fn start(path: Option<&Path>) -> Self {
        let mut helper = Self {
            path: path.map(Path::to_path_buf),
            child: None,
            input: None,
            credentials: None,
            directory: None,
            #[cfg(windows)]
            job: None,
            status: CapabilityStatus::Unavailable("native_capability_unavailable"),
        };
        helper.launch();
        helper
    }
    fn launch(&mut self) {
        let Some(path) = self.path.clone().filter(|path| path.is_file()) else {
            return;
        };
        let result = self.launch_inner(&path);
        if let Err(code) = result {
            self.stop();
            if self.status != CapabilityStatus::Unavailable("native_helper_cleanup_uncertain") {
                self.status = CapabilityStatus::Unavailable(code);
            }
        }
    }
    fn launch_inner(&mut self, path: &Path) -> Result<(), &'static str> {
        let mut key = [0; 32];
        getrandom::fill(&mut key).map_err(|_| "native_helper_start_failed")?;
        let mut nonce = [0; 16];
        getrandom::fill(&mut nonce).map_err(|_| "native_helper_start_failed")?;
        #[cfg(unix)]
        let endpoint = {
            use std::os::unix::fs::PermissionsExt;
            // macOS TMPDIR exceeds the Unix socket path limit with a nonce.
            // The random, owner-only directory preserves the legacy short root.
            let directory =
                PathBuf::from("/tmp").join(format!("gamja-helper-{}", protocol::hex(&nonce)));
            std::fs::create_dir(&directory).map_err(|_| "native_helper_start_failed")?;
            self.directory = Some(directory.clone());
            std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700))
                .map_err(|_| "native_helper_start_failed")?;
            directory
                .join("desktop.sock")
                .to_string_lossy()
                .into_owned()
        };
        #[cfg(windows)]
        let endpoint = format!(r"\\.\pipe\gamja-desktop-{}", protocol::hex(&nonce));
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        let (child, input, output) = spawn_native_helper(path)?;
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        let (child, input, output) = {
            let mut command = Command::new(path);
            command
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null());
            #[cfg(unix)]
            {
                use std::os::unix::process::CommandExt;
                command.process_group(0);
            }
            #[cfg(windows)]
            {
                use std::os::windows::process::CommandExt;
                command.creation_flags(
                    windows_sys::Win32::System::Threading::CREATE_SUSPENDED
                        | windows_sys::Win32::System::Threading::CREATE_NO_WINDOW,
                );
            }
            let mut child = command.spawn().map_err(|_| "native_helper_start_failed")?;
            let input = child.stdin.take().ok_or("native_helper_start_failed")?;
            let output = child.stdout.take().ok_or("native_helper_start_failed")?;
            (child, input, output)
        };
        self.input = Some(input);
        self.child = Some(child);
        #[cfg(windows)]
        {
            self.job = Some(supervision_windows::HelperJob::attach(
                self.child.as_ref().unwrap(),
            )?);
        }
        let bootstrap = Bootstrap {
            version: VERSION,
            key: protocol::hex(&key),
            endpoint: endpoint.clone(),
        };
        let input = self.input.as_mut().ok_or("native_helper_start_failed")?;
        serde_json::to_writer(&mut *input, &bootstrap).map_err(|_| "native_helper_start_failed")?;
        input
            .write_all(b"\n")
            .and_then(|_| input.flush())
            .map_err(|_| "native_helper_start_failed")?;
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        let readiness = read_native_ready(output);
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        let (readiness, reader) = {
            let (sender, receiver) = mpsc::channel();
            let reader = thread::spawn(move || {
                let mut line = String::new();
                let result = match BufReader::new(output.take(1024)).read_line(&mut line) {
                    Ok(_) if !line.ends_with('\n') => Err("native_helper_start_failed"),
                    Ok(_) => serde_json::from_str::<Ready>(&line)
                        .map_err(|_| "native_helper_start_failed"),
                    Err(_) => Err("native_helper_start_failed"),
                };
                let _ = sender.send(result);
            });
            let readiness = receiver.recv_timeout(Duration::from_secs(2));
            (
                readiness.unwrap_or(Err("native_helper_start_failed")),
                reader,
            )
        };
        let result = match readiness {
            Ok(ready) if ready.version != VERSION => Err("native_helper_version_mismatch"),
            Ok(ready) if ready.ready && ready.endpoint == endpoint => {
                self.credentials = Some((endpoint, bootstrap.key));
                self.status = CapabilityStatus::Available;
                Ok(())
            }
            Err(code) => Err(code),
            _ => Err("native_helper_start_failed"),
        };
        if result.is_err() {
            self.stop();
        }
        // Closing/killing the owned child closes its readiness pipe. Its process
        // group also covers malformed fixture descendants holding that pipe.
        #[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
        let _ = reader.join();
        result
    }
    pub fn capability_status(&mut self) -> CapabilityStatus {
        if self
            .child
            .as_mut()
            .is_some_and(|child| owned_child_exited(child).unwrap_or(true))
        {
            self.stop();
            // stop() preserves an explicit cleanup failure.
        }
        self.status
    }
    pub fn native_permission_status(&mut self) -> Result<protocol::PermissionStatus, &'static str> {
        let (endpoint, key) = self.credentials().ok_or("native_capability_unavailable")?;
        control::permissions(control::call(
            &endpoint,
            &key,
            protocol::OwnerOperation::PermissionStatus,
            None,
        )?)
    }
    pub fn open_native_settings(
        &mut self,
        panel: protocol::SettingsPanel,
    ) -> Result<(), &'static str> {
        let (endpoint, key) = self.credentials().ok_or("native_capability_unavailable")?;
        control::call(
            &endpoint,
            &key,
            protocol::OwnerOperation::OpenSettings,
            Some(panel),
        )
        .map(|_| ())
    }
    pub fn credentials(&mut self) -> Option<(String, String)> {
        self.capability_status();
        self.credentials.clone()
    }
    /// Explicit restart creates a fresh key/endpoint. Effects are never replayed.
    pub fn restart(&mut self) {
        self.stop();
        if self.status == CapabilityStatus::Unavailable("native_helper_cleanup_uncertain") {
            return;
        }
        self.status = CapabilityStatus::Unavailable("native_capability_unavailable");
        self.launch();
    }
    pub fn stop(&mut self) {
        self.credentials = None;
        self.input = None; // EOF is the helper's core-death lifeline.
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        let mut cleanup_uncertain = false;
        if let Some(mut child) = self.child.take() {
            #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
            if stop_owned_unix_child(&mut child).is_err() {
                self.child = Some(child);
                cleanup_uncertain = true;
            }
            #[cfg(all(unix, not(all(target_os = "macos", target_arch = "aarch64"))))]
            stop_owned_unix_child(&mut child);
            #[cfg(windows)]
            {
                let deadline = Instant::now() + Duration::from_millis(500);
                while owned_child_exited(&mut child).is_ok_and(|exited| !exited)
                    && Instant::now() < deadline
                {
                    thread::sleep(Duration::from_millis(10));
                }
                self.job.take(); // Handles retain ownership independent of numeric PID reuse.
                let _ = child.kill();
                let _ = child.wait();
            }
        }
        #[cfg(all(target_os = "macos", target_arch = "aarch64"))]
        if cleanup_uncertain {
            self.status = CapabilityStatus::Unavailable("native_helper_cleanup_uncertain");
            return;
        }
        if let Some(directory) = self.directory.take() {
            let _ = std::fs::remove_file(directory.join("desktop.sock"));
            let _ = std::fs::remove_dir(directory);
        }
        self.status = CapabilityStatus::Unavailable("native_helper_stopped");
    }
}
impl Drop for PlatformHelper {
    fn drop(&mut self) {
        self.stop();
    }
}

/// Unix polling must not reap the leader while numeric process-group cleanup
/// remains possible. Windows cleanup uses retained process/job handles.
#[cfg(not(all(target_os = "macos", target_arch = "aarch64")))]
fn owned_child_exited(child: &mut Child) -> std::io::Result<bool> {
    #[cfg(unix)]
    {
        loop {
            let mut information: libc::siginfo_t = unsafe { std::mem::zeroed() };
            let observed = unsafe {
                libc::waitid(
                    libc::P_PID,
                    child.id() as libc::id_t,
                    &mut information,
                    libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
                )
            };
            if observed == 0 {
                return Ok(unsafe { information.si_pid() } != 0);
            }
            let error = std::io::Error::last_os_error();
            if error.kind() != std::io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
    }
    #[cfg(windows)]
    {
        child.try_wait().map(|status| status.is_some())
    }
}
#[cfg(all(unix, not(all(target_os = "macos", target_arch = "aarch64"))))]
fn stop_owned_unix_child(child: &mut Child) {
    // If an external reaper consumed the leader, the numeric group identity is
    // no longer proven. Never signal an unproven group or wait indefinitely.
    if owned_child_exited(child).is_err() {
        return;
    }
    let deadline = Instant::now() + Duration::from_millis(500);
    while Instant::now() < deadline {
        if owned_child_exited(child).is_err() {
            return;
        }
        // Allow orderly pipe-EOF shutdown while retaining even an exited leader
        // until all possible descendant/group termination has finished.
        thread::sleep(Duration::from_millis(10));
    }
    unsafe {
        libc::kill(-(child.id() as i32), libc::SIGKILL);
    }
    // This is the one final reap. No numeric signal occurs after it.
    let _ = child.wait();
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn spawn_native_helper(
    path: &Path,
) -> Result<(HelperChild, HelperInput, std::fs::File), &'static str> {
    use std::os::fd::AsRawFd;
    let finite = |_| "native_helper_start_failed";
    let (input_child, input_parent) = macos_spawn::cloexec_pipe().map_err(finite)?;
    let (output_parent, output_child) = macos_spawn::cloexec_pipe().map_err(finite)?;
    let error = std::fs::OpenOptions::new()
        .write(true)
        .open("/dev/null")
        .map_err(|_| "native_helper_start_failed")?;
    let child = macos_spawn::OwnedChild::spawn(
        path,
        &[] as &[&str],
        macos_spawn::inherited_environment().map_err(finite)?,
        &[
            (input_child.as_raw_fd(), 0),
            (output_child.as_raw_fd(), 1),
            (error.as_raw_fd(), 2),
        ],
        Duration::from_secs(2),
    )
    .map_err(finite)?;
    Ok((child, input_parent, output_parent))
}
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn owned_child_exited(child: &mut HelperChild) -> std::io::Result<bool> {
    child
        .try_wait()
        .map(|status| status.is_some())
        .map_err(|_| std::io::Error::other("native_child_monitor_failed"))
}
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn stop_owned_unix_child(child: &mut HelperChild) -> Result<(), macos_spawn::SpawnError> {
    let deadline = Instant::now() + Duration::from_millis(500);
    while Instant::now() < deadline {
        // Preserve the existing EOF grace even for an exited leader: a live
        // descendant can still own its pipes and must remain in the owned group.
        child.try_wait()?;
        thread::sleep(Duration::from_millis(10));
    }
    // OwnedChild retains the leader until group cleanup and final reap.
    child.shutdown(Duration::from_secs(1)).map(|_| ())
}

/// ARM readiness has no reader thread which can outlive a timeout. This file
/// belongs only to the parent; switching it to nonblocking cannot alter stdin.
#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
fn read_native_ready(mut output: std::fs::File) -> Result<Ready, &'static str> {
    use std::os::fd::AsRawFd;
    let fd = output.as_raw_fd();
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFL, flags | libc::O_NONBLOCK) } < 0 {
        return Err("native_helper_start_failed");
    }
    let deadline = Instant::now() + Duration::from_secs(2);
    let mut line = Vec::new();
    let mut buffer = [0; 256];
    loop {
        if Instant::now() >= deadline {
            return Err("native_helper_start_failed");
        }
        match output.read(&mut buffer) {
            Ok(0) => return Err("native_helper_start_failed"),
            Ok(n) => {
                line.extend_from_slice(&buffer[..n]);
                if let Some(end) = line.iter().position(|b| *b == b'\n') {
                    if end >= 1024 {
                        return Err("native_helper_start_failed");
                    }
                    return serde_json::from_slice(&line[..=end])
                        .map_err(|_| "native_helper_start_failed");
                }
                if line.len() >= 1024 {
                    return Err("native_helper_start_failed");
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(5))
            }
            Err(_) => return Err("native_helper_start_failed"),
        }
    }
}
