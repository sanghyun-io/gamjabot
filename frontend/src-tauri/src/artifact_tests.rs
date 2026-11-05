use crate::{
    artifact::{
        build_list_request, build_request, decode_response, running_executable_matches, verify_executable_at,
        ArtifactChildren, ArtifactInvocation, PARENT_TIMEOUT,
    },
    core_client::CoreClient,
    local_endpoint::LocalEndpoint,
};
use gamja_artifact::{write_frame, ActionId, ArtifactError, ArtifactListItem, ArtifactListResponse,
    ArtifactListState, ArtifactOperation, RequestId, ResponseHeader};
use sha2::{Digest, Sha256};
use std::{
    fs::{self, File},
    os::{
        fd::AsRawFd,
        unix::fs::{symlink, MetadataExt, PermissionsExt},
    },
    path::{Path, PathBuf},
    sync::Arc,
    thread,
    time::{Duration, Instant},
};

const REQUEST_ID: &str = "123e4567-e89b-42d3-a456-426614174000";
const ACTION_ID: &str = "1d1e0e6c-9eeb-4571-864b-f2592c44db38";

struct TestDir(PathBuf);

impl TestDir {
    fn new() -> Self {
        let mut random = [0u8; 16];
        getrandom::fill(&mut random).unwrap();
        let path =
            std::env::temp_dir().join(format!("gamja-artifact-gui-{:x}", Sha256::digest(random)));
        fs::create_dir(&path).unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).unwrap();
        Self(fs::canonicalize(path).unwrap())
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TestDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn executable(path: &Path, script: &str) {
    fs::write(path, script).unwrap();
    fs::set_permissions(path, fs::Permissions::from_mode(0o700)).unwrap();
}

fn native_fd3_probe(path: &Path, marker: &Path, fork_marker: &Path) {
    let source = path.with_extension("c");
    fs::write(
        &source,
        format!(
            "#include <fcntl.h>\n#include <sys/stat.h>\n#include <unistd.h>\n\nint main(void) {{\n  struct stat value;\n  if (fstat(3, &value) == 0) {{\n    int fd = open(\"{}\", O_WRONLY | O_CREAT | O_EXCL, 0600);\n    if (fd >= 0) {{ write(fd, \"fd3\", 3); close(fd); }}\n    if (fork() == 0) {{\n      fd = open(\"{}\", O_WRONLY | O_CREAT | O_EXCL, 0600);\n      if (fd >= 0) {{ write(fd, \"fork\", 4); close(fd); }}\n      _exit(0);\n    }}\n  }}\n  return 0;\n}}\n",
            marker.display(),
            fork_marker.display()
        ),
    )
    .unwrap();
    let status = std::process::Command::new("/usr/bin/clang")
        .args(["-Os", "-Wall", "-Wextra", "-Werror"])
        .arg(&source)
        .arg("-o")
        .arg(path)
        .status()
        .unwrap();
    assert!(status.success());
}

fn invocation(operation: ArtifactOperation) -> ArtifactInvocation {
    let request_id = RequestId::parse(REQUEST_ID).unwrap();
    let bytes = serde_json::to_vec(&serde_json::json!({
        "version": 1,
        "request_id": REQUEST_ID,
        "deadline_ms": 4_000_000_000_000u64,
        "operation": operation,
        "room_id": 7,
        "action_id": ACTION_ID,
        "event_seq": 13,
        "auth": {
            "api_url": "http://127.0.0.1:4105",
            "session_token": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "bearer_token": "private-owner-token"
        }
    }))
    .unwrap();
    ArtifactInvocation {
        request_id,
        operation,
        bytes,
    }
}

fn frame(path: &Path, header: ResponseHeader, body: &[u8]) {
    let mut bytes = Vec::new();
    write_frame(&mut bytes, &header, body).unwrap();
    fs::write(path, bytes).unwrap();
}

#[test]
fn canonical_signed_hash_pinned_executable_is_the_only_accepted_path() {
    let root = TestDir::new();
    let expected = root.path().join("gamja-artifact");
    executable(&expected, "#!/bin/sh\nexit 0\n");
    let digest = format!("{:x}", Sha256::digest(fs::read(&expected).unwrap()));
    let mut verified = Vec::new();
    let resolved = verify_executable_at(&expected, &expected, &digest, &mut |path| {
        verified.push(path.to_path_buf());
        true
    })
    .unwrap();
    assert_eq!(resolved.path(), expected);
    assert_eq!(verified, [expected.clone()]);

    let wrong = root.path().join("elsewhere");
    executable(&wrong, "#!/bin/sh\nexit 0\n");
    assert!(matches!(
        verify_executable_at(&wrong, &expected, &digest, &mut |_| true),
        Err("artifact_unavailable")
    ));
    let link = root.path().join("linked-artifact");
    symlink(&expected, &link).unwrap();
    assert!(matches!(
        verify_executable_at(&link, &link, &digest, &mut |_| true),
        Err("artifact_unavailable")
    ));
    assert!(matches!(
        verify_executable_at(&expected, &expected, &"0".repeat(64), &mut |_| true),
        Err("artifact_unavailable")
    ));
    assert!(matches!(
        verify_executable_at(&expected, &expected, &digest, &mut |_| false),
        Err("artifact_unavailable")
    ));
}

#[test]
fn replacement_after_verification_is_rejected_before_secret_dispatch() {
    let root = TestDir::new();
    let expected = root.path().join("gamja-artifact");
    let verified_bytes = "#!/bin/sh\nexit 0\n";
    executable(&expected, verified_bytes);
    let digest = format!("{:x}", Sha256::digest(verified_bytes.as_bytes()));
    let verified = verify_executable_at(&expected, &expected, &digest, &mut |_| true).unwrap();

    fs::rename(&expected, root.path().join("verified-original")).unwrap();
    let received = root.path().join("replacement-stdin");
    executable(
        &expected,
        &format!("#!/bin/sh\n/bin/cat > '{}'\n", received.display()),
    );

    let owner = ArtifactChildren::default();
    assert_eq!(
        owner.run_verified(
            &verified,
            File::open(root.path()).unwrap(),
            invocation(ArtifactOperation::Get),
            Duration::from_secs(1),
        ),
        Err("artifact_unavailable")
    );
    assert!(!fs::read_to_string(received)
        .unwrap_or_default()
        .contains("private-owner-token"));
    assert_eq!(owner.active_count(), 0);
}

#[test]
fn native_replacement_never_executes_with_the_fd_three_capability() {
    let root = TestDir::new();
    let expected = root.path().join("gamja-artifact");
    let verified_bytes = "#!/bin/sh\nexit 0\n";
    executable(&expected, verified_bytes);
    let digest = format!("{:x}", Sha256::digest(verified_bytes.as_bytes()));
    let verified = verify_executable_at(&expected, &expected, &digest, &mut |_| true).unwrap();

    fs::rename(&expected, root.path().join("verified-original")).unwrap();
    let marker = root.path().join("fd3-used");
    let fork_marker = root.path().join("fork-escaped");
    native_fd3_probe(&expected, &marker, &fork_marker);

    let owner = ArtifactChildren::default();
    assert_eq!(
        owner.run_verified(
            &verified,
            File::open(root.path()).unwrap(),
            invocation(ArtifactOperation::Get),
            Duration::from_secs(1),
        ),
        Err("artifact_unavailable")
    );
    assert!(!marker.exists());
    assert!(!fork_marker.exists());
    assert_eq!(owner.active_count(), 0);
}

#[test]
fn running_process_identity_matches_the_hash_verified_vnode() {
    let expected = fs::canonicalize("/bin/cat").unwrap();
    let digest = format!("{:x}", Sha256::digest(fs::read(&expected).unwrap()));
    let verified = verify_executable_at(&expected, &expected, &digest, &mut |_| true).unwrap();
    let mut child = std::process::Command::new(&expected)
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::null())
        .spawn()
        .unwrap();
    assert!(running_executable_matches(child.id(), &verified));
    drop(child.stdin.take());
    assert!(child.wait().unwrap().success());
}

