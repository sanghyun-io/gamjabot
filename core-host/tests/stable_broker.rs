#![cfg(all(target_os = "macos", target_arch = "aarch64"))]

use gamja_core_host::{credential::*, stable_broker::StableBrokerBackend};
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    net::Shutdown,
    os::unix::net::UnixStream,
    thread,
    time::{Duration, Instant},
};

const TIMEOUT: Duration = Duration::from_millis(100);
fn reference() -> String {
    format!("1:2:{}", "a".repeat(32))
}
fn pair(timeout: Duration) -> (StableBrokerBackend, UnixStream) {
    let (client, peer) = UnixStream::pair().unwrap();
    peer.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
    peer.set_write_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    (StableBrokerBackend::new(client, timeout).unwrap(), peer)
}
fn read(peer: &mut UnixStream) -> Value {
    let mut header = [0; 4];
    peer.read_exact(&mut header).unwrap();
    let size = u32::from_be_bytes(header) as usize;
    assert!((1..=65_536).contains(&size));
    let mut body = vec![0; size];
    peer.read_exact(&mut body).unwrap();
    serde_json::from_slice(&body).unwrap()
}
fn write(peer: &mut UnixStream, value: Value) {
    let body = serde_json::to_vec(&value).unwrap();
    peer.write_all(&(body.len() as u32).to_be_bytes()).unwrap();
    peer.write_all(&body).unwrap();
}
fn no_more_bytes(peer: &mut UnixStream) {
    let mut byte = [0];
    assert_eq!(peer.read(&mut byte).unwrap(), 0);
}

#[test]
fn four_internal_namespaces_send_exact_frozen_requests() {
    let (mut client, mut peer) = pair(Duration::from_secs(2));
    let cases = [
        (PROVIDER_SERVICE, "provider", reference()),
        (
            RECOVERY_SERVICE,
            "recovery",
            format!("recovery:{}", "b".repeat(64)),
        ),
        (
            PREPARED_SERVICE,
            "prepared",
            format!("prepared:{}", "c".repeat(64)),
        ),
        (
            COMPLETION_SERVICE,
            "completion",
            format!("completion:{}", "d".repeat(64)),
        ),
    ];
    let expected = cases.clone();
    let server = thread::spawn(move || {
        for (_, ns, account) in expected {
            for op in ["put", "get", "delete"] {
                let mut want =
                    json!({"version":1,"operation":op,"namespace":ns,"reference":account});
                if op == "put" {
                    want["value"] = json!("가짜 fixture");
                }
                assert_eq!(read(&mut peer), want);
                write(
                    &mut peer,
                    if op == "get" {
                        json!({"status":"ok","value":"가짜 fixture"})
                    } else {
                        json!({"status":"ok"})
                    },
                );
            }
        }
        no_more_bytes(&mut peer);
    });
    for (service, _, account) in cases {
        client
            .put(service, &account, "가짜 fixture".as_bytes())
            .unwrap();
        assert_eq!(
            client.get(service, &account).unwrap().unwrap(),
            "가짜 fixture".as_bytes()
        );
        client.delete(service, &account).unwrap();
    }
    drop(client);
    server.join().unwrap();
}

#[test]
fn validation_sends_nothing_and_does_not_poison_valid_channel() {
    let (mut client, mut peer) = pair(Duration::from_secs(2));
    for (service, account) in [
        (
            "cloud.gamja.credential-core.v1.remembered-login",
            "current".into(),
        ),
        (
            PROVIDER_SERVICE,
            "01:2:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
        ),
        (
            PROVIDER_SERVICE,
            "1:2:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA".into(),
        ),
        (RECOVERY_SERVICE, format!("prepared:{}", "a".repeat(64))),
        (COMPLETION_SERVICE, "completion:abc".into()),
    ] {
        assert_eq!(
            client.get(service, &account).unwrap_err().error_class(),
            "request_validation"
        );
    }
    for bytes in [vec![], vec![0xff], vec![b'a'; 32_769], vec![0; 32_768]] {
        assert_eq!(
            client
                .put(PROVIDER_SERVICE, &reference(), &bytes)
                .unwrap_err()
                .error_class(),
            "request_validation"
        );
    }
    let server = thread::spawn(move || {
        assert_eq!(
            read(&mut peer),
            json!({"version":1,"operation":"get","namespace":"provider","reference":reference()})
        );
        write(&mut peer, json!({"status":"missing"}));
        no_more_bytes(&mut peer);
    });
    assert!(client
        .get(PROVIDER_SERVICE, &reference())
        .unwrap()
        .is_none());
    drop(client);
    server.join().unwrap();
}

