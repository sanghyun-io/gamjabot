use gamja_core_host::runtime_install::{
    broker_availability, validate_runtime, BrokerAvailability, BrokerUnavailableReason,
    RuntimePins, RuntimeScope, RuntimeStatus,
};
use std::ffi::OsString;

#[test]
fn pins_are_exact_lowercase_sha256_values() {
    let valid = "1".repeat(64);
    assert!(RuntimePins::new(valid.clone(), valid.clone(), valid.clone()).is_ok());
    assert_eq!(
        RuntimePins::new("A".repeat(64), valid.clone(), valid.clone()).unwrap_err(),
        "core_runtime_pin_invalid"
    );
    assert_eq!(
        RuntimePins::new("1".repeat(63), valid.clone(), valid).unwrap_err(),
        "core_runtime_pin_invalid"
    );
}

#[test]
fn broker_status_is_honestly_unavailable_until_a_safe_probe_is_wired() {
    assert_eq!(
        broker_availability(),
        BrokerAvailability::Unavailable {
            reason: BrokerUnavailableReason::BrokerUnwired,
        }
    );
}

#[test]
fn broker_public_readiness_requires_healthy_bound_core() {
    use gamja_core_host::runtime_install::broker_availability_from_status;
    use serde_json::json;
    assert_eq!(
        broker_availability_from_status(
            &json!({"status":"healthy","engine":"ready","credential_broker":"available"})
        ),
        BrokerAvailability::Available
    );
    for value in [
        json!({"status":"healthy","engine":"ready"}),
        json!({"status":"diagnostic_only","engine":"ready","credential_broker":"available"}),
        json!({"status":"healthy","engine":"unavailable","credential_broker":"available"}),
        json!({"status":"healthy","engine":"ready","credential_broker":true}),
        json!(null),
    ] {
        assert_eq!(
            broker_availability_from_status(&value),
            BrokerAvailability::Unavailable {
                reason: BrokerUnavailableReason::BrokerUnwired
            }
        );
    }
}

#[test]
fn canonical_cli_exposes_the_shared_broker_status_without_runtime_startup() {
    assert_eq!(
        gamja_core_host::cli::run(
            [OsString::from("runtime"), OsString::from("broker-status")].into_iter()
        ),
        Ok(0)
    );
}

#[cfg(target_os = "macos")]
mod macos {
    use super::*;
    use serde_json::json;
    use sha2::{Digest, Sha256};
    use std::{
        fs,
        os::unix::fs::{MetadataExt, PermissionsExt},
        path::{Path, PathBuf},
        sync::atomic::{AtomicUsize, Ordering},
    };

    static NEXT: AtomicUsize = AtomicUsize::new(0);

