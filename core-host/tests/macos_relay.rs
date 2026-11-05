//! Integration tests for macOS relay process lifecycle and Core adoption.
//! Tests verify the detached relay → Core → adoption → release flow.
#![cfg(all(target_os = "macos", target_arch = "aarch64"))]

use gamja_core_host::macos_spawn::{cloexec_pipe, inherited_environment, OwnedChild};
use gamja_core_host::EndpointFile;
use serde_json::{json, Value};
use std::{
    fs::{self, File},
    io::Read,
    os::fd::AsRawFd,
    path::PathBuf,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const LIMIT: Duration = Duration::from_secs(4);

/// RAII temporary directory that cleans up on drop, even after panic.
struct TempRoot {
    path: PathBuf,
    retain: bool,
}

impl TempRoot {
    fn new(tag: &str) -> Self {
        static NEXT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
        let path = std::env::temp_dir().join(format!(
            "gamja-relay-{}-{}-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos(),
            NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed),
            tag
        ));
        fs::create_dir_all(&path).expect("temp root create");
        Self { path, retain: false }
    }

    fn app_data(&self) -> PathBuf {
        let p = self.path.join("appdata");
        fs::create_dir_all(&p).expect("app_data create");
        p
    }

    fn path(&self) -> &PathBuf {
        &self.path
    }
}

impl Drop for TempRoot {
    fn drop(&mut self) {
        if self.retain { return; }
        if let Err(error) = fs::remove_dir_all(&self.path) {
            if std::thread::panicking() {
                eprintln!("fixture cleanup failed: {error}");
            } else {
                panic!("fixture cleanup failed: {error}");
            }
        }
    }
}

/// RAII wrapper for a spawned relay that ensures cleanup on drop.
struct SpawnedRelay {
    relay: OwnedChild,
    core_pid: u32,
    app_data: PathBuf,
}

