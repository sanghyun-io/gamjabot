#![cfg(unix)]

use gamja_credential_broker::{
    run_isolated_first_write_proof, serve_broker_with_timeout, serve_data_channel_with_timeout,
    validate_binding_evidence, BindRequestV1, BindingEvidence, BindingVerifier, BootstrapRequestV1,
    ChannelKind, CredentialBackend, CredentialBackendError, CredentialOperation,
    ReleaseAuthorizationV1, StableExecutable, COMPLETION_SERVICE, PREPARED_SERVICE,
    PROVIDER_SERVICE, RECOVERY_SERVICE, REMEMBERED_LOGIN_SERVICE,
};
use serde_json::{json, Value};
use std::fs::{self, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[derive(Clone, Default)]
struct MemoryBackend {
    calls: Arc<Mutex<Vec<(String, String, String, Option<Vec<u8>>)>>>,
}

impl CredentialBackend for MemoryBackend {
    fn get(
        &mut self,
        service: &str,
        account: &str,
    ) -> Result<Option<Vec<u8>>, CredentialBackendError> {
        self.calls
            .lock()
            .unwrap()
            .push(("get".into(), service.into(), account.into(), None));
        Ok((account == "current").then(|| b"synthetic-value".to_vec()))
    }

    fn put(
        &mut self,
        service: &str,
        account: &str,
        value: &[u8],
    ) -> Result<(), CredentialBackendError> {
        self.calls.lock().unwrap().push((
            "put".into(),
            service.into(),
            account.into(),
            Some(value.to_vec()),
        ));
        Ok(())
    }

    fn delete(&mut self, service: &str, account: &str) -> Result<(), CredentialBackendError> {
        self.calls
            .lock()
            .unwrap()
            .push(("delete".into(), service.into(), account.into(), None));
        Ok(())
    }
}

fn exchange(stream: &mut UnixStream, body: Value) -> Value {
    let bytes = serde_json::to_vec(&body).unwrap();
    stream
        .write_all(&(bytes.len() as u32).to_be_bytes())
        .unwrap();
    stream.write_all(&bytes).unwrap();
    let mut header = [0; 4];
    stream.read_exact(&mut header).unwrap();
    let mut response = vec![0; u32::from_be_bytes(header) as usize];
    stream.read_exact(&mut response).unwrap();
    serde_json::from_slice(&response).unwrap()
}

struct FakeVerifier {
    bootstrap_calls: usize,
    bind_calls: usize,
    alive: Arc<AtomicBool>,
}

impl BindingVerifier for FakeVerifier {
    type Bootstrap = ();
    type Bound = Arc<AtomicBool>;
    fn bootstrap(&mut self, request: &BootstrapRequestV1) -> Result<Self::Bootstrap, &'static str> {
        self.bootstrap_calls += 1;
        if request.authority_sha256() == "a".repeat(64) { Ok(()) } else { Err("authority_invalid") }
    }
    fn bind(
        &mut self,
        _: &Self::Bootstrap,
        request: &BindRequestV1,
    ) -> Result<Self::Bound, &'static str> {
        self.bind_calls += 1;
        if request.parent_pid() > 1 && request.engine_pid() > 1 {
            Ok(self.alive.clone())
        } else {
            Err("live_identity_invalid")
        }
    }

    fn check_bound(&mut self, bound: &Self::Bound) -> Result<(), &'static str> {
        bound
            .load(Ordering::SeqCst)
            .then_some(())
            .ok_or("process_dead")
    }
}