#[test]
fn in_place_change_is_rejected_before_secret_dispatch() {
    let root = TestDir::new();
    let expected = root.path().join("gamja-artifact");
    let verified_bytes = "#!/bin/sh\nexit 0\n";
    executable(&expected, verified_bytes);
    let digest = format!("{:x}", Sha256::digest(verified_bytes.as_bytes()));
    let original = fs::metadata(&expected).unwrap();
    let verified = verify_executable_at(&expected, &expected, &digest, &mut |_| true).unwrap();

    let received = root.path().join("replacement-stdin");
    fs::write(
        &expected,
        format!("#!/bin/sh\n/bin/cat > '{}'\n", received.display()),
    )
    .unwrap();
    fs::set_permissions(&expected, fs::Permissions::from_mode(0o700)).unwrap();
    let changed = fs::metadata(&expected).unwrap();
    assert_eq!(original.dev(), changed.dev());
    assert_eq!(original.ino(), changed.ino());
    assert_eq!(verified.revalidate_opened(), Err("artifact_unavailable"));

    let owner = ArtifactChildren::default();
    assert_eq!(
        owner.run_test_verified(
            &verified,
            File::open(root.path()).unwrap(),
            invocation(ArtifactOperation::Get),
            Duration::from_secs(1),
        ),
        Err("artifact_unavailable")
    );
    assert!(!fs::read_to_string(received)
        .unwrap_or_default()
        .contains("private-owner-token"));
    assert_eq!(owner.active_count(), 0);
}

