//! Owner-only filesystem and owned-child process boundaries, independent of Tauri.
use crate::Result;
use fs2::FileExt;
use std::{
    fs::{self, File, OpenOptions},
    path::{Path, PathBuf},
    process::{Child, Command},
};

#[cfg(unix)]
use std::{
    thread,
    time::{Duration, Instant},
};

pub fn app_data_dir() -> Result<PathBuf> {
    #[cfg(target_os = "macos")]
    let root = std::env::var_os("HOME")
        .map(PathBuf::from)
        .map(|p| p.join("Library/Application Support"));
    #[cfg(windows)]
    let root = std::env::var_os("APPDATA").map(PathBuf::from);
    #[cfg(all(unix, not(target_os = "macos")))]
    let root = std::env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|p| PathBuf::from(p).join(".local/share")));
    Ok(root.ok_or("app_data_unavailable")?.join("top.gamja.bot"))
}

pub fn secure_directory(path: &Path) -> Result<()> {
    if let Ok(metadata) = fs::symlink_metadata(path) {
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err("private_directory_required");
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            if metadata.uid() != unsafe { libc::geteuid() } {
                return Err("private_directory_required");
            }
        }
    }
    fs::create_dir_all(path).map_err(|_| "private_directory_required")?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
            .map_err(|_| "private_directory_required")?;
    }
    #[cfg(windows)]
    windows::secure(path)?;
    verify_directory(path)
}
pub fn verify_directory(path: &Path) -> Result<()> {
    let metadata = fs::symlink_metadata(path).map_err(|_| "private_directory_required")?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("private_directory_required");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if metadata.uid() != unsafe { libc::geteuid() } || metadata.mode() & 0o777 != 0o700 {
            return Err("private_directory_required");
        }
    }
    #[cfg(windows)]
    windows::verify(path)?;
    Ok(())
}
fn options() -> OpenOptions {
    let mut options = OpenOptions::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.custom_flags(windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OPEN_REPARSE_POINT);
    }
    options
}
pub fn create_private(path: &Path) -> Result<File> {
    let file = options()
        .write(true)
        .read(true)
        .create_new(true)
        .open(path)
        .map_err(|_| "private_file_required")?;
    #[cfg(windows)]
    windows::secure(path)?;
    verify_file(&file, path)?;
    Ok(file)
}
pub fn open_private(path: &Path) -> Result<File> {
    let file = options()
        .read(true)
        .open(path)
        .map_err(|_| "private_file_required")?;
    verify_file(&file, path)?;
    Ok(file)
}
fn verify_file(file: &File, _path: &Path) -> Result<()> {
    let metadata = file.metadata().map_err(|_| "private_file_required")?;
    if !metadata.is_file() {
        return Err("private_file_required");
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let _ = _path;
        if metadata.uid() != unsafe { libc::geteuid() }
            || metadata.mode() & 0o777 != 0o600
            || metadata.nlink() != 1
        {
            return Err("private_file_required");
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if metadata.file_attributes()
            & windows_sys::Win32::Storage::FileSystem::FILE_ATTRIBUTE_REPARSE_POINT
            != 0
        {
            return Err("private_file_required");
        }
        windows::verify_handle(file)?;
    }
    Ok(())
}
/// A held OS lock is evidence of ownership; PIDs alone never authorize cleanup.
pub fn acquire_lock(path: &Path) -> Result<File> {
    let file = options()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)
        .map_err(|_| "runtime_lock_unavailable")?;
    #[cfg(windows)]
    windows::secure(path)?;
    verify_file(&file, path)?;
    file.try_lock_exclusive()
        .map_err(|_| "runtime_already_running")?;
    Ok(file)
}
pub fn configure_engine(command: &mut Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(
            windows_sys::Win32::System::Threading::CREATE_NO_WINDOW
                | windows_sys::Win32::System::Threading::CREATE_SUSPENDED,
        );
    }
}
pub fn configure_detached(command: &mut Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        unsafe {
            command.pre_exec(|| {
                if libc::setsid() < 0 {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(
            windows_sys::Win32::System::Threading::DETACHED_PROCESS
                | windows_sys::Win32::System::Threading::CREATE_NEW_PROCESS_GROUP,
        );
    }
}
/// Observe an owned leader without releasing its Unix PID/process-group identity.
/// All group signals must finish before the one final `Child::wait` reaps it.
pub fn child_exited(child: &mut Child) -> Result<bool> {
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
            if std::io::Error::last_os_error().kind() != std::io::ErrorKind::Interrupted {
                return Err("engine_monitor_failed");
            }
        }
    }
    #[cfg(windows)]
    {
        // Windows cleanup retains a job/process handle, independent of numeric PID reuse.
        child
            .try_wait()
            .map(|status| status.is_some())
            .map_err(|_| "engine_monitor_failed")
    }
}

pub fn stop_child(child: &mut Child) {
    #[cfg(unix)]
    {
        // If another reaper unexpectedly consumed the leader, numeric group ownership
        // is no longer proven. Fail closed rather than signal a possibly reused ID.
        if child_exited(child).is_err() {
            return;
        }
        let group = -(child.id() as i32);
        unsafe {
            libc::kill(group, libc::SIGTERM);
        }
        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline && unsafe { libc::kill(group, 0) } == 0 {
            // Keep even an exited leader waitable until the final possible SIGKILL.
            thread::sleep(Duration::from_millis(25));
        }
        if unsafe { libc::kill(group, 0) } == 0 {
            unsafe {
                libc::kill(group, libc::SIGKILL);
            }
        }
    }
    #[cfg(windows)]
    {
        let _ = child.kill();
    }
    let _ = child.wait();
}

#[cfg(windows)]
pub(crate) mod windows;