#[test]
fn utf8_maximum_and_legacy_uuid_work() {
    let (mut client, mut peer) = pair(Duration::from_secs(2));
    let bytes = format!("{}aa", "가".repeat(10_922));
    assert_eq!(bytes.len(), 32_768);
    let account = "1:2:12345678-abcd-abcd-abcd-123456789abc";
    let expected = bytes.clone();
    let server = thread::spawn(move || {
        assert_eq!(
            read(&mut peer),
            json!({"version":1,"operation":"put","namespace":"provider","reference":account,"value":expected})
        );
        write(&mut peer, json!({"status":"ok"}));
        assert_eq!(read(&mut peer)["operation"], "get");
        write(&mut peer, json!({"status":"ok","value":expected}));
    });
    client
        .put(PROVIDER_SERVICE, account, bytes.as_bytes())
        .unwrap();
    assert_eq!(
        client.get(PROVIDER_SERVICE, account).unwrap().unwrap(),
        bytes.as_bytes()
    );
    server.join().unwrap();
}

#[test]
fn native_errors_keep_real_status_and_allow_next_call_including_update() {
    for (op, status, operation, class) in [
        (
            "get",
            -25308,
            "sec_item_copy_matching",
            "interaction_required",
        ),
        ("delete", -50, "sec_item_delete", "native_os_status"),
        ("put", -25293, "sec_item_update", "native_os_status"),
    ] {
        let (mut client, mut peer) = pair(Duration::from_secs(2));
        let server = thread::spawn(move || {
            assert_eq!(read(&mut peer)["operation"], op);
            write(
                &mut peer,
                json!({"status":if class=="interaction_required" {class}else{"failed"},"os_status":status,"operation":operation,"error_class":class}),
            );
            read(&mut peer);
            write(&mut peer, json!({"status":"missing"}));
            no_more_bytes(&mut peer);
        });
        let error = match op {
            "get" => client.get(PROVIDER_SERVICE, &reference()).unwrap_err(),
            "put" => client
                .put(PROVIDER_SERVICE, &reference(), b"fixture")
                .unwrap_err(),
            _ => client.delete(PROVIDER_SERVICE, &reference()).unwrap_err(),
        };
        assert_eq!(error.os_status(), Some(status));
        assert_eq!(error.error_class(), class);
        assert_eq!(
            serde_json::to_value(error.operation().unwrap()).unwrap(),
            operation
        );
        assert!(client
            .get(PROVIDER_SERVICE, &reference())
            .unwrap()
            .is_none());
        drop(client);
        server.join().unwrap();
    }
}