impl SpawnedRelay {
    /// Wait for relay to exit with bounded timeout.
    fn wait_relay_exit(&mut self) -> std::process::ExitStatus {
        let start = Instant::now();
        loop {
            if let Some(status) = self.relay.try_wait().expect("try_wait") {
                return status;
            }
            assert!(start.elapsed() < LIMIT, "relay exit timeout");
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    /// Verify Core is still in relay's process group.
    fn verify_core_in_group(&self) {
        let pgid = unsafe { libc::getpgid(self.core_pid as i32) };
        assert!(pgid >= 0, "getpgid failed for Core");
        assert_eq!(
            pgid as u32,
            self.relay.id(),
            "Core not in relay's group"
        );
    }

    /// Verify Core is alive using kill(pid, 0).
    fn verify_core_alive(&self) {
        let rc = unsafe { libc::kill(self.core_pid as i32, 0) };
        assert_eq!(rc, 0, "Core not alive");
    }

    /// Read health from Core's endpoint file.
    fn read_health(&self) -> Value {
        let endpoint_path = self.app_data.join("owner-cli.json");
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            if endpoint_path.exists() {
                if let Ok(content) = fs::read_to_string(&endpoint_path) {
                    if let Ok(endpoint) = EndpointFile::from_json(&content) {
                        if let Ok(health) = control_health(&endpoint) {
                            return health;
                        }
                    }
                }
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        panic!("health endpoint not available within timeout");
    }

    /// Release the relay after authenticated health, reaping only the relay.
    fn release_relay(self) -> Result<std::process::ExitStatus, String> {
        self.relay
            .release_exited_relay_for_core(self.core_pid, Duration::from_secs(1))
            .map_err(|e| format!("release failed: {:?}", e))
    }
}

/// Fixture for relay tests that manages temp directory and cleanup.
struct RelayFixture {
    temp: TempRoot,
    app_data: PathBuf,
    binary: PathBuf,
}

impl RelayFixture {
    fn new(tag: &str) -> Self {
        let temp = TempRoot::new(tag);
        let app_data = temp.app_data();
        let binary = option_env!("CARGO_BIN_EXE_gamja")
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("target/debug/gamja")
            });
        Self {
            temp,
            app_data,
            binary,
        }
    }

    /// Spawn relay with given engine and helper paths.
    fn spawn_relay(&self, engine: &str, helper: Option<&str>) -> SpawnedRelay {
        let (mut reader, writer) = cloexec_pipe().expect("cloexec_pipe");
        let null_in = File::open("/dev/null").expect("null_in");
        let null_out1 = File::options()
            .write(true)
            .open("/dev/null")
            .expect("null_out1");
        let null_out2 = File::options()
            .write(true)
            .open("/dev/null")
            .expect("null_out2");

        let mut args = vec![
            "__relay-start",
            "--app-data",
            self.app_data.to_str().unwrap(),
            "--engine",
            engine,
            "--startup-timeout-ms",
            "2000",
        ];

        let default_helper;
        let helper_str = if let Some(h) = helper {
            h
        } else {
            default_helper = self.temp.path().join("missing-helper");
            default_helper.to_str().unwrap()
        };
        args.push("--helper");
        args.push(helper_str);

        let relay = OwnedChild::spawn_detached(
            &self.binary,
            &args,
            inherited_environment().expect("inherited_environment"),
            &[
                (null_in.as_raw_fd(), 0),
                (null_out1.as_raw_fd(), 1),
                (null_out2.as_raw_fd(), 2),
                (writer.as_raw_fd(), 3),
            ],
            Duration::from_secs(2),
        )
        .expect("relay spawn");

        // Parent closes writer
        drop(writer);

        // Set reader nonblocking
        let flags = unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_GETFL) };
        assert!(flags >= 0);
        unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) };

        // Read exactly 4 bytes (Core PID) followed by EOF
        let mut buf = [0u8; 4];
        let deadline = Instant::now() + Duration::from_secs(2);
        let mut total = 0;
        while total < 4 {
            if Instant::now() >= deadline {
                panic!("handshake timeout");
            }
            match reader.read(&mut buf[total..]) {
                Ok(0) => panic!("handshake EOF before 4 bytes"),
                Ok(n) => total += n,
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(10));
                }
                Err(_) => panic!("handshake read error"),
            }
        }

        // Verify EOF (no extra bytes)
        let mut extra = [0u8; 1];
        loop {
            if Instant::now() >= deadline {
                panic!("EOF timeout");
            }
            match reader.read(&mut extra) {
                Ok(0) => break, // EOF
                Ok(_) => panic!("handshake has extra bytes"),
                Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(10));
                }
                Err(_) => panic!("EOF read error"),
            }
        }

        let core_pid = u32::from_be_bytes(buf);
        assert!(core_pid > 1, "invalid Core PID");
        assert_ne!(core_pid, relay.id(), "Core PID equals relay PID");

        // Reader is dropped here, closing the pipe
        drop(reader);

        SpawnedRelay {
            relay,
            core_pid,
            app_data: self.app_data.clone(),
        }
    }

    /// Stop Core using CLI stop command.
    fn stop_core(&self) -> Result<(), String> {
        let path = self.app_data.join("owner-cli.json");
        if !path.exists() { return Ok(()); }
        let text = fs::read_to_string(&path).map_err(|_| "fixture_endpoint_unreadable")?;
        let endpoint = EndpointFile::from_json(&text).map_err(|_| "fixture_endpoint_invalid")?;
        control_stop(&endpoint)?;
        let deadline = Instant::now() + Duration::from_secs(3);
        while Instant::now() < deadline {
            if !path.exists() { return Ok(()); }
            std::thread::sleep(Duration::from_millis(25));
        }
        Err("fixture_stop_timeout".into())
    }
}

impl Drop for RelayFixture {
    fn drop(&mut self) {
        if let Err(error) = self.stop_core() {
            // Preserve the private capability for recovery if shutdown is uncertain.
            self.temp.retain = true;
            if std::thread::panicking() {
                eprintln!("fixture stop failed: {error}; recovery directory preserved");
            } else {
                panic!("fixture stop failed: {error}; recovery directory preserved");
            }
        }
    }
}

fn control_health(endpoint: &EndpointFile) -> Result<Value, String> {
    control_request(endpoint, json!({"operation": "health"}))
}

fn control_stop(endpoint: &EndpointFile) -> Result<Value, String> {
    control_request(endpoint, json!({"operation": "stop"}))
}

fn control_request(endpoint: &EndpointFile, value: Value) -> Result<Value, String> {
    let request = match value["operation"].as_str() {
        Some("health") => gamja_core_host::ControlRequest::Health {},
        Some("stop") => gamja_core_host::ControlRequest::Stop {},
        _ => return Err("unsupported_fixture_request".into()),
    };
    // Reuse the product's bounded client, including HTTP status validation.
    gamja_core_host::control::request(endpoint, &request).map_err(str::to_owned)
}

