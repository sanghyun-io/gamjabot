use gamja_platform_helper::{CapabilityStatus, PlatformHelper};
use std::path::Path;

#[test]
fn absent_helper_only_removes_native_capability() {
    for path in [None, Some(Path::new("/gamja-fixture/missing-helper"))] {
        let mut helper = PlatformHelper::start(path);
        assert_eq!(
            helper.capability_status(),
            CapabilityStatus::Unavailable("native_capability_unavailable")
        );
        assert!(helper.credentials().is_none());
        helper.stop();
    }
}

#[cfg(unix)]
mod unix {
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    use std::time::{Duration, Instant};
    // Each fixture owns a shell/process group. Serialize them so bounded
    // startup assertions do not compete with another CPU-busy fixture on the
    // resource-limited source-test host; production deadlines are unchanged.
    static FIXTURE_RESOURCES: std::sync::Mutex<()> = std::sync::Mutex::new(());
    struct Fixture {
        path: std::path::PathBuf,
        _resources: std::sync::MutexGuard<'static, ()>,
    }
    impl Fixture {
        fn new(script: &str) -> Self {
            let resources = FIXTURE_RESOURCES
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            let dir = std::env::temp_dir().join(format!(
                "gamja-helper-fixture-{}-{}",
                std::process::id(),
                {
                    let mut nonce = [0; 16];
                    getrandom::fill(&mut nonce).unwrap();
                    gamja_platform_helper::protocol::hex(&nonce)
                }
            ));
            std::fs::create_dir(&dir).unwrap();
            let path = dir.join("helper");
            std::fs::write(&path, format!("#!/bin/sh\n{script}\n")).unwrap();
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
            Self {
                path,
                _resources: resources,
            }
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(self.path.parent().unwrap());
        }
    }
    #[test]
    fn version_mismatch_never_exposes_credentials() {
        let fixture = Fixture::new("read bootstrap\nprintf '%s\\n' '{\"version\":99,\"endpoint\":\"/tmp/secret\",\"ready\":true}'\nread stop");
        let mut helper = PlatformHelper::start(Some(&fixture.path));
        assert_eq!(
            helper.capability_status(),
            CapabilityStatus::Unavailable("native_helper_version_mismatch")
        );
        assert!(helper.credentials().is_none());
    }
    #[test]
    fn startup_and_shutdown_are_bounded_without_readiness() {
        let fixture = Fixture::new("read bootstrap\nread stop");
        let start = Instant::now();
        let mut helper = PlatformHelper::start(Some(&fixture.path));
        assert_eq!(
            helper.capability_status(),
            CapabilityStatus::Unavailable("native_helper_start_failed")
        );
        helper.stop();
        assert!(start.elapsed() < Duration::from_secs(5));
    }
    #[test]
    fn crash_requires_fresh_generation_and_never_replays_effects() {
        // A local fixture handshake cannot access the desktop. Exit on an empty
        // control channel to mimic helper death after readiness.
        let fixture = Fixture::new("read bootstrap\nendpoint=$(printf '%s' \"$bootstrap\" | sed 's/.*\"endpoint\":\"\\([^\"]*\\)\".*/\\1/')\nprintf '{\"version\":1,\"endpoint\":\"%s\",\"ready\":true}\\n' \"$endpoint\"\nexit 0");
        let mut helper = PlatformHelper::start(Some(&fixture.path));
        std::thread::sleep(Duration::from_millis(100));
        assert_eq!(
            helper.capability_status(),
            CapabilityStatus::Unavailable("native_helper_stopped")
        );
        assert!(helper.credentials().is_none());
        helper.restart();
        std::thread::sleep(Duration::from_millis(100));
        assert_eq!(
            helper.capability_status(),
            CapabilityStatus::Unavailable("native_helper_stopped")
        );
    }
    #[test]
    fn shutdown_terminates_a_helper_ignoring_control_eof() {
        let fixture = Fixture::new("read bootstrap\nendpoint=$(printf '%s' \"$bootstrap\" | sed 's/.*\"endpoint\":\"\\([^\"]*\\)\".*/\\1/')\nprintf '{\"version\":1,\"endpoint\":\"%s\",\"ready\":true}\\n' \"$endpoint\"\nwhile :; do :; done");
        let mut helper = PlatformHelper::start(Some(&fixture.path));
        assert_eq!(helper.capability_status(), CapabilityStatus::Available);
        assert!(helper.credentials().is_some());
        let start = Instant::now();
        helper.stop();
        assert!(start.elapsed() < Duration::from_secs(3));
        assert!(helper.credentials().is_none());
    }
    #[test]
    fn explicit_restart_replaces_endpoint_and_authentication_key() {
        let fixture = Fixture::new("read bootstrap\nendpoint=$(printf '%s' \"$bootstrap\" | sed 's/.*\"endpoint\":\"\\([^\"]*\\)\".*/\\1/')\nprintf '{\"version\":1,\"endpoint\":\"%s\",\"ready\":true}\\n' \"$endpoint\"\nread stop");
        let mut helper = PlatformHelper::start(Some(&fixture.path));
        let first = helper.credentials().unwrap();
        helper.restart();
        let second = helper.credentials().unwrap();
        assert_ne!(first.0, second.0);
        assert_ne!(first.1, second.1);
        helper.stop();
        assert!(helper.credentials().is_none());
    }
    fn exited_unreaped(pid: i32) -> std::io::Result<bool> {
        let mut info: libc::siginfo_t = unsafe { std::mem::zeroed() };
        let result = unsafe {
            libc::waitid(
                libc::P_PID,
                pid as libc::id_t,
                &mut info,
                libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
            )
        };
        if result != 0 {
            return Err(std::io::Error::last_os_error());
        }
        Ok(unsafe { info.si_pid() } == pid)
    }
    fn process_running(pid: i32) -> bool {
        let output = std::process::Command::new("ps")
            .args(["-o", "stat=", "-p", &pid.to_string()])
            .output()
            .unwrap();
        let state = String::from_utf8_lossy(&output.stdout);
        output.status.success() && !state.trim().is_empty() && !state.trim().starts_with('Z')
    }
    fn cleanup_preserves_leader_until_group_signal(crash: bool) {
        let fixture = Fixture::new("");
        let root = fixture.path.parent().unwrap();
        let shell_root = format!("'{}'", root.display());
        let script = format!(
            r#"read bootstrap
printf '%s' "$$" > {root}/leader.pid
/bin/sh -c 'trap "" TERM; printf ready > "$1"; while :; do sleep 1; done' fixture {root}/descendant.ready &
printf '%s' "$!" > {root}/descendant.pid
while [ ! -f {root}/descendant.ready ]; do sleep 0.01; done
endpoint=$(printf '%s' "$bootstrap" | sed 's/.*"endpoint":"\([^"]*\)".*/\1/')
printf '{{"version":1,"endpoint":"%s","ready":true}}\n' "$endpoint"
{ending}
"#,
            root = shell_root,
            ending = if crash { "exit 0" } else { "read stop\nexit 0" }
        );
        std::fs::write(&fixture.path, format!("#!/bin/sh\n{script}")).unwrap();
        let mut helper = PlatformHelper::start(Some(&fixture.path));
        let pid: i32 = std::fs::read_to_string(root.join("leader.pid"))
            .unwrap()
            .parse()
            .unwrap();
        let descendant: i32 = std::fs::read_to_string(root.join("descendant.pid"))
            .unwrap()
            .parse()
            .unwrap();
        assert_eq!(unsafe { libc::kill(descendant, libc::SIGTERM) }, 0);
        std::thread::sleep(Duration::from_millis(30));
        assert!(process_running(descendant), "fixture did not resist TERM");
        if crash {
            let deadline = Instant::now() + Duration::from_secs(2);
            while !exited_unreaped(pid).unwrap() {
                assert!(Instant::now() < deadline, "fixture leader did not exit");
                std::thread::sleep(Duration::from_millis(5));
            }
        }
        let (started_tx, started_rx) = std::sync::mpsc::channel();
        let worker = std::thread::spawn(move || {
            started_tx.send(()).unwrap();
            if crash {
                helper.capability_status();
            } else {
                helper.stop();
            }
            helper
        });
        started_rx.recv_timeout(Duration::from_secs(1)).unwrap();
        std::thread::sleep(Duration::from_millis(100));
        let waitable = exited_unreaped(pid);
        // Join before assertions so a regression still cleans its descendants.
        let mut helper = worker.join().unwrap();
        assert!(
            waitable.unwrap_or(false),
            "leader was reaped before final owned-group cleanup"
        );
        assert_eq!(
            helper.capability_status(),
            CapabilityStatus::Unavailable("native_helper_stopped")
        );
        assert_eq!(
            exited_unreaped(pid).unwrap_err().raw_os_error(),
            Some(libc::ECHILD)
        );
        let deadline = Instant::now() + Duration::from_secs(2);
        while process_running(descendant) {
            assert!(
                Instant::now() < deadline,
                "owned resistant descendant survived cleanup"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        helper.stop();
        assert_eq!(
            exited_unreaped(pid).unwrap_err().raw_os_error(),
            Some(libc::ECHILD)
        );
    }
    #[test]
    fn crash_poll_preserves_unreaped_leader_through_resistant_descendant_cleanup() {
        cleanup_preserves_leader_until_group_signal(true);
    }
    #[test]
    fn eof_shutdown_preserves_unreaped_leader_through_resistant_descendant_cleanup() {
        cleanup_preserves_leader_until_group_signal(false);
    }
}
