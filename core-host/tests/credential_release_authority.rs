#![cfg(all(target_os = "macos", target_arch = "aarch64"))]

use std::ffi::OsStr;
use std::os::unix::ffi::OsStrExt;
use std::process::{Command, Stdio};

const BIN_PATH: &str = env!("CARGO_BIN_EXE_gamja-credential-release-authority");

fn compiled_payload() -> Option<serde_json::Value> {
    Some(serde_json::json!({
        "protocol_version": 1,
        "build_id": option_env!("GAMJA_BUILD_ID")?,
        "broker_sha256": option_env!("GAMJA_BROKER_SHA256")?,
        "core_sha256": option_env!("GAMJA_CORE_SHA256")?,
        "engine_sha256": option_env!("GAMJA_ENGINE_SHA256")?,
    }))
}

#[test]
fn test_compiled_configuration_contract() {
    let output = Command::new(BIN_PATH)
        .env_clear()
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(Stdio::null())
        .output()
        .expect("Failed to execute binary");

    match compiled_payload() {
        Some(expected) => {
            assert!(output.status.success());
            assert!(output.stderr.is_empty());
            assert!(output.stdout.len() <= 4096);
            let json: serde_json::Value =
                serde_json::from_slice(&output.stdout).expect("invalid JSON");
            assert_eq!(json, expected);
        }
        None => {
            // An ordinary unconfigured build must compile and actively assert
            // failure closure. This branch is an assertion, never a test skip.
            assert_eq!(output.status.code(), Some(1));
            assert!(output.stdout.is_empty());
            assert_eq!(output.stderr, b"authority_configuration_missing\n");
        }
    }
}

#[test]
fn test_runtime_env_override_ignored() {
    let output_default = Command::new(BIN_PATH)
        .env_clear()
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(Stdio::null())
        .output()
        .expect("Failed to execute binary (default)");

    let output_override = Command::new(BIN_PATH)
        .env_clear()
        .env("GAMJA_BUILD_ID", "RUNTIME_BUILD")
        .env("GAMJA_BROKER_SHA256", "runtime-broker-override")
        .env("GAMJA_CORE_SHA256", "runtime-core-override")
        .env("GAMJA_ENGINE_SHA256", "runtime-engine-override")
        .current_dir("/")
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(Stdio::null())
        .output()
        .expect("Failed to execute binary (override)");

    assert_eq!(output_default.status.code(), output_override.status.code());
    assert_eq!(output_default.stderr, output_override.stderr);

    assert_eq!(output_default.stdout, output_override.stdout);
}

#[test]
fn test_argv_rejected() {
    for arg in ["--help", "--version", "--exact", "arbitrary"] {
        let output = Command::new(BIN_PATH)
            .env_clear()
            .arg(arg)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .stdin(Stdio::null())
            .output()
            .expect("Failed to execute binary");

        assert_eq!(output.status.code(), Some(1));
        assert!(output.stdout.is_empty());
        assert_eq!(output.stderr, b"authority_arguments_rejected\n");
    }

    let non_utf8 = OsStr::from_bytes(b"invalid\xffarg");
    let output = Command::new(BIN_PATH)
        .env_clear()
        .arg(non_utf8)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(Stdio::null())
        .output()
        .expect("Failed to execute binary");

    assert_eq!(output.status.code(), Some(1));
    assert!(output.stdout.is_empty());
    assert_eq!(output.stderr, b"authority_arguments_rejected\n");
}
