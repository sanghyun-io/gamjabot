use std::io::{self, Write};
use std::process::ExitCode;

const PROTOCOL_VERSION: u64 = 1;

fn validate_build_id(s: &str) -> bool {
    let bytes = s.as_bytes();
    (1..=128).contains(&bytes.len())
        && bytes
            .iter()
            .all(|&c| c.is_ascii_alphanumeric() || c == b'-' || c == b'_' || c == b'.')
}

fn validate_sha256_hex(s: &str) -> bool {
    s.len() == 64 && s.chars().all(|c| matches!(c, '0'..='9' | 'a'..='f'))
}

fn main() -> ExitCode {
    if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        if std::env::args_os().nth(1).is_some() {
            let _ = io::stderr().write_all(b"authority_arguments_rejected\n");
            return ExitCode::from(1);
        }

        let build_id = option_env!("GAMJA_BUILD_ID");
        let broker_sha = option_env!("GAMJA_PUBLISHER_BROKER_SHA256");
        let core_sha = option_env!("GAMJA_CORE_SHA256");
        let engine_sha = option_env!("GAMJA_ENGINE_SHA256");

        if let (Some(id), Some(broker), Some(core), Some(engine)) =
            (build_id, broker_sha, core_sha, engine_sha)
        {
            if !validate_build_id(id) {
                let _ = io::stderr().write_all(b"authority_configuration_invalid\n");
                return ExitCode::from(1);
            }
            if !validate_sha256_hex(broker) {
                let _ = io::stderr().write_all(b"authority_configuration_invalid\n");
                return ExitCode::from(1);
            }
            if !validate_sha256_hex(core) {
                let _ = io::stderr().write_all(b"authority_configuration_invalid\n");
                return ExitCode::from(1);
            }
            if !validate_sha256_hex(engine) {
                let _ = io::stderr().write_all(b"authority_configuration_invalid\n");
                return ExitCode::from(1);
            }

            let json = serde_json::json!({
                "protocol_version": PROTOCOL_VERSION,
                "build_id": id,
                "broker_sha256": broker,
                "core_sha256": core,
                "engine_sha256": engine
            });

            let output = match serde_json::to_string(&json) {
                Ok(output) => output,
                Err(_) => return ExitCode::from(1),
            };
            if output.len() > 4096 {
                let _ = io::stderr().write_all(b"authority_output_failed\n");
                return ExitCode::from(1);
            }

            if io::stdout().write_all(output.as_bytes()).is_err() {
                return ExitCode::from(1);
            }
            ExitCode::SUCCESS
        } else {
            let _ = io::stderr().write_all(b"authority_configuration_missing\n");
            ExitCode::from(1)
        }
    } else {
        let _ = io::stderr().write_all(b"authority_platform_unsupported\n");
        ExitCode::from(1)
    }
}