/// Test 1: Detached relay writes Core PID, exits cleanly, Core stays in group.
/// After authenticated health, release reaps only relay, Core remains alive.
/// CLI stop cleans up Core.
#[test]
fn detached_relay_writes_pid_and_exits_cleanly() {
    let fixture = RelayFixture::new("test1");
    let mut spawned = fixture.spawn_relay("/bin/false", None);

    // Verify relay exits 0
    let relay_status = spawned.wait_relay_exit();
    assert!(
        relay_status.success(),
        "relay did not exit successfully: {:?}",
        relay_status
    );

    // Verify Core is in relay's group
    spawned.verify_core_in_group();
    spawned.verify_core_alive();

    // Read health (should be diagnostic_only since engine is /bin/false)
    let health = spawned.read_health();
    assert_eq!(health["status"], "diagnostic_only");
    assert_eq!(health["engine"], "unavailable");
    assert_eq!(
        health["core_pid"].as_u64(),
        Some(spawned.core_pid as u64),
        "health core_pid mismatch"
    );

    // Save core_pid before consuming spawned
    let core_pid = spawned.core_pid;

    // Release relay (reaps only relay, Core stays alive)
    let release_status = spawned.release_relay().expect("release relay");
    assert!(
        release_status.success(),
        "relay release status not success"
    );

    // Verify Core is still alive after relay release
    let rc = unsafe { libc::kill(core_pid as i32, 0) };
    assert_eq!(rc, 0, "Core not alive after relay release");

    // Wait a bit for Core to be adopted (PPID=1)
    std::thread::sleep(Duration::from_millis(200));

    // CLI stop should clean up Core
    fixture.stop_core().expect("fixture stop");

    // Verify Core is gone
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        if unsafe { libc::kill(core_pid as i32, 0) } == -1 {
            let err = std::io::Error::last_os_error().raw_os_error();
            if err == Some(libc::ESRCH) {
                return; // Core is gone
            }
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    panic!("Core still alive after CLI stop");
}

/// Test 2: Pending relay shutdown (without release) cleans up Core too.
#[test]
fn pending_relay_shutdown_cleans_up_core() {
    let fixture = RelayFixture::new("test2");
    let mut spawned = fixture.spawn_relay("/bin/false", None);

    // Wait for relay to exit
    let relay_status = spawned.wait_relay_exit();
    assert!(relay_status.success());

    // Verify Core is in relay's group
    spawned.verify_core_in_group();
    let core_pid = spawned.core_pid;

    // Don't release - just shutdown the relay (which should clean up Core too)
    let shutdown_status = spawned
        .relay
        .shutdown(Duration::from_secs(1))
        .expect("relay shutdown");
    assert!(shutdown_status.success());

    // Verify Core is also gone (group cleanup)
    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        if unsafe { libc::kill(core_pid as i32, 0) } == -1 {
            let err = std::io::Error::last_os_error().raw_os_error();
            if err == Some(libc::ESRCH) {
                return; // Core is gone
            }
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    panic!("Core still alive after relay shutdown");
}

/// Test 3: Relay refuses to run if not session leader.
#[test]
fn relay_refuses_non_session_leader() {
    let fixture = RelayFixture::new("test3");
    let app_data = fixture.app_data.clone();
    let binary = fixture.binary.clone();

    // Spawn relay in parent's group (not detached)
    let (mut reader, writer) = cloexec_pipe().expect("cloexec_pipe");
    let null_in = File::open("/dev/null").expect("null_in");
    let null_out1 = File::options()
        .write(true)
        .open("/dev/null")
        .expect("null_out1");
    let null_out2 = File::options()
        .write(true)
        .open("/dev/null")
        .expect("null_out2");

    let helper_path = fixture.temp.path().join("missing-helper");
    let args = vec![
        "__relay-start",
        "--app-data",
        app_data.to_str().unwrap(),
        "--engine",
        "/bin/false",
        "--helper",
        helper_path.to_str().unwrap(),
        "--startup-timeout-ms",
        "2000",
    ];

    // Use spawn (not spawn_detached) so relay is NOT session leader
    let mut relay = OwnedChild::spawn(
        &binary,
        &args,
        inherited_environment().expect("inherited_environment"),
        &[
            (null_in.as_raw_fd(), 0),
            (null_out1.as_raw_fd(), 1),
            (null_out2.as_raw_fd(), 2),
            (writer.as_raw_fd(), 3),
        ],
        Duration::from_secs(2),
    )
    .expect("relay spawn");

    drop(writer);

    // Set reader nonblocking
    let flags = unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_GETFL) };
    assert!(flags >= 0);
    unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_SETFL, flags | libc::O_NONBLOCK) };

    // Try to read - should get nothing or error (relay should exit immediately)
    let mut buf = [0u8; 4];
    let deadline = Instant::now() + Duration::from_millis(500);
    let mut total = 0;
    while total < 4 && Instant::now() < deadline {
        match reader.read(&mut buf[total..]) {
            Ok(0) => break, // EOF
            Ok(n) => total += n,
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(10));
            }
            Err(_) => break,
        }
    }

    // Relay should have exited with error
    let relay_status = relay.wait_timeout(Duration::from_secs(1)).expect("wait");
    assert!(
        !relay_status.success(),
        "relay should fail when not session leader"
    );

    // Should not have written any PID
    assert_eq!(total, 0, "relay should not write PID when not session leader");
}

