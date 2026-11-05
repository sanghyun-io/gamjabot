//! ARM macOS child ownership and exact FD delegation.
//!
//! This is a low-level spawn primitive, not signer authorization. Callers must
//! authorize executable identity before spawning. No other component may reap an
//! owned leader; fresh WNOWAIT observations detect an already completed external
//! reap before signalling. Group signals precede the final leader reap.
use std::{
    collections::HashSet,
    ffi::{CString, OsStr},
    os::{
        fd::{AsRawFd, FromRawFd, OwnedFd, RawFd},
        unix::{ffi::OsStrExt, process::ExitStatusExt},
    },
    process::ExitStatus,
    time::{Duration, Instant},
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SpawnError {
    NulInString,
    InvalidEnvironment,
    DuplicateFd,
    InvalidFd,
    SpawnFailed(i32),
    Timeout,
    AlreadyReaped,
    CleanupUncertain,
}
impl std::fmt::Display for SpawnError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NulInString => write!(f, "spawn_string_invalid"),
            Self::InvalidEnvironment => write!(f, "spawn_environment_invalid"),
            Self::DuplicateFd => write!(f, "spawn_fd_duplicate"),
            Self::InvalidFd => write!(f, "spawn_fd_invalid"),
            Self::SpawnFailed(_) => write!(f, "spawn_failed"),
            Self::Timeout => write!(f, "child_timeout"),
            Self::AlreadyReaped => write!(f, "child_already_reaped"),
            Self::CleanupUncertain => write!(f, "child_cleanup_uncertain"),
        }
    }
}
fn checked(rc: i32) -> Result<(), SpawnError> {
    if rc == 0 {
        Ok(())
    } else {
        Err(SpawnError::SpawnFailed(rc))
    }
}
fn errno() -> i32 {
    std::io::Error::last_os_error()
        .raw_os_error()
        .unwrap_or(libc::EIO)
}
// Public libproc API: querying only this retained process group, not all PIDs.
#[link(name = "proc")]
extern "C" {
    fn proc_listpids(kind: u32, info: u32, buffer: *mut libc::c_void, bytes: i32) -> i32;
}
struct Attr(libc::posix_spawnattr_t);
impl Attr {
    fn new() -> Result<Self, SpawnError> {
        let mut raw = std::ptr::null_mut();
        checked(unsafe { libc::posix_spawnattr_init(&mut raw) })?;
        Ok(Self(raw))
    }
}
impl Drop for Attr {
    fn drop(&mut self) {
        unsafe { libc::posix_spawnattr_destroy(&mut self.0) };
    }
}
struct Actions(libc::posix_spawn_file_actions_t);
impl Actions {
    fn new() -> Result<Self, SpawnError> {
        let mut raw = std::ptr::null_mut();
        checked(unsafe { libc::posix_spawn_file_actions_init(&mut raw) })?;
        Ok(Self(raw))
    }
}
impl Drop for Actions {
    fn drop(&mut self) {
        unsafe { libc::posix_spawn_file_actions_destroy(&mut self.0) };
    }
}