#[test]
fn malformed_or_inconsistent_responses_are_terminal() {
    for body in [
        br#"{"status":"ok"}"#.to_vec(),
        br#"{"status":"ok","value":"fixture","os_status":-50}"#.to_vec(),
        br#"{"status":"missing","value":"fixture"}"#.to_vec(),
        br#"{"status":"missing","operation":null}"#.to_vec(),
        br#"{"status":"missing","unexpected":"fixture"}"#.to_vec(),
        br#"{"status":"other"}"#.to_vec(),
        br#"{"status":"failed","os_status":0,"operation":"sec_item_copy_matching","error_class":"native_os_status"}"#.to_vec(),
        br#"{"status":"interaction_required","os_status":-25308,"operation":"sec_item_delete","error_class":"interaction_required"}"#.to_vec(),
        br#"{"status":"failed","os_status":-50,"operation":"sec_item_copy_matching","error_class":"interaction_required"}"#.to_vec(),
        br#"{"status":"failed","os_status":-25308,"operation":"sec_item_copy_matching","error_class":"native_os_status"}"#.to_vec(),
        br#"{"status":"failed","value":"fixture"}"#.to_vec(),
        br#"{"status":"ok","value":null}"#.to_vec(),
        br#"{"status":"ok","value":""}"#.to_vec(),
        b"not-json".to_vec(),
        serde_json::to_vec(&json!({"status":"ok","value":"a".repeat(32_769)})).unwrap(),
    ] {
        let (mut client,mut peer)=pair(Duration::from_secs(2));
        let server=thread::spawn(move||{read(&mut peer);peer.write_all(&(body.len() as u32).to_be_bytes()).unwrap();peer.write_all(&body).unwrap();no_more_bytes(&mut peer);});
        let error=client.get(PROVIDER_SERVICE,&reference()).unwrap_err();
        assert_eq!(error.error_class(),"bridge_protocol");assert_eq!(error.os_status(),None);
        assert!(client.get(PROVIDER_SERVICE,&reference()).is_err());server.join().unwrap();
    }
}

#[test]
fn invalid_get_only_status_for_put_or_delete_is_terminal_and_bare_failure_is_finite() {
    for op in ["put", "delete"] {
        let (mut client, mut peer) = pair(Duration::from_secs(2));
        let server = thread::spawn(move || {
            read(&mut peer);
            write(&mut peer, json!({"status":"missing"}));
            no_more_bytes(&mut peer);
        });
        let e = if op == "put" {
            client
                .put(PROVIDER_SERVICE, &reference(), b"fixture")
                .unwrap_err()
        } else {
            client.delete(PROVIDER_SERVICE, &reference()).unwrap_err()
        };
        assert_eq!(e.error_class(), "bridge_protocol");
        server.join().unwrap();
    }
    let (mut client, mut peer) = pair(Duration::from_secs(2));
    let server = thread::spawn(move || {
        read(&mut peer);
        write(&mut peer, json!({"status":"failed"}));
        no_more_bytes(&mut peer);
    });
    let e = client.get(PROVIDER_SERVICE, &reference()).unwrap_err();
    assert_eq!(e.error_class(), "store_unavailable");
    assert_eq!(e.os_status(), None);
    server.join().unwrap();
}

#[test]
fn zero_oversize_truncated_and_eof_frames_close_channel() {
    for body in [
        vec![],
        0u32.to_be_bytes().to_vec(),
        65_537u32.to_be_bytes().to_vec(),
        vec![0, 0],
        vec![0, 0, 0, 20, b'{'],
    ] {
        let (mut client, mut peer) = pair(Duration::from_secs(2));
        let server = thread::spawn(move || {
            read(&mut peer);
            peer.write_all(&body).unwrap();
            peer.shutdown(Shutdown::Write).unwrap();
            no_more_bytes(&mut peer);
        });
        let e = client.get(PROVIDER_SERVICE, &reference()).unwrap_err();
        assert_eq!(e.os_status(), None);
        assert!(matches!(
            e.error_class(),
            "bridge_transport" | "bridge_protocol"
        ));
        assert!(client.delete(PROVIDER_SERVICE, &reference()).is_err());
        server.join().unwrap();
    }
}