/// Test 4: Relay rejects invalid FD3 (regular file, readonly pipe, missing).
#[test]
fn relay_rejects_invalid_fd3() {
    let fixture = RelayFixture::new("test4");
    let app_data = fixture.app_data.clone();
    let binary = fixture.binary.clone();

    // Test 4a: FD3 is a regular file (not a pipe)
    let regular_file = File::create(fixture.temp.path().join("fd3-file")).expect("create fd3 file");
    let null_in = File::open("/dev/null").expect("null_in");
    let null_out1 = File::options()
        .write(true)
        .open("/dev/null")
        .expect("null_out1");
    let null_out2 = File::options()
        .write(true)
        .open("/dev/null")
        .expect("null_out2");

    let helper_path = fixture.temp.path().join("missing-helper");
    let args = vec![
        "__relay-start",
        "--app-data",
        app_data.to_str().unwrap(),
        "--engine",
        "/bin/false",
        "--helper",
        helper_path.to_str().unwrap(),
        "--startup-timeout-ms",
        "2000",
    ];

    let (readonly_pipe, _writer) = cloexec_pipe().expect("readonly pipe");
    for (case, fd3) in [
        ("regular", Some(regular_file.as_raw_fd())),
        ("readonly_pipe", Some(readonly_pipe.as_raw_fd())),
        ("missing", None),
    ] {
        let stderr_path = fixture.temp.path().join(format!("{case}.stderr"));
        let stderr = File::create(&stderr_path).expect("capture stderr");
        let mut mappings = vec![
            (null_in.as_raw_fd(), 0),
            (null_out1.as_raw_fd(), 1),
            (stderr.as_raw_fd(), 2),
        ];
        if let Some(fd) = fd3 { mappings.push((fd, 3)); }
        let mut relay = OwnedChild::spawn_detached(
            &binary, &args, inherited_environment().expect("environment"),
            &mappings, Duration::from_secs(2),
        ).expect("relay spawn");
        let status = relay.wait_timeout(Duration::from_secs(2)).expect("rejection cleanup");
        assert!(!status.success(), "invalid FD3 accepted: {case}");
        let error: Value = serde_json::from_slice(&fs::read(&stderr_path).unwrap()).unwrap();
        assert_eq!(error["code"], "relay_fd3_invalid", "wrong rejection: {case}");
        assert!(!app_data.join("owner-cli.json").exists(), "Core created: {case}");
    }

}

/// Test 5: __adopted-serve requires adoption (PPID=1).
#[test]
fn adopted_serve_requires_adoption() {
    let fixture = RelayFixture::new("test5");
    let app_data = fixture.app_data.clone();
    let binary = fixture.binary.clone();

    let null_in = File::open("/dev/null").expect("null_in");
    let null_out1 = File::options()
        .write(true)
        .open("/dev/null")
        .expect("null_out1");
    let null_out2 = File::options()
        .write(true)
        .open("/dev/null")
        .expect("null_out2");

    let helper_path = fixture.temp.path().join("missing-helper");
    let args = vec![
        "__adopted-serve",
        "--app-data",
        app_data.to_str().unwrap(),
        "--engine",
        "/bin/false",
        "--helper",
        helper_path.to_str().unwrap(),
        "--startup-timeout-ms",
        "2000",
    ];

    // Spawn directly (not via relay), so PPID != 1
    let mut core = OwnedChild::spawn(
        &binary,
        &args,
        inherited_environment().expect("inherited_environment"),
        &[
            (null_in.as_raw_fd(), 0),
            (null_out1.as_raw_fd(), 1),
            (null_out2.as_raw_fd(), 2),
        ],
        Duration::from_secs(2),
    )
    .expect("core spawn");

    // Should exit with error (core_adoption_timeout) after waiting 2 seconds
    // Use a manual loop to observe the exit
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut exited = false;
    while Instant::now() < deadline {
        if let Some(status) = core.try_wait().expect("try_wait") {
            assert!(
                !status.success(),
                "__adopted-serve should fail when not adopted"
            );
            exited = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(50));
    }
    assert!(exited, "__adopted-serve did not exit within timeout");
}