#[test]
fn core_client_opens_only_its_private_app_data_directory_with_cloexec() {
    let root = TestDir::new();
    let client = CoreClient::new(root.path().to_path_buf(), root.path().join("unused-core"));
    let directory = client.open_app_data_dir().unwrap();
    let flags = unsafe { libc::fcntl(directory.as_raw_fd(), libc::F_GETFD) };
    assert_ne!(flags & libc::FD_CLOEXEC, 0);
    assert!(directory.metadata().unwrap().is_dir());
}

#[test]
fn request_uses_epoch_deadline_exact_session_and_no_root_override() {
    let session = LocalEndpoint {
        base_url: "http://127.0.0.1:4105".into(),
        session_token: "a".repeat(64),
    };
    let built = build_request(
        ArtifactOperation::Get,
        7,
        ACTION_ID,
        13,
        "private-owner-token",
        &session,
        1_000_000,
    )
    .unwrap();
    let value: serde_json::Value = serde_json::from_slice(&built.bytes).unwrap();
    assert_eq!(value["deadline_ms"], 1_030_000);
    assert_eq!(value["operation"], "get");
    assert_eq!(value["room_id"], 7);
    assert_eq!(value["event_seq"], 13);
    assert_eq!(value["auth"]["api_url"], session.base_url);
    assert_eq!(value["auth"]["session_token"], session.session_token);
    assert_eq!(value["auth"]["bearer_token"], "private-owner-token");
    assert!(value.get("app_data").is_none());
    assert!(built.bytes.len() <= gamja_artifact::MAX_REQUEST_BYTES);
    assert_eq!(PARENT_TIMEOUT, Duration::from_secs(30));
}

#[test]
fn list_request_uses_only_room_cursor_limit_and_current_private_auth() {
    let session = LocalEndpoint {
        base_url: "http://127.0.0.1:4105".into(),
        session_token: "a".repeat(64),
    };
    let built = build_list_request(7, 41, 50, "private-owner-token", &session, 1_000_000).unwrap();
    let value: serde_json::Value = serde_json::from_slice(&built.bytes).unwrap();
    assert_eq!(value["operation"], "list");
    assert_eq!(value["room_id"], 7);
    assert_eq!(value["after_seq"], 41);
    assert_eq!(value["limit"], 50);
    assert!(value.get("action_id").is_none());
    assert!(value.get("event_seq").is_none());
    assert_eq!(value["auth"]["session_token"], session.session_token);
    assert_eq!(value["auth"]["bearer_token"], "private-owner-token");
}

