#![cfg(unix)]

use gamja_core_host::credential::{
    library_constraints_supported_version, serve_channel, serve_channel_with_timeout,
    CredentialBackend, CredentialBackendError, CredentialOperation,
};
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::sync::{Arc, Mutex};

#[test]
fn library_constraints_require_macos_14_or_newer() {
    for supported in ["14", "14.0", "14.6.1", "15.0", "26.5.1"] {
        assert!(
            library_constraints_supported_version(supported),
            "{supported}"
        );
    }
    for unsupported in [
        "", "13", "13.6.9", "0", ".14", "14.", "14.beta", " 14.0", "14.0 ",
    ] {
        assert!(
            !library_constraints_supported_version(unsupported),
            "{unsupported}"
        );
    }
}

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
        Ok((account == "present").then(|| b"synthetic-value".to_vec()))
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

struct FailingBackend;

impl CredentialBackend for FailingBackend {
    fn get(
        &mut self,
        _service: &str,
        _account: &str,
    ) -> Result<Option<Vec<u8>>, CredentialBackendError> {
        Err(CredentialBackendError::interaction_required(
            CredentialOperation::SecItemCopyMatching,
            -25308,
        ))
    }
    fn put(
        &mut self,
        _service: &str,
        _account: &str,
        value: &[u8],
    ) -> Result<(), CredentialBackendError> {
        assert_eq!(value, b"sentinel-provider-secret");
        Err(CredentialBackendError::failed(
            CredentialOperation::SecItemAdd,
            -25293,
        ))
    }
    fn delete(&mut self, _service: &str, _account: &str) -> Result<(), CredentialBackendError> {
        Err(CredentialBackendError::failed(
            CredentialOperation::SecItemDelete,
            -25293,
        ))
    }
}

fn exchange(stream: &mut UnixStream, body: Value) -> Value {
    let body = serde_json::to_vec(&body).unwrap();
    stream
        .write_all(&(body.len() as u32).to_be_bytes())
        .unwrap();
    stream.write_all(&body).unwrap();
    let mut header = [0; 4];
    stream.read_exact(&mut header).unwrap();
    let mut response = vec![0; u32::from_be_bytes(header) as usize];
    stream.read_exact(&mut response).unwrap();
    serde_json::from_slice(&response).unwrap()
}

#[test]
fn inherited_channel_maps_only_fixed_namespaces_and_redacts_failures() {
    let (mut client, server) = UnixStream::pair().unwrap();
    let backend = MemoryBackend::default();
    let calls = backend.calls.clone();
    let worker = std::thread::spawn(move || serve_channel(server, backend));

    let provider_reference = "1:2:00000000000000000000000000000000";
    assert_eq!(
        exchange(
            &mut client,
            json!({"version":1,"operation":"put","namespace":"provider","reference":provider_reference,"value":"synthetic-value"})
        ),
        json!({"status":"ok"})
    );
    assert_eq!(
        exchange(
            &mut client,
            json!({"version":1,"operation":"get","namespace":"remembered_login","reference":"current"})
        ),
        json!({"status":"missing"})
    );
    let remembered_reference = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
    assert_eq!(
        exchange(
            &mut client,
            json!({"version":1,"operation":"put","namespace":"remembered_login","reference":remembered_reference,"value":"rotated-value"})
        ),
        json!({"status":"ok"})
    );
    for invalid in [
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ] {
        assert_eq!(
            exchange(
                &mut client,
                json!({"version":1,"operation":"get","namespace":"remembered_login","reference":invalid})
            ),
            json!({"status":"failed"})
        );
    }
    assert_eq!(
        exchange(
            &mut client,
            json!({"version":1,"operation":"get","namespace":"arbitrary","reference":provider_reference})
        ),
        json!({"status":"failed"})
    );
    assert_eq!(
        exchange(
            &mut client,
            json!({"version":1,"operation":"get","namespace":"completion","reference":format!("completion:{}", "a".repeat(64))})
        ),
        json!({"status":"failed"})
    );
    assert_eq!(
        exchange(
            &mut client,
            json!({"version":1,"operation":"delete","namespace":"provider","reference":provider_reference,"value":"must-not-be-accepted"})
        ),
        json!({"status":"failed"})
    );
    assert_eq!(
        exchange(
            &mut client,
            json!({"version":1,"operation":"get","namespace":"provider","reference":"arbitrary-selector"})
        ),
        json!({"status":"failed"})
    );
    assert_eq!(
        exchange(
            &mut client,
            json!({"version":1,"operation":"delete","namespace":"remembered_login","reference":"someone-else"})
        ),
        json!({"status":"failed"})
    );
    drop(client);
    assert!(worker.join().unwrap().is_ok());
    assert_eq!(
        *calls.lock().unwrap(),
        vec![
            (
                "put".into(),
                "cloud.gamja.credential-core.v1.provider".into(),
                provider_reference.into(),
                Some(b"synthetic-value".to_vec())
            ),
            (
                "get".into(),
                "cloud.gamja.credential-core.v1.remembered-login".into(),
                "current".into(),
                None
            ),
            (
                "put".into(),
                "cloud.gamja.credential-core.v1.remembered-login".into(),
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".into(),
                Some(b"rotated-value".to_vec())
            ),
        ]
    );
}

