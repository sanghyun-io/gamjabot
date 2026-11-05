// Bounded pipe intake, with no provider, credentials, process output or user content.
use gamja_core_host::runtime::drain_backend_diagnostics;
use serde_json::{json, Value};

#[test]
fn reporting_pipe_keeps_typed_v2_and_rejects_raw_unknown_oversized_data() {
    let detail = json!({
        "version":1,"correlation_id":"a".repeat(32),"occurred_at":"2026-09-30T00:00:00Z",
        "stage":"tool_execution","failure_code":"provider_unavailable","reason":"unexpected_error","error_class":"value_error",
        "room_id":4,"agent_id":7,"connection_id":3,"trigger_event_id":null,"action_id":null,
        "exception_chain":[{"module":"builtins","class":"ValueError"}],"frames":[{"file":"app/services/scheduler.py","function":"_run","line":382}]
    });
    let valid = json!({"code":"backend_runtime_failed","stage":"runtime","diagnostic":detail});
    let mut unknown = valid.clone();
    unknown["exception"] = json!("secret-token-must-not-appear");
    let mut bad_detail = valid.clone();
    bad_detail["diagnostic"]["frames"][0]["file"] = json!("/Users/private/secret.py");
    let input = format!("raw-secret-token\nGAMJA_DIAGNOSTIC_V2:{unknown}\nGAMJA_DIAGNOSTIC_V2:{bad_detail}\nGAMJA_DIAGNOSTIC_V2:{}\nGAMJA_DIAGNOSTIC_V2:{valid}\nGAMJA_DIAGNOSTIC_V1:{{\"stage\":\"startup\",\"code\":\"backend_init_failed\"}}\n", "x".repeat(2305));
    let mut events: Vec<Value> = vec![];
    drain_backend_diagnostics(input.as_bytes(), |event| events.push(event));
    assert_eq!(events.len(), 2);
    assert_eq!(events[0]["diagnostic"], detail);
    assert_eq!(events[1]["code"], "backend_init_failed");
    assert!(!serde_json::to_string(&events).unwrap().contains("secret"));
}

#[test]
fn reporting_pipe_preserves_native_unavailable_producer_detail() {
    let line = include_str!("../../reporting/fixtures/diagnostic-v2-native-unavailable.txt");
    let detail =
        serde_json::from_str::<Value>(line.trim().strip_prefix("GAMJA_DIAGNOSTIC_V2:").unwrap())
            .unwrap()["diagnostic"]
            .clone();
    assert_eq!(detail["failure_code"], "native_capability_unavailable");
    assert_eq!(detail["reason"], "native_capability_unavailable");
    let mut events = vec![];
    drain_backend_diagnostics(line.as_bytes(), |event| events.push(event));
    assert_eq!(events.len(), 1);
    assert_eq!(events[0]["diagnostic"], detail);
}

#[test]
fn reporting_pipe_captures_auth_remember_diagnostic() {
    let detail = json!({
        "version":1,"correlation_id":"b".repeat(32),"occurred_at":"2026-10-10T12:00:00Z",
        "stage":"auth_remember","failure_code":"secret_store_unavailable","reason":"keychain_query_failed",
        "error_class":"native_os_status","room_id":null,"agent_id":null,"connection_id":null,
        "trigger_event_id":null,"action_id":null,"exception_chain":[],"frames":[],
        "os_status":-25293,"operation":"sec_item_add","cleanup":"success"
    });
    let line = format!(
        "GAMJA_DIAGNOSTIC_V2:{}\n",
        json!({"code":"backend_runtime_failed","stage":"runtime","diagnostic":detail})
    );
    let mut events: Vec<Value> = vec![];
    drain_backend_diagnostics(line.as_bytes(), |event| events.push(event));
    assert_eq!(events.len(), 1);
    assert_eq!(events[0]["diagnostic"]["stage"], "auth_remember");
    assert_eq!(
        events[0]["diagnostic"]["failure_code"],
        "secret_store_unavailable"
    );
    assert_eq!(events[0]["diagnostic"]["os_status"], -25293);
    assert_eq!(events[0]["diagnostic"]["operation"], "sec_item_add");
    assert_eq!(events[0]["diagnostic"]["cleanup"], "success");
}

