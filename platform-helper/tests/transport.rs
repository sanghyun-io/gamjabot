//! Local private IPC only: never invoke a capture/input/permission operation.
#[cfg(unix)]
mod unix {
    use gamja_platform_helper::{CapabilityStatus, PlatformHelper};
    use std::io::{Read, Write};
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    use std::os::unix::net::UnixStream;
    use std::path::Path;
    use std::time::{Duration, Instant};

    #[test]
    fn authenticated_readiness_and_denied_frames_use_private_socket() {
        let mut helper =
            PlatformHelper::start(Some(Path::new(env!("CARGO_BIN_EXE_gamja-platform-helper"))));
        assert_eq!(helper.capability_status(), CapabilityStatus::Available);
        let (endpoint, key) = helper.credentials().unwrap();
        assert_eq!(key.len(), 64);
        let metadata = std::fs::symlink_metadata(&endpoint).unwrap();
        assert_eq!(metadata.permissions().mode() & 0o777, 0o600);
        assert_eq!(metadata.uid(), unsafe { libc::geteuid() });
        let directory = Path::new(&endpoint).parent().unwrap().to_path_buf();
        assert_eq!(
            std::fs::metadata(&directory).unwrap().permissions().mode() & 0o777,
            0o700
        );
        let mut stream = UnixStream::connect(&endpoint).unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let denied = br#"{"body":"e30=","mac":"invalid"}"#;
        stream
            .write_all(&(denied.len() as u32).to_be_bytes())
            .unwrap();
        stream.write_all(denied).unwrap();
        let mut header = [0; 4];
        stream.read_exact(&mut header).unwrap();
        let mut response = vec![0; u32::from_be_bytes(header) as usize];
        stream.read_exact(&mut response).unwrap();
        let response: serde_json::Value = serde_json::from_slice(&response).unwrap();
        assert_eq!(response["state"], "failed");
        assert_eq!(response["code"], "native_request_denied");
        helper.stop();
        assert!(!directory.exists());
    }
    #[test]
    fn core_pipe_eof_stops_incomplete_frame_and_removes_socket() {
        let mut helper =
            PlatformHelper::start(Some(Path::new(env!("CARGO_BIN_EXE_gamja-platform-helper"))));
        let (endpoint, _) = helper.credentials().unwrap();
        let mut stream = UnixStream::connect(&endpoint).unwrap();
        stream.write_all(&[0, 0]).unwrap();
        std::thread::sleep(Duration::from_millis(75));
        let start = Instant::now();
        helper.stop();
        assert!(start.elapsed() < Duration::from_secs(2));
        assert!(!Path::new(&endpoint).exists());
    }
}

#[cfg(unix)]
#[test]
fn inherited_owner_pipe_eof_exits_helper_without_supervisor_force_kill() {
    use gamja_platform_helper::protocol::{Bootstrap, Ready};
    use std::io::{BufRead, BufReader, Write};
    use std::os::unix::fs::PermissionsExt;
    use std::process::{Child, Command, Stdio};
    use std::time::{Duration, Instant};
    struct Fixture {
        child: Child,
        root: std::path::PathBuf,
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = self.child.kill();
            let _ = self.child.wait();
            let _ = std::fs::remove_file(self.root.join("desktop.sock"));
            let _ = std::fs::remove_dir(&self.root);
        }
    }
    let mut nonce = [0; 16];
    getrandom::fill(&mut nonce).unwrap();
    let root = std::path::PathBuf::from("/tmp").join(format!(
        "gamja-helper-lifeline-{}",
        gamja_platform_helper::protocol::hex(&nonce)
    ));
    std::fs::create_dir(&root).unwrap();
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o700)).unwrap();
    let child = Command::new(env!("CARGO_BIN_EXE_gamja-platform-helper"))
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .unwrap();
    let mut fixture = Fixture { child, root };
    let mut input = fixture.child.stdin.take().unwrap();
    let endpoint = fixture.root.join("desktop.sock");
    let bootstrap = Bootstrap {
        version: 1,
        key: "00".repeat(32),
        endpoint: endpoint.to_string_lossy().into_owned(),
    };
    serde_json::to_writer(&mut input, &bootstrap).unwrap();
    input.write_all(b"\n").unwrap();
    input.flush().unwrap();
    let mut ready = String::new();
    BufReader::new(fixture.child.stdout.take().unwrap())
        .read_line(&mut ready)
        .unwrap();
    assert!(serde_json::from_str::<Ready>(&ready).unwrap().ready);
    drop(input);
    let deadline = Instant::now() + Duration::from_secs(2);
    while fixture.child.try_wait().unwrap().is_none() {
        assert!(Instant::now() < deadline, "helper ignored owner pipe EOF");
        std::thread::sleep(Duration::from_millis(10));
    }
    assert!(!endpoint.exists());
}
