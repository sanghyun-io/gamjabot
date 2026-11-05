#![cfg(all(target_os = "macos", target_arch = "aarch64"))]
use gamja_core_host::macos_spawn::{inherited_environment, OwnedChild};
use std::{
    fs::File,
    io::Read,
    os::{fd::AsRawFd, unix::net::UnixStream},
    time::Duration,
};

struct Reaper(u32);
impl Drop for Reaper {
    fn drop(&mut self) {
        let mut status = 0;
        let observed = unsafe { libc::waitpid(self.0 as i32, &mut status, libc::WNOHANG) };
        if observed == 0 {
            unsafe {
                libc::kill(self.0 as i32, libc::SIGKILL);
                libc::waitpid(self.0 as i32, &mut status, 0);
            }
        }
    }
}
#[test]
fn detached_session_transfers_lifetime_and_exits_on_its_owned_lifeline() {
    let (mut parent, child_end) = UnixStream::pair().unwrap();
    parent
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let input = File::open("/dev/null").unwrap();
    let output = File::options().write(true).open("/dev/null").unwrap();
    let error = File::options().write(true).open("/dev/null").unwrap();
    let child=OwnedChild::spawn_detached("/usr/bin/python3", &[
        "-c", "import os,socket,json; s=socket.socket(fileno=3); s.sendall((json.dumps({'pid':os.getpid(),'pgid':os.getpgrp(),'sid':os.getsid(0)})+'\\n').encode()); s.recv(1)"
    ], inherited_environment().unwrap(), &[(input.as_raw_fd(),0),(output.as_raw_fd(),1),(error.as_raw_fd(),2),(child_end.as_raw_fd(),3)], Duration::from_secs(2)).unwrap();
    drop(child_end);
    let pid = child.pid();
    let mut data = Vec::new();
    while !data.ends_with(b"\n") {
        let mut b = [0];
        parent.read_exact(&mut b).unwrap();
        data.extend(b);
        assert!(data.len() < 256);
    }
    let value: serde_json::Value = serde_json::from_slice(&data).unwrap();
    assert_eq!(value, serde_json::json!({"pid":pid,"pgid":pid,"sid":pid}));
    assert_eq!(child.detach_after_authenticated_health(), pid);
    let _reaper = Reaper(pid);
    assert_eq!(unsafe { libc::kill(pid as i32, 0) }, 0);
    drop(parent);
    let deadline = std::time::Instant::now() + Duration::from_secs(2);
    let mut status = 0;
    loop {
        let done = unsafe { libc::waitpid(pid as i32, &mut status, libc::WNOHANG) };
        assert!(done >= 0);
        if done == pid as i32 {
            assert!(libc::WIFEXITED(status));
            assert_eq!(libc::WEXITSTATUS(status), 0);
            break;
        }
        assert!(std::time::Instant::now() < deadline);
        std::thread::sleep(Duration::from_millis(5));
    }
}