#[test]
fn reporting_pipe_captures_auth_restore_diagnostic() {
    let detail = json!({
        "version":1,"correlation_id":"c".repeat(32),"occurred_at":"2026-10-10T12:00:00Z",
        "stage":"auth_restore","failure_code":"credential_locked","reason":"credential_reentry_required",
        "error_class":"credential_reentry_required","room_id":null,"agent_id":null,"connection_id":null,
        "trigger_event_id":null,"action_id":null,"exception_chain":[],"frames":[],"os_status":-25293
    });
    let line = format!(
        "GAMJA_DIAGNOSTIC_V2:{}\n",
        json!({"code":"backend_runtime_failed","stage":"runtime","diagnostic":detail})
    );
    let mut events: Vec<Value> = vec![];
    drain_backend_diagnostics(line.as_bytes(), |event| events.push(event));
    assert_eq!(events.len(), 1);
    assert_eq!(events[0]["diagnostic"]["stage"], "auth_restore");
    assert_eq!(events[0]["diagnostic"]["failure_code"], "credential_locked");
    assert!(events[0]["diagnostic"].get("operation").is_none());
    assert!(events[0]["diagnostic"].get("cleanup").is_none());
}

#[test]
fn reporting_pipe_rejects_auth_fields_on_turn_stage() {
    let detail = json!({
        "version":1,"correlation_id":"d".repeat(32),"occurred_at":"2026-10-10T12:00:00Z",
        "stage":"tool_execution","failure_code":"provider_unavailable","reason":"unexpected_error","error_class":"value_error",
        "room_id":4,"agent_id":7,"connection_id":3,"trigger_event_id":null,"action_id":null,
        "exception_chain":[],"frames":[],"os_status":-25293
    });
    let line = format!(
        "GAMJA_DIAGNOSTIC_V2:{}\n",
        json!({"code":"backend_runtime_failed","stage":"runtime","diagnostic":detail})
    );
    let mut events: Vec<Value> = vec![];
    drain_backend_diagnostics(line.as_bytes(), |event| events.push(event));
    assert_eq!(events.len(), 0);
}

#[test]
fn reporting_pipe_rejects_turn_classifications_on_auth_stage() {
    let detail = json!({
        "version":1,"correlation_id":"e".repeat(32),"occurred_at":"2026-10-10T12:00:00Z",
        "stage":"auth_remember","failure_code":"provider_unavailable","reason":"unexpected_error","error_class":"value_error",
        "room_id":null,"agent_id":null,"connection_id":null,"trigger_event_id":null,"action_id":null,
        "exception_chain":[],"frames":[]
    });
    let line = format!(
        "GAMJA_DIAGNOSTIC_V2:{}\n",
        json!({"code":"backend_runtime_failed","stage":"runtime","diagnostic":detail})
    );
    let mut events: Vec<Value> = vec![];
    drain_backend_diagnostics(line.as_bytes(), |event| events.push(event));
    assert_eq!(events.len(), 0);
}

#[test]
fn reporting_pipe_rejects_present_null_auth_metadata() {
    let base = json!({
        "version":1,"correlation_id":"a".repeat(32),"occurred_at":"2026-10-10T00:00:00Z",
        "stage":"auth_remember","failure_code":"secret_store_unavailable","reason":"keychain_store_unavailable",
        "error_class":"native_os_status","room_id":null,"agent_id":null,"connection_id":null,
        "trigger_event_id":null,"action_id":null,"exception_chain":[],"frames":[],"cleanup":"success"
    });
    for field in ["os_status", "operation"] {
        let mut bad = base.clone();
        bad[field] = Value::Null;
        let input = format!(
            "GAMJA_DIAGNOSTIC_V2:{}\n",
            json!({"code":"backend_runtime_failed","stage":"runtime","diagnostic":bad})
        );
        let mut events = vec![];
        drain_backend_diagnostics(input.as_bytes(), |e| events.push(e));
        assert!(events.is_empty(), "present null {field} was accepted");
    }
    for field in ["os_status", "operation", "cleanup"] {
        let mut bad = base.clone();
        bad["stage"] = json!("tool_execution");
        bad["failure_code"] = json!("provider_unavailable");
        bad["reason"] = json!("unexpected_error");
        bad["error_class"] = json!("value_error");
        bad.as_object_mut().unwrap().remove("cleanup");
        bad[field] = Value::Null;
        let input = format!(
            "GAMJA_DIAGNOSTIC_V2:{}\n",
            json!({"code":"backend_runtime_failed","stage":"runtime","diagnostic":bad})
        );
        let mut events = vec![];
        drain_backend_diagnostics(input.as_bytes(), |e| events.push(e));
        assert!(events.is_empty(), "turn accepted present null {field}");
    }
}