#[test]
fn inherited_channel_rejects_oversized_frame_before_backend_dispatch() {
    let (mut client, server) = UnixStream::pair().unwrap();
    let backend = MemoryBackend::default();
    let calls = backend.calls.clone();
    let worker = std::thread::spawn(move || serve_channel(server, backend));
    client.write_all(&(65_537u32).to_be_bytes()).unwrap();
    drop(client);
    assert!(worker.join().unwrap().is_err());
    assert!(calls.lock().unwrap().is_empty());
}

#[test]
fn inherited_channel_stops_when_the_engine_stalls_mid_frame() {
    use std::time::{Duration, Instant};

    let (mut client, server) = UnixStream::pair().unwrap();
    let backend = MemoryBackend::default();
    let calls = backend.calls.clone();
    let worker = std::thread::spawn(move || {
        serve_channel_with_timeout(server, backend, Duration::from_millis(40))
    });
    client.write_all(&(16u32).to_be_bytes()).unwrap();
    let started = Instant::now();
    for _ in 0..16 {
        if client.write_all(b"x").is_err() {
            break;
        }
        std::thread::sleep(Duration::from_millis(15));
    }
    assert!(worker.join().unwrap().is_err());
    assert!(started.elapsed() < Duration::from_millis(150));
    assert!(calls.lock().unwrap().is_empty());
}

#[test]
fn inherited_channel_reports_only_bounded_status_and_never_echoes_provider_values() {
    let (mut client, server) = UnixStream::pair().unwrap();
    let worker = std::thread::spawn(move || serve_channel(server, FailingBackend));
    let reference = "1:2:00000000000000000000000000000000";

    let failed = exchange(
        &mut client,
        json!({"version":1,"operation":"put","namespace":"provider","reference":reference,"value":"sentinel-provider-secret"}),
    );
    assert_eq!(
        failed,
        json!({
            "status":"failed",
            "operation":"sec_item_add",
            "error_class":"native_os_status",
            "os_status":-25293
        })
    );
    assert!(!failed.to_string().contains("sentinel-provider-secret"));
    assert_eq!(
        exchange(
            &mut client,
            json!({"version":1,"operation":"get","namespace":"provider","reference":reference})
        ),
        json!({
            "status":"interaction_required",
            "operation":"sec_item_copy_matching",
            "error_class":"interaction_required",
            "os_status":-25308
        })
    );
    drop(client);
    assert!(worker.join().unwrap().is_ok());
}

#[cfg(target_os = "macos")]
#[test]
fn credential_channel_requires_the_exact_owned_engine_bytes() {
    use gamja_core_host::credential::verify_engine;
    use sha2::{Digest, Sha256};
    use std::fs;
    use std::os::unix::fs::{symlink, PermissionsExt};
    use std::time::{SystemTime, UNIX_EPOCH};

    let root = std::env::temp_dir().join(format!(
        "gamja-credential-engine-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir(&root).unwrap();
    let engine = root.join("engine");
    fs::write(&engine, b"signed-engine-fixture").unwrap();
    fs::set_permissions(&engine, fs::Permissions::from_mode(0o755)).unwrap();
    let expected = Sha256::digest(b"signed-engine-fixture")
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    assert!(verify_engine(&engine, &expected).is_ok());
    fs::write(&engine, b"mutated-engine-fixture").unwrap();
    assert_eq!(
        verify_engine(&engine, &expected),
        Err("credential_engine_untrusted")
    );
    let link = root.join("engine-link");
    symlink(&engine, &link).unwrap();
    assert_eq!(
        verify_engine(&link, &expected),
        Err("credential_engine_untrusted")
    );
    fs::remove_file(link).unwrap();
    fs::remove_file(engine).unwrap();
    fs::remove_dir(root).unwrap();
}