#[test]
fn control_binding_is_one_shot_and_data_is_not_read_before_bind() {
    let (mut control_client, control_server) = UnixStream::pair().unwrap();
    let (mut engine_client, engine_server) = UnixStream::pair().unwrap();
    let (_internal_client, internal_server) = UnixStream::pair().unwrap();
    let backend = MemoryBackend::default();
    let calls = backend.calls.clone();
    let alive = Arc::new(AtomicBool::new(true));
    let verifier = FakeVerifier {
        bootstrap_calls: 0,
        bind_calls: 0,
        alive,
    };
    let worker = std::thread::spawn(move || {
        serve_broker_with_timeout(
            control_server,
            engine_server,
            internal_server,
            backend,
            verifier,
            Duration::from_secs(1),
        )
    });
    let pending = serde_json::to_vec(&json!({"version":1,"operation":"get","namespace":"remembered_login","reference":"current"})).unwrap();
    engine_client.write_all(&(pending.len() as u32).to_be_bytes()).unwrap();
    engine_client.write_all(&pending).unwrap();
    engine_client.set_read_timeout(Some(Duration::from_millis(30))).unwrap();
    let mut byte = [0u8; 1];
    assert!(engine_client.read(&mut byte).is_err());
    assert!(calls.lock().unwrap().is_empty());
    assert_eq!(
        exchange(&mut control_client, json!({
            "version":1,"operation":"bootstrap","authority_path":"/owned/authority",
            "authority_sha256":"a".repeat(64)
        })),
        json!({"status":"bootstrapped"})
    );
    assert_eq!(
        exchange(&mut control_client, json!({
            "version":1,"operation":"bind","parent_path":"/owned/core","parent_pid":42,
            "engine_path":"/owned/engine","engine_pid":43
        })),
        json!({"status":"bound"})
    );
    engine_client.set_read_timeout(Some(Duration::from_secs(1))).unwrap();
    let mut header = [0; 4];
    engine_client.read_exact(&mut header).unwrap();
    let mut response = vec![0; u32::from_be_bytes(header) as usize];
    engine_client.read_exact(&mut response).unwrap();
    assert_eq!(serde_json::from_slice::<Value>(&response).unwrap(), json!({"status":"ok","value":"synthetic-value"}));
    assert_eq!(
        exchange(&mut control_client, json!({"version":1,"operation":"shutdown"})),
        json!({"status":"shutdown"})
    );
    assert!(worker.join().unwrap().is_ok());
}

#[test]
fn bound_process_death_rejects_the_next_frame_without_backend_access() {
    let (mut control_client, control_server) = UnixStream::pair().unwrap();
    let (mut engine_client, engine_server) = UnixStream::pair().unwrap();
    let (_internal_client, internal_server) = UnixStream::pair().unwrap();
    let backend = MemoryBackend::default();
    let calls = backend.calls.clone();
    let alive = Arc::new(AtomicBool::new(true));
    let verifier = FakeVerifier {
        bootstrap_calls: 0,
        bind_calls: 0,
        alive: alive.clone(),
    };
    let worker = std::thread::spawn(move || {
        serve_broker_with_timeout(
            control_server,
            engine_server,
            internal_server,
            backend,
            verifier,
            Duration::from_millis(50),
        )
    });
    assert_eq!(
        exchange(&mut control_client, json!({
            "version":1,"operation":"bootstrap","authority_path":"/owned/authority",
            "authority_sha256":"a".repeat(64)
        })),
        json!({"status":"bootstrapped"})
    );
    assert_eq!(
        exchange(&mut control_client, json!({
            "version":1,"operation":"bind","parent_path":"/owned/core","parent_pid":42,
            "engine_path":"/owned/engine","engine_pid":43
        })),
        json!({"status":"bound"})
    );
    alive.store(false, Ordering::SeqCst);
    let request = serde_json::to_vec(&json!({
        "version":1,"operation":"get","namespace":"remembered_login","reference":"current"
    })).unwrap();
    engine_client.write_all(&(request.len() as u32).to_be_bytes()).unwrap();
    engine_client.write_all(&request).unwrap();
    assert!(worker.join().unwrap().is_err());
    assert!(calls.lock().unwrap().is_empty());
}