#[derive(Debug)]
pub struct OwnedChild {
    pid: u32,
    reaped: bool,
    group_owned: bool,
}
impl OwnedChild {
    /// Spawn without a shell or inherited environment. Only exact dup2 mappings
    /// survive CLOEXEC_DEFAULT. The final Duration is retained for API compatibility;
    /// posix_spawn itself has no cancellable startup deadline. Use wait_timeout for
    /// the child's protocol deadline. Source descriptors are never modified.
    pub fn spawn(
        program: impl AsRef<OsStr>,
        args: &[impl AsRef<OsStr>],
        env: Vec<(String, String)>,
        fd_mappings: &[(RawFd, RawFd)],
        _startup_timeout: Duration,
    ) -> Result<Self, SpawnError> {
        Self::spawn_mode(program, args, env, fd_mappings, true, false)
    }
    /// A new session preserves background daemon isolation from the terminal.
    pub fn spawn_detached(
        program: impl AsRef<OsStr>,
        args: &[impl AsRef<OsStr>],
        env: Vec<(String, String)>,
        fd_mappings: &[(RawFd, RawFd)],
        _startup_timeout: Duration,
    ) -> Result<Self, SpawnError> {
        Self::spawn_mode(program, args, env, fd_mappings, true, true)
    }
    /// Spawn in the parent's process group (no new session, no new group).
    /// Used for relay processes that must stay in the CLI's group.
    pub fn spawn_inherited_group(
        program: impl AsRef<OsStr>,
        args: &[impl AsRef<OsStr>],
        env: Vec<(String, String)>,
        fd_mappings: &[(RawFd, RawFd)],
        _startup_timeout: Duration,
    ) -> Result<Self, SpawnError> {
        Self::spawn_mode(program, args, env, fd_mappings, false, false)
    }
    fn spawn_mode(
        program: impl AsRef<OsStr>,
        args: &[impl AsRef<OsStr>],
        env: Vec<(String, String)>,
        fd_mappings: &[(RawFd, RawFd)],
        group_owned: bool,
        detached: bool,
    ) -> Result<Self, SpawnError> {
        let program =
            CString::new(program.as_ref().as_bytes()).map_err(|_| SpawnError::NulInString)?;
        let mut strings = vec![program.clone()];
        for a in args {
            strings.push(CString::new(a.as_ref().as_bytes()).map_err(|_| SpawnError::NulInString)?);
        }
        let mut keys = HashSet::new();
        let mut environment = Vec::new();
        for (key, value) in env {
            if key.is_empty() || key.contains('=') || !keys.insert(key.clone()) {
                return Err(SpawnError::InvalidEnvironment);
            }
            environment
                .push(CString::new(format!("{key}={value}")).map_err(|_| SpawnError::NulInString)?);
        }
        let mut targets = HashSet::new();
        let mut sources = HashSet::new();
        let mut min = 3i32;
        for &(source, target) in fd_mappings {
            if source < 0 || target < 0 {
                return Err(SpawnError::InvalidFd);
            }
            if !targets.insert(target) || !sources.insert(source) {
                return Err(SpawnError::DuplicateFd);
            }
            min = min.max(target.checked_add(1).ok_or(SpawnError::InvalidFd)?);
            if unsafe { libc::fcntl(source, libc::F_GETFD) } < 0 {
                return Err(SpawnError::InvalidFd);
            }
        }
        // Allocate fresh CLOEXEC copies outside every destination. Unlike dup2 into a
        // chosen number this cannot overwrite unrelated open descriptors, and RAII
        // closes each unique copy on every attribute/action/spawn error path.
        let mut staged = Vec::<OwnedFd>::new();
        for &(source, _) in fd_mappings {
            let fd = unsafe { libc::fcntl(source, libc::F_DUPFD_CLOEXEC, min) };
            if fd < 0 {
                return Err(SpawnError::SpawnFailed(errno()));
            }
            staged.push(unsafe { OwnedFd::from_raw_fd(fd) });
        }
        let mut attr = Attr::new()?;
        let mut flags = libc::POSIX_SPAWN_CLOEXEC_DEFAULT as libc::c_short;
        if detached {
            // Public Darwin sys/spawn.h flag; libc does not export this constant.
            flags |= 0x0400; // POSIX_SPAWN_SETSID
        } else if group_owned {
            flags |= libc::POSIX_SPAWN_SETPGROUP as libc::c_short;
            checked(unsafe { libc::posix_spawnattr_setpgroup(&mut attr.0, 0) })?;
        }
        checked(unsafe { libc::posix_spawnattr_setflags(&mut attr.0, flags) })?;
        let mut actions = Actions::new()?;
        for (copy, &(_, target)) in staged.iter().zip(fd_mappings) {
            checked(unsafe {
                libc::posix_spawn_file_actions_adddup2(&mut actions.0, copy.as_raw_fd(), target)
            })?;
        }
        let argv: Vec<_> = strings
            .iter()
            .map(|s| s.as_ptr())
            .chain(std::iter::once(std::ptr::null()))
            .collect();
        let envp: Vec<_> = environment
            .iter()
            .map(|s| s.as_ptr())
            .chain(std::iter::once(std::ptr::null()))
            .collect();
        let mut pid = 0;
        checked(unsafe {
            libc::posix_spawn(
                &mut pid,
                program.as_ptr(),
                &actions.0,
                &attr.0,
                argv.as_ptr() as *mut _,
                envp.as_ptr() as *mut _,
            )
        })?;
        Ok(Self {
            pid: pid as u32,
            reaped: false,
            group_owned,
        })
    }
    pub fn pid(&self) -> u32 {
        self.pid
    }
    pub fn id(&self) -> u32 {
        self.pid
    }
    /// The caller must first authenticate this exact PID's Core health and
    /// transfer lifecycle ownership to its private control/lifeline mechanism.
    /// This consumes the wrapper; no numeric signalling occurs after transfer.
    pub fn detach_after_authenticated_health(mut self) -> u32 {
        self.reaped = true;
        self.pid
    }
    pub fn pgid(&self) -> u32 {
        self.pid
    }
    /// Observe but retain an exited leader. Never substitute a cached exit status
    /// for a fresh waitability check: an external reap removes signal authority.
    pub fn try_wait(&mut self) -> Result<Option<ExitStatus>, SpawnError> {
        if self.reaped {
            return Err(SpawnError::AlreadyReaped);
        }
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        if unsafe {
            libc::waitid(
                libc::P_PID,
                self.pid,
                &mut info,
                libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
            )
        } < 0
        {
            let e = errno();
            if e == libc::ECHILD {
                self.reaped = true;
                return Err(SpawnError::AlreadyReaped);
            }
            return Err(SpawnError::SpawnFailed(e));
        }
        if info.si_pid != self.pid as i32 {
            return Ok(None);
        }
        let raw = match info.si_code {
            libc::CLD_EXITED => (info.si_status & 0xff) << 8,
            libc::CLD_KILLED => info.si_status & 0x7f,
            libc::CLD_DUMPED => (info.si_status & 0x7f) | 0x80,
            _ => return Err(SpawnError::SpawnFailed(libc::EIO)),
        };
        Ok(Some(ExitStatus::from_raw(raw)))
    }
    pub fn kill(&mut self) -> Result<(), SpawnError> {
        if self.try_wait()?.is_some() {
            return Ok(());
        }
        if unsafe { libc::kill(self.pid as i32, libc::SIGKILL) } < 0 {
            let e = errno();
            if e != libc::ESRCH {
                return Err(SpawnError::SpawnFailed(e));
            }
        }
        Ok(())
    }
    fn group_quiescent(&self) -> Result<bool, SpawnError> {
        let mut members = [0i32; 256];
        let capacity = std::mem::size_of_val(&members) as i32;
        let n = unsafe { proc_listpids(2, self.pid, members.as_mut_ptr().cast(), capacity) };
        // Empty/error, malformed, or potentially truncated snapshots are uncertain.
        if n <= 0 || n >= capacity || n % 4 != 0 {
            return Err(SpawnError::CleanupUncertain);
        }
        let members = &members[..n as usize / 4];
        if !members.contains(&(self.pid as i32)) || members.iter().any(|p| *p <= 0) {
            return Err(SpawnError::CleanupUncertain);
        }
        Ok(members.iter().all(|p| *p == self.pid as i32))
    }
    pub fn kill_group(&mut self) -> Result<(), SpawnError> {
        self.signal_group(libc::SIGKILL)
    }
    /// Send TERM while retaining the leader. The caller owns a bounded grace
    /// period and must subsequently call shutdown to reclaim the entire group.
    pub fn terminate_group(&mut self) -> Result<(), SpawnError> {
        self.signal_group(libc::SIGTERM)
    }
    fn signal_group(&mut self, signal: i32) -> Result<(), SpawnError> {
        if !self.group_owned {
            return Err(SpawnError::CleanupUncertain);
        }
        let exited = self.try_wait()?.is_some();
        if unsafe { libc::kill(-(self.pid as i32), signal) } < 0 {
            let e = errno();
            if e == libc::ESRCH {
                return Ok(());
            }
            // Darwin filters zombies from group signal recipients and returns EPERM
            // when none are addressable. Accept this only with a freshly exited leader
            // and proof that it is the sole remaining group member.
            if e == libc::EPERM && exited {
                return if self.group_quiescent()? {
                    Ok(())
                } else {
                    Err(SpawnError::CleanupUncertain)
                };
            }
            return Err(SpawnError::SpawnFailed(e));
        }
        Ok(())
    }
    fn reap_bounded(&mut self, timeout: Duration) -> Result<ExitStatus, SpawnError> {
        let start = Instant::now();
        loop {
            // Keep the leader/PID reserved until every other group member is gone.
            // Signal success alone cannot prove cleanup (e.g. mixed credential groups).
            if self.group_owned && !matches!(self.group_quiescent(), Ok(true)) {
                if start.elapsed() >= timeout {
                    return Err(SpawnError::CleanupUncertain);
                }
                std::thread::sleep(
                    Duration::from_millis(5).min(timeout.saturating_sub(start.elapsed())),
                );
                continue;
            }
            let mut raw = 0;
            let rc = unsafe { libc::waitpid(self.pid as i32, &mut raw, libc::WNOHANG) };
            if rc == self.pid as i32 {
                self.reaped = true;
                return Ok(ExitStatus::from_raw(raw));
            }
            if rc < 0 {
                let e = errno();
                if e == libc::ECHILD {
                    self.reaped = true;
                    return Err(SpawnError::AlreadyReaped);
                }
                if e != libc::EINTR {
                    return Err(SpawnError::SpawnFailed(e));
                }
            }
            if start.elapsed() >= timeout {
                return Err(SpawnError::CleanupUncertain);
            }
            std::thread::sleep(
                Duration::from_millis(5).min(timeout.saturating_sub(start.elapsed())),
            );
        }
    }
    /// Explicit bounded group cleanup, including descendants of an exited leader.
    /// Callers must consume errors; Drop alone cannot report cleanup uncertainty.
    pub fn shutdown(&mut self, timeout: Duration) -> Result<ExitStatus, SpawnError> {
        let termination = if self.group_owned {
            self.kill_group()
        } else {
            self.kill()
        };
        match termination {
            Ok(()) | Err(SpawnError::CleanupUncertain) => self.reap_bounded(timeout),
            Err(e) => Err(e),
        }
    }
    pub fn wait_timeout(&mut self, timeout: Duration) -> Result<ExitStatus, SpawnError> {
        let start = Instant::now();
        loop {
            if self.try_wait()?.is_some() {
                return self.shutdown(Duration::from_secs(1));
            }
            if start.elapsed() >= timeout {
                self.shutdown(Duration::from_secs(1))?;
                return Err(SpawnError::Timeout);
            }
            std::thread::sleep(
                Duration::from_millis(5).min(timeout.saturating_sub(start.elapsed())),
            );
        }
    }
    /// Indefinite waiting is an explicit caller choice. Cleanup itself is bounded.
    pub fn wait(mut self) -> Result<ExitStatus, SpawnError> {
        loop {
            if self.try_wait()?.is_some() {
                return self.shutdown(Duration::from_secs(1));
            }
            std::thread::sleep(Duration::from_millis(5));
        }
    }
    /// Release an exited relay after authenticating the exact Core PID.
    /// Verifies: group_owned, core_pid>1 and !=relay, freshly exited with success,
    /// getpgid(core)==relay. Then clears group_owned and reaps only the relay.
    /// Caller MUST authenticate Core health before calling this.
    pub fn release_exited_relay_for_core(
        mut self,
        core_pid: u32,
        timeout: Duration,
    ) -> Result<ExitStatus, SpawnError> {
        if !self.group_owned {
            return Err(SpawnError::CleanupUncertain);
        }
        if core_pid <= 1 || core_pid == self.pid {
            return Err(SpawnError::CleanupUncertain);
        }
        // Fresh try_wait with WNOWAIT to verify relay just exited successfully
        let _status = match self.try_wait()? {
            Some(s) if s.success() => s,
            _ => return Err(SpawnError::CleanupUncertain),
        };
        // Verify Core's pgid matches relay
        let pgid = unsafe { libc::getpgid(core_pid as i32) };
        if pgid < 0 || pgid as u32 != self.pid {
            return Err(SpawnError::CleanupUncertain);
        }
        // Clear group ownership before reaping relay
        self.group_owned = false;
        // Reap only the relay with bounded timeout
        self.reap_bounded(timeout)
    }
}
impl Drop for OwnedChild {
    fn drop(&mut self) {
        if !self.reaped {
            let _ = self.shutdown(Duration::from_secs(1));
        }
    }
}

