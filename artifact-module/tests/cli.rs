#![cfg(unix)]

#[path = "../src/main.rs"]
#[allow(dead_code)]
mod binary;

use gamja_artifact::{
    read_frame, ActionId, ArtifactListResponse, ArtifactListState, ArtifactState, ArtifactStore,
    VerifiedArtifact,
};
use serde_json::{json, Value};
use std::{
    ffi::OsString,
    fs::{self, File},
    io::{Cursor, Read, Write},
    net::{TcpListener, TcpStream},
    os::{
        fd::{AsRawFd, OwnedFd},
        unix::{ffi::OsStrExt, fs::PermissionsExt, process::CommandExt},
    },
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::{mpsc, Mutex},
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

const ACTION_A: &str = "6ba7b810-9dad-41d1-80b4-00c04fd430c8";
const ACTION_B: &str = "6ba7b811-9dad-41d1-80b4-00c04fd430c8";
const ACTION_C: &str = "6ba7b812-9dad-41d1-80b4-00c04fd430c8";
const REQUEST_ID: &str = "550e8400-e29b-41d4-a716-446655440000";
const TEST_FIFO_ROOT_ENV: &str = "GAMJA_ARTIFACT_TEST_FIFO_ROOT";

static ENV_LOCK: Mutex<()> = Mutex::new(());

struct TempRoot(PathBuf);

impl TempRoot {
    fn new(label: &str) -> Self {
        let path = std::env::temp_dir().join(format!(
            "gamja-artifact-cli-{label}-{}-{}",
            std::process::id(),
            now_ms()
        ));
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        Self(path)
    }
}

impl Drop for TempRoot {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

struct Server {
    base_url: String,
    requests: mpsc::Receiver<Vec<u8>>,
    thread: Option<thread::JoinHandle<()>>,
}

impl Drop for Server {
    fn drop(&mut self) {
        if let Some(handle) = self.thread.take() {
            handle.join().unwrap();
        }
    }
}

fn server(responses: Vec<(u16, Vec<u8>)>) -> Server {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let address = listener.local_addr().unwrap();
    let (sender, requests) = mpsc::channel();
    let thread = thread::spawn(move || {
        for (status, body) in responses {
            let deadline = Instant::now() + Duration::from_secs(2);
            let mut stream = loop {
                match listener.accept() {
                    Ok((stream, _)) => break stream,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        if Instant::now() >= deadline {
                            return;
                        }
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("fixture accept failed: {error}"),
                }
            };
            let request = read_http_head(&mut stream);
            sender.send(request).unwrap();
            let reason = match status {
                200 => "OK",
                401 => "Unauthorized",
                404 => "Not Found",
                _ => "Error",
            };
            write!(
                stream,
                "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            )
            .unwrap();
            stream.write_all(&body).unwrap();
        }
    });
    Server {
        base_url: format!("http://{address}"),
        requests,
        thread: Some(thread),
    }
}

fn read_http_head(stream: &mut TcpStream) -> Vec<u8> {
    let mut all = Vec::new();
    let mut chunk = [0u8; 1024];
    while !all.windows(4).any(|window| window == b"\r\n\r\n") && all.len() < 16 * 1024 {
        let count = stream.read(&mut chunk).unwrap();
        if count == 0 {
            break;
        }
        all.extend_from_slice(&chunk[..count]);
    }
    all
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis()
        .try_into()
        .unwrap()
}

fn event(action: &str, seq: u64, width: u32, height: u32) -> Value {
    json!({
        "event_id": format!("123e4567-e89b-42d3-a456-{seq:012}"),
        "device_id": null,
        "room_id": 7,
        "seq": seq,
        "actor_type": "tool",
        "actor_id": null,
        "actor_name_snapshot": "Tool",
        "kind": "tool_result",
        "payload": {
            "state": "completed",
            "action_id": action,
            "screenshot_ref": format!("rooms/7/screenshots/{action}.png"),
            "image_mime": "image/png",
            "image_width": width,
            "image_height": height
        },
        "created_at": "2026-10-02T01:02:03+00:00",
        "idempotency_key": format!("tool-result:{action}")
    })
}

fn capability(root: &Path, api_url: &str) -> PathBuf {
    let path = root.join("owner-cli.json");
    fs::write(
        &path,
        serde_json::to_vec(&json!({
            "version": 1,
            "api_url": api_url,
            "control_url": api_url,
            "capability": "ab".repeat(32),
            "core_version": 1,
            "engine_version": 1
        }))
        .unwrap(),
    )
    .unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    path
}

fn restore() -> (u16, Vec<u8>) {
    (
        200,
        br#"{"access_token":"synthetic-owner-jwt","token_type":"bearer"}"#.to_vec(),
    )
}

fn run(root: &Path, arguments: &[&str]) -> (i32, Vec<u8>, Vec<u8>) {
    let mut output = Vec::new();
    let mut error = Vec::new();
    let status = binary::run_with_root_for_test(
        arguments.iter().map(OsString::from).collect(),
        root,
        &mut Cursor::new(Vec::<u8>::new()),
        &mut output,
        &mut error,
    );
    (status, output, error)
}

fn root_fd(path: &Path) -> OwnedFd {
    let file = File::open(path).unwrap();
    file.into()
}

fn verified(action: &str, seq: u64, width: u32, height: u32) -> VerifiedArtifact {
    VerifiedArtifact {
        room_id: 7,
        action_id: ActionId::parse(action).unwrap(),
        event_seq: seq,
        screenshot_ref: format!("rooms/7/screenshots/{action}.png"),
        width,
        height,
        created_at: "2026-10-02T01:02:03+00:00".into(),
    }
}

fn write_artifact(root: &Path, action: &str, bytes: &[u8]) {
    let directory = root.join("rooms/7/screenshots");
    fs::create_dir_all(&directory).unwrap();
    for ancestor in [root.join("rooms"), root.join("rooms/7"), directory.clone()] {
        fs::set_permissions(ancestor, fs::Permissions::from_mode(0o700)).unwrap();
    }
    let path = directory.join(format!("{action}.png"));
    fs::write(&path, bytes).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
}

fn png(width: u32, height: u32) -> Vec<u8> {
    let mut bytes = Vec::new();
    {
        let mut encoder = png::Encoder::new(&mut bytes, width, height);
        encoder.set_color(png::ColorType::Grayscale);
        encoder.set_depth(png::BitDepth::Eight);
        let mut writer = encoder.write_header().unwrap();
        writer
            .write_image_data(&vec![0x7f; (width * height) as usize])
            .unwrap();
    }
    bytes
}

#[test]
fn public_cli_rejects_secret_root_source_and_malformed_command_shapes() {
    for arguments in [
        vec!["--root", "/tmp/secret"],
        vec!["--token", "sensitive-token"],
        vec!["get", "--source", "/tmp/source.png"],
        vec!["list", "--room", "7", "--limit", "0"],
        vec!["list", "--room", "7", "--limit", "501"],
        vec!["delete", "--room", "7", "--room", "8"],
    ] {
        let output = Command::new(env!("CARGO_BIN_EXE_gamja-artifact"))
            .args(&arguments)
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(2), "{arguments:?}");
        assert!(output.stdout.is_empty());
        assert_eq!(output.stderr, b"invalid_request\n");
        assert!(!String::from_utf8_lossy(&output.stderr).contains("sensitive-token"));
        assert!(!String::from_utf8_lossy(&output.stderr).contains("/tmp/secret"));
        assert!(!String::from_utf8_lossy(&output.stderr).contains("/tmp/source.png"));
    }
}

#[test]
fn canonical_app_data_root_ignores_home_cwd_and_environment() {
    let _guard = ENV_LOCK.lock().unwrap();
    let original_home = std::env::var_os("HOME");
    let original_xdg = std::env::var_os("XDG_DATA_HOME");
    let original_dir = std::env::current_dir().unwrap();
    let hostile = TempRoot::new("hostile-env");
    std::env::set_var("HOME", hostile.0.join("home"));
    std::env::set_var("XDG_DATA_HOME", hostile.0.join("xdg"));
    std::env::set_current_dir(&hostile.0).unwrap();

    let actual = binary::canonical_app_data_root_for_test().unwrap();
    let passwd_home = unsafe {
        let record = libc::getpwuid(libc::geteuid());
        assert!(!record.is_null());
        std::ffi::CStr::from_ptr((*record).pw_dir)
            .to_string_lossy()
            .into_owned()
    };
    #[cfg(target_os = "macos")]
    let expected = PathBuf::from(passwd_home).join("Library/Application Support/top.gamja.bot");
    #[cfg(not(target_os = "macos"))]
    let expected = PathBuf::from(passwd_home).join(".local/share/top.gamja.bot");
    assert_eq!(actual, expected);

    std::env::set_current_dir(original_dir).unwrap();
    match original_home {
        Some(value) => std::env::set_var("HOME", value),
        None => std::env::remove_var("HOME"),
    }
    match original_xdg {
        Some(value) => std::env::set_var("XDG_DATA_HOME", value),
        None => std::env::remove_var("XDG_DATA_HOME"),
    }
}

#[test]
fn capability_reader_requires_exact_owner_private_regular_file_and_restore_absence_is_fixed() {
    let root = TempRoot::new("capability");
    let missing = run(&root.0, &["list", "--room", "7"]);
    assert_eq!(
        (missing.0, missing.1, missing.2),
        (1, vec![], b"owner_capability_unavailable\n".to_vec())
    );

    let denied = server(vec![(401, br#"{"detail":"owner data"}"#.to_vec())]);
    let path = capability(&root.0, &denied.base_url);
    let absent = run(&root.0, &["list", "--room", "7"]);
    assert_eq!(
        (absent.0, absent.1, absent.2),
        (1, vec![], b"remembered_login_unavailable\n".to_vec())
    );

    fs::set_permissions(&path, fs::Permissions::from_mode(0o640)).unwrap();
    let public = run(&root.0, &["list", "--room", "7"]);
    assert_eq!(
        (public.0, public.1, public.2),
        (1, vec![], b"owner_capability_unavailable\n".to_vec())
    );

    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    let moved = root.0.join("moved-owner-cli.json");
    fs::rename(&path, &moved).unwrap();
    std::os::unix::fs::symlink(&moved, &path).unwrap();
    let symlink = run(&root.0, &["list", "--room", "7"]);
    assert_eq!(
        (symlink.0, symlink.1, symlink.2),
        (1, vec![], b"owner_capability_unavailable\n".to_vec())
    );
}

#[test]
fn capability_reader_rejects_unknown_duplicate_and_hard_linked_files() {
    let root = TempRoot::new("strict-capability");
    let path = root.0.join("owner-cli.json");
    let cases = [
        br#"{"version":1,"api_url":"http://127.0.0.1:9","control_url":"http://127.0.0.1:9","capability":"abababababababababababababababababababababababababababababababab","core_version":1,"engine_version":1,"extra":true}"#.as_slice(),
        br#"{"version":1,"version":1,"api_url":"http://127.0.0.1:9","control_url":"http://127.0.0.1:9","capability":"abababababababababababababababababababababababababababababababab","core_version":1,"engine_version":1}"#.as_slice(),
    ];
    for bytes in cases {
        fs::write(&path, bytes).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
        let result = run(&root.0, &["list", "--room", "7"]);
        assert_eq!(
            (result.0, result.1, result.2),
            (1, vec![], b"owner_capability_unavailable\n".to_vec())
        );
    }
    let valid = json!({
        "version": 1,
        "api_url": "http://127.0.0.1:9",
        "control_url": "http://127.0.0.1:9",
        "capability": "ab".repeat(32),
        "core_version": 1,
        "engine_version": 1
    });
    fs::write(&path, serde_json::to_vec(&valid).unwrap()).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
    fs::hard_link(&path, root.0.join("second-link.json")).unwrap();
    let result = run(&root.0, &["list", "--room", "7"]);
    assert_eq!(
        (result.0, result.1, result.2),
        (1, vec![], b"owner_capability_unavailable\n".to_vec())
    );
}

#[test]
fn capability_fifo_process_helper() {
    let Some(root) = std::env::var_os(TEST_FIFO_ROOT_ENV) else {
        return;
    };
    let result = run(Path::new(&root), &["list", "--room", "7"]);
    assert_eq!(
        (result.0, result.1, result.2),
        (1, vec![], b"owner_capability_unavailable\n".to_vec())
    );
}

#[test]
fn capability_fifo_is_rejected_without_blocking() {
    let root = TempRoot::new("capability-fifo");
    let path = root.0.join("owner-cli.json");
    let encoded = std::ffi::CString::new(path.as_os_str().as_bytes()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(encoded.as_ptr(), 0o600) }, 0);
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();

    let mut child = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "capability_fifo_process_helper", "--nocapture"])
        .env(TEST_FIFO_ROOT_ENV, &root.0)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = Instant::now() + Duration::from_millis(750);
    loop {
        if child.try_wait().unwrap().is_some() {
            let output = child.wait_with_output().unwrap();
            assert!(
                output.status.success(),
                "{}{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            );
            break;
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            let _ = child.wait();
            panic!("capability FIFO blocked past the bounded test deadline");
        }
        thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn capability_path_swap_is_rejected_before_and_after_open() {
    for (label, point) in [
        ("before-open", binary::CapabilitySwapPoint::BeforeOpen),
        ("after-open", binary::CapabilitySwapPoint::AfterOpen),
    ] {
        let root = TempRoot::new(label);
        let path = capability(&root.0, "http://127.0.0.1:9");
        let replacement = root.0.join("replacement.json");
        fs::copy(&path, &replacement).unwrap();
        fs::set_permissions(&replacement, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(binary::capability_swap_rejected_for_test(
            &root.0,
            &replacement,
            point
        ));
    }
}

#[test]
fn opaque_restore_rejects_noncanonical_response_without_echoing_it() {
    let root = TempRoot::new("restore-shape");
    let secret = "raw-server-secret";
    let api = server(vec![(
        200,
        serde_json::to_vec(&json!({
            "access_token": "synthetic-owner-jwt",
            "token_type": "bearer",
            "detail": secret
        }))
        .unwrap(),
    )]);
    capability(&root.0, &api.base_url);
    let (status, stdout, stderr) = run(&root.0, &["list", "--room", "7"]);
    assert_eq!(status, 1);
    assert!(stdout.is_empty());
    assert_eq!(stderr, b"invalid_engine_response\n");
    assert!(!String::from_utf8_lossy(&stderr).contains(secret));
}

#[test]
fn list_defaults_to_one_hundred_from_sequence_zero() {
    let root = TempRoot::new("list-defaults");
    let api = server(vec![restore(), (200, b"[]".to_vec())]);
    capability(&root.0, &api.base_url);
    let (status, stdout, stderr) = run(&root.0, &["list", "--room", "7"]);
    assert_eq!((status, stdout, stderr), (0, b"[]\n".to_vec(), vec![]));
    let _ = api.requests.recv().unwrap();
    let request = String::from_utf8(api.requests.recv().unwrap()).unwrap();
    assert!(request
        .starts_with("GET /api/v1/conversations/7/events?after_seq=0&limit=100 HTTP/1.1\r\n"));
}

#[test]
fn list_filters_canonical_events_bounds_output_and_reports_metadata_only_states() {
    let root = TempRoot::new("list");
    write_artifact(&root.0, ACTION_A, b"not a png and must not be pixel-decoded");
    write_artifact(&root.0, ACTION_B, &png(1, 1));
    ArtifactStore::from_app_data_fd(root_fd(&root.0), now_ms() + 10_000)
        .unwrap()
        .delete(&verified(ACTION_B, 14, 1, 1))
        .unwrap();
    let mut malformed = event(ACTION_C, 16, 1, 1);
    malformed["payload"]["image_mime"] = json!("image/jpeg");
    let events = json!([
        event(ACTION_A, 13, 1, 1),
        event(ACTION_B, 14, 1, 1),
        event(ACTION_C, 15, 1, 1),
        malformed,
        {"kind": "message", "private": "must-not-leak"}
    ]);
    let api = server(vec![restore(), (200, serde_json::to_vec(&events).unwrap())]);
    capability(&root.0, &api.base_url);

    let (status, stdout, stderr) = run(
        &root.0,
        &["list", "--room", "7", "--after-seq", "12", "--limit", "5"],
    );
    assert_eq!(status, 0);
    assert!(stderr.is_empty());
    let records: Value = serde_json::from_slice(&stdout).unwrap();
    assert_eq!(records.as_array().unwrap().len(), 3);
    assert_eq!(
        records[0],
        json!({
            "event_seq": 13, "action_id": ACTION_A, "created_at": "2026-10-02T01:02:03+00:00",
            "width": 1, "height": 1, "state": "unavailable"
        })
    );
    assert_eq!(records[1]["state"], "removed");
    assert_eq!(records[2]["state"], "unavailable");
    let text = String::from_utf8(stdout).unwrap();
    assert!(!text.contains("screenshot_ref"));
    assert!(!text.contains("private"));
    assert!(!text.contains(&root.0.to_string_lossy().to_string()));
    let restore_request = String::from_utf8(api.requests.recv().unwrap()).unwrap();
    let list_request = String::from_utf8(api.requests.recv().unwrap()).unwrap();
    assert!(restore_request.starts_with("POST /api/v1/auth/restore HTTP/1.1\r\n"));
    assert!(list_request
        .starts_with("GET /api/v1/conversations/7/events?after_seq=12&limit=5 HTTP/1.1\r\n"));
    let restore_headers = restore_request.to_ascii_lowercase();
    assert!(restore_headers.contains("x-gamja-cli-capability: "));
    assert!(!restore_headers.contains("authorization: bearer"));
    let list_headers = list_request.to_ascii_lowercase();
    assert!(list_headers.contains("x-gamja-cli-capability: "));
    assert!(list_headers.contains("authorization: bearer synthetic-owner-jwt"));
}

#[test]
fn list_never_outputs_more_than_the_requested_limit() {
    let root = TempRoot::new("list-limit");
    let events = json!([event(ACTION_A, 13, 1, 1), event(ACTION_B, 14, 1, 1)]);
    let api = server(vec![restore(), (200, serde_json::to_vec(&events).unwrap())]);
    capability(&root.0, &api.base_url);
    let (status, stdout, stderr) = run(&root.0, &["list", "--room", "7", "--limit", "1"]);
    assert_eq!(status, 1);
    assert!(stdout.is_empty());
    assert_eq!(stderr, b"invalid_metadata\n");
}

#[test]
fn get_revalidates_then_creates_exclusive_private_durable_output() {
    let root = TempRoot::new("get");
    let image = png(2, 3);
    write_artifact(&root.0, ACTION_A, &image);
    let response = serde_json::to_vec(&json!([event(ACTION_A, 13, 2, 3)])).unwrap();
    let api = server(vec![restore(), (200, response.clone()), (200, response)]);
    capability(&root.0, &api.base_url);
    let output_dir = TempRoot::new("get-output");
    let output = output_dir.0.join("saved.png");

    let (status, stdout, stderr) = run(
        &root.0,
        &[
            "get",
            "--room",
            "7",
            "--action",
            ACTION_A,
            "--event-seq",
            "13",
            "--output",
            output.to_str().unwrap(),
        ],
    );
    assert_eq!(status, 0);
    assert_eq!(stdout, b"{\"state\":\"ok\"}\n");
    assert!(stderr.is_empty());
    assert_eq!(fs::read(&output).unwrap(), image);
    assert_eq!(
        fs::metadata(&output).unwrap().permissions().mode() & 0o777,
        0o600
    );
    assert_eq!(api.requests.iter().count(), 3);

    let before = fs::read(&output).unwrap();
    let retry_response = serde_json::to_vec(&json!([event(ACTION_A, 13, 2, 3)])).unwrap();
    let retry_api = server(vec![restore(), (200, retry_response)]);
    capability(&root.0, &retry_api.base_url);
    let no_server_needed = run(
        &root.0,
        &[
            "get",
            "--room",
            "7",
            "--action",
            ACTION_A,
            "--event-seq",
            "13",
            "--output",
            output.to_str().unwrap(),
        ],
    );
    assert_eq!(no_server_needed.0, 2);
    assert!(no_server_needed.1.is_empty());
    assert_eq!(no_server_needed.2, b"invalid_output\n");
    assert_eq!(fs::read(&output).unwrap(), before);
}

#[test]
fn unauthorized_get_does_not_probe_the_output_filesystem() {
    let root = TempRoot::new("get-auth-first");
    let denied = server(vec![(401, br#"{"detail":"denied"}"#.to_vec())]);
    capability(&root.0, &denied.base_url);
    let missing_parent = root.0.join("must-not-be-probed/saved.png");
    let (status, stdout, stderr) = run(
        &root.0,
        &[
            "get",
            "--room",
            "7",
            "--action",
            ACTION_A,
            "--event-seq",
            "13",
            "--output",
            missing_parent.to_str().unwrap(),
        ],
    );
    assert_eq!(status, 1);
    assert!(stdout.is_empty());
    assert_eq!(stderr, b"remembered_login_unavailable\n");
    assert!(!missing_parent.parent().unwrap().exists());
    let request = denied
        .requests
        .recv_timeout(Duration::from_secs(1))
        .unwrap();
    assert!(request.starts_with(b"POST /api/v1/auth/restore HTTP/1.1\r\n"));
}

#[test]
fn delete_authenticates_exact_event_and_prints_only_fixed_success() {
    let root = TempRoot::new("delete");
    write_artifact(&root.0, ACTION_A, &png(2, 3));
    let response = serde_json::to_vec(&json!([event(ACTION_A, 13, 2, 3)])).unwrap();
    let api = server(vec![restore(), (200, response)]);
    capability(&root.0, &api.base_url);
    let (status, stdout, stderr) = run(
        &root.0,
        &[
            "delete",
            "--room",
            "7",
            "--action",
            ACTION_A,
            "--event-seq",
            "13",
        ],
    );
    assert_eq!(status, 0);
    assert_eq!(stdout, b"{\"state\":\"ok\"}\n");
    assert!(stderr.is_empty());
    assert!(!root
        .0
        .join(format!("rooms/7/screenshots/{ACTION_A}.png"))
        .exists());
    assert_eq!(
        ArtifactStore::from_app_data_fd(root_fd(&root.0), now_ms() + 10_000)
            .unwrap()
            .inspect(&verified(ACTION_A, 13, 2, 3))
            .unwrap(),
        ArtifactState::Removed
    );
}

#[test]
fn get_rejects_relative_output_and_symlink_parent_without_creating_files() {
    let root = TempRoot::new("unsafe-output");
    let relative = run(
        &root.0,
        &[
            "get",
            "--room",
            "7",
            "--action",
            ACTION_A,
            "--event-seq",
            "13",
            "--output",
            "relative.png",
        ],
    );
    assert_eq!(relative.0, 2);
    assert!(relative.1.is_empty());
    assert_eq!(relative.2, b"invalid_output\n");

    let real_parent = TempRoot::new("real-parent");
    let link_holder = TempRoot::new("link-holder");
    let link = link_holder.0.join("linked-parent");
    std::os::unix::fs::symlink(&real_parent.0, &link).unwrap();
    let linked_output = link.join("saved.png");
    let missing_parent = root.0.join("missing-parent/saved.png");
    write_artifact(&root.0, ACTION_A, &png(2, 3));
    let response = serde_json::to_vec(&json!([event(ACTION_A, 13, 2, 3)])).unwrap();
    let api = server(vec![
        restore(),
        (200, response.clone()),
        restore(),
        (200, response),
    ]);
    capability(&root.0, &api.base_url);
    for output in [&missing_parent, &linked_output] {
        let result = run(
            &root.0,
            &[
                "get",
                "--room",
                "7",
                "--action",
                ACTION_A,
                "--event-seq",
                "13",
                "--output",
                output.to_str().unwrap(),
            ],
        );
        assert_eq!(
            (result.0, result.1, result.2),
            (2, vec![], b"invalid_output\n".to_vec())
        );
    }
    assert!(!missing_parent.parent().unwrap().exists());
    assert!(!real_parent.0.join("saved.png").exists());
}

#[test]
fn private_ipc_requires_pipe_and_fixed_inherited_root_fd_three_and_emits_only_frame() {
    let root = TempRoot::new("ipc");
    let image = png(2, 3);
    write_artifact(&root.0, ACTION_A, &image);
    let api = server(vec![(
        200,
        serde_json::to_vec(&json!([event(ACTION_A, 13, 2, 3)])).unwrap(),
    )]);
    let root_file = File::open(&root.0).unwrap();
    let source_fd = root_file.as_raw_fd();
    let request = serde_json::to_vec(&json!({
        "version": 1,
        "request_id": REQUEST_ID,
        "deadline_ms": now_ms() + 10_000,
        "operation": "get",
        "room_id": 7,
        "action_id": ACTION_A,
        "event_seq": 13,
        "auth": {
            "api_url": api.base_url,
            "session_token": "ab".repeat(32),
            "bearer_token": "synthetic-owner-jwt"
        }
    }))
    .unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_gamja-artifact"));
    command
        .arg("--ipc")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    unsafe {
        command.pre_exec(move || {
            if libc::dup2(source_fd, 3) == -1 {
                return Err(std::io::Error::last_os_error());
            }
            if libc::fcntl(3, libc::F_SETFD, 0) == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = command.spawn().unwrap();
    child.stdin.take().unwrap().write_all(&request).unwrap();
    let result = child.wait_with_output().unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
    assert!(result.stderr.is_empty());
    let frame = read_frame(Cursor::new(result.stdout)).unwrap();
    assert_eq!(frame.header.request_id.as_str(), REQUEST_ID);
    assert_eq!(frame.body, image);
    assert_eq!(
        ArtifactStore::from_app_data_fd(root_fd(&root.0), now_ms() + 10_000)
            .unwrap()
            .inspect(&verified(ACTION_A, 13, 2, 3))
            .unwrap(),
        ArtifactState::Available
    );
}

#[test]
fn private_list_uses_local_session_and_returns_only_bounded_validated_metadata() {
    let root = TempRoot::new("ipc-list");
    write_artifact(&root.0, ACTION_A, &png(2, 3));
    write_artifact(&root.0, ACTION_B, &png(1, 1));
    ArtifactStore::from_app_data_fd(root_fd(&root.0), now_ms() + 10_000)
        .unwrap()
        .delete(&verified(ACTION_B, 14, 1, 1))
        .unwrap();
    write_artifact(&root.0, ACTION_C, b"corrupt-header");
    let api = server(vec![(
        200,
        serde_json::to_vec(&json!([
            event(ACTION_A, 13, 2, 3),
            event(ACTION_B, 14, 1, 1),
            event(ACTION_C, 15, 1, 1)
        ]))
        .unwrap(),
    )]);
    let root_file = File::open(&root.0).unwrap();
    let source_fd = root_file.as_raw_fd();
    let request = serde_json::to_vec(&json!({
        "version": 1,
        "request_id": REQUEST_ID,
        "deadline_ms": now_ms() + 10_000,
        "operation": "list",
        "room_id": 7,
        "after_seq": 12,
        "limit": 3,
        "auth": {
            "api_url": api.base_url,
            "session_token": "ab".repeat(32),
            "bearer_token": "synthetic-owner-jwt"
        }
    }))
    .unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_gamja-artifact"));
    command.arg("--ipc").stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
    unsafe {
        command.pre_exec(move || {
            if libc::dup2(source_fd, 3) == -1 || libc::fcntl(3, libc::F_SETFD, 0) == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = command.spawn().unwrap();
    child.stdin.take().unwrap().write_all(&request).unwrap();
    let result = child.wait_with_output().unwrap();
    assert!(result.status.success(), "{}", String::from_utf8_lossy(&result.stderr));
    assert!(result.stderr.is_empty());
    let frame = read_frame(Cursor::new(result.stdout)).unwrap();
    assert_eq!(frame.header.request_id.as_str(), REQUEST_ID);
    assert_eq!(frame.header.media_type.as_deref(), Some("application/json"));
    let listed = ArtifactListResponse::decode_json(&frame.body, 7, 12, 3).unwrap();
    assert_eq!(listed.scanned_through_seq, 15);
    assert_eq!(listed.next_after_seq, Some(15));
    assert_eq!(
        listed.items.iter().map(|item| item.state).collect::<Vec<_>>(),
        vec![ArtifactListState::Available, ArtifactListState::Removed, ArtifactListState::Unavailable]
    );
    let serialized = String::from_utf8(frame.body).unwrap();
    assert!(!serialized.contains("screenshot_ref"));
    assert!(!serialized.contains(&root.0.to_string_lossy().to_string()));
    let http = String::from_utf8(api.requests.recv().unwrap()).unwrap().to_ascii_lowercase();
    assert!(http.contains("x-gamja-session: "));
    assert!(http.contains("authorization: bearer synthetic-owner-jwt"));
    assert!(!http.contains("x-gamja-cli-capability"));
}

#[test]
fn private_ipc_rejects_regular_file_stdin_before_reading_request() {
    let root = TempRoot::new("ipc-regular-stdin");
    let request_file = root.0.join("request.json");
    fs::write(&request_file, b"{}").unwrap();
    let input = File::open(request_file).unwrap();
    let root_file = File::open(&root.0).unwrap();
    let source_fd = root_file.as_raw_fd();
    let mut command = Command::new(env!("CARGO_BIN_EXE_gamja-artifact"));
    command
        .arg("--ipc")
        .stdin(Stdio::from(input))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    unsafe {
        command.pre_exec(move || {
            if libc::dup2(source_fd, 3) == -1 {
                return Err(std::io::Error::last_os_error());
            }
            if libc::fcntl(3, libc::F_SETFD, 0) == -1 {
                return Err(std::io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let result = command.output().unwrap();
    assert_eq!(result.status.code(), Some(1));
    assert!(result.stdout.is_empty());
    assert_eq!(result.stderr, b"invalid_request\n");
}