#[test]
fn isolated_proof_generates_its_own_test_namespace_and_cleans_it() {
    #[derive(Default)]
    struct ProofBackend { value: Option<Vec<u8>>, services: Vec<String>, calls: Vec<&'static str> }
    impl CredentialBackend for ProofBackend {
        fn get(&mut self, service: &str, account: &str) -> Result<Option<Vec<u8>>, CredentialBackendError> {
            assert!(service.starts_with("cloud.gamja.credential-broker.v1.proof."));
            assert!(account.starts_with("proof:"));
            self.services.push(service.into()); self.calls.push("get"); Ok(self.value.clone())
        }
        fn put(&mut self, service: &str, account: &str, value: &[u8]) -> Result<(), CredentialBackendError> {
            assert!(service.starts_with("cloud.gamja.credential-broker.v1.proof."));
            assert!(account.starts_with("proof:"));
            assert!(value.starts_with(b"broker-proof-"));
            self.services.push(service.into()); self.calls.push("put"); self.value = Some(value.to_vec()); Ok(())
        }
        fn delete(&mut self, service: &str, account: &str) -> Result<(), CredentialBackendError> {
            assert!(service.starts_with("cloud.gamja.credential-broker.v1.proof."));
            assert!(account.starts_with("proof:"));
            self.services.push(service.into()); self.calls.push("delete"); self.value = None; Ok(())
        }
    }
    let mut backend = ProofBackend::default();
    let receipt = run_isolated_first_write_proof(&mut backend);
    assert!(
        receipt.passed()
            && receipt.cleanup_confirmed
            && receipt.initial_missing == Some(true)
            && receipt.final_missing == Some(true)
    );
    assert_eq!(backend.calls, ["get", "put", "get", "delete", "get"]);
    assert!(backend.services.windows(2).all(|pair| pair[0] == pair[1]));
    assert!(backend.services.iter().all(|service| ![
        PROVIDER_SERVICE, RECOVERY_SERVICE, PREPARED_SERVICE, COMPLETION_SERVICE,
        REMEMBERED_LOGIN_SERVICE,
    ].contains(&service.as_str())));
}

#[test]
fn isolated_proof_deletes_its_owned_item_when_readback_fails() {
    #[derive(Default)]
    struct FailingReadback { gets: u8, delete_calls: u8, value_present: bool }
    impl CredentialBackend for FailingReadback {
        fn get(&mut self, _: &str, _: &str) -> Result<Option<Vec<u8>>, CredentialBackendError> {
            self.gets += 1;
            if self.gets == 2 {
                Err(CredentialBackendError::failed(CredentialOperation::SecItemCopyMatching, -1))
            } else {
                Ok(self.value_present.then(|| b"unexpected".to_vec()))
            }
        }
        fn put(&mut self, _: &str, _: &str, _: &[u8]) -> Result<(), CredentialBackendError> {
            self.value_present = true;
            Ok(())
        }
        fn delete(&mut self, _: &str, _: &str) -> Result<(), CredentialBackendError> {
            self.delete_calls += 1;
            self.value_present = false;
            Ok(())
        }
    }
    let mut backend = FailingReadback::default();
    let receipt = run_isolated_first_write_proof(&mut backend);
    assert_eq!(receipt.status, "failed");
    assert!(receipt.cleanup_confirmed);
    assert_eq!(receipt.query_count, 3);
    assert_eq!(receipt.write_count, 1);
    assert_eq!(receipt.delete_count, 1);
    let closed = serde_json::to_value(&receipt).unwrap();
    assert_eq!(closed["operation"], "sec_item_copy_matching");
    assert_eq!(closed["os_status"], -1);
    assert_eq!(backend.delete_calls, 1);
    assert!(!backend.value_present);
}

#[test]
fn isolated_proof_reports_interaction_and_cleanup_uncertainty_with_closed_counts() {
    struct InitialInteraction;
    impl CredentialBackend for InitialInteraction {
        fn get(&mut self, _: &str, _: &str) -> Result<Option<Vec<u8>>, CredentialBackendError> {
            Err(CredentialBackendError::interaction_required(
                CredentialOperation::SecItemCopyMatching,
                -25308,
            ))
        }
        fn put(&mut self, _: &str, _: &str, _: &[u8]) -> Result<(), CredentialBackendError> {
            unreachable!()
        }
        fn delete(&mut self, _: &str, _: &str) -> Result<(), CredentialBackendError> {
            unreachable!()
        }
    }
    let receipt = run_isolated_first_write_proof(&mut InitialInteraction);
    let closed = serde_json::to_value(&receipt).unwrap();
    assert_eq!(closed["status"], "interaction_required");
    assert_eq!(closed["failure_stage"], "initial_query");
    assert_eq!(closed["operation"], "sec_item_copy_matching");
    assert_eq!(closed["os_status"], -25308);
    assert_eq!(closed["query_count"], 1);
    assert_eq!(closed["write_count"], 0);
    assert_eq!(closed["delete_count"], 0);
    assert_eq!(closed["cleanup_confirmed"], false);
    assert!(!serde_json::to_string(&closed).unwrap().contains("broker-proof"));

    #[derive(Default)]
    struct DeleteFailure { present: bool }
    impl CredentialBackend for DeleteFailure {
        fn get(&mut self, _: &str, _: &str) -> Result<Option<Vec<u8>>, CredentialBackendError> {
            Ok(self.present.then(|| b"broker-proof-synthetic".to_vec()))
        }
        fn put(&mut self, _: &str, _: &str, value: &[u8]) -> Result<(), CredentialBackendError> {
            self.present = true;
            assert!(value.starts_with(b"broker-proof-"));
            Ok(())
        }
        fn delete(&mut self, _: &str, _: &str) -> Result<(), CredentialBackendError> {
            Err(CredentialBackendError::failed(CredentialOperation::SecItemDelete, -50))
        }
    }
    let receipt = run_isolated_first_write_proof(&mut DeleteFailure::default());
    let closed = serde_json::to_value(&receipt).unwrap();
    assert_eq!(closed["status"], "cleanup_uncertain");
    assert_eq!(closed["failure_stage"], "delete");
    assert_eq!(closed["operation"], "sec_item_delete");
    assert_eq!(closed["os_status"], -50);
    assert_eq!(closed["query_count"], 3);
    assert_eq!(closed["write_count"], 1);
    assert_eq!(closed["delete_count"], 1);
    assert_eq!(closed["cleanup_confirmed"], false);
    assert!(!serde_json::to_string(&closed).unwrap().contains("broker-proof-synthetic"));
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
#[test]
fn proof_mode_rejects_every_caller_supplied_namespace_or_value() {
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_gamja-credential-broker"))
        .args(["--isolated-first-write-proof", "provider"])
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(output.stdout.is_empty());
    assert!(output.stderr.is_empty());
}

#[cfg(all(target_os = "macos", target_arch = "aarch64"))]
#[test]
fn proof_mode_without_compiled_trust_pins_stops_before_keychain() {
    let output = std::process::Command::new(env!("CARGO_BIN_EXE_gamja-credential-broker"))
        .arg("--isolated-first-write-proof")
        .output()
        .unwrap();
    assert!(!output.status.success());
    assert!(output.stdout.is_empty());
    assert!(output.stderr.is_empty());
}

#[test]
fn engine_and_core_channels_have_disjoint_closed_service_unions() {
    let provider_ref = "1:2:00000000000000000000000000000000";
    let operation = "a".repeat(64);

    let (mut engine_client, engine_server) = UnixStream::pair().unwrap();
    let engine_backend = MemoryBackend::default();
    let engine_calls = engine_backend.calls.clone();
    let engine_worker = std::thread::spawn(move || {
        serve_data_channel_with_timeout(
            ChannelKind::Engine,
            engine_server,
            engine_backend,
            Duration::from_secs(1),
        )
    });
    assert_eq!(
        exchange(&mut engine_client, json!({"version":1,"operation":"put","namespace":"provider","reference":provider_ref,"value":"secret"})),
        json!({"status":"ok"})
    );
    assert_eq!(
        exchange(&mut engine_client, json!({"version":1,"operation":"get","namespace":"remembered_login","reference":"current"})),
        json!({"status":"ok","value":"synthetic-value"})
    );
    assert_eq!(
        exchange(&mut engine_client, json!({"version":1,"operation":"get","namespace":"recovery","reference":format!("recovery:{operation}")})),
        json!({"status":"failed"})
    );
    drop(engine_client);
    assert!(engine_worker.join().unwrap().is_ok());
    assert_eq!(engine_calls.lock().unwrap().len(), 2);

    let (mut core_client, core_server) = UnixStream::pair().unwrap();
    let core_backend = MemoryBackend::default();
    let core_calls = core_backend.calls.clone();
    let core_worker = std::thread::spawn(move || {
        serve_data_channel_with_timeout(
            ChannelKind::CoreInternal,
            core_server,
            core_backend,
            Duration::from_secs(1),
        )
    });
    for (namespace, prefix) in [
        ("recovery", "recovery"),
        ("prepared", "prepared"),
        ("completion", "completion"),
    ] {
        assert_eq!(
            exchange(&mut core_client, json!({"version":1,"operation":"put","namespace":namespace,"reference":format!("{prefix}:{operation}"),"value":"envelope"})),
            json!({"status":"ok"})
        );
    }
    assert_eq!(
        exchange(&mut core_client, json!({"version":1,"operation":"get","namespace":"provider","reference":provider_ref})),
        json!({"status":"missing"})
    );
    assert_eq!(
        exchange(&mut core_client, json!({"version":1,"operation":"get","namespace":"remembered_login","reference":"current"})),
        json!({"status":"failed"})
    );
    drop(core_client);
    assert!(core_worker.join().unwrap().is_ok());
    let calls = core_calls.lock().unwrap();
    assert_eq!(calls.len(), 4);
    assert_eq!(calls[0].1, RECOVERY_SERVICE);
    assert_eq!(calls[1].1, PREPARED_SERVICE);
    assert_eq!(calls[2].1, COMPLETION_SERVICE);
    assert_eq!(calls[3].1, PROVIDER_SERVICE);
    assert_ne!(PROVIDER_SERVICE, REMEMBERED_LOGIN_SERVICE);
}

#[test]
fn frames_values_references_and_failures_are_strict_and_redacted() {
    struct Failing;
    impl CredentialBackend for Failing {
        fn get(&mut self, _: &str, _: &str) -> Result<Option<Vec<u8>>, CredentialBackendError> {
            Err(CredentialBackendError::interaction_required(
                CredentialOperation::SecItemCopyMatching,
                -25308,
            ))
        }
        fn put(&mut self, _: &str, _: &str, value: &[u8]) -> Result<(), CredentialBackendError> {
            assert_eq!(value, b"sentinel-secret");
            Err(CredentialBackendError::failed(
                CredentialOperation::SecItemAdd,
                -25293,
            ))
        }
        fn delete(&mut self, _: &str, _: &str) -> Result<(), CredentialBackendError> {
            unreachable!()
        }
    }

    let (mut client, server) = UnixStream::pair().unwrap();
    let worker = std::thread::spawn(move || {
        serve_data_channel_with_timeout(
            ChannelKind::Engine,
            server,
            Failing,
            Duration::from_secs(1),
        )
    });
    let reference = "1:2:00000000000000000000000000000000";
    let put = exchange(&mut client, json!({"version":1,"operation":"put","namespace":"provider","reference":reference,"value":"sentinel-secret"}));
    assert_eq!(put, json!({"status":"failed","os_status":-25293,"operation":"sec_item_add","error_class":"native_os_status"}));
    assert!(!serde_json::to_string(&put).unwrap().contains("sentinel-secret"));
    assert_eq!(
        exchange(&mut client, json!({"version":1,"operation":"get","namespace":"provider","reference":reference})),
        json!({"status":"interaction_required","os_status":-25308,"operation":"sec_item_copy_matching","error_class":"interaction_required"})
    );
    for invalid in [
        json!({"version":true,"operation":"get","namespace":"provider","reference":reference}),
        json!({"version":1,"operation":"get","namespace":"provider","reference":"01:2:00000000000000000000000000000000"}),
        json!({"version":1,"operation":"put","namespace":"provider","reference":reference,"value":""}),
        json!({"version":1,"operation":"delete","namespace":"provider","reference":reference,"value":"x"}),
        json!({"version":1,"operation":"get","namespace":"provider","reference":reference,"extra":1}),
    ] {
        assert_eq!(exchange(&mut client, invalid), json!({"status":"failed"}));
    }
    drop(client);
    assert!(worker.join().unwrap().is_ok());

    let (mut client, server) = UnixStream::pair().unwrap();
    let backend = MemoryBackend::default();
    let calls = backend.calls.clone();
    let worker = std::thread::spawn(move || {
        serve_data_channel_with_timeout(
            ChannelKind::Engine,
            server,
            backend,
            Duration::from_millis(50),
        )
    });
    client.write_all(&(65_537u32).to_be_bytes()).unwrap();
    drop(client);
    assert!(worker.join().unwrap().is_err());
    assert!(calls.lock().unwrap().is_empty());
}

#[test]
fn release_authorization_is_closed_bounded_and_has_no_manifest_cycle() {
    let hash = "a".repeat(64);
    let bytes = serde_json::to_vec(&json!({
        "protocol_version":1,
        "build_id":"release-2026-10-09",
        "broker_sha256":hash,
        "core_sha256":"b".repeat(64),
        "engine_sha256":"c".repeat(64)
    })).unwrap();
    let claims = ReleaseAuthorizationV1::parse(&bytes).unwrap();
    assert_eq!(claims.protocol_version(), 1);
    assert_eq!(claims.build_id(), "release-2026-10-09");
    assert!(ReleaseAuthorizationV1::parse(&serde_json::to_vec(&json!({
        "protocol_version":1,
        "build_id":"release-2026-10-09",
        "broker_sha256":"a".repeat(64),
        "core_sha256":"b".repeat(64),
        "engine_sha256":"c".repeat(64),
        "manifest_sha256":"d".repeat(64)
    })).unwrap()).is_err());
    assert!(ReleaseAuthorizationV1::parse(&vec![b'x'; 4097]).is_err());
    assert!(ReleaseAuthorizationV1::parse(&serde_json::to_vec(&json!({
        "protocol_version":1,
        "build_id":"bad id",
        "broker_sha256":"A".repeat(64),
        "core_sha256":"b".repeat(64),
        "engine_sha256":"c".repeat(64)
    })).unwrap()).is_err());
}

#[test]
fn binding_requires_exact_static_live_identity_and_all_hashes() {
    let claims = ReleaseAuthorizationV1::parse(&serde_json::to_vec(&json!({
        "protocol_version":1,
        "build_id":"release-1",
        "broker_sha256":"a".repeat(64),
        "core_sha256":"b".repeat(64),
        "engine_sha256":"c".repeat(64)
    })).unwrap()).unwrap();
    let exact = BindingEvidence {
        broker_sha256: "a".repeat(64),
        broker_static_unique: vec![10, 11, 12],
        broker_live_unique: vec![10, 11, 12],
        authority_static_unique: vec![1, 2, 3],
        authority_live_unique: vec![1, 2, 3],
        core_sha256: "b".repeat(64),
        core_static_unique: vec![4, 5, 6],
        core_live_unique: vec![4, 5, 6],
        engine_sha256: "c".repeat(64),
        engine_static_unique: vec![7, 8, 9],
        engine_live_unique: vec![7, 8, 9],
    };
    assert!(validate_binding_evidence(&claims, &exact).is_ok());
    for drift in [
        BindingEvidence { broker_live_unique: vec![9], ..exact.clone() },
        BindingEvidence { authority_live_unique: vec![9], ..exact.clone() },
        BindingEvidence { core_sha256: "d".repeat(64), ..exact.clone() },
        BindingEvidence { core_live_unique: vec![9], ..exact.clone() },
        BindingEvidence { engine_live_unique: vec![9], ..exact.clone() },
    ] {
        assert!(validate_binding_evidence(&claims, &drift).is_err());
    }
}

fn temp_path(label: &str) -> PathBuf {
    let nonce = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_nanos();
    std::env::temp_dir().join(format!("gamja-broker-{label}-{}-{nonce}", std::process::id()))
}

#[test]
fn stable_executable_detects_path_replacement_without_following_symlinks() {
    let root = temp_path("stable-file");
    fs::create_dir(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let path = root.join("authority");
    let mut file = OpenOptions::new().write(true).create_new(true).mode(0o700).open(&path).unwrap();
    file.write_all(b"first-authority").unwrap();
    drop(file);
    let snapshot = StableExecutable::open(&path).unwrap();
    assert!(snapshot.revalidate_path().is_ok());
    let archived = root.join("authority.old");
    fs::rename(&path, &archived).unwrap();
    let mut replacement = OpenOptions::new().write(true).create_new(true).mode(0o700).open(&path).unwrap();
    replacement.write_all(b"replacement").unwrap();
    drop(replacement);
    assert!(snapshot.revalidate_path().is_err());
    assert_ne!(fs::metadata(&path).unwrap().ino(), snapshot.inode());
    fs::remove_dir_all(root).unwrap();
}

#[cfg(target_os = "macos")]
#[test]
fn spawn_closes_extra_descriptors_and_preserves_only_the_explicit_mapping() {
    use gamja_credential_broker::{spawn_allowlisted, AllowlistedFd};
    use std::os::fd::AsRawFd;

    let (allowed_left, _allowed_right) = UnixStream::pair().unwrap();
    let (extra_left, _extra_right) = UnixStream::pair().unwrap();
    for fd in [allowed_left.as_raw_fd(), extra_left.as_raw_fd()] {
        let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
        assert!(flags >= 0);
        assert_eq!(unsafe { libc::fcntl(fd, libc::F_SETFD, flags & !libc::FD_CLOEXEC) }, 0);
    }
    let script = format!(
        "test -e /dev/fd/9 && test ! -e /dev/fd/{}",
        extra_left.as_raw_fd()
    );
    let status = spawn_allowlisted(
        std::path::Path::new("/bin/sh"),
        &["sh", "-c", &script],
        &[AllowlistedFd { source: allowed_left.as_raw_fd(), target: 9 }],
    ).unwrap();
    assert!(status.success());
}