    struct Fixture {
        outer: PathBuf,
        runtime: PathBuf,
        pins: RuntimePins,
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.outer);
        }
    }

    fn digest(path: &Path) -> String {
        format!("{:x}", Sha256::digest(fs::read(path).unwrap()))
    }

    fn fixture() -> Fixture {
        let outer = std::env::temp_dir().join(format!(
            "gamja-runtime-install-integration-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let runtime = outer.join("runtime");
        fs::create_dir_all(runtime.join("bin")).unwrap();
        fs::create_dir_all(runtime.join("libexec/gamja")).unwrap();
        fs::set_permissions(&outer, fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(&runtime, fs::Permissions::from_mode(0o700)).unwrap();

        let core = runtime.join("bin/gamja");
        let engine = runtime.join("libexec/gamja/gamja-engine");
        fs::write(&core, b"core-one").unwrap();
        fs::write(&engine, b"engine-one").unwrap();
        fs::set_permissions(&core, fs::Permissions::from_mode(0o700)).unwrap();
        fs::set_permissions(&engine, fs::Permissions::from_mode(0o755)).unwrap();

        let manifest = json!({"files": {
            "bin/gamja": {"bytes": 8, "executable": true, "sha256": digest(&core)},
            "libexec/gamja/gamja-engine": {
                "bytes": 10, "executable": true, "sha256": digest(&engine)
            }
        }});
        let manifest_path = runtime.join("core-manifest.json");
        fs::write(&manifest_path, serde_json::to_vec(&manifest).unwrap()).unwrap();
        fs::set_permissions(&manifest_path, fs::Permissions::from_mode(0o600)).unwrap();
        let pins =
            RuntimePins::new(digest(&manifest_path), digest(&core), digest(&engine)).unwrap();
        let core_meta = core.metadata().unwrap();
        let engine_meta = engine.metadata().unwrap();
        let receipt = json!({
            "version": 1,
            "runtime_path": runtime.to_str().unwrap(),
            "manifest_sha256": pins.manifest_sha256,
            "core_sha256": pins.core_sha256,
            "engine_sha256": pins.engine_sha256,
            "core_device": core_meta.dev(),
            "core_inode": core_meta.ino(),
            "core_uid": core_meta.uid(),
            "core_mode": core_meta.mode() & 0o777,
            "engine_device": engine_meta.dev(),
            "engine_inode": engine_meta.ino(),
            "engine_uid": engine_meta.uid(),
            "engine_mode": engine_meta.mode() & 0o777,
        });
        let receipt_path = runtime.join(".gamja-runtime-receipt.json");
        fs::write(&receipt_path, serde_json::to_vec(&receipt).unwrap()).unwrap();
        fs::set_permissions(&receipt_path, fs::Permissions::from_mode(0o600)).unwrap();
        Fixture {
            outer,
            runtime,
            pins,
        }
    }

    #[test]
    fn candidate_validation_accepts_a_genuine_bound_receipt() {
        let fixture = fixture();
        let result = validate_runtime(
            RuntimeScope::CandidatePrefix(fixture.runtime.clone()),
            &fixture.pins,
        )
        .unwrap();
        assert_eq!(result.status, RuntimeStatus::Valid);
        assert_eq!(result.pins, fixture.pins);

        let arguments: [OsString; 10] = [
            "runtime".into(),
            "verify".into(),
            "--source".into(),
            fixture.runtime.as_os_str().into(),
            "--manifest-sha256".into(),
            fixture.pins.manifest_sha256.clone().into(),
            "--core-sha256".into(),
            fixture.pins.core_sha256.clone().into(),
            "--engine-sha256".into(),
            fixture.pins.engine_sha256.clone().into(),
        ];
        assert_eq!(gamja_core_host::cli::run(arguments.into_iter()), Ok(0));
    }

    #[test]
    fn receipt_unknown_fields_and_legacy_wrong_engine_mode_fail_closed() {
        let first = fixture();
        let receipt_path = first.runtime.join(".gamja-runtime-receipt.json");
        let mut receipt: serde_json::Value =
            serde_json::from_slice(&fs::read(&receipt_path).unwrap()).unwrap();
        receipt["unexpected"] = json!(true);
        fs::write(&receipt_path, serde_json::to_vec(&receipt).unwrap()).unwrap();
        assert!(validate_runtime(
            RuntimeScope::CandidatePrefix(first.runtime.clone()),
            &first.pins
        )
        .is_err());

        let bound = fixture();
        let receipt_path = bound.runtime.join(".gamja-runtime-receipt.json");
        let mut receipt: serde_json::Value =
            serde_json::from_slice(&fs::read(&receipt_path).unwrap()).unwrap();
        receipt["core_inode"] = json!(receipt["core_inode"].as_u64().unwrap() + 1);
        fs::write(&receipt_path, serde_json::to_vec(&receipt).unwrap()).unwrap();
        assert!(validate_runtime(
            RuntimeScope::CandidatePrefix(bound.runtime.clone()),
            &bound.pins
        )
        .is_err());

        let fixture = fixture();
        let engine = fixture.runtime.join("libexec/gamja/gamja-engine");
        fs::set_permissions(&engine, fs::Permissions::from_mode(0o700)).unwrap();
        assert!(validate_runtime(
            RuntimeScope::CandidatePrefix(fixture.runtime.clone()),
            &fixture.pins
        )
        .is_err());
    }
}