#[test]
fn reporting_pipe_rejects_missing_auth_context_and_null_request_fields() {
    let base = json!({
        "version":1,"correlation_id":"b".repeat(32),"occurred_at":"2026-10-10T00:00:00Z",
        "stage":"auth_restore","failure_code":"secret_store_unavailable","reason":"keychain_store_unavailable",
        "error_class":"secret_store_unavailable","room_id":null,"agent_id":null,"connection_id":null,
        "trigger_event_id":null,"action_id":null,"exception_chain":[],"frames":[]
    });
    for field in [
        "room_id",
        "agent_id",
        "connection_id",
        "trigger_event_id",
        "action_id",
        "http_status",
        "vendor_request_id",
    ] {
        let mut bad = base.clone();
        if field == "http_status" || field == "vendor_request_id" {
            bad[field] = Value::Null;
        } else {
            bad.as_object_mut().unwrap().remove(field);
        }
        let input = format!(
            "GAMJA_DIAGNOSTIC_V2:{}\n",
            json!({"code":"backend_runtime_failed","stage":"runtime","diagnostic":bad})
        );
        let mut events = vec![];
        drain_backend_diagnostics(input.as_bytes(), |e| events.push(e));
        assert!(
            events.is_empty(),
            "invalid presence for {field} was accepted"
        );
    }
}

#[test]
fn auth_diagnostic_survives_manual_automatic_and_restart() {
    use gamja_core_host::reporting::{DiagnosticEvent, Mode, ReportStore};
    let mut random_bytes = [0u8; 16];
    getrandom::fill(&mut random_bytes).unwrap();
    let fixture_id: String = random_bytes.iter().map(|b| format!("{b:02x}")).collect();
    struct Cleanup(std::path::PathBuf);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    let root = std::env::temp_dir().join(format!("gamja-auth-report-{}", fixture_id));
    std::fs::create_dir(&root).unwrap();
    let cleanup = Cleanup(root);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&cleanup.0, std::fs::Permissions::from_mode(0o700)).unwrap();
    }
    let detail = json!({
        "version":1,"correlation_id":"f".repeat(32),"occurred_at":"2026-10-10T00:00:00Z",
        "stage":"auth_remember","failure_code":"secret_store_unavailable","reason":"keychain_store_unavailable",
        "error_class":"native_os_status","room_id":null,"agent_id":null,"connection_id":null,
        "trigger_event_id":null,"action_id":null,"exception_chain":[],"frames":[],"cleanup":"success","operation":"sec_item_add","os_status":-25293
    });
    let line = format!(
        "GAMJA_DIAGNOSTIC_V2:{}\n",
        json!({"code":"backend_runtime_failed","stage":"runtime","diagnostic":detail})
    );
    let mut events = vec![];
    drain_backend_diagnostics(line.as_bytes(), |e| events.push(e));
    let first: DiagnosticEvent = serde_json::from_value(events.remove(0)).unwrap();
    let mut store = ReportStore::open(&cleanup.0).unwrap();
    store.record(first.clone()).unwrap();
    let manual = store
        .create_manual_draft(&[first.event_id.clone()], None)
        .unwrap();
    assert_eq!(
        serde_json::to_value(&manual.envelope.diagnostics[0]).unwrap(),
        detail
    );
    store.set_auto_consent(true).unwrap();
    let mut second = first.clone();
    second.event_id = "2".repeat(32);
    store.record(second).unwrap();
    let snapshot = store.list();
    let auto = snapshot
        .reports
        .iter()
        .find(|r| r.draft.envelope.mode == Mode::Automatic)
        .unwrap();
    assert_eq!(
        serde_json::to_value(&auto.draft.envelope.diagnostics[0]).unwrap(),
        detail
    );
    drop(store);
    let mut reopened = ReportStore::open(&cleanup.0).unwrap();
    let restored = reopened.get_draft(&manual.envelope.report_id).unwrap();
    assert_eq!(restored.body(), manual.body());
    assert!(!reopened.list().auto_reporting_enabled);
}