#[test]
fn list_response_frame_and_strict_metadata_are_validated_before_webview_return() {
    let request = RequestId::parse(REQUEST_ID).unwrap();
    let list = ArtifactListResponse { version: 1, room_id: 7, after_seq: 0,
        scanned_through_seq: 13, next_after_seq: None, items: vec![ArtifactListItem {
            room_id: 7, event_seq: 13, action_id: ActionId::parse(ACTION_ID).unwrap(),
            created_at: "2026-10-05T01:00:00Z".into(), width: 2, height: 2,
            state: ArtifactListState::Available,
        }] };
    let body = list.encode_json().unwrap();
    let mut frame = Vec::new();
    write_frame(&mut frame, &ResponseHeader::json(request.clone(), &body).unwrap(), &body).unwrap();
    let decoded = decode_response(&frame, true, &request, ArtifactOperation::List).unwrap().unwrap();
    assert_eq!(ArtifactListResponse::decode_json(&decoded, 7, 0, 50).unwrap(), list);
    assert_eq!(ArtifactListResponse::decode_json(&decoded, 8, 0, 50), Err(ArtifactError::InvalidMetadata));
}

#[test]
fn response_validation_checks_exit_request_operation_frame_and_digest() {
    let request = RequestId::parse(REQUEST_ID).unwrap();
    let png = b"synthetic-png";
    let mut valid = Vec::new();
    write_frame(
        &mut valid,
        &ResponseHeader::png(request.clone(), 1, 1, png).unwrap(),
        png,
    )
    .unwrap();
    assert_eq!(
        decode_response(&valid, true, &request, ArtifactOperation::Get).unwrap(),
        Some(png.to_vec())
    );
    assert_eq!(
        decode_response(&valid, false, &request, ArtifactOperation::Get),
        Err("artifact_unavailable")
    );
    assert_eq!(
        decode_response(&valid, true, &request, ArtifactOperation::Delete),
        Err("artifact_unavailable")
    );

    let other = RequestId::parse("223e4567-e89b-42d3-a456-426614174000").unwrap();
    let mut wrong_request = Vec::new();
    write_frame(
        &mut wrong_request,
        &ResponseHeader::png(other, 1, 1, png).unwrap(),
        png,
    )
    .unwrap();
    assert_eq!(
        decode_response(&wrong_request, true, &request, ArtifactOperation::Get),
        Err("artifact_unavailable")
    );
    let last = valid.len() - 1;
    valid[last] ^= 1;
    assert_eq!(
        decode_response(&valid, true, &request, ArtifactOperation::Get),
        Err("artifact_unavailable")
    );

    let mut removed = Vec::new();
    write_frame(
        &mut removed,
        &ResponseHeader::error(request.clone(), ArtifactError::ArtifactRemoved),
        &[],
    )
    .unwrap();
    assert_eq!(
        decode_response(&removed, true, &request, ArtifactOperation::Get),
        Err("artifact_removed")
    );
}

