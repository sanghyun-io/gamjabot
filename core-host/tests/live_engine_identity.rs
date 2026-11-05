#![cfg(target_os = "macos")]

use gamja_core_host::credential::{inherit_descriptor_in_child, verify_live_engine};
use std::io::Read;
use std::os::fd::AsRawFd;
use std::os::unix::net::UnixStream;
use std::process::{Command, Stdio};
use std::time::Duration;

fn designated_requirement(path: &str) -> String {
    let output = Command::new("/usr/bin/codesign")
        .args(["-dr", "-", path])
        .output()
        .unwrap();
    assert!(output.status.success());
    String::from_utf8_lossy(&output.stderr)
        .lines()
        .chain(String::from_utf8_lossy(&output.stdout).lines())
        .find(|line| line.trim().starts_with("designated =>"))
        .unwrap()
        .trim()
        .to_owned()
}

#[test]
fn dynamic_code_gate_validates_the_live_unreaped_child() {
    let requirement = designated_requirement("/bin/sleep");
    let mut child = Command::new("/bin/sleep").arg("5").spawn().unwrap();
    let checked = verify_live_engine(child.id(), &requirement);
    assert!(
        checked.is_ok(),
        "live check failed for {requirement}: {checked:?}"
    );
    assert_eq!(
        verify_live_engine(
            child.id(),
            "designated => identifier \"top.gamja.wrong-engine\""
        ),
        Err("credential_engine_untrusted")
    );
    let _ = child.kill();
    let _ = child.wait();

    let mut exited = Command::new("/usr/bin/true").spawn().unwrap();
    std::thread::sleep(Duration::from_millis(50));
    assert_eq!(
        verify_live_engine(exited.id(), &designated_requirement("/usr/bin/true")),
        Err("credential_engine_untrusted")
    );
    let _ = exited.wait();
}

#[test]
fn credential_descriptor_is_inherited_only_by_the_intended_child() {
    let (child_channel, _server) = UnixStream::pair().unwrap();
    let descriptor = child_channel.as_raw_fd();
    let parent_flags_before = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
    assert_ne!(parent_flags_before & libc::FD_CLOEXEC, 0);

    let script = r#"
import os, subprocess, sys
fd = int(sys.argv[1])
inherited = os.get_inheritable(fd)
os.set_inheritable(fd, False)
probe = subprocess.run(
    [sys.executable, '-c', 'import os,sys;\ntry: os.fstat(int(sys.argv[1])); print("open")\nexcept OSError: print("closed")', str(fd)],
    close_fds=False, capture_output=True, text=True, check=True)
print(('inherited' if inherited else 'closed') + ':' + probe.stdout.strip())
"#;
    let mut command = Command::new("/usr/bin/python3");
    command.args(["-c", script, &descriptor.to_string()]);
    command.stdout(Stdio::piped());
    inherit_descriptor_in_child(&mut command, descriptor).unwrap();
    let mut child = command.spawn().unwrap();
    let parent_flags_during = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
    assert_ne!(parent_flags_during & libc::FD_CLOEXEC, 0);
    let mut output = String::new();
    child
        .stdout
        .take()
        .unwrap()
        .read_to_string(&mut output)
        .unwrap();
    assert!(child.wait().unwrap().success());
    assert_eq!(output.trim(), "inherited:closed");
    let parent_flags_after = unsafe { libc::fcntl(descriptor, libc::F_GETFD) };
    assert_ne!(parent_flags_after & libc::FD_CLOEXEC, 0);
}