/// Create both pipe ends as owner-held CLOEXEC descriptors. CLOEXEC_DEFAULT at
/// every native spawn closes even descriptors between creation and this seal.
pub fn cloexec_pipe() -> Result<(std::fs::File, std::fs::File), SpawnError> {
    let mut fds = [-1; 2];
    if unsafe { libc::pipe(fds.as_mut_ptr()) } < 0 {
        return Err(SpawnError::SpawnFailed(errno()));
    }
    let reader = unsafe { std::fs::File::from_raw_fd(fds[0]) };
    let writer = unsafe { std::fs::File::from_raw_fd(fds[1]) };
    for fd in fds {
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        if flags < 0 || unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) } < 0 {
            return Err(SpawnError::SpawnFailed(errno()));
        }
    }
    Ok((reader, writer))
}
/// Preserve the former helper's inherited environment without logging values.
/// Invalid Unicode is a finite error rather than a panic or silent omission.
pub fn inherited_environment() -> Result<Vec<(String, String)>, SpawnError> {
    std::env::vars_os()
        .map(|(key, value)| {
            Ok((
                key.into_string()
                    .map_err(|_| SpawnError::InvalidEnvironment)?,
                value
                    .into_string()
                    .map_err(|_| SpawnError::InvalidEnvironment)?,
            ))
        })
        .collect()
}

/// Foreground clients retain the terminal group and never signal that group.
/// Only the retained direct child is killed/reaped on abandonment.
pub struct ForegroundChild(OwnedChild);
impl ForegroundChild {
    pub fn spawn(
        program: impl AsRef<OsStr>,
        args: &[impl AsRef<OsStr>],
        env: Vec<(String, String)>,
        fd_mappings: &[(RawFd, RawFd)],
    ) -> Result<Self, SpawnError> {
        OwnedChild::spawn_mode(program, args, env, fd_mappings, false, false).map(Self)
    }
    pub fn wait(self) -> Result<ExitStatus, SpawnError> {
        self.0.wait()
    }
}
