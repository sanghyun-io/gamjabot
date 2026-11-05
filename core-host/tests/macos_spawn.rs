//! Child probes are confined to this integration-test executable.
#![cfg(all(target_os = "macos", target_arch = "aarch64"))]
use gamja_core_host::macos_spawn::{OwnedChild, SpawnError};
use std::{
    fs::File,
    io::{Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd, OwnedFd, RawFd},
        unix::{net::UnixStream, process::ExitStatusExt},
    },
    process::{Command, Stdio},
    time::{Duration, Instant},
};
const LIMIT: Duration = Duration::from_secs(2);
fn identity(fd: RawFd) -> String {
    let mut s: libc::stat = unsafe { std::mem::zeroed() };
    assert_eq!(unsafe { libc::fstat(fd, &mut s) }, 0);
    format!("{}:{}:{}", s.st_dev, s.st_ino, s.st_mode & libc::S_IFMT)
}
fn flags(fd: RawFd) -> i32 {
    let v = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    assert!(v >= 0);
    v
}
fn high(fd: RawFd, min: RawFd, clo: bool) -> OwnedFd {
    let v = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, min) };
    assert!(v >= min);
    let copy = unsafe { OwnedFd::from_raw_fd(v) };
    if !clo {
        assert_eq!(unsafe { libc::fcntl(v, libc::F_SETFD, 0) }, 0);
    }
    copy
}
fn stdio() -> Vec<(RawFd, RawFd)> {
    vec![(0, 0), (1, 1), (2, 2)]
}
fn probe(mode: &str, extra: Vec<(String, String)>, map: &[(RawFd, RawFd)]) -> OwnedChild {
    let mut env = vec![("GAMJA_TEST_PROBE".into(), mode.into())];
    env.extend(extra);
    OwnedChild::spawn(
        std::env::current_exe().unwrap(),
        &["--exact", "child_probe", "--nocapture", "--test-threads=1"],
        env,
        map,
        LIMIT,
    )
    .unwrap()
}
fn observed(child: &mut OwnedChild) -> std::process::ExitStatus {
    let start = Instant::now();
    loop {
        if let Some(s) = child.try_wait().unwrap() {
            return s;
        }
        assert!(start.elapsed() < LIMIT);
        std::thread::sleep(Duration::from_millis(5));
    }
}
fn reaped(pid: u32) {
    assert_eq!(
        unsafe { libc::waitpid(pid as i32, std::ptr::null_mut(), libc::WNOHANG) },
        -1
    );
    assert_eq!(
        std::io::Error::last_os_error().raw_os_error(),
        Some(libc::ECHILD)
    );
}
#[test]
fn child_probe() {
    let Ok(mode) = std::env::var("GAMJA_TEST_PROBE") else {
        return;
    };
    match mode.as_str() {
        "channels" => {
            for raw in std::env::var("EXCLUDED")
                .unwrap()
                .split(',')
                .filter(|s| !s.is_empty())
            {
                let fd: i32 = raw.parse().unwrap();
                assert_eq!(unsafe { libc::fcntl(fd, libc::F_GETFD) }, -1);
                assert_eq!(
                    std::io::Error::last_os_error().raw_os_error(),
                    Some(libc::EBADF)
                );
            }
            let count: usize = std::env::var("CHANNELS").unwrap().parse().unwrap();
            for i in 0..count {
                let fd: i32 = std::env::var(format!("FD_{i}")).unwrap().parse().unwrap();
                assert_eq!(
                    identity(fd),
                    std::env::var(format!("IDENTITY_{i}")).unwrap()
                );
                let mut s = unsafe { UnixStream::from_raw_fd(fd) };
                s.set_read_timeout(Some(LIMIT)).unwrap();
                let mut b = [0];
                s.read_exact(&mut b).unwrap();
                assert_eq!(b[0], b'A' + i as u8);
                s.write_all(&b).unwrap();
            }
            std::process::exit(0);
        }
        "exit7" => std::process::exit(7),
        "descendant" => {
            // Command owns fork/exec; no Rust runs after a raw fork.
            let mut descendant = Command::new("/bin/sleep")
                .arg("60")
                .env_clear()
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            let mut s = unsafe { UnixStream::from_raw_fd(300) };
            if s.write_all(&descendant.id().to_be_bytes()).is_err() {
                let _ = descendant.kill();
                let _ = descendant.wait();
                std::process::exit(1);
            }
            std::process::exit(0);
        }
        _ => std::process::exit(99),
    }
}
#[test]
fn three_excluded_fds_and_allowed_identity_challenge() {
    let mut p = [-1; 2];
    assert_eq!(unsafe { libc::pipe(p.as_mut_ptr()) }, 0);
    let read = unsafe { OwnedFd::from_raw_fd(p[0]) };
    let write = unsafe { OwnedFd::from_raw_fd(p[1]) };
    for fd in [read.as_raw_fd(), write.as_raw_fd()] {
        assert_eq!(
            unsafe { libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC) },
            0
        );
    }
    let (s, _peer) = UnixStream::pair().unwrap();
    let extra = [
        high(read.as_raw_fd(), 200, false),
        high(write.as_raw_fd(), 200, false),
        high(s.as_raw_fd(), 200, false),
    ];
    let (mut parent, end) = UnixStream::pair().unwrap();
    parent.set_read_timeout(Some(LIMIT)).unwrap();
    let before = flags(end.as_raw_fd());
    let mut map = stdio();
    map.push((end.as_raw_fd(), 300));
    let mut child = probe(
        "channels",
        vec![
            (
                "EXCLUDED".into(),
                extra
                    .iter()
                    .map(|f| f.as_raw_fd().to_string())
                    .collect::<Vec<_>>()
                    .join(","),
            ),
            ("CHANNELS".into(), "1".into()),
            ("FD_0".into(), "300".into()),
            ("IDENTITY_0".into(), identity(end.as_raw_fd())),
        ],
        &map,
    );
    assert_eq!(flags(end.as_raw_fd()), before);
    parent.write_all(b"A").unwrap();
    let mut b = [0];
    parent.read_exact(&mut b).unwrap();
    assert_eq!(b, *b"A");
    assert!(child.wait_timeout(LIMIT).unwrap().success());
    reaped(child.pid());
    assert!(extra.iter().all(|f| flags(f.as_raw_fd()) == 0));
}
#[test]
fn alias_swap_preserves_parent_descriptors() {
    let (mut a, end_a) = UnixStream::pair().unwrap();
    let (mut b, end_b) = UnixStream::pair().unwrap();
    for s in [&a, &b] {
        s.set_read_timeout(Some(LIMIT)).unwrap();
    }
    let ac = high(end_a.as_raw_fd(), 400, true);
    let bc = high(end_b.as_raw_fd(), 400, true);
    let f = File::open("/dev/null").unwrap();
    let marker = high(f.as_raw_fd(), 1000, true);
    let before = identity(marker.as_raw_fd());
    let mut map = stdio();
    map.extend([
        (ac.as_raw_fd(), bc.as_raw_fd()),
        (bc.as_raw_fd(), ac.as_raw_fd()),
    ]);
    let mut child = probe(
        "channels",
        vec![
            ("EXCLUDED".into(), "".into()),
            ("CHANNELS".into(), "2".into()),
            ("FD_0".into(), bc.as_raw_fd().to_string()),
            ("IDENTITY_0".into(), identity(ac.as_raw_fd())),
            ("FD_1".into(), ac.as_raw_fd().to_string()),
            ("IDENTITY_1".into(), identity(bc.as_raw_fd())),
        ],
        &map,
    );
    assert_eq!(
        identity(marker.as_raw_fd()),
        before,
        "unrelated parent FD overwritten"
    );
    a.write_all(b"A").unwrap();
    b.write_all(b"B").unwrap();
    let mut echo = [0];
    a.read_exact(&mut echo).unwrap();
    assert_eq!(echo, *b"A");
    b.read_exact(&mut echo).unwrap();
    assert_eq!(echo, *b"B");
    assert!(child.wait_timeout(LIMIT).unwrap().success());
    assert_ne!(flags(ac.as_raw_fd()) & libc::FD_CLOEXEC, 0);
    assert_ne!(flags(bc.as_raw_fd()) & libc::FD_CLOEXEC, 0);
}
#[test]
fn exit_and_signal_statuses_are_exact_and_reaped() {
    let mut child = probe("exit7", vec![], &stdio());
    assert_eq!(observed(&mut child).code(), Some(7));
    assert_eq!(child.wait_timeout(LIMIT).unwrap().code(), Some(7));
    reaped(child.pid());
    let mut child = OwnedChild::spawn("/bin/sleep", &["60"], vec![], &stdio(), LIMIT).unwrap();
    child.kill().unwrap();
    assert_eq!(observed(&mut child).signal(), Some(libc::SIGKILL));
    assert_eq!(
        child.wait_timeout(LIMIT).unwrap().signal(),
        Some(libc::SIGKILL)
    );
    reaped(child.pid());
}
#[test]
fn timeout_and_drop_are_finite_and_reap() {
    let mut child = OwnedChild::spawn("/bin/sleep", &["60"], vec![], &stdio(), LIMIT).unwrap();
    let start = Instant::now();
    assert_eq!(
        child.wait_timeout(Duration::from_millis(20)).unwrap_err(),
        SpawnError::Timeout
    );
    assert!(start.elapsed() < LIMIT);
    reaped(child.pid());
    let child = OwnedChild::spawn("/bin/sleep", &["60"], vec![], &stdio(), LIMIT).unwrap();
    let pid = child.pid();
    let start = Instant::now();
    drop(child);
    assert!(start.elapsed() < LIMIT);
    reaped(pid);
}
#[test]
fn exited_leader_retains_group_until_descendant_cleanup() {
    let (mut parent, end) = UnixStream::pair().unwrap();
    parent.set_read_timeout(Some(LIMIT)).unwrap();
    let mut map = stdio();
    map.push((end.as_raw_fd(), 300));
    let mut child = probe("descendant", vec![], &map);
    let mut bytes = [0; 4];
    parent.read_exact(&mut bytes).unwrap();
    let descendant = u32::from_be_bytes(bytes);
    assert!(descendant > 0);
    assert_eq!(unsafe { libc::kill(descendant as i32, 0) }, 0);
    assert!(observed(&mut child).success());
    child.kill_group().unwrap();
    assert!(child.wait_timeout(LIMIT).unwrap().success());
    let start = Instant::now();
    loop {
        if unsafe { libc::kill(descendant as i32, 0) } == -1 {
            assert_eq!(
                std::io::Error::last_os_error().raw_os_error(),
                Some(libc::ESRCH)
            );
            break;
        }
        assert!(
            start.elapsed() < LIMIT,
            "descendant still exists after cleanup"
        );
        std::thread::sleep(Duration::from_millis(10));
    }
    reaped(child.pid());
}
#[test]
fn externally_reaped_leader_cannot_signal_group() {
    let mut child = probe("exit7", vec![], &stdio());
    assert_eq!(observed(&mut child).code(), Some(7));
    let mut status = 0;
    assert_eq!(
        unsafe { libc::waitpid(child.pid() as i32, &mut status, 0) },
        child.pid() as i32
    );
    assert_eq!(child.kill_group().unwrap_err(), SpawnError::AlreadyReaped);
    assert_eq!(child.kill().unwrap_err(), SpawnError::AlreadyReaped);
    drop(child);
}
#[test]
fn malformed_inputs_and_failed_spawn_preserve_fds() {
    let envs = [
        vec![("".into(), "value".into())],
        vec![("BAD=KEY".into(), "value".into())],
        vec![("K".into(), "a".into()), ("K".into(), "b".into())],
        vec![("K".into(), "a\0b".into())],
    ];
    for env in envs {
        assert!(OwnedChild::spawn("/bin/sleep", &["0"], env, &stdio(), LIMIT).is_err());
    }
    assert!(OwnedChild::spawn("/bin/sleep", &["a\0b"], vec![], &stdio(), LIMIT).is_err());
    assert!(OwnedChild::spawn("/bin/sleep", &["0"], vec![], &[(0, 5), (1, 5)], LIMIT).is_err());
    assert!(OwnedChild::spawn("/bin/sleep", &["0"], vec![], &[(-1, 5)], LIMIT).is_err());
    let before: Vec<_> = (0..1100)
        .filter(|&fd| unsafe { libc::fcntl(fd, libc::F_GETFD) } >= 0)
        .collect();
    for _ in 0..3 {
        assert!(OwnedChild::spawn(
            "/definitely-missing-gamja-test-executable",
            &[] as &[&str],
            vec![],
            &[(0, 1), (1, 0)],
            LIMIT
        )
        .is_err());
    }
    let after: Vec<_> = (0..1100)
        .filter(|&fd| unsafe { libc::fcntl(fd, libc::F_GETFD) } >= 0)
        .collect();
    assert_eq!(before, after, "staged descriptors leaked on spawn error");
}