#[test]
fn each_exchange_gets_new_deadline_and_slow_body_cannot_reset_it() {
    let (mut client, mut peer) = pair(TIMEOUT);
    let server = thread::spawn(move || {
        for _ in 0..2 {
            read(&mut peer);
            write(&mut peer, json!({"status":"missing"}));
        }
    });
    thread::sleep(Duration::from_millis(130));
    assert!(client
        .get(PROVIDER_SERVICE, &reference())
        .unwrap()
        .is_none());
    thread::sleep(Duration::from_millis(130));
    assert!(client
        .get(PROVIDER_SERVICE, &reference())
        .unwrap()
        .is_none());
    server.join().unwrap();
    let (mut client, mut peer) = pair(TIMEOUT);
    let server = thread::spawn(move || {
        read(&mut peer);
        peer.write_all(&24u32.to_be_bytes()).unwrap();
        for _ in 0..8 {
            thread::sleep(Duration::from_millis(30));
            if peer.write_all(b"x").is_err() {
                break;
            }
        }
    });
    let start = Instant::now();
    let e = client.get(PROVIDER_SERVICE, &reference()).unwrap_err();
    assert_eq!(e.error_class(), "bridge_transport");
    assert!(start.elapsed() < Duration::from_millis(400));
    assert!(client.get(PROVIDER_SERVICE, &reference()).is_err());
    server.join().unwrap();
}

#[test]
fn stalled_header_is_bounded_and_invalid_timeouts_are_rejected() {
    let (s, p) = UnixStream::pair().unwrap();
    assert_eq!(
        StableBrokerBackend::new(s, Duration::ZERO)
            .err()
            .unwrap()
            .error_class(),
        "request_validation"
    );
    drop(p);
    let (s, p) = UnixStream::pair().unwrap();
    assert_eq!(
        StableBrokerBackend::new(s, Duration::MAX)
            .err()
            .unwrap()
            .error_class(),
        "request_validation"
    );
    drop(p);
    let (mut client, peer) = pair(TIMEOUT);
    let start = Instant::now();
    assert!(client.get(PROVIDER_SERVICE, &reference()).is_err());
    assert!(start.elapsed() < Duration::from_millis(400));
    drop(peer);
}

#[test]
fn a_full_send_buffer_bounds_writing_and_does_not_wait_for_a_response() {
    let (mut socket, peer) = UnixStream::pair().unwrap();
    socket.set_nonblocking(true).unwrap();
    let mut full = false;
    for _ in 0..64 {
        match socket.write(&[b'x'; 65_536]) {
            Ok(0) => panic!("socket closed before saturation"),
            Ok(_) => {}
            Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                full = true;
                break;
            }
            Err(e) => panic!("fixture socket failure: {e:?}"),
        }
    }
    assert!(
        full,
        "fixture must fill the send buffer before exercising the client"
    );
    socket.set_nonblocking(false).unwrap();
    let mut client = StableBrokerBackend::new(socket, TIMEOUT).unwrap();
    let start = Instant::now();
    let e = client.get(PROVIDER_SERVICE, &reference()).unwrap_err();
    assert_eq!(e.error_class(), "bridge_transport");
    assert!(start.elapsed() < Duration::from_millis(400));
    drop(peer);
}

#[test]
fn channel_serializes_non_os_failure_without_fabricating_status_or_operation() {
    struct Failed;
    impl CredentialBackend for Failed {
        fn get(&mut self, _: &str, _: &str) -> Result<Option<Vec<u8>>, CredentialBackendError> {
            Err(CredentialBackendError::unavailable(
                CredentialFailureClass::BridgeTransport,
            ))
        }
        fn put(&mut self, _: &str, _: &str, _: &[u8]) -> Result<(), CredentialBackendError> {
            unreachable!()
        }
        fn delete(&mut self, _: &str, _: &str) -> Result<(), CredentialBackendError> {
            unreachable!()
        }
    }
    let (mut client, server) = UnixStream::pair().unwrap();
    client
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let task = thread::spawn(move || serve_channel(server, Failed).unwrap());
    write(
        &mut client,
        json!({"version":1,"operation":"get","namespace":"provider","reference":reference()}),
    );
    assert_eq!(
        read(&mut client),
        json!({"status":"failed","error_class":"bridge_transport"})
    );
    drop(client);
    task.join().unwrap();
}
