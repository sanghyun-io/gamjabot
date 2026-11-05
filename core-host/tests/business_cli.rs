#![cfg(unix)]
// Public adapter process tests use a synthetic child, never an OS secret store.
use serde_json::Value;
use std::{
    fs,
    io::Write,
    path::PathBuf,
    process::{Command, Stdio},
};
#[cfg(unix)]
use std::{
    io::{BufRead, BufReader, Read},
    os::unix::process::CommandExt,
};

struct Fixture(PathBuf);
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "gamja-business-cli-{}-{}",
            std::process::id(),
            gamja_core_host::capability::random_hex().unwrap()
        ));
        fs::create_dir(&root).unwrap();
        fs::write(
            root.join("child.py"),
            r#"import json,sys
print(json.dumps({'args':sys.argv[1:], 'stdin':sys.stdin.read()}))
sys.exit(23)
"#,
        )
        .unwrap();
        Self(root)
    }
    fn command(&self) -> Command {
        let mut command = Command::new(env!("CARGO_BIN_EXE_gamja"));
        command
            .args(["--app-data"])
            .arg(&self.0)
            .arg("--engine")
            .arg("/usr/bin/python3")
            .arg("--engine-arg")
            .arg(self.0.join("child.py"));
        command
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn business_adapter_preserves_arguments_stdin_exit_and_no_shell() {
    let fixture = Fixture::new();
    let text = "quoted $(touch should-not-exist); `literal`";
    let mut child = fixture
        .command()
        .args(["send", "42", text])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(b"synthetic pipe\n")
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert_eq!(output.status.code(), Some(23));
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        value["args"],
        serde_json::json!([
            "--cli",
            "--capability-file",
            fixture.0.join("owner-cli.json"),
            "send",
            "42",
            text
        ])
    );
    assert_eq!(value["stdin"], "synthetic pipe\n");
    assert!(!fixture.0.join("should-not-exist").exists());
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
#[test]
fn business_child_excludes_three_ambient_fds_and_keeps_foreground_group() {
    use std::os::{fd::{AsRawFd, FromRawFd, OwnedFd}, unix::net::UnixStream};
    let fixture = Fixture::new();
    fs::write(fixture.0.join("child.py"), r#"import os,sys,json
closed=[]
for number in sys.argv[-3:]:
    try: os.fstat(int(number)); closed.append(False)
    except OSError as e: closed.append(e.errno==9)
print(json.dumps({'closed':closed,'pgid':os.getpgrp()}))
"#).unwrap();
    let mut pipe = [-1;2];
    assert_eq!(unsafe { libc::pipe(pipe.as_mut_ptr()) }, 0);
    let reader = unsafe { OwnedFd::from_raw_fd(pipe[0]) };
    let writer = unsafe { OwnedFd::from_raw_fd(pipe[1]) };
    let (socket,_peer)=UnixStream::pair().unwrap();
    let extras: Vec<OwnedFd> = [reader.as_raw_fd(),writer.as_raw_fd(),socket.as_raw_fd()].into_iter().map(|fd| {
        let high=unsafe { libc::fcntl(fd,libc::F_DUPFD_CLOEXEC,200) };
        assert!(high>=200);
        let held=unsafe { OwnedFd::from_raw_fd(high) };
        assert_eq!(unsafe { libc::fcntl(high,libc::F_SETFD,0) },0);
        held
    }).collect();
    let mut command=fixture.command();
    command.process_group(0).arg("me").args(extras.iter().map(|fd|fd.as_raw_fd().to_string())).stdout(Stdio::piped());
    let child=command.spawn().unwrap(); let expected_group=child.id();
    let output=child.wait_with_output().unwrap();assert!(output.status.success());
    let result:Value=serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(result["closed"],serde_json::json!([true,true,true]));
    assert_eq!(result["pgid"],expected_group);
}

#[test]
fn exact_request_run_forwards_arguments_and_owned_stdin() {
    let fixture = Fixture::new();
    let mut child = fixture
        .command()
        .args(["run", "42", "--stdin", "--idempotency-key", "stable", "--timeout", "30"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(b"exact request\n").unwrap();
    let output = child.wait_with_output().unwrap();
    assert_eq!(output.status.code(), Some(23));
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["args"], serde_json::json!([
        "--cli", "--capability-file", fixture.0.join("owner-cli.json"),
        "run", "42", "--stdin", "--idempotency-key", "stable", "--timeout", "30"
    ]));
    assert_eq!(value["stdin"], "exact request\n");
}

#[test]
fn starter_setup_forwards_exact_public_body_and_idempotency_key() {
    let fixture = Fixture::new();
    let body = r#"{"project_title":"First","room_title":"Decision","agents":[]}"#;
    let output = fixture.command().args([
        "setup", "apply", "--json", body, "--idempotency-key", "starter-key"
    ]).stdout(Stdio::piped()).output().unwrap();
    assert_eq!(output.status.code(), Some(23));
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["args"], serde_json::json!([
        "--cli", "--capability-file", fixture.0.join("owner-cli.json"),
        "setup", "apply", "--json", body, "--idempotency-key", "starter-key"
    ]));
}

#[test]
fn request_finalize_stays_in_existing_public_request_family() {
    let fixture = Fixture::new();
    let plan_hash = "a".repeat(64);
    let output = fixture.command().args([
        "request", "finalize", "42", "11111111-1111-4111-8111-111111111111",
        "9", &plan_hash, "--idempotency-key", "final-key"
    ]).stdout(Stdio::piped()).output().unwrap();
    assert_eq!(output.status.code(), Some(23));
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(value["args"][3], "request");
    assert_eq!(value["args"][4], "finalize");
    assert_eq!(value["args"].as_array().unwrap().last().unwrap(), "final-key");
}

#[test]
#[cfg(unix)]
fn exact_request_sigint_detaches_child_and_wrapper_reaps_it() {
    let fixture = Fixture::new();
    fs::write(
        fixture.0.join("detach.py"),
        r#"import json,os,pathlib,sys,time
log = pathlib.Path(__file__).parent / 'detach.log'
log.write_text('POST request\nGET request\n')
print(json.dumps({'request_uuid':'11111111-1111-4111-8111-111111111111'}), flush=True)
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    with log.open('a') as output:
        output.write('DETACH observer\n')
    print(json.dumps({'wait_status':'detached','child_pid':os.getpid()}), flush=True)
"#,
    )
    .unwrap();
    let mut command = Command::new(env!("CARGO_BIN_EXE_gamja"));
    command
        .process_group(0)
        .args(["--app-data"])
        .arg(&fixture.0)
        .arg("--engine")
        .arg("/usr/bin/python3")
        .arg("--engine-arg")
        .arg(fixture.0.join("detach.py"))
        .args(["request", "wait", "42", "11111111-1111-4111-8111-111111111111", "--timeout", "30"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut wrapper = command.spawn().unwrap();
    let mut stdout = BufReader::new(wrapper.stdout.take().unwrap());
    let mut first = String::new();
    stdout.read_line(&mut first).unwrap();
    assert_eq!(serde_json::from_str::<Value>(&first).unwrap()["request_uuid"],
               "11111111-1111-4111-8111-111111111111");

    assert_eq!(unsafe { libc::kill(-(wrapper.id() as i32), libc::SIGINT) }, 0);
    let mut remainder = String::new();
    stdout.read_to_string(&mut remainder).unwrap();
    let status = wrapper.wait().unwrap();
    assert!(status.success(), "wrapper must wait for the detached observer: {status:?}");
    let detached: Value = serde_json::from_str(remainder.trim()).unwrap();
    assert_eq!(detached["wait_status"], "detached");
    let child_pid = detached["child_pid"].as_i64().unwrap() as i32;
    assert_eq!(unsafe { libc::kill(child_pid, 0) }, -1);
    assert_eq!(std::io::Error::last_os_error().raw_os_error(), Some(libc::ESRCH));
    let log = fs::read_to_string(fixture.0.join("detach.log")).unwrap();
    assert_eq!(log, "POST request\nGET request\nDETACH observer\n");
    assert!(!log.contains("STOP"));
}

#[test]
fn interactive_and_batch_aliases_use_same_private_engine() {
    let fixture = Fixture::new();
    for words in [
        vec![],
        vec!["configure"],
        vec!["batch", "operations.ndjson"],
        vec!["--batch", "operations.ndjson"],
    ] {
        let output = fixture
            .command()
            .args(&words)
            .stdin(Stdio::null())
            .output()
            .unwrap();
        assert_eq!(output.status.code(), Some(23));
        let value: Value = serde_json::from_slice(&output.stdout).unwrap();
        let tail = if words.is_empty() || words == ["configure"] {
            vec![]
        } else {
            vec!["--batch", "operations.ndjson"]
        };
        assert_eq!(
            value["args"].as_array().unwrap()[3..],
            serde_json::json!(tail).as_array().unwrap()[..]
        );
    }
}

#[test]
fn remember_device_login_is_forwarded_exactly_to_the_private_engine() {
    let fixture = Fixture::new();
    let output = fixture
        .command()
        .args(["login", "--remember-device"])
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(23));
    let value: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(
        value["args"],
        serde_json::json!([
            "--cli",
            "--capability-file",
            fixture.0.join("owner-cli.json"),
            "login",
            "--remember-device"
        ])
    );
}

#[test]
fn activity_and_existing_events_route_unchanged_to_private_engine() {
    let fixture = Fixture::new();
    for words in [["activity", "42"], ["events", "42"]] {
        let output = fixture.command().args(words).output().unwrap();
        assert_eq!(output.status.code(), Some(23));
        let value: Value = serde_json::from_slice(&output.stdout).unwrap();
        assert_eq!(
            value["args"],
            serde_json::json!([
                "--cli",
                "--capability-file",
                fixture.0.join("owner-cli.json"),
                words[0],
                "42"
            ])
        );
    }
}

#[test]
fn unknown_commands_and_capability_override_do_not_spawn() {
    let fixture = Fixture::new();
    for words in [
        vec!["unknown", "synthetic-secret"],
        vec!["--capability-file", "synthetic-secret"],
        vec!["--batch", "file", "--capability-file", "synthetic-secret"],
        vec!["--batch", "file", "--capability-file=synthetic-secret"],
        vec!["serve", "--capability-file", "synthetic-secret"],
    ] {
        let output = fixture.command().args(words).output().unwrap();
        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert!(!String::from_utf8_lossy(&output.stderr).contains("synthetic-secret"));
    }
}

#[test]
fn help_distinguishes_lifecycle_and_business_without_engine() {
    let output = Command::new(env!("CARGO_BIN_EXE_gamja"))
        .arg("--help")
        .output()
        .unwrap();
    assert!(output.status.success());
    let text = String::from_utf8(output.stdout).unwrap();
    assert!(text.contains("serve | start | status | stop"));
    assert!(text.contains("configure"));
    assert!(text.contains("activity"));
    assert!(text.contains("diagnostic"));
    assert!(text.contains("--batch"));
    assert!(text.contains("login [--remember-device]"));
}
