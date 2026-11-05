#![cfg(all(target_os = "macos", target_arch = "aarch64"))]
use gamja_core_host::broker_runtime::{ControlChannel, SourcePin};
use serde_json::{json, Value};
use std::{
    fs,
    io::{Read, Write},
    os::unix::{fs::PermissionsExt, net::UnixStream},
    path::PathBuf,
    thread,
    time::{Duration, Instant},
};

fn recv(s: &mut UnixStream) -> Value {
    let mut n = [0; 4];
    s.read_exact(&mut n).unwrap();
    let mut b = vec![0; u32::from_be_bytes(n) as usize];
    s.read_exact(&mut b).unwrap();
    serde_json::from_slice(&b).unwrap()
}
fn send(s: &mut UnixStream, v: Value) {
    let b = serde_json::to_vec(&v).unwrap();
    s.write_all(&(b.len() as u32).to_be_bytes()).unwrap();
    s.write_all(&b).unwrap();
}
#[test]
fn exact_control_sequence_and_no_private_data() {
    let (a, mut b) = UnixStream::pair().unwrap();
    let peer = thread::spawn(move || {
        assert_eq!(
            recv(&mut b),
            json!({"version":1,"operation":"bootstrap","authority_path":"/authority","authority_sha256":"a".repeat(64)})
        );
        send(&mut b, json!({"status":"bootstrapped"}));
        assert_eq!(
            recv(&mut b),
            json!({"version":1,"operation":"bind","parent_path":"/core","parent_pid":42,"engine_path":"/engine","engine_pid":43})
        );
        send(&mut b, json!({"status":"bound"}));
        assert_eq!(recv(&mut b), json!({"version":1,"operation":"shutdown"}));
        send(&mut b, json!({"status":"shutdown"}));
    });
    let mut c = ControlChannel::new(a, Duration::from_secs(1)).unwrap();
    c.bootstrap("/authority".as_ref(), &"a".repeat(64)).unwrap();
    c.bind("/core".as_ref(), 42, "/engine".as_ref(), 43)
        .unwrap();
    assert!(c.is_bound());
    c.shutdown().unwrap();
    assert!(!c.is_bound());
    peer.join().unwrap();
}
#[test]
fn malformed_response_is_terminal_and_writes_no_retry() {
    for response in [
        json!({"status":"bound"}),
        json!({"status":null}),
        json!({"status":"bootstrapped","extra":true}),
        json!({"status":true}),
    ] {
        let (a, mut b) = UnixStream::pair().unwrap();
        let peer = thread::spawn(move || {
            recv(&mut b);
            send(&mut b, response);
            let mut byte = [0];
            assert_eq!(b.read(&mut byte).unwrap(), 0);
        });
        let mut c = ControlChannel::new(a, Duration::from_secs(1)).unwrap();
        assert!(c.bootstrap("/authority".as_ref(), &"a".repeat(64)).is_err());
        assert!(c.bootstrap("/authority".as_ref(), &"a".repeat(64)).is_err());
        peer.join().unwrap();
    }
}
#[test]
fn bounded_eof_truncation_and_slow_partial_frames() {
    for payload in [
        vec![],
        vec![0, 0],
        vec![0, 0, 0, 0],
        vec![0, 1, 0, 1],
        vec![0, 0, 0, 9, b'{'],
    ] {
        let (a, mut b) = UnixStream::pair().unwrap();
        let peer = thread::spawn(move || {
            recv(&mut b);
            b.write_all(&payload).unwrap();
        });
        let mut c = ControlChannel::new(a, Duration::from_millis(100)).unwrap();
        let t = Instant::now();
        assert!(c.bootstrap("/authority".as_ref(), &"a".repeat(64)).is_err());
        assert!(t.elapsed() < Duration::from_secs(1));
        peer.join().unwrap();
    }
    let (a, mut b) = UnixStream::pair().unwrap();
    let peer = thread::spawn(move || {
        recv(&mut b);
        b.write_all(&[0]).unwrap();
        thread::sleep(Duration::from_millis(160));
    });
    let mut c = ControlChannel::new(a, Duration::from_millis(60)).unwrap();
    let t = Instant::now();
    assert!(c.bootstrap("/authority".as_ref(), &"a".repeat(64)).is_err());
    assert!(t.elapsed() < Duration::from_millis(150));
    peer.join().unwrap();
}
#[test]
fn invalid_bootstrap_and_sequence_have_no_io() {
    let (a, mut b) = UnixStream::pair().unwrap();
    b.set_nonblocking(true).unwrap();
    let mut c = ControlChannel::new(a, Duration::from_secs(1)).unwrap();
    assert!(c.bootstrap("relative".as_ref(), &"a".repeat(64)).is_err());
    assert!(c.bootstrap("/authority".as_ref(), &"A".repeat(64)).is_err());
    assert!(c
        .bind("/core".as_ref(), 42, "/engine".as_ref(), 43)
        .is_err());
    let mut byte = [0];
    assert_eq!(
        b.read(&mut byte).unwrap_err().kind(),
        std::io::ErrorKind::WouldBlock
    );
    assert!(ControlChannel::new(b, Duration::ZERO).is_err());
}
struct Temp(PathBuf);
impl Temp {
    fn new() -> Self {
        let p = std::env::temp_dir().join(format!(
            "gamja-broker-pin-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&p).unwrap();
        Self(p)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
#[test]
fn source_pin_accepts_public_readable_but_rejects_replacement_and_in_place_write() {
    let d = Temp::new();
    let f = d.0.join("binary");
    fs::write(&f, b"one").unwrap();
    fs::set_permissions(&f, fs::Permissions::from_mode(0o755)).unwrap();
    let pin = SourcePin::open(&f, None).unwrap();
    pin.revalidate().unwrap();
    SourcePin::open(&f, Some(pin.sha256())).unwrap().revalidate().unwrap();
    fs::write(&f, b"two").unwrap();
    assert!(pin.revalidate().is_err());
    let pin = SourcePin::open(&f, None).unwrap();
    fs::rename(&f, d.0.join("old")).unwrap();
    fs::write(&f, b"two").unwrap();
    assert!(pin.revalidate().is_err());
}
#[test]
fn source_pin_rejects_symlink_hardlink_writable_and_wrong_hash() {
    let d = Temp::new();
    let f = d.0.join("binary");
    fs::write(&f, b"data").unwrap();
    fs::set_permissions(&f, fs::Permissions::from_mode(0o755)).unwrap();
    assert!(SourcePin::open(&f, Some(&"z".repeat(64))).is_err());
    assert!(SourcePin::open(&f, Some(&"a".repeat(64))).is_err());
    let symlink = d.0.join("link");
    std::os::unix::fs::symlink(&f, &symlink).unwrap();
    assert!(SourcePin::open(&symlink, None).is_err());
    let hard = d.0.join("hard");
    fs::hard_link(&f, &hard).unwrap();
    assert!(SourcePin::open(&f, None).is_err());
    fs::remove_file(hard).unwrap();
    fs::set_permissions(&f, fs::Permissions::from_mode(0o775)).unwrap();
    assert!(SourcePin::open(&f, None).is_err());
}