#[test]
fn one_shot_uses_only_ipc_private_stdio_and_inherited_fd_three() {
    let root = TestDir::new();
    let request = invocation(ArtifactOperation::Get);
    let body = b"private-png";
    let response = root.path().join("response.bin");
    frame(
        &response,
        ResponseHeader::png(request.request_id.clone(), 1, 1, body).unwrap(),
        body,
    );
    let args = root.path().join("args");
    let environment = root.path().join("environment");
    let received = root.path().join("request");
    let child = root.path().join("gamja-artifact");
    executable(&child, &format!(
        "#!/bin/sh\nprintf '%s\\n' \"$#:$1\" > '{}'\nenv > '{}'\n[ -d /dev/fd/3 ] || exit 9\n/bin/cat > '{}'\n/bin/cat '{}'\n",
        args.display(), environment.display(), received.display(), response.display()
    ));
    let app_data = File::open(root.path()).unwrap();
    let owner = ArtifactChildren::default();
    let output = owner
        .run_test_child(&child, app_data, request, Duration::from_secs(2))
        .unwrap();
    assert_eq!(output, Some(body.to_vec()));
    assert_eq!(fs::read_to_string(args).unwrap().trim(), "1:--ipc");
    assert!(!fs::read_to_string(environment)
        .unwrap()
        .contains("private-owner-token"));
    assert!(fs::read_to_string(received)
        .unwrap()
        .contains("private-owner-token"));
    assert_eq!(owner.active_count(), 0);
}

#[test]
fn monotonic_timeout_kills_and_reaps_the_owned_child() {
    let root = TestDir::new();
    let pid_file = root.path().join("pid");
    let child = root.path().join("gamja-artifact");
    executable(
        &child,
        &format!(
            "#!/bin/sh\nprintf '%s' \"$$\" > '{}'\n/bin/cat >/dev/null\n/bin/sleep 30\n",
            pid_file.display()
        ),
    );
    let owner = Arc::new(ArtifactChildren::default());
    let started = Instant::now();
    let worker = {
        let owner = owner.clone();
        let child = child.clone();
        let app_data = File::open(root.path()).unwrap();
        thread::spawn(move || {
            owner.run_test_child(
                &child,
                app_data,
                invocation(ArtifactOperation::Get),
                Duration::from_secs(2),
            )
        })
    };
    for _ in 0..100 {
        if pid_file.exists() && owner.active_count() == 1 {
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    assert!(pid_file.exists());
    assert_eq!(owner.active_count(), 1);
    assert_eq!(worker.join().unwrap(), Err("deadline_expired"));
    assert!(started.elapsed() < Duration::from_secs(3));
    let pid: libc::pid_t = fs::read_to_string(pid_file).unwrap().parse().unwrap();
    assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
    assert_eq!(owner.active_count(), 0);
}

#[test]
fn gui_shutdown_kills_and_reaps_artifact_children_without_core_shutdown() {
    let root = TestDir::new();
    let pid_file = root.path().join("pid");
    let child = root.path().join("gamja-artifact");
    executable(
        &child,
        &format!(
            "#!/bin/sh\nprintf '%s' \"$$\" > '{}'\n/bin/cat >/dev/null\n/bin/sleep 30\n",
            pid_file.display()
        ),
    );
    let owner = Arc::new(ArtifactChildren::default());
    let worker = {
        let owner = owner.clone();
        let child = child.clone();
        let app_data = File::open(root.path()).unwrap();
        thread::spawn(move || {
            owner.run_test_child(
                &child,
                app_data,
                invocation(ArtifactOperation::Delete),
                Duration::from_secs(10),
            )
        })
    };
    for _ in 0..100 {
        if pid_file.exists() && owner.active_count() == 1 {
            break;
        }
        thread::sleep(Duration::from_millis(10));
    }
    assert_eq!(owner.active_count(), 1);
    owner.shutdown();
    assert_eq!(worker.join().unwrap(), Err("artifact_unavailable"));
    let pid: libc::pid_t = fs::read_to_string(pid_file).unwrap().parse().unwrap();
    assert_eq!(unsafe { libc::kill(pid, 0) }, -1);
    assert_eq!(owner.active_count(), 0);
}

#[test]
fn child_has_no_webview_environment_root_or_token_override() {
    let source =
        fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("src/artifact.rs")).unwrap();
    for forbidden in [
        "std::env::var(",
        "std::env::var_os(",
        "HOME",
        "GAMJA_APP_DATA",
        "--app-data",
        "--root",
    ] {
        assert!(
            !source.contains(forbidden),
            "found forbidden bridge input {forbidden}"
        );
    }
}
